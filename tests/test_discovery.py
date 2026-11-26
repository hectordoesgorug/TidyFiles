from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import tidyfiles.discovery as discovery_module
from tidyfiles.collection import (
    WorkingCollection,
)
from tidyfiles.discovery import (
    DiscoveryDisposition,
    DiscoveryState,
    FilesystemDiscovery,
)


def _complete(session, timeout: float = 10):
    session.start()
    items = []
    deadline = time.monotonic() + timeout
    while not session.is_terminal and time.monotonic() < deadline:
        items.extend(session.consume_batch(timeout=min(0.05, deadline - time.monotonic())))
    assert session.is_terminal
    return tuple(items), session.report


def test_add_files_imports_individually_and_reports_duplicates(tmp_path: Path) -> None:
    first = tmp_path / "one.tar.gz"
    second = tmp_path / "extensionless"
    zero = tmp_path / "zero.dat"
    first.write_bytes(b"data")
    second.write_bytes(b"")
    zero.write_bytes(b"")
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection, batch_size=2)

    session = discovery.begin_files((first, second, zero, first))
    items, report = _complete(session)

    assert [item.disposition for item in items].count(DiscoveryDisposition.ACCEPTED) == 3
    assert [item.disposition for item in items].count(DiscoveryDisposition.ALREADY_LISTED) == 1
    assert report.state is DiscoveryState.COMPLETE
    assert report.counters.accepted == 3
    assert report.counters.already_listed == 1
    assert all(item.session_id == report.session_id for item in items)
    entries = collection.snapshot().entries
    assert [entry.original_filename for entry in entries] == [
        "one.tar.gz",
        "extensionless",
        "zero.dat",
    ]
    assert entries[0].original_stem == "one.tar"
    assert entries[0].source_extension == ".gz"
    assert entries[0].import_root_path == str(tmp_path)
    assert first.read_bytes() == b"data"
    assert entries[1].source_extension == ""
    assert entries[1].metadata is not None
    assert entries[1].metadata.size_bytes == 0
    assert entries[1].metadata.modified_time_ns == second.stat().st_mtime_ns
    created_time = getattr(second.stat(), "st_birthtime_ns", None)
    if created_time is None and os.name == "nt":
        created_time = second.stat().st_ctime_ns
    assert entries[1].metadata.created_time_ns == created_time
    assert collection.snapshot().folder_roots == ()


def test_recursive_scan_excludes_only_approved_clutter_and_managed_paths(tmp_path: Path) -> None:
    root = tmp_path / "import"
    (root / "Sub").mkdir(parents=True)
    managed = root / "app-data" / "Trash"
    managed.mkdir(parents=True)
    ordinary_named_trash = root / "user-data" / "Trash"
    ordinary_named_trash.mkdir(parents=True)
    included = [
        root / ".hidden",
        root / ".gitignore",
        root / ".env",
        root / "folder.jpg",
        root / "Sub" / "extensionless",
        root / "Sub" / "zero-byte",
        ordinary_named_trash / "keep.txt",
    ]
    for path in included:
        path.write_bytes(b"" if path.name in {"zero-byte", "extensionless"} else b"x")
    for name in ("THUMBS.DB", ".DS_STORE", "Desktop.INI"):
        (root / name).write_bytes(b"clutter")
    (root / "$RECYCLE.BIN").mkdir()
    (root / "$RECYCLE.BIN" / "hidden.txt").write_bytes(b"excluded")
    (root / "System Volume Information").mkdir()
    (root / "System Volume Information" / "hidden.txt").write_bytes(b"excluded")
    (managed / "hidden.txt").write_bytes(b"excluded")
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection, managed_paths=(managed,), queue_capacity=2)

    session = discovery.begin_folder(root)
    items, report = _complete(session)

    accepted_names = {entry.original_filename for entry in collection.snapshot().entries}
    assert accepted_names == {path.name for path in included}
    assert report.state is DiscoveryState.COMPLETE
    assert report.counters.accepted == len(included)
    excluded = [item for item in items if item.disposition is DiscoveryDisposition.EXCLUDED]
    assert {Path(item.path).name.casefold() for item in excluded} >= {
        "thumbs.db",
        ".ds_store",
        "desktop.ini",
        "$recycle.bin",
        "system volume information",
        "trash",
    }
    assert all(entry.import_root_path == str(root) for entry in collection.snapshot().entries)
    assert len(collection.snapshot().folder_roots) == 1


def test_empty_folder_completes_and_retains_eligible_root(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection).begin_folder(root)
    _, report = _complete(session)
    assert report.state is DiscoveryState.COMPLETE
    assert collection.snapshot().entries == ()
    assert collection.snapshot().folder_roots[0].path == str(root)


@pytest.mark.parametrize("begin", ["begin_files", "begin_folder"])
def test_empty_selected_path_is_rejected_without_opening_import(begin: str) -> None:
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection)

    with pytest.raises(ValueError, match="must not be empty"):
        if begin == "begin_files":
            discovery.begin_files(("",))
        else:
            discovery.begin_folder("")

    assert collection.snapshot().import_sessions == ()


def test_overlapping_folder_import_preserves_first_context_and_identity(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    source = child / "same.txt"
    source.write_text("unchanged")
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection)

    first, _ = _complete(discovery.begin_folder(child))
    original = collection.snapshot().entries[0]
    second, report = _complete(discovery.begin_folder(parent))

    assert first[0].disposition is DiscoveryDisposition.ACCEPTED
    assert second[0].disposition is DiscoveryDisposition.ALREADY_LISTED
    assert report.counters.accepted == 0
    assert report.counters.already_listed == 1
    entry = collection.snapshot().entries[0]
    assert entry.entry_id == original.entry_id
    assert entry.import_root_path == str(child)
    assert len(collection.snapshot().folder_roots) == 2


def test_cumulative_import_resorts_all_entries_in_automatic_mode(tmp_path: Path) -> None:
    first = tmp_path / "Zebra 2.txt"
    later = tmp_path / "Alpha 10.txt"
    first.write_text("first")
    later.write_text("later")
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection)

    _complete(discovery.begin_files((first,)))
    assert [entry.original_filename for entry in collection.snapshot().entries] == [
        "Zebra 2.txt"
    ]
    _complete(discovery.begin_files((later,)))

    assert [entry.original_filename for entry in collection.snapshot().entries] == [
        "Alpha 10.txt",
        "Zebra 2.txt",
    ]


@pytest.mark.parametrize("manual", [False, True])
def test_cumulative_incomplete_import_obeys_ordering_mode(tmp_path: Path, manual: bool) -> None:
    first = tmp_path / "Zebra 2.txt"
    second = tmp_path / "Alpha 10.txt"
    new_z = tmp_path / "Zulu 1.txt"
    new_b = tmp_path / "Beta 2.txt"
    missing = tmp_path / "inaccessible.txt"
    for path in (first, second, new_z, new_b):
        path.write_text(path.name)
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection)

    _complete(discovery.begin_files((first, second)))
    existing = collection.snapshot().entries
    if manual:
        collection.move_to_bottom((existing[0].entry_id,))
    before_names = [entry.original_filename for entry in collection.snapshot().entries]

    items, report = _complete(discovery.begin_files((new_z, new_b, missing)))

    assert report.state is DiscoveryState.INCOMPLETE
    expected_names = (
        [*before_names, "Beta 2.txt", "Zulu 1.txt"]
        if manual
        else ["Alpha 10.txt", "Beta 2.txt", "Zebra 2.txt", "Zulu 1.txt"]
    )
    assert [entry.original_filename for entry in collection.snapshot().entries] == expected_names
    assert collection.snapshot().ordering_mode.value == ("manual" if manual else "automatic")
    assert collection.snapshot().has_incomplete_imports
    assert any(item.path == str(missing) for item in items)


def test_managed_selected_root_is_excluded_without_retaining_authorization(tmp_path: Path) -> None:
    managed = tmp_path / "TidyFiles" / "Trash"
    managed.mkdir(parents=True)
    (managed / "file.txt").write_text("keep")
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection, managed_paths=(managed,)).begin_folder(managed)
    items, report = _complete(session)
    assert report.state is DiscoveryState.COMPLETE
    assert items[0].reason == "managed_location"
    assert collection.snapshot().folder_roots == ()
    assert collection.snapshot().entries == ()


def test_direct_file_import_applies_clutter_and_managed_path_exclusions(tmp_path: Path) -> None:
    clutter = tmp_path / "ThUmBs.Db"
    managed = tmp_path / "managed" / "Staging"
    managed_file = managed / "temporary.bin"
    clutter.write_bytes(b"keep")
    managed.mkdir(parents=True)
    managed_file.write_bytes(b"keep")
    collection = WorkingCollection()

    items, report = _complete(
        FilesystemDiscovery(collection, managed_paths=(managed,)).begin_files(
            (clutter, managed_file)
        )
    )

    assert report.state is DiscoveryState.COMPLETE
    assert [item.reason for item in items] == ["clutter_file", "managed_location"]
    assert report.counters.excluded == 2
    assert collection.snapshot().entries == ()


def test_unknown_reparse_point_is_excluded_without_traversal(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "unknown-reparse"
    root.mkdir()
    (root / "would-be-child.txt").write_text("keep")
    original_lstat = os.lstat

    def unclassified(path, *args, **kwargs):
        if os.fspath(path) == str(root):
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_file_attributes=0x400,
                st_reparse_tag=0,
            )
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(discovery_module.os, "lstat", unclassified)
    collection = WorkingCollection()
    items, report = _complete(FilesystemDiscovery(collection).begin_folder(root))
    assert report.state is DiscoveryState.COMPLETE
    assert items[0].reason == "reparse_point"
    assert collection.snapshot().entries == ()
    assert collection.snapshot().folder_roots == ()


def test_discovery_is_progressive_before_root_traversal_finishes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "progressive"
    root.mkdir()
    for name in ("one", "two"):
        (root / name).write_text(name)
    original_scandir = os.scandir
    traversal_paused = threading.Event()
    continue_traversal = threading.Event()

    class PausedIterator:
        def __init__(self, iterator):
            self.iterator = iterator
            self.calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self.calls == 1:
                traversal_paused.set()
                assert continue_traversal.wait(3)
            self.calls += 1
            return next(self.iterator)

        def close(self):
            self.iterator.close()

    monkeypatch.setattr(
        discovery_module.os,
        "scandir",
        lambda path: PausedIterator(original_scandir(path)) if os.fspath(path) == str(root)
        else original_scandir(path),
    )
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection, queue_capacity=1).begin_folder(root)
    session.start()
    assert traversal_paused.wait(3)
    first_batch = session.consume_batch(limit=1, timeout=1)
    continue_traversal.set()
    session.wait_until_terminal()
    assert first_batch
    assert first_batch[0].disposition is DiscoveryDisposition.ACCEPTED
    assert session.report.time_to_first_result_seconds is not None
    assert session.report.peak_queue_occupancy <= session.report.queue_capacity == 1


def test_partial_enumeration_error_keeps_discovered_files_and_is_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "partial"
    root.mkdir()
    good = root / "good.txt"
    good.write_text("good")
    original_scandir = os.scandir

    class FailsAfterFirst:
        def __init__(self, iterator):
            self.iterator = iterator
            self.calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self.calls:
                raise PermissionError("simulated mid-enumeration denial")
            self.calls += 1
            return next(self.iterator)

        def close(self):
            self.iterator.close()

    monkeypatch.setattr(
        discovery_module.os,
        "scandir",
        lambda path: FailsAfterFirst(original_scandir(path))
        if os.fspath(path) == str(root)
        else original_scandir(path),
    )
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection).begin_folder(root)
    items, report = _complete(session)

    assert report.state is DiscoveryState.INCOMPLETE
    assert any(entry.original_filename == good.name for entry in collection.snapshot().entries)
    assert any(item.disposition is DiscoveryDisposition.INACCESSIBLE for item in items)
    assert collection.snapshot().has_incomplete_imports
    assert collection.snapshot().incomplete_imports[0].diagnostics


def test_inaccessible_subdirectory_does_not_stop_accessible_siblings(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "sibling-recovery"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    good = root / "good.txt"
    good.write_text("good")
    (blocked / "unseen.txt").write_text("not discovered")
    original_scandir = os.scandir

    def deny_blocked(path):
        if os.fspath(path) == str(blocked):
            raise PermissionError("simulated directory denial")
        return original_scandir(path)

    monkeypatch.setattr(discovery_module.os, "scandir", deny_blocked)
    collection = WorkingCollection()

    items, report = _complete(FilesystemDiscovery(collection).begin_folder(root))

    assert report.state is DiscoveryState.INCOMPLETE
    assert [entry.original_filename for entry in collection.snapshot().entries] == ["good.txt"]
    assert any(item.path == str(blocked) and item.disposition is DiscoveryDisposition.INACCESSIBLE
               for item in items)


def test_incomplete_import_history_survives_later_success(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "incomplete"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("data")
    collection = WorkingCollection()
    discovery = FilesystemDiscovery(collection)
    original_lstat = os.lstat

    def inaccessible(path, *args, **kwargs):
        if os.fspath(path) == str(source):
            raise PermissionError("simulated access denial")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(discovery_module.os, "lstat", inaccessible)
    _, failed = _complete(discovery.begin_files((source,)))
    assert failed.state is DiscoveryState.INCOMPLETE
    monkeypatch.setattr(discovery_module.os, "lstat", original_lstat)
    _, successful = _complete(discovery.begin_files((source,)))
    assert successful.state is DiscoveryState.COMPLETE
    assert collection.snapshot().has_incomplete_imports
    assert len(collection.snapshot().incomplete_imports) == 1


def test_cancellation_before_start_and_late_results(tmp_path: Path) -> None:
    root = tmp_path / "not-started"
    root.mkdir()
    collection = WorkingCollection()
    service = FilesystemDiscovery(collection)
    pending = service.begin_folder(root)
    consumer_started = threading.Event()
    consumer_results = []

    def waiting_consumer():
        consumer_started.set()
        consumer_results.extend(pending.consume_batch(timeout=3))

    consumer = threading.Thread(target=waiting_consumer)
    consumer.start()
    assert consumer_started.wait(1)
    pending.cancel()
    consumer.join(timeout=1)
    assert not consumer.is_alive()
    assert consumer_results == []
    assert pending.state is DiscoveryState.CANCELLED
    assert collection.snapshot().folder_roots == ()

    next_file = tmp_path / "next.txt"
    next_file.write_text("next")
    _, report = _complete(service.begin_files((next_file,)))
    assert report.state is DiscoveryState.COMPLETE
    assert collection.snapshot().entries[0].original_filename == "next.txt"


def test_cancellation_while_queue_full_rolls_back_only_active_import(tmp_path: Path) -> None:
    prior_path = tmp_path / "prior.txt"
    prior_path.write_text("prior")
    root = tmp_path / "many"
    root.mkdir()
    for index in range(40):
        (root / f"{index:03}.txt").write_bytes(b"")
    collection = WorkingCollection()
    service = FilesystemDiscovery(collection, queue_capacity=1, batch_size=1)
    _complete(service.begin_files((prior_path,)))
    prior = collection.snapshot()

    session = service.begin_folder(root)
    session.start()
    assert session._producer_waiting.wait(3)
    session.cancel()
    report = session.wait_until_terminal(timeout=3)

    after = collection.snapshot()
    assert report.state is DiscoveryState.CANCELLED
    assert after.entries == prior.entries
    assert after.folder_roots == prior.folder_roots
    assert report.cancellation_latency_seconds is not None
    assert report.peak_queue_occupancy <= 1


@pytest.mark.parametrize("manual", [False, True])
def test_cancellation_after_partial_registration_restores_ordering_state(
    tmp_path: Path, manual: bool
) -> None:
    prior_paths = [tmp_path / "prior-a.txt", tmp_path / "prior-b.txt"]
    for path in prior_paths:
        path.write_text(path.name)
    root = tmp_path / "many-after-partial"
    root.mkdir()
    for index in range(20):
        (root / f"{index:03}.txt").write_bytes(b"")
    collection = WorkingCollection()
    service = FilesystemDiscovery(collection, queue_capacity=1, batch_size=1)
    _complete(service.begin_files(prior_paths))
    if manual:
        collection.set_order(
            tuple(reversed([entry.entry_id for entry in collection.snapshot().entries]))
        )
    prior = collection.snapshot()

    session = service.begin_folder(root)
    session.start()
    first = session.consume_batch(limit=1, timeout=2)
    assert first and first[0].disposition is DiscoveryDisposition.ACCEPTED
    assert session._producer_waiting.wait(3)
    session.cancel()
    report = session.wait_until_terminal(timeout=3)

    assert report.state is DiscoveryState.CANCELLED
    after = collection.snapshot()
    assert after.entries == prior.entries
    assert after.folder_roots == prior.folder_roots
    assert after.ordering_mode is prior.ordering_mode
    assert after.sort_preferences == prior.sort_preferences
    assert not collection.is_current(prior)


def test_symlink_files_and_linked_ancestors_are_excluded(tmp_path: Path) -> None:
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target_file = target_dir / "real.txt"
    target_file.write_text("keep")
    linked_dir = tmp_path / "linked"
    linked_file = tmp_path / "linked-file.txt"
    try:
        os.symlink(target_dir, linked_dir, target_is_directory=True)
        os.symlink(target_file, linked_file)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    collection = WorkingCollection()
    service = FilesystemDiscovery(collection)

    folder_items, folder_report = _complete(service.begin_folder(linked_dir))
    file_items, file_report = _complete(service.begin_files((linked_file,)))

    assert folder_report.state is DiscoveryState.COMPLETE
    assert folder_items[0].reason in {"reparse_point", "linked_ancestor"}
    assert file_report.state is DiscoveryState.COMPLETE
    assert file_items[0].disposition is DiscoveryDisposition.EXCLUDED
    assert collection.snapshot().entries == ()


def test_linked_ancestor_is_excluded_for_direct_file_import(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "safe.txt"
    source.write_text("safe")
    link = tmp_path / "alias"
    try:
        os.symlink(actual, link, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    collection = WorkingCollection()

    items, report = _complete(FilesystemDiscovery(collection).begin_files((link / "safe.txt",)))

    assert report.state is DiscoveryState.COMPLETE
    assert items[0].reason == "linked_ancestor"
    assert collection.snapshot().entries == ()


def test_parent_traversal_is_rejected_before_absolute_path_normalization(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-parent"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    selected = link / ".." / "outside.txt"
    collection = WorkingCollection()

    items, report = _complete(FilesystemDiscovery(collection).begin_files((selected,)))

    assert report.state is DiscoveryState.COMPLETE
    assert items[0].reason == "parent_traversal"
    assert collection.snapshot().entries == ()


def test_hidden_and_system_attributes_do_not_exclude_regular_files(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows file attributes are unavailable")
    source = tmp_path / "ordinary.txt"
    source.write_text("ordinary")
    attributes = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
    if not ctypes.windll.kernel32.SetFileAttributesW(str(source), attributes):
        pytest.skip("Windows did not apply hidden/system attributes")
    collection = WorkingCollection()

    items, report = _complete(FilesystemDiscovery(collection).begin_files((source,)))

    assert report.state is DiscoveryState.COMPLETE
    assert items[0].disposition is DiscoveryDisposition.ACCEPTED
    assert collection.snapshot().entries[0].metadata.attributes & attributes == attributes


def test_distinct_hard_link_paths_keep_distinct_collection_entries(tmp_path: Path) -> None:
    source = tmp_path / "original.bin"
    linked = tmp_path / "hard-link.bin"
    source.write_bytes(b"same filesystem object")
    try:
        os.link(source, linked)
    except OSError as error:
        pytest.skip(f"hard-link creation unavailable: {error}")
    collection = WorkingCollection()

    items, report = _complete(FilesystemDiscovery(collection).begin_files((source, linked)))

    assert report.state is DiscoveryState.COMPLETE
    assert len(collection.snapshot().entries) == 2
    assert collection.snapshot().entries[0].entry_id != collection.snapshot().entries[1].entry_id
    assert collection.snapshot().entries[0].normalized_path_key != (
        collection.snapshot().entries[1].normalized_path_key
    )
    first_identity = collection.snapshot().entries[0].filesystem_identity
    second_identity = collection.snapshot().entries[1].filesystem_identity
    if first_identity is not None or second_identity is not None:
        assert first_identity is not None and first_identity == second_identity
    assert all(item.disposition is DiscoveryDisposition.ACCEPTED for item in items)


def test_junction_is_excluded_when_windows_can_create_one(tmp_path: Path) -> None:
    if os.name != "nt" or not hasattr(os.path, "isjunction"):
        pytest.skip("junction detection is unavailable on this platform")
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "hidden.txt").write_text("keep")
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not os.path.isjunction(junction):
        pytest.skip(f"junction creation unavailable: {result.stdout} {result.stderr}")
    collection = WorkingCollection()

    items, _ = _complete(FilesystemDiscovery(collection).begin_folder(junction))

    assert items[0].reason == "reparse_point"
    assert collection.snapshot().entries == ()


def test_nonregular_entry_is_reported_without_import(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "special"
    original_lstat = os.lstat

    def as_fifo(path, *args, **kwargs):
        if os.fspath(path) == str(source):
            return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_file_attributes=0)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(discovery_module.os, "lstat", as_fifo)
    collection = WorkingCollection()
    items, report = _complete(FilesystemDiscovery(collection).begin_files((source,)))
    assert report.state is DiscoveryState.COMPLETE
    assert items[0].reason == "non_regular_entry"
    assert items[0].entry_type == "fifo"
    assert collection.snapshot().entries == ()


def test_file_removed_during_direct_import_marks_incomplete(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "gone.txt"
    source.write_text("initial")
    original_lstat = os.lstat

    def disappears(path, *args, **kwargs):
        if os.fspath(path) == str(source):
            raise FileNotFoundError("simulated removal")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(discovery_module.os, "lstat", disappears)
    collection = WorkingCollection()
    items, report = _complete(FilesystemDiscovery(collection).begin_files((source,)))
    assert report.state is DiscoveryState.INCOMPLETE
    assert items[0].disposition is DiscoveryDisposition.INACCESSIBLE
    assert collection.snapshot().entries == ()


def test_metadata_is_a_snapshot_when_a_file_changes_during_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "changing.txt"
    source.write_bytes(b"before")
    original_classify = discovery_module.DiscoverySession._classify_file

    def change_after_snapshot(session, path, info):
        record = original_classify(session, path, info)
        if path == str(source):
            source.write_bytes(b"changed while discovery was running")
        return record

    monkeypatch.setattr(
        discovery_module.DiscoverySession, "_classify_file", change_after_snapshot
    )
    collection = WorkingCollection()
    items, report = _complete(FilesystemDiscovery(collection).begin_files((source,)))

    assert report.state is DiscoveryState.COMPLETE
    entry = collection.snapshot().entries[0]
    assert entry.metadata is not None
    assert entry.metadata.size_bytes == len(b"before")
    assert source.stat().st_size != entry.metadata.size_bytes
    discovered_entry = items[0].entry
    assert discovered_entry is not None
    assert discovered_entry.entry_id == entry.entry_id
    assert discovered_entry.metadata == entry.metadata


def test_unavailable_nonessential_metadata_does_not_invent_values_or_incomplete_scan(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "partial-metadata.txt"
    source.write_text("file")
    original_lstat = os.lstat
    original_stat = os.stat
    unavailable = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_size=None,
        st_mtime_ns=None,
        st_file_attributes=None,
        st_dev=None,
        st_ino=0,
    )

    def limited_lstat(path, *args, **kwargs):
        if os.fspath(path) == str(source):
            return unavailable
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(discovery_module.os, "lstat", limited_lstat)
    monkeypatch.setattr(
        discovery_module.os,
        "stat",
        lambda path, *args, **kwargs: unavailable
        if os.fspath(path) == str(source)
        else original_stat(path, *args, **kwargs),
    )
    collection = WorkingCollection()

    items, report = _complete(FilesystemDiscovery(collection).begin_files((source,)))

    assert report.state is DiscoveryState.COMPLETE
    entry = collection.snapshot().entries[0]
    assert entry.metadata is not None
    assert entry.metadata.size_bytes is None
    assert entry.metadata.modified_time_ns is None
    assert entry.metadata.created_time_ns is None
    assert entry.metadata.attributes is None
    assert entry.filesystem_identity is None
    assert items[0].reason == "filesystem_identity_unavailable"
    assert not collection.snapshot().has_incomplete_imports


def test_worker_failure_rolls_back_and_service_accepts_next_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "good.txt"
    source.write_text("good")
    collection = WorkingCollection()
    service = FilesystemDiscovery(collection)
    failed = service.begin_files((source,))

    def broken_walk():
        raise RuntimeError("simulated worker failure")
        yield

    monkeypatch.setattr(failed, "_walk_files", broken_walk)
    _, report = _complete(failed)
    assert report.state is DiscoveryState.FAILED
    assert collection.snapshot().entries == ()
    _, next_report = _complete(service.begin_files((source,)))
    assert next_report.state is DiscoveryState.COMPLETE
    assert collection.snapshot().entries[0].original_filename == source.name


def test_no_premature_completion_until_results_are_consumed(tmp_path: Path) -> None:
    root = tmp_path / "queued"
    root.mkdir()
    (root / "one.txt").write_text("one")
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection, queue_capacity=1).begin_folder(root)
    session.start()
    assert session._producer_done.wait(3)
    assert session.state is DiscoveryState.FINALIZING
    assert not session.is_terminal
    assert len(collection.snapshot().finalized_entries) == 0
    session.wait_until_terminal()
    assert session.state is DiscoveryState.COMPLETE


def test_deferred_finalization_waits_for_revision_checked_order(tmp_path: Path) -> None:
    z_path = tmp_path / "z.txt"
    a_path = tmp_path / "a.txt"
    z_path.write_text("z")
    a_path.write_text("a")
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection, queue_capacity=1).begin_files(
        (z_path, a_path)
    )
    session.start()
    deadline = time.monotonic() + 3
    discovered = []
    while not session.ready_to_finalize and not session.is_terminal:
        discovered.extend(
            session.consume_batch(limit=1, defer_finalization=True)
        )
        assert time.monotonic() < deadline

    assert [item.entry.original_filename for item in discovered if item.entry] == [
        "z.txt",
        "a.txt",
    ]
    assert session.ready_to_finalize
    assert session.state is DiscoveryState.FINALIZING
    assert len(collection.snapshot().finalized_entries) == 0
    prepared = collection.prepare_import_order(session.session_id)
    session.finish_deferred(prepared)

    assert session.state is DiscoveryState.COMPLETE
    assert [entry.original_filename for entry in collection.snapshot().entries] == [
        "a.txt",
        "z.txt",
    ]
