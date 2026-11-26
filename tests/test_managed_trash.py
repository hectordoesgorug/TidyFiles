from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_discovery import _complete

import tidyfiles.managed_trash as trash_module
import tidyfiles.storage as storage
import tidyfiles.transaction_journal as journal_module
import tidyfiles.windows_trash as native
from tidyfiles.collection import WorkingCollection
from tidyfiles.destination_observation import (
    _INVALID_HANDLE,
    PathOccupancy,
    WindowsPathObserver,
)
from tidyfiles.discovery import FilesystemDiscovery
from tidyfiles.managed_trash import TrashExecutor, TrashOutcome, TrashScope, TrashSource, TrashState
from tidyfiles.modifying_jobs import ModifyingJobCoordinator
from tidyfiles.transaction_journal import JournalSession, JournalStore, RecoveryStatus
from tidyfiles.transaction_record import (
    TRASH_COPY_BEFORE_REMOVE,
    TRASH_COPY_REMOVED,
    OperationType,
    TransactionState,
    VerifiedOutcome,
    validate_verification,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows Trash tests")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    app = tmp_path / "application"
    app.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    store = JournalStore(app_dir=app, local_app_data=local)
    coordinator = ModifyingJobCoordinator()
    monkeypatch.setattr(storage, "application_directory", lambda: app)
    storage.managed_trash_path.cache_clear()
    try:
        yield app, store, coordinator, TrashExecutor(coordinator, store)
    finally:
        storage.managed_trash_path.cache_clear()


@pytest.fixture
def other_volume(runtime):
    app, *_ = runtime
    checkout = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="tidyfiles-m3e-volume-", dir=checkout) as temporary:
        root = Path(temporary).resolve(strict=True)
        assert root.parent == checkout and root.name.startswith("tidyfiles-m3e-volume-")
        observe = WindowsPathObserver(capture_identity=True)
        one, other = observe(str(app / "probe")), observe(str(root / "probe"))
        assert one.occupancy is other.occupancy is PathOccupancy.VACANT
        if one.volume_guid == other.volume_guid:
            pytest.skip(
                "Cross-volume Trash verification needs checkout and TEMP on distinct volumes"
            )
        yield root


def _file(parent, name="original.txt", payload=b"original contents"):
    path = parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _scope(*paths):
    observe = WindowsPathObserver(capture_identity=True)
    rows = tuple(TrashSource(uuid4(), str(path), observe(str(path))) for path in paths)
    assert all(row.observation.occupancy is PathOccupancy.FILE for row in rows)
    return TrashScope(rows)


def _record(store):
    discovery = store.discover()
    assert not discovery.storage_issues
    (transaction,) = discovery.transactions
    (location,) = transaction.locations
    assert location.status is RecoveryStatus.UNRESOLVED
    return location.record


def _retired(store, coordinator):
    discovery = store.discover()
    assert not discovery.transactions and not discovery.storage_issues
    assert not coordinator.busy and coordinator.normal_close_permitted


def _times(path):
    facts = path.stat()
    return facts.st_birthtime_ns, facts.st_mtime_ns


def test_e01_e16_same_volume_preserves_identity_and_retires(runtime, tmp_path, caplog):
    app, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    facts, content = source.stat(), source.read_bytes()
    caplog.set_level(logging.INFO, logger=trash_module.__name__)

    result = executor.execute(authorized=_scope(source))

    assert result.state is TrashState.SUCCEEDED, result
    assert result.transaction_id is not None
    (entry,) = result.entries
    assert entry.outcome is TrashOutcome.TRASHED
    target = Path(entry.trash_path)
    assert target.name == source.name
    assert target.parent.parent == app / "trash"
    assert target.read_bytes() == content and not source.exists()
    assert target.stat().st_ino == facts.st_ino
    assert _times(target) == (facts.st_birthtime_ns, facts.st_mtime_ns)
    assert str(source) in caplog.text and str(target) in caplog.text
    _retired(store, coordinator)


def test_e02_native_cross_volume_verifies_before_removing_source(
    runtime, other_volume, monkeypatch
):
    _, store, coordinator, executor = runtime
    content = (b"complete payload\0" * 70001) + b"tail"
    source = _file(other_volume, payload=content)
    scope = _scope(source)
    times = _times(source)
    remove = native._remove_original
    checked = []

    def before_remove(handle):
        row = _record(store).entries[0]
        assert source.exists()
        assert row.stage == TRASH_COPY_BEFORE_REMOVE
        assert row.verification.outcome is VerifiedOutcome.TRASH_COPY
        assert row.verification.content_sha256 == hashlib.sha256(content).hexdigest()
        assert row.verification.observation.volume_guid != row.source_observation.volume_guid
        with pytest.raises(ValueError, match="cross-volume Trash content evidence"):
            validate_verification(row, row.verification, OperationType.RENAME)
        with pytest.raises(ValueError, match="cross-volume Trash content evidence"):
            validate_verification(
                row, replace(row.verification, content_sha256=None), OperationType.TRASH
            )
        checked.append(row.verification)
        remove(handle)

    monkeypatch.setattr(native, "_remove_original", before_remove)
    result = executor.execute(authorized=scope)
    assert result.state is TrashState.SUCCEEDED, result
    (proof,) = checked
    target = Path(result.entries[0].trash_path)
    assert target == Path(proof.path) and not source.exists()
    assert target.read_bytes() == content and _times(target) == times
    _retired(store, coordinator)


def test_e03_e04_basename_collisions_and_new_batches(runtime, tmp_path):
    app, store, coordinator, executor = runtime
    first = _file(tmp_path / "first", payload=b"one")
    second = _file(tmp_path / "second", name="ORIGINAL.TXT", payload=b"two")
    result = executor.execute(authorized=_scope(first, second))
    assert result.state is TrashState.SUCCEEDED, result
    one, two = (Path(entry.trash_path) for entry in result.entries)
    assert one.name == first.name and one.name.casefold() != two.name.casefold()
    assert one.parent == two.parent
    assert {one.read_bytes(), two.read_bytes()} == {b"one", b"two"}

    third = _file(first.parent, payload=b"three")
    current = executor.execute(authorized=_scope(third))
    assert current.state is TrashState.SUCCEEDED, current
    new = Path(current.entries[0].trash_path)
    assert new.parent != one.parent
    assert set((app / "trash").iterdir()) == {new.parent, one.parent}
    assert [path.read_bytes() for path in (one, two, new)] == [b"one", b"two", b"three"]
    _retired(store, coordinator)


@pytest.mark.parametrize("fault", ["obstructed", "unwritable"])
def test_e05_unavailable_trash_preserves_source(runtime, tmp_path, monkeypatch, fault):
    app, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    if fault == "obstructed":
        (app / "trash").write_bytes(b"unrelated existing file")
    else:

        def unavailable(*args, **kwargs):
            raise PermissionError("injected Trash access failure")

        monkeypatch.setattr(native, "_directory", unavailable)
    identity = source.stat().st_ino
    result = executor.execute(authorized=_scope(source))
    assert result.state is TrashState.FAILED_SAFE and result.transaction_id is None
    assert source.stat().st_ino == identity and source.read_bytes() == b"original contents"
    if fault == "obstructed":
        assert (app / "trash").read_bytes() == b"unrelated existing file"
    assert not (tmp_path / "local" / "TidyFiles" / "trash").exists()
    _retired(store, coordinator)


def test_e06_insufficient_space_blocks_cross_volume_copy(runtime, other_volume, monkeypatch):
    app, store, coordinator, executor = runtime
    source = _file(other_volume)
    monkeypatch.setattr(trash_module.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    result = executor.execute(authorized=_scope(source))
    assert result.state is TrashState.FAILED_SAFE and result.transaction_id is None
    assert "Insufficient space" in result.detail
    assert source.read_bytes() == b"original contents"
    assert not list((app / "trash").rglob("*.txt"))
    _retired(store, coordinator)


@pytest.mark.parametrize("fault", ["interrupted", "corrupt", "source_changed", "removal"])
def test_e07_e08_e09_failed_cross_volume_transfer_preserves_evidence(
    runtime, other_volume, monkeypatch, fault
):
    _, store, coordinator, executor = runtime
    source = _file(other_volume)
    scope = _scope(source)
    identity = source.stat().st_ino
    copy = native._write_data

    def bad_copy(reader, writer):
        if fault == "interrupted":
            writer.write(b"partial")
            raise OSError("injected interruption")
        elif fault == "corrupt":
            writer.write(b"damaged contents!")
            writer.flush()
        else:
            copy(reader, writer)
            observer = WindowsPathObserver(capture_identity=True)
            handle = observer._open(str(source), 0x100, 7, None, 3, 0x200000, None)
            assert handle != _INVALID_HANDLE
            try:
                written = scope.sources[0].observation.object_metadata.modified_time_ns // 100
                native._set_information(
                    handle,
                    0,
                    native._BasicInfo(written=written + native._FILETIME_UNIX_EPOCH + 10000000),
                )
            finally:
                observer._close(handle)

    def denied(handle):
        raise PermissionError("injected removal failure")

    if fault == "removal":
        monkeypatch.setattr(native, "_remove_original", denied)
    else:
        monkeypatch.setattr(native, "_write_data", bad_copy)
    result = executor.execute(authorized=scope)
    assert result.state is TrashState.RECOVERY_REQUIRED, result
    assert source.read_bytes() == b"original contents" and source.stat().st_ino == identity
    row = _record(store).entries[0]
    target = Path(row.destination_path)
    assert target.is_file() and not coordinator.busy
    if fault == "removal":
        assert result.entries[0].trash_path == str(target)
        assert row.stage == TRASH_COPY_BEFORE_REMOVE and row.verification is not None
        assert target.read_bytes() == source.read_bytes()
    else:
        assert row.stage == "before_trash_copy" and row.verification is None
        if fault == "source_changed":
            assert "changed" in result.detail or "no longer matches" in result.detail
            assert (
                source.stat().st_mtime_ns
                != scope.sources[0].observation.object_metadata.modified_time_ns
            )


def test_e10_destination_appearing_after_checkpoint_is_preserved(runtime, tmp_path, monkeypatch):
    _, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    checkpoint = JournalSession.checkpoint

    def occupy(session, *rows, **kwargs):
        result = checkpoint(session, *rows, **kwargs)
        if rows and rows[0].stage == "before_trash_move":
            Path(rows[0].destination_path).write_bytes(b"unrelated destination")
        return result

    monkeypatch.setattr(JournalSession, "checkpoint", occupy)
    result = executor.execute(authorized=_scope(source))
    assert result.state is TrashState.RECOVERY_REQUIRED, result
    assert source.read_bytes() == b"original contents"
    row = _record(store).entries[0]
    assert Path(row.destination_path).read_bytes() == b"unrelated destination"
    assert result.entries[0].trash_path is None and not coordinator.busy


def test_late_failure_preserves_completed_and_unprocessed_files(runtime, tmp_path, monkeypatch):
    _, store, coordinator, executor = runtime
    sources = tuple(
        _file(tmp_path / "sources", name) for name in ("one.txt", "two.txt", "three.txt")
    )
    publish = journal_module._publish

    def fail(temporary, destination, *, overwrite):
        if destination != store.locator_path:
            stages = [row["stage"] for row in json.loads(temporary.read_bytes())["entries"]]
            if stages == ["trashed", "before_trash_move", "prepared"]:
                raise OSError("injected failure before second move")
        return publish(temporary, destination, overwrite=overwrite)

    monkeypatch.setattr(journal_module, "_publish", fail)
    result = executor.execute(authorized=_scope(*sources))
    assert result.state is TrashState.RECOVERY_REQUIRED, result
    assert [entry.outcome for entry in result.entries] == [
        TrashOutcome.TRASHED,
        TrashOutcome.FAILED_SAFE,
        TrashOutcome.NOT_PROCESSED,
    ]
    assert not sources[0].exists() and not coordinator.busy
    assert Path(result.entries[0].trash_path).read_bytes() == b"original contents"
    assert all(source.read_bytes() == b"original contents" for source in sources[1:])
    assert [entry.stage for entry in _record(store).entries] == ["trashed", "prepared", "prepared"]


def test_e11_replacement_after_authorization_is_not_removed(runtime, tmp_path):
    app, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    scope = _scope(source)
    old = source.rename(source.with_suffix(".held"))
    _file(source.parent, payload=b"unrelated replacement")

    result = executor.execute(authorized=scope)
    assert result.state is TrashState.FAILED_SAFE and result.transaction_id is None
    assert old.read_bytes() == b"original contents"
    assert source.read_bytes() == b"unrelated replacement"
    assert not (app / "trash").exists()
    _retired(store, coordinator)


@pytest.mark.parametrize("redirect", ["source", "trash"])
def test_e12_redirected_source_or_trash_is_rejected(runtime, tmp_path, redirect):
    from _winapi import CreateJunction

    app, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    scope = _scope(source)
    if redirect == "source":
        junction = tmp_path / "junction"
        CreateJunction(str(source.parent), str(junction))
        scope = TrashScope((replace(scope.sources[0], path=str(junction / source.name)),))
    else:
        target = tmp_path / "unrelated"
        target.mkdir()
        CreateJunction(str(target), str(app / "trash"))
    result = executor.execute(authorized=scope)
    assert result.state is TrashState.FAILED_SAFE, result
    assert source.read_bytes() == b"original contents"
    if redirect == "trash":
        assert list(target.iterdir()) == []
    _retired(store, coordinator)


@pytest.mark.parametrize("fault", ["journal", "locator"])
def test_e13_journal_and_locator_preparation_fail_closed(runtime, tmp_path, monkeypatch, fault):
    _, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    publish = journal_module._publish

    def fail(temporary, destination, *, overwrite):
        if (destination == store.locator_path) == (fault == "locator"):
            raise PermissionError("injected preparation failure")
        return publish(temporary, destination, overwrite=overwrite)

    monkeypatch.setattr(journal_module, "_publish", fail)
    result = executor.execute(authorized=_scope(source))
    assert result.state is TrashState.FAILED_SAFE
    assert result.transaction_id is not None
    assert source.read_bytes() == b"original contents"
    assert not Path(result.entries[0].destination_path).exists() and not coordinator.busy
    if fault == "locator":
        assert _record(store).state is TransactionState.PREPARED
    else:
        _retired(store, coordinator)


@pytest.mark.parametrize("fault", [TRASH_COPY_BEFORE_REMOVE, TRASH_COPY_REMOVED])
def test_e14_e15_checkpoint_failure_around_source_removal(
    runtime, other_volume, monkeypatch, fault
):
    _, store, coordinator, executor = runtime
    source = _file(other_volume)
    publish = journal_module._publish

    def fail(temporary, destination, *, overwrite):
        if destination != store.locator_path:
            record = json.loads(temporary.read_bytes())
            if record["entries"][0]["stage"] == fault:
                raise OSError("injected checkpoint failure")
        return publish(temporary, destination, overwrite=overwrite)

    monkeypatch.setattr(journal_module, "_publish", fail)
    result = executor.execute(authorized=_scope(source))
    assert result.state is TrashState.RECOVERY_REQUIRED, result
    assert not coordinator.busy and result.entries[0].outcome is TrashOutcome.RECOVERY_REQUIRED
    row = _record(store).entries[0]
    assert Path(row.destination_path).read_bytes() == b"original contents"
    if fault == TRASH_COPY_BEFORE_REMOVE:
        assert source.read_bytes() == b"original contents"
        assert row.stage == "before_trash_copy" and row.verification is None
    else:
        assert not source.exists()
        assert row.stage == TRASH_COPY_BEFORE_REMOVE and row.verification is not None


def test_e17_busy_coordinator_and_missing_authorization(runtime, tmp_path):
    app, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources")
    assert executor.execute(authorized=None).state is TrashState.REJECTED
    job = coordinator.reserve(OperationType.RENAME)
    try:
        result = executor.execute(authorized=_scope(source))
        assert result.state is TrashState.REJECTED and coordinator.current == job
        assert not (app / "trash").exists() and source.is_file()
    finally:
        coordinator.finish(job.job_id)
    _retired(store, coordinator)


def test_e18_e19_discovery_excludes_actual_trash_not_the_basename(runtime, tmp_path):
    app, _, _, _ = runtime
    managed = _file(app / "trash" / "batch", name="managed.txt")
    ordinary = _file(tmp_path / "user" / "trash", name="ordinary.txt")
    collection = WorkingCollection()
    _complete(FilesystemDiscovery(collection).begin_folder(tmp_path))
    assert tuple(row.source_path for row in collection.snapshot().entries) == (str(ordinary),)
    observer = WindowsPathObserver(capture_identity=True)
    assert observer(str(managed)).reason == "managed_location"
    assert observer(str(ordinary)).occupancy is PathOccupancy.FILE


def test_e20_same_volume_zero_byte_distinct_hard_links(runtime, tmp_path):
    _, store, coordinator, executor = runtime
    source = _file(tmp_path / "sources", payload=b"")
    second = source.with_name("other.txt")
    outside = source.with_name("outside.txt")
    os.link(source, second)
    os.link(source, outside)
    identity = source.stat().st_ino
    result = executor.execute(authorized=_scope(source, second))
    assert result.state is TrashState.SUCCEEDED, result
    assert not source.exists() and not second.exists()
    assert outside.stat().st_ino == identity and outside.read_bytes() == b""
    targets = tuple(Path(row.trash_path) for row in result.entries)
    assert len(set(targets)) == 2
    assert all(
        target.stat().st_ino == identity and target.read_bytes() == b"" for target in targets
    )
    _retired(store, coordinator)


def test_e20_cross_volume_zero_byte_and_hard_links(runtime, other_volume):
    _, store, coordinator, executor = runtime
    source = _file(other_volume, payload=b"")
    second = source.with_name("other.txt")
    outside = source.with_name("outside.txt")
    os.link(source, second)
    os.link(source, outside)
    identity = source.stat().st_ino
    result = executor.execute(authorized=_scope(source, second))
    assert result.state is TrashState.SUCCEEDED, result
    assert not source.exists() and not second.exists()
    assert outside.stat().st_ino == identity and outside.read_bytes() == b""
    targets = [Path(row.trash_path) for row in result.entries]
    assert all(target.read_bytes() == b"" for target in targets)
    assert len({target.stat().st_ino for target in targets}) == 2
    _retired(store, coordinator)


def test_cross_volume_source_with_alternate_stream_is_preserved(runtime, other_volume):
    _, store, coordinator, executor = runtime
    source = _file(other_volume)
    stream = Path(f"{source}:additional")
    stream.write_bytes(b"other file data")
    result = executor.execute(authorized=_scope(source))
    assert result.state is TrashState.FAILED_SAFE, result
    assert "Alternate streams" in result.detail
    assert source.read_bytes() == b"original contents" and stream.read_bytes() == b"other file data"
    _retired(store, coordinator)
