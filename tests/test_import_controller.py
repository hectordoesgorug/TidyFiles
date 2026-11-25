from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from uuid import UUID

import pytest

import tidyfiles.discovery as discovery_module
from tidyfiles.collection import ImportMode, WorkingCollection
from tidyfiles.discovery import DiscoveryDisposition, DiscoveryReport, DiscoverySession
from tidyfiles.import_controller import ImportController


class _FakeRoot:
    def __init__(self) -> None:
        self._callbacks: dict[str, tuple[float, object]] = {}
        self._next = 0

    def after(self, delay_ms: int, callback):
        self._next += 1
        callback_id = str(self._next)
        self._callbacks[callback_id] = (time.monotonic() + delay_ms / 1000, callback)
        return callback_id

    def after_cancel(self, callback_id: str) -> None:
        self._callbacks.pop(callback_id, None)

    def step(self) -> None:
        now = time.monotonic()
        due = [
            (callback_id, callback)
            for callback_id, (deadline, callback) in self._callbacks.items()
            if deadline <= now
        ]
        if not due:
            time.sleep(0.001)
            return
        callback_id, callback = min(
            due,
            key=lambda item: self._callbacks[item[0]][0],
        )
        self._callbacks.pop(callback_id, None)
        callback()


class _Harness:
    def __init__(self, collection: WorkingCollection | None = None) -> None:
        self.root = _FakeRoot()
        self.collection = collection or WorkingCollection()
        self.rows = {
            entry.entry_id: entry for entry in self.collection.snapshot().entries
        }
        self.order = [entry.entry_id for entry in self.collection.snapshot().entries]
        self.selection: tuple[UUID, ...] = ()
        self.reports: list[DiscoveryReport] = []
        self.errors: list[str] = []
        self.item_batches: list[int] = []
        self.callback_threads: set[int] = set()
        self.controller = ImportController(
            self.root,
            self.collection,
            capture_view_state=self.capture,
            on_activity=lambda *_: self._record_thread(),
            on_items=self.add_items,
            on_order_chunk=self.apply_order,
            on_remove_chunk=self.remove_rows,
            on_rollback_complete=self.finish_rollback,
            on_finished=self.reports.append,
            on_error=self.errors.append,
            on_shutdown=lambda: None,
        )

    def _record_thread(self) -> None:
        self.callback_threads.add(threading.get_ident())

    def capture(self):
        return tuple(self.order), self.selection

    def add_items(self, items) -> None:
        self._record_thread()
        self.item_batches.append(len(items))
        for item in items:
            if item.disposition is DiscoveryDisposition.ACCEPTED and item.entry:
                self.rows[item.entry.entry_id] = item.entry
                self.order.append(item.entry.entry_id)

    def apply_order(self, order, start, end, _finalized) -> None:
        self._record_thread()
        for position in range(start, end):
            entry_id = order[position]
            if entry_id in self.order:
                self.order.remove(entry_id)
            self.order.insert(position, entry_id)

    def remove_rows(self, entry_ids) -> None:
        self._record_thread()
        removed = set(entry_ids)
        self.order = [entry_id for entry_id in self.order if entry_id not in removed]

    def finish_rollback(self, entry_ids, view_state) -> None:
        for entry_id in entry_ids:
            self.rows.pop(entry_id, None)
        self.order = list(view_state[0])
        self.selection = view_state[1]

    def pump_until(self, predicate, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.step()
        assert predicate(), "controller did not reach the expected state"


def _file(path: Path, content: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_cumulative_import_reuses_paths_and_preserves_entry_identity(tmp_path: Path) -> None:
    harness = _Harness()
    z_path = _file(tmp_path / "z.txt")
    a_path = _file(tmp_path / "a.txt")
    b_path = _file(tmp_path / "b.txt")
    main_thread = threading.get_ident()

    assert harness.controller.start_files((z_path, a_path))
    assert not harness.controller.start_files((b_path,))
    harness.pump_until(lambda: not harness.controller.busy)
    first_ids = {
        entry.source_path: entry.entry_id
        for entry in harness.collection.snapshot().entries
    }
    assert [harness.rows[item].original_filename for item in harness.order] == [
        "a.txt",
        "z.txt",
    ]
    assert harness.reports[-1].state.value == "complete"
    assert harness.reports[-1].queue_capacity == 128
    assert harness.reports[-1].worker_count == 1
    assert max(harness.item_batches) <= ImportController.MAX_CONSUMED_PER_TICK
    assert harness.callback_threads == {main_thread}
    assert harness.controller.performance["time_to_first_visible_seconds"] is not None

    assert harness.controller.start_files((z_path, b_path))
    harness.pump_until(lambda: not harness.controller.busy)
    entries = harness.collection.snapshot().entries
    assert len(entries) == 3
    z_entry = next(entry for entry in entries if entry.source_path == str(z_path))
    assert z_entry.entry_id == first_ids[str(z_path)]
    assert len(harness.rows) == 3
    assert len(set(harness.order)) == 3
    assert harness.reports[-1].counters.accepted == 1
    assert harness.reports[-1].counters.already_listed == 1


def test_folder_results_render_before_walk_finishes_and_cancel_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = WorkingCollection()
    prior = _file(tmp_path / "prior.txt")
    harness = _Harness(collection)
    assert harness.controller.start_files((prior,))
    harness.pump_until(lambda: not harness.controller.busy)
    previous = collection.snapshot()
    harness.selection = (previous.entries[0].entry_id,)

    root = tmp_path / "nested"
    _file(root / "one.txt")
    _file(root / "sub" / "two.txt")
    entered_wait = threading.Event()
    release_walk = threading.Event()
    original_walk = DiscoverySession._walk_folder

    def blocked_walk(session, selected_root):
        for index, record in enumerate(original_walk(session, selected_root)):
            yield record
            if index == 0:
                entered_wait.set()
                release_walk.wait(5)

    monkeypatch.setattr(DiscoverySession, "_walk_folder", blocked_walk)
    try:
        assert harness.controller.start_folder(root)
        harness.pump_until(
            lambda: entered_wait.is_set() and len(harness.rows) > len(previous.entries)
        )
        assert harness.controller.busy
        assert not harness.controller.session.is_terminal
        assert len(harness.rows) == 2
        assert harness.controller.cancel()
        release_walk.set()
        harness.pump_until(lambda: not harness.controller.busy)
    finally:
        release_walk.set()

    after = collection.snapshot()
    assert harness.reports[-1].state.value == "cancelled"
    assert tuple(entry.entry_id for entry in after.entries) == tuple(
        entry.entry_id for entry in previous.entries
    )
    assert after.folder_roots == previous.folder_roots
    assert after.incomplete_imports == previous.incomplete_imports
    assert harness.order == [previous.entries[0].entry_id]
    assert harness.selection == (previous.entries[0].entry_id,)
    assert len(harness.rows) == 1

    next_file = _file(tmp_path / "after-cancel.txt")
    assert harness.controller.start_files((next_file,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "complete"
    assert len(collection.snapshot().entries) == 2


def test_cancel_during_incremental_final_order_restores_previous_order_and_selection(
    tmp_path: Path,
) -> None:
    collection = WorkingCollection()
    initial = collection.begin_import(ImportMode.ADD_FILES)
    middle = collection.register_path(initial, r"C:\Prior\m.txt").entry
    collection.finalize_import(initial)
    harness = _Harness(collection)
    harness.selection = (middle.entry_id,)
    harness.controller.RENDER_CHUNK = 1
    a_path = _file(tmp_path / "a.txt")
    z_path = _file(tmp_path / "z.txt")

    assert harness.controller.start_files((z_path, a_path))
    harness.pump_until(
        lambda: harness.controller._phase == "rendering"
        and harness.controller._render_offset == 1
    )
    assert [harness.rows[entry_id].original_filename for entry_id in harness.order] == [
        "a.txt",
        "m.txt",
        "z.txt",
    ]
    assert [entry.original_filename for entry in collection.snapshot().entries] == [
        "m.txt",
        "z.txt",
        "a.txt",
    ]

    assert harness.controller.cancel()
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "cancelled"
    assert tuple(entry.entry_id for entry in collection.snapshot().entries) == (
        middle.entry_id,
    )
    assert harness.order == [middle.entry_id]
    assert harness.selection == (middle.entry_id,)
    assert set(harness.rows) == {middle.entry_id}


def test_incomplete_warning_survives_later_success_and_failure_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness()
    denied = _file(tmp_path / "denied.txt")
    original_lstat = os.lstat

    def inaccessible(path, *args, **kwargs):
        if os.fspath(path) == str(denied):
            raise PermissionError("simulated denial")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(discovery_module.os, "lstat", inaccessible)
    assert harness.controller.start_files((denied,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "incomplete"
    assert len(harness.collection.snapshot().incomplete_imports) == 1

    monkeypatch.setattr(discovery_module.os, "lstat", original_lstat)
    recovered = _file(tmp_path / "recovered.txt")
    assert harness.controller.start_files((recovered,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "complete"
    assert len(harness.collection.snapshot().incomplete_imports) == 1

    original_walk = DiscoverySession._walk_files

    def broken_walk(_session):
        raise RuntimeError("simulated worker failure")
        yield

    monkeypatch.setattr(DiscoverySession, "_walk_files", broken_walk)
    failed_path = _file(tmp_path / "failed.txt")
    assert harness.controller.start_files((failed_path,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "failed"
    assert {entry.source_path for entry in harness.collection.snapshot().entries} == {
        str(recovered)
    }

    monkeypatch.setattr(DiscoverySession, "_walk_files", original_walk)
    final_path = _file(tmp_path / "final.txt")
    assert harness.controller.start_files((final_path,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "complete"
    assert len(harness.collection.snapshot().incomplete_imports) == 1


def test_failed_worker_start_retires_import_and_restores_idle_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness()
    path = _file(tmp_path / "worker-start.txt")
    original_start = DiscoverySession.start

    def fail_start(_session):
        raise RuntimeError("simulated thread startup failure")

    monkeypatch.setattr(DiscoverySession, "start", fail_start)
    assert harness.controller.start_files((path,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "failed"
    assert harness.collection.snapshot().entries == ()
    assert not harness.collection.capabilities.is_importing

    monkeypatch.setattr(DiscoverySession, "start", original_start)
    assert harness.controller.start_files((path,))
    harness.pump_until(lambda: not harness.controller.busy)
    assert harness.reports[-1].state.value == "complete"
    assert len(harness.collection.snapshot().entries) == 1
