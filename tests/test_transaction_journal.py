from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

import tidyfiles.transaction_journal as journal_module
from tidyfiles.collection import (
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportMode,
    WorkingCollection,
)
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
    WindowsPathObserver,
)
from tidyfiles.execution_preflight import PreflightRequest, capture_review_plan, run_preflight
from tidyfiles.filename_integration import evaluate_collection
from tidyfiles.modifying_jobs import ModifyingJobCoordinator, ModifyingJobError
from tidyfiles.transaction_journal import JournalError, JournalStore, RecoveryStatus
from tidyfiles.transaction_record import (
    EntryOutcome,
    EntryVerification,
    OperationType,
    TransactionEntry,
    TransactionRecord,
    TransactionState,
    VerifiedOutcome,
)


@pytest.fixture
def locations(tmp_path):
    portable = tmp_path / "portable"
    local = tmp_path / "local"
    portable.mkdir()
    local.mkdir()
    return portable, local, JournalStore(app_dir=portable, local_app_data=local)


def _record(tmp_path):
    serial = (1 << 64) - 13
    identity = FileSystemIdentitySnapshot(serial, (1 << 128) - 17)
    parent = FileSystemIdentitySnapshot(serial, (1 << 127) + 19)
    source = PathObservation(
        PathOccupancy.FILE,
        actual_name="before.txt",
        actual_parent=r"\example",
        volume_key=r"\DEVICE\HARDDISKVOLUME2",
        volume_guid="test-volume-guid",
        parent_identity=parent,
        object_metadata=WindowsObjectObservation(identity, 10, 20, 30, 40, 32),
    )
    target = replace(source, occupancy=PathOccupancy.VACANT, actual_name=None, object_metadata=None)
    entry = TransactionEntry(
        uuid4(), str(tmp_path / "before.txt"), str(tmp_path / "after.txt"), source, target
    )
    return TransactionRecord.prepared(OperationType.RENAME, (entry,))


def _index(store):
    return json.loads(store.locator_path.read_text(encoding="utf-8"))["journals"]


def _found(store):
    discovered = store.discover()
    assert not discovered.storage_issues
    (transaction,) = discovered.transactions
    (location,) = transaction.locations
    return location


def _evidence(entry, outcome=VerifiedOutcome.DESTINATION, artifacts=()):
    path = entry.destination_path if outcome is VerifiedOutcome.DESTINATION else entry.source_path
    actual = replace(entry.source_observation, actual_name=Path(path).name)
    return (EntryVerification(entry.entry_id, outcome, path, actual, tuple(artifacts)),)


def test_c01_c02_c03_c06_portable_round_trip_and_checkpoint(locations, tmp_path):
    portable, local, store = locations
    record = _record(tmp_path)
    session = store.prepare(record)
    assert session.path == portable / "journals" / f"{record.transaction_id}.json"
    assert not (local / "TidyFiles" / "journals").exists()
    assert _index(store) == {str(record.transaction_id): str(session.path)}
    assert not session.can_modify
    payload = json.loads(session.path.read_text(encoding="utf-8"))
    encoded = payload["entries"][0]["source_observation"]["object"]["identity"]
    assert encoded == {
        "file_id": f"{((1 << 128) - 17):032x}",
        "volume_serial": f"{((1 << 64) - 13):016x}",
    }
    assert _found(store).record == record

    session.start()
    assert session.can_modify
    row = replace(record.entries[0], stage="before_staging", staging_path=str(tmp_path / "stage"))
    updated = session.checkpoint(row)
    assert updated.state is TransactionState.IN_PROGRESS
    assert updated.entries == (row,)
    assert _found(store).record == updated
    mtime = session.path.stat().st_mtime_ns
    assert session.checkpoint(row) == updated
    assert session.path.stat().st_mtime_ns == mtime
    with pytest.raises(ValueError, match="recorded artifact"):
        session.checkpoint(replace(row, staging_path=None))


@pytest.mark.parametrize("boundary", ["initial", "locator"])
def test_c04_c08_failed_preparation_never_returns_a_modification_session(
    locations, tmp_path, monkeypatch, boundary
):
    portable, local, store = locations
    publish = journal_module._publish

    def fail(source, target, *, overwrite):
        if (boundary == "locator") == (target.name == "recovery_index.json"):
            raise PermissionError("injected publication failure")
        publish(source, target, overwrite=overwrite)

    monkeypatch.setattr(journal_module, "_publish", fail)
    record = _record(tmp_path)
    with pytest.raises(JournalError, match="Cannot prepare"):
        store.prepare(record)
    assert not store.locator_path.exists()
    portable_journal = portable / "journals" / f"{record.transaction_id}.json"
    assert portable_journal.exists() == (boundary == "locator")
    assert not list(local.rglob("*.tmp"))
    assert not list(portable.rglob("*.tmp"))
    if boundary == "locator":
        assert _found(store).record.state is TransactionState.PREPARED


@pytest.mark.parametrize("boundary", ["flush", "replace", "index_lost", "damaged"])
def test_c05_failed_update_latches_closed_and_retains_evidence(
    locations, tmp_path, monkeypatch, boundary
):
    _, local, store = locations
    session = store.prepare(_record(tmp_path))
    session.start()
    expected = session.path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("injected checkpoint failure")

    if boundary == "flush":
        monkeypatch.setattr(journal_module.os, "fsync", fail)
    elif boundary == "replace":
        monkeypatch.setattr(journal_module, "_publish", fail)
    elif boundary == "index_lost":
        store.locator_path.unlink()
    else:
        expected = b"damaged record"
        session.path.write_bytes(expected)
    row = replace(session.record.entries[0], stage="before_install")
    with pytest.raises(JournalError, match="Cannot persist"):
        session.checkpoint(row)
    assert session.path.read_bytes() == expected
    assert not session.can_modify
    with pytest.raises(JournalError):
        session.require_modification()
    assert not (local / "TidyFiles" / "journals").exists()
    assert not list(session.path.parent.glob("*.tmp"))


def test_c07_c10_fallback_journal_stays_pinned_when_portable_becomes_available(locations, tmp_path):
    portable, local, store = locations
    unavailable = portable / "journals"
    unavailable.write_bytes(b"test-owned obstruction")
    record = _record(tmp_path)
    session = store.prepare(record)
    assert session.path == local / "TidyFiles" / "journals" / f"{record.transaction_id}.json"
    unavailable.unlink()
    session.start()
    assert not unavailable.exists()
    assert _found(store).path == session.path
    assert _index(store) == {str(record.transaction_id): str(session.path)}


def test_c09_relocation_and_multiple_discovery_paths(locations, tmp_path):
    portable, local, store = locations
    session = store.prepare(_record(tmp_path))
    assert _found(store).path == session.path  # Directory plus locator produce one observation.
    session.start()
    moved_app = tmp_path / "new-app-location"
    moved_app.mkdir()
    relocated = JournalStore(app_dir=moved_app, local_app_data=local)
    assert _found(relocated).record == session.record
    assert not (moved_app / "journals").exists()
    moved_journals = (portable / "journals").rename(moved_app / "journals")
    (found,) = relocated.discover().transactions
    by_status = {item.status: item.path for item in found.locations}
    assert by_status == {
        RecoveryStatus.UNRESOLVED: moved_journals / session.path.name,
        RecoveryStatus.MISSING: session.path,
    }
    assert _index(relocated) == {str(session.record.transaction_id): str(session.path)}


@pytest.mark.parametrize(
    ("condition", "status"),
    [
        ("missing", RecoveryStatus.MISSING),
        ("permissions", RecoveryStatus.INACCESSIBLE),
        ("malformed", RecoveryStatus.DAMAGED),
        ("version", RecoveryStatus.DAMAGED),
    ],
)
def test_c11_c12_unavailable_and_damaged_journals_are_preserved(
    locations, tmp_path, monkeypatch, condition, status
):
    _, _, store = locations
    session = store.prepare(_record(tmp_path))
    before = store.locator_path.read_bytes()
    if condition == "missing":
        session.path.unlink()
    elif condition == "permissions":
        read = journal_module._read

        def inaccessible(path):
            if path == session.path:
                raise PermissionError("injected access failure")
            return read(path)

        monkeypatch.setattr(journal_module, "_read", inaccessible)
    elif condition == "malformed":
        session.path.write_bytes(b'{"bad":')
    else:
        payload = json.loads(session.path.read_text(encoding="utf-8"))
        payload["schema_version"] = 2
        session.path.write_text(json.dumps(payload), encoding="utf-8")
    content = session.path.read_bytes() if session.path.exists() else None
    found = _found(store)
    assert found.status is status and found.record is None
    assert store.locator_path.read_bytes() == before
    assert (session.path.read_bytes() if session.path.exists() else None) == content


def test_c12_damaged_locator_is_visible_and_blocks_new_records(locations, tmp_path):
    portable, _, store = locations
    store.prepare(_record(tmp_path))
    corrupted = b'{"schema_version":1,"journals":{},"journals":{}}'
    store.locator_path.write_bytes(corrupted)
    report = store.discover()
    assert report.storage_issues[0].status is RecoveryStatus.DAMAGED
    assert len(report.transactions) == 1
    with pytest.raises(JournalError, match="Duplicate JSON field"):
        store.prepare(_record(tmp_path))
    assert store.locator_path.read_bytes() == corrupted
    assert len(list((portable / "journals").glob("*.json"))) == 1


def test_c08_failed_registration_preserves_other_locator_entries(locations, tmp_path, monkeypatch):
    _, _, store = locations
    prior = store.prepare(_record(tmp_path))
    before = store.locator_path.read_bytes()
    native = journal_module._publish

    def publish(source, target, *, overwrite):
        if target == store.locator_path:
            raise PermissionError("injected locator update failure")
        native(source, target, overwrite=overwrite)

    monkeypatch.setattr(journal_module, "_publish", publish)
    with pytest.raises(JournalError, match="Cannot prepare"):
        store.prepare(_record(tmp_path))
    assert store.locator_path.read_bytes() == before
    assert len(store.discover().transactions) == 2
    assert prior.path.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows junction")
def test_journal_store_refuses_redirected_portable_directory(locations, tmp_path):
    import _winapi

    portable, _, store = locations
    target = tmp_path / "unrelated"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_bytes(b"preserve")
    _winapi.CreateJunction(str(target), str(portable / "journals"))
    session = store.prepare(_record(tmp_path))
    assert session.path.parent != portable / "journals"
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"
    assert store.discover().storage_issues[0].status is RecoveryStatus.INACCESSIBLE


def test_c13_recovery_discovery_never_recovers_or_removes_any_file(locations, tmp_path):
    _, local, store = locations
    record = _record(tmp_path)
    row = record.entries[0]
    source = Path(row.source_path)
    source.write_bytes(b"test-owned original")
    stage = tmp_path / "stage.txt"
    stage.write_bytes(b"protected evidence")
    session = store.prepare(record)
    session.start()
    session.checkpoint(replace(row, staging_path=str(stage), stage="before_install"))
    expected = {
        path: path.read_bytes() for path in (source, stage, store.locator_path, session.path)
    }
    new_instance = JournalStore(app_dir=tmp_path / "absent-portable", local_app_data=local)
    location = _found(new_instance)
    assert location.status is RecoveryStatus.UNRESOLVED
    assert location.record.entries[0].stage == "before_install"
    assert location.record.entries[0].staging_path == str(stage)
    assert {path: path.read_bytes() for path in expected} == expected
    assert not (tmp_path / "absent-portable").exists()


@pytest.mark.parametrize("outcome", [VerifiedOutcome.DESTINATION, VerifiedOutcome.ORIGINAL])
def test_c14_verified_finalization_retires_only_owned_records(locations, tmp_path, outcome):
    _, _, store = locations
    session = store.prepare(_record(tmp_path))
    coordinator = ModifyingJobCoordinator()
    job = coordinator.reserve(OperationType.RENAME)
    coordinator.begin_modification(job.job_id, session)
    second = store.prepare(_record(tmp_path))
    stage = tmp_path / "retained-temp.txt"
    stage.write_text("independently verified redundant artifact", encoding="utf-8")
    row = replace(session.record.entries[0], staging_path=str(stage), stage="applied")
    session.checkpoint(row)
    result = session.finalize(_evidence(row, outcome, artifacts=(str(stage),)))
    expected = (
        TransactionState.FAILED_SAFE
        if outcome is VerifiedOutcome.ORIGINAL
        else TransactionState.FINALIZED
    )
    assert result.state is expected
    assert not session.path.exists()
    assert not session.can_modify
    coordinator.finish(job.job_id)
    assert not coordinator.busy
    assert stage.is_file()
    assert _index(store) == {str(second.record.transaction_id): str(second.path)}
    assert _found(store).record == second.record


def test_c15_uncertain_outcomes_retain_record_and_all_registered_paths(locations, tmp_path):
    _, _, store = locations
    session = store.prepare(_record(tmp_path))
    session.start()
    row = replace(
        session.record.entries[0],
        stage="staged",
        staging_path=str(tmp_path / "original"),
        outcome=EntryOutcome.RECOVERY_REQUIRED,
    )
    interrupted = session.checkpoint(row)
    assert interrupted.state is TransactionState.RECOVERY_REQUIRED
    assert not session.can_modify
    before = session.path.read_bytes(), store.locator_path.read_bytes()
    with pytest.raises(ValueError, match="all recorded artifacts"):
        session.finalize(_evidence(row))
    with pytest.raises(ValueError, match="complete ordered"):
        session.finalize(())
    (proof,) = _evidence(row, artifacts=(row.staging_path,))
    other = replace(proof.observation, actual_name="another.txt")
    with pytest.raises(ValueError, match="intended location"):
        session.finalize((replace(proof, observation=other),))
    assert (session.path.read_bytes(), store.locator_path.read_bytes()) == before
    assert _found(store).record.state is TransactionState.RECOVERY_REQUIRED


@pytest.mark.parametrize("failure", ["locator", "journal", "replaced", "restore_changed"])
def test_c15_failed_finalization_retains_evidence(locations, tmp_path, monkeypatch, failure):
    _, _, store = locations
    session = store.prepare(_record(tmp_path))
    session.start()
    original_publish, original_unlink = journal_module._publish, Path.unlink

    def publish(source, target, *, overwrite):
        if target == store.locator_path and failure == "locator":
            raise OSError("locator cleanup fault")
        original_publish(source, target, overwrite=overwrite)
        if target == store.locator_path and failure == "replaced" and not _index(store):
            session.path.write_bytes(b"external journal edit")

    def unlink(path, *args, **kwargs):
        if path == session.path and failure in {"journal", "restore_changed"}:
            if failure == "restore_changed":
                store.locator_path.write_bytes(b"damaged locator during cleanup")
            raise OSError("journal cleanup fault")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(journal_module, "_publish", publish)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(JournalError):
        session.finalize(_evidence(session.record.entries[0]))
    assert session.path.exists() and not session.can_modify
    if failure == "restore_changed":
        assert store.locator_path.read_bytes() == b"damaged locator during cleanup"
        report = store.discover()
        assert report.storage_issues[0].status is RecoveryStatus.DAMAGED
        assert report.transactions[0].locations[0].status is RecoveryStatus.CLEANUP_PENDING
        return
    assert _index(store) == {str(session.record.transaction_id): str(session.path)}
    if failure == "replaced":
        assert _found(store).status is RecoveryStatus.DAMAGED
        assert session.path.read_bytes() == b"external journal edit"
    else:
        assert _found(store).status is RecoveryStatus.CLEANUP_PENDING
        assert _found(store).record == session.record


def test_c16_c17_c18_exclusive_job_requires_persisted_modification_boundary(locations, tmp_path):
    _, _, store = locations
    coordinator = ModifyingJobCoordinator()
    session = store.prepare(_record(tmp_path))
    job = coordinator.reserve(OperationType.RENAME)
    assert coordinator.busy and coordinator.can_cancel and coordinator.normal_close_permitted
    with pytest.raises(ModifyingJobError, match="active"):
        coordinator.reserve(OperationType.TRASH)
    coordinator.begin_modification(job.job_id, session)
    assert coordinator.current.modifying and session.can_modify
    assert not coordinator.can_cancel and not coordinator.normal_close_permitted
    assert not coordinator.cancel_preparation(job.job_id)
    with pytest.raises(ModifyingJobError, match="verified outcome"):
        coordinator.finish(job.job_id)
    session.checkpoint(recovery_required="failed during modification")
    coordinator.finish(job.job_id)
    assert not coordinator.busy and coordinator.normal_close_permitted

    next_job = coordinator.reserve(OperationType.REPACK)
    assert coordinator.cancel_preparation(next_job.job_id)
    with pytest.raises(ModifyingJobError):
        coordinator.begin_modification(next_job.job_id, session)
    with pytest.raises(ModifyingJobError, match="does not own"):
        coordinator.finish(job.job_id)
    coordinator.finish(next_job.job_id)


def test_c17_start_boundary_failure_does_not_activate_modification(
    locations, tmp_path, monkeypatch
):
    _, _, store = locations
    session = store.prepare(_record(tmp_path))
    coordinator = ModifyingJobCoordinator()
    job = coordinator.reserve(OperationType.RENAME)

    def fail(*_args, **_kwargs):
        raise PermissionError("injected start failure")

    monkeypatch.setattr(journal_module, "_publish", fail)
    with pytest.raises(JournalError):
        coordinator.begin_modification(job.job_id, session)
    assert not coordinator.current.modifying and coordinator.normal_close_permitted
    assert not session.can_modify
    coordinator.finish(job.job_id)


def test_c16_two_threads_cannot_claim_the_same_job_slot():
    coordinator = ModifyingJobCoordinator()
    barrier = Barrier(2)

    def start(operation):
        barrier.wait(5)
        try:
            return coordinator.reserve(operation)
        except ModifyingJobError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(start, (OperationType.RENAME, OperationType.TRASH)))
    (winner,) = (job for job in results if job is not None)
    coordinator.finish(winner.job_id)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file IDs")
def test_windows_preflight_intended_set_round_trips_into_journal(locations, tmp_path):
    _, _, store = locations
    source = tmp_path / "actual.txt"
    source.write_bytes(b"original")
    observation = WindowsPathObserver(capture_identity=True)(str(source))
    metadata = observation.object_metadata
    assert metadata is not None
    collection = WorkingCollection()
    batch = collection.begin_import(ImportMode.ADD_FILES)
    entry = collection.register_path(
        batch,
        source,
        filesystem_identity=metadata.identity,
        metadata=FileMetadataSnapshot(
            size_bytes=metadata.size_bytes,
            modified_time_ns=metadata.modified_time_ns,
            created_time_ns=metadata.created_time_ns,
            attributes=metadata.attributes,
        ),
    ).entry
    collection.finalize_import(batch)
    snapshot = collection.snapshot()
    evaluated = evaluate_collection(snapshot, "candidate")
    reviewed = capture_review_plan(snapshot, evaluated, current_request_id=evaluated.request_id)
    request = PreflightRequest(
        snapshot,
        reviewed,
        evaluated,
        evaluated.request_id,
        "candidate",
        evaluated.settings,
        reviewed.options,
        (entry.entry_id,),
    )
    record = TransactionRecord.from_rename_preflight(run_preflight(request))
    store.prepare(record)
    assert _found(store).record == record
    (row,) = record.entries
    assert row.entry_id == entry.entry_id and row.source_observation == observation
    assert row.destination_path == str(tmp_path / "candidate.txt")
    assert source.read_bytes() == b"original"
    assert not Path(row.destination_path).exists()
