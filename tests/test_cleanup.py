from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

import tidyfiles.cleanup as cleanup_module
import tidyfiles.storage as storage
from tidyfiles.cleanup import (
    AutoCleanRequest,
    AutoCleanService,
    CleanupStatus,
    TemporaryArtifact,
    TemporaryArtifactCleaner,
    TrashClearService,
)
from tidyfiles.collection import ImportDiagnostic, ImportMode, WorkingCollection
from tidyfiles.destination_observation import (
    WindowsPathObserver,
)
from tidyfiles.modifying_jobs import ModifyingJobCoordinator
from tidyfiles.transaction_journal import JournalStore
from tidyfiles.transaction_record import (
    EntryOutcome,
    EntryVerification,
    OperationType,
    TransactionEntry,
    TransactionRecord,
    VerifiedOutcome,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows cleanup tests")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    app = tmp_path / "application"
    local = tmp_path / "local"
    app.mkdir()
    local.mkdir()
    monkeypatch.setattr(storage, "application_directory", lambda: app)
    storage.managed_trash_path.cache_clear()
    coordinator = ModifyingJobCoordinator()
    journals = JournalStore(app_dir=app, local_app_data=local)
    try:
        yield app, coordinator, journals
    finally:
        storage.managed_trash_path.cache_clear()


def _snapshot(*roots: Path, incomplete: bool = False):
    collection = WorkingCollection()
    for root in roots:
        session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=str(root))
        collection.finalize_import(
            session,
            incomplete=incomplete,
            diagnostics=(ImportDiagnostic(str(root), "incomplete"),) if incomplete else (),
        )
    return collection.snapshot()


def _request(snapshot, **updates):
    values = dict(snapshot=snapshot, enabled=True, primary_succeeded=True)
    values.update(updates)
    return AutoCleanRequest(**values)


def test_auto_clean_removes_allowlist_and_empty_descendants_only(runtime, tmp_path, monkeypatch):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    nested = root / "nested"
    empty = nested / "empty" / "deeper"
    empty.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    allowed = (root / "Thumbs.DB", root / ".DS_Store", nested / "desktop.ini")
    for path in allowed:
        path.write_bytes(b"approved clutter")
    preserved = (root / ".env", root / ".gitignore", root / "folder.jpg")
    for path in preserved:
        path.write_bytes(b"keep")
    snapshot = _snapshot(root)
    service = AutoCleanService(coordinator, journals)
    removed_directories = []
    remove_directory = cleanup_module.remove_empty_directory

    def track(observer, path, expected):
        removed_directories.append(path)
        return remove_directory(observer, path, expected)

    monkeypatch.setattr(cleanup_module, "remove_empty_directory", track)
    result = service.run(_request(snapshot))

    assert {Path(path) for path in result.deleted_paths} == set(allowed) | {
        nested / "empty" / "deeper",
        nested / "empty",
        nested,
    }
    assert removed_directories == [
        str(nested / "empty" / "deeper"),
        str(nested / "empty"),
        str(nested),
    ]
    assert all(path.exists() and path.read_bytes() == b"keep" for path in preserved)
    assert root.is_dir()
    assert not coordinator.busy


def test_auto_clean_keeps_a_directory_when_contents_arrive_before_removal(
    runtime, tmp_path, monkeypatch
):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    child = root / "empty"
    child.mkdir(parents=True)
    add_content = cleanup_module.remove_empty_directory

    def race(observer, path, expected):
        (Path(path) / "arrived.txt").write_text("keep", encoding="utf-8")
        return add_content(observer, path, expected)

    monkeypatch.setattr(cleanup_module, "remove_empty_directory", race)
    result = AutoCleanService(coordinator, journals).run(_request(_snapshot(root)))

    assert child.is_dir()
    assert (child / "arrived.txt").read_text(encoding="utf-8") == "keep"
    assert CleanupStatus.SKIPPED in {item.status for item in result.items}


@pytest.mark.parametrize(
    "updates",
    [
        {"enabled": False},
        {"primary_succeeded": False},
        {"partial": True},
        {"unresolved": True},
    ],
)
def test_auto_clean_respects_operation_eligibility(runtime, tmp_path, updates):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    clutter = root / "Thumbs.db"
    clutter.write_text("preserve", encoding="utf-8")

    result = AutoCleanService(coordinator, journals).run(_request(_snapshot(root), **updates))

    assert result.detail
    assert clutter.read_text(encoding="utf-8") == "preserve"


def test_auto_clean_rejects_incomplete_import_and_add_files_only(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    clutter = root / "desktop.ini"
    clutter.write_text("preserve", encoding="utf-8")
    service = AutoCleanService(coordinator, journals)
    incomplete = service.run(_request(_snapshot(root, incomplete=True)))

    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    collection.register_path(session, str(root / "ordinary.txt"))
    collection.finalize_import(session)
    add_files_only = service.run(_request(collection.snapshot()))

    assert "incomplete" in (incomplete.detail or "")
    assert "roots" in (add_files_only.detail or "")
    assert clutter.read_text(encoding="utf-8") == "preserve"


def test_auto_clean_rejects_unfinalized_collection_entries(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    clutter = root / "Thumbs.db"
    clutter.write_text("preserve", encoding="utf-8")
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=str(root))
    collection.register_path(session, str(root / "ordinary.txt"))
    collection.finalize_import(session)
    snapshot = collection.snapshot()
    snapshot = replace(snapshot, entries=(replace(snapshot.entries[0], is_finalized=False),))

    result = AutoCleanService(coordinator, journals).run(_request(snapshot))

    assert "finalized" in (result.detail or "")
    assert clutter.read_text(encoding="utf-8") == "preserve"


def test_auto_clean_requires_and_preserves_primary_output_paths(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    output = root / "Thumbs.db"
    output.write_text("renamed user file", encoding="utf-8")
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=str(root))
    collection.register_path(session, str(root / "before.jpg"))
    collection.finalize_import(session)
    snapshot = collection.snapshot()
    service = AutoCleanService(coordinator, journals)

    missing = service.run(_request(snapshot))
    protected = service.run(_request(snapshot, output_paths=(str(output),)))

    assert "output paths" in (missing.detail or "")
    assert any(
        item.path == str(output) and item.reason == "primary_output"
        for item in protected.items
    )
    assert output.read_text(encoding="utf-8") == "renamed user file"


def test_auto_clean_uses_each_explicit_root_and_preserves_managed_and_system_roots(
    runtime, tmp_path
):
    app, coordinator, journals = runtime
    first, second = tmp_path / "first", tmp_path / "second"
    protected = tmp_path / "$RECYCLE.BIN" / "selected"
    managed = app / "journals"
    for root in (first, second, protected, managed):
        root.mkdir(parents=True, exist_ok=True)
    allowlisted = (first / "Thumbs.db", second / "desktop.ini")
    preserved = (protected / ".DS_Store", managed / "Thumbs.db")
    for path in (*allowlisted, *preserved):
        path.write_bytes(b"fixture")

    result = AutoCleanService(coordinator, journals).run(
        _request(_snapshot(first, second, protected, managed))
    )

    assert set(result.deleted_paths) == {str(path) for path in allowlisted}
    assert all(path.exists() and path.read_bytes() == b"fixture" for path in preserved)
    assert all(root.is_dir() for root in (first, second, protected, managed))


def test_auto_clean_does_not_follow_junctions(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "desktop.ini"
    target.write_text("preserve", encoding="utf-8")
    junction = root / "linked-folder"
    result = subprocess.run(
        f'cmd.exe /d /c mklink /J "{junction}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"The test environment could not create a junction: {result.stderr}")

    AutoCleanService(coordinator, journals).run(_request(_snapshot(root)))

    assert junction.is_dir()
    assert target.read_text(encoding="utf-8") == "preserve"


def test_auto_clean_preserves_a_locked_allowlisted_file(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    clutter = root / "Thumbs.db"
    clutter.write_text("preserve", encoding="utf-8")
    observer = WindowsPathObserver(capture_identity=True)
    handle = observer._open(str(clutter), 0x80000000, 0x01, None, 3, 0, None)
    assert handle not in (None, -1, (1 << 64) - 1)
    try:
        result = AutoCleanService(coordinator, journals).run(_request(_snapshot(root)))
    finally:
        observer._close(handle)

    assert any(item.status is CleanupStatus.FAILED for item in result.items)
    assert clutter.read_text(encoding="utf-8") == "preserve"


def test_auto_clean_logging_failure_does_not_undo_success(runtime, tmp_path, monkeypatch):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    clutter = root / "desktop.ini"
    clutter.write_text("remove", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("logging unavailable")

    monkeypatch.setattr(cleanup_module._LOG, "info", fail)
    result = AutoCleanService(coordinator, journals).run(_request(_snapshot(root)))

    assert str(clutter) in result.deleted_paths
    assert not clutter.exists()


def test_auto_clean_runs_inside_its_owning_job(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "imported"
    root.mkdir()
    clutter = root / "thumbs.db"
    clutter.write_text("remove", encoding="utf-8")
    source = root.parent / "source.txt"
    destination = root.parent / "destination.txt"
    source.write_text("source", encoding="utf-8")
    observer = WindowsPathObserver(capture_identity=True)
    row = TransactionEntry(
        uuid4(),
        str(source),
        str(destination),
        observer(str(source)),
        observer(str(destination)),
    )
    journal = journals.prepare(TransactionRecord.prepared(OperationType.RENAME, (row,)))
    job = coordinator.reserve(OperationType.RENAME)
    coordinator.begin_modification(job.job_id, journal)

    result = AutoCleanService(coordinator, journals).run(
        _request(_snapshot(root), owning_job_id=job.job_id)
    )

    assert str(clutter) in result.deleted_paths
    assert not clutter.exists()
    assert coordinator.current.job_id == job.job_id
    assert coordinator.current.modifying
    journal.checkpoint(recovery_required="test completed post-processing")
    coordinator.finish(job.job_id)


def test_temporary_artifact_cleanup_uses_journal_scope_and_creation_identity(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "files"
    root.mkdir()
    source = root / "source.txt"
    target = root / "target.txt"
    temporary = root / "generated.tmp"
    unrelated = root / "unrelated.txt"
    source.write_bytes(b"original")
    temporary.write_bytes(b"generated output")
    unrelated.write_bytes(b"unrelated")
    observer = WindowsPathObserver(capture_identity=True)
    source_observation = observer(str(source))
    destination_observation = observer(str(target))
    temporary_observation = observer(str(temporary))
    entry_id = uuid4()
    entry = TransactionEntry(
        entry_id,
        str(source),
        str(target),
        source_observation,
        destination_observation,
        temporary_path=str(temporary),
    )
    journal = journals.prepare(TransactionRecord.prepared(OperationType.REPACK, (entry,)))
    job = coordinator.reserve(OperationType.REPACK)
    coordinator.begin_modification(job.job_id, journal)
    journal.checkpoint(replace(entry, stage="output_verified", outcome=EntryOutcome.SUCCEEDED))

    result = TemporaryArtifactCleaner(coordinator, journals).clean_unneeded(
        job.job_id,
        journal,
        (TemporaryArtifact(entry_id, str(temporary), temporary_observation),),
    )

    assert result.deleted_paths == (str(temporary),)
    assert not temporary.exists()
    assert source.read_bytes() == b"original"
    assert unrelated.read_bytes() == b"unrelated"
    target.write_bytes(b"verified output")
    verified = WindowsPathObserver(capture_identity=True)(str(target))
    journal.finalize(
        (
            EntryVerification(
                entry_id, VerifiedOutcome.DESTINATION, str(target), verified, (str(temporary),)
            ),
        )
    )
    coordinator.finish(job.job_id)
    assert not coordinator.busy


def test_temporary_artifact_cleanup_preserves_changed_and_unrelated_files(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "files"
    root.mkdir()
    source = root / "source.txt"
    target = root / "target.txt"
    temporary = root / "generated.tmp"
    unrelated = root / "unrelated.txt"
    source.write_bytes(b"original")
    temporary.write_bytes(b"old output")
    unrelated.write_bytes(b"unrelated")
    observer = WindowsPathObserver(capture_identity=True)
    source_observation = observer(str(source))
    entry_id = uuid4()
    entry = TransactionEntry(
        entry_id,
        str(source),
        str(target),
        source_observation,
        observer(str(target)),
        temporary_path=str(temporary),
    )
    journal = journals.prepare(TransactionRecord.prepared(OperationType.REPACK, (entry,)))
    job = coordinator.reserve(OperationType.REPACK)
    coordinator.begin_modification(job.job_id, journal)
    journal.checkpoint(replace(entry, stage="output_verified", outcome=EntryOutcome.SUCCEEDED))
    original = observer(str(temporary))
    temporary.write_bytes(b"replacement output with changed identity")

    result = TemporaryArtifactCleaner(coordinator, journals).clean_unneeded(
        job.job_id,
        journal,
        (
            TemporaryArtifact(entry_id, str(temporary), original),
            TemporaryArtifact(entry_id, str(unrelated), observer(str(unrelated))),
        ),
    )

    assert all(item.status is CleanupStatus.SKIPPED for item in result.items)
    assert temporary.read_bytes() == b"replacement output with changed identity"
    assert unrelated.read_bytes() == b"unrelated"
    assert source.read_bytes() == b"original"
    journal.checkpoint(recovery_required="test retained the temporary output")
    coordinator.finish(job.job_id)


def test_temporary_artifact_cleanup_removes_only_an_empty_owned_directory(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "files"
    temporary = root / "generated"
    root.mkdir()
    temporary.mkdir()
    source = root / "source.txt"
    target = root / "target.txt"
    source.write_bytes(b"original")
    observer = WindowsPathObserver(capture_identity=True)
    entry_id = uuid4()
    entry = TransactionEntry(
        entry_id,
        str(source),
        str(target),
        observer(str(source)),
        observer(str(target)),
        temporary_path=str(temporary),
    )
    journal = journals.prepare(TransactionRecord.prepared(OperationType.REPACK, (entry,)))
    job = coordinator.reserve(OperationType.REPACK)
    coordinator.begin_modification(job.job_id, journal)
    journal.checkpoint(replace(entry, stage="output_verified", outcome=EntryOutcome.SUCCEEDED))

    result = TemporaryArtifactCleaner(coordinator, journals).clean_unneeded(
        job.job_id,
        journal,
        (TemporaryArtifact(entry_id, str(temporary), observer.observe_directory(str(temporary))),),
    )

    assert result.deleted_paths == (str(temporary),)
    assert not temporary.exists()
    assert source.read_bytes() == b"original"
    journal.checkpoint(recovery_required="test completed temporary-directory cleanup")
    coordinator.finish(job.job_id)


def test_finalization_rejects_an_artifact_that_was_not_cleaned(runtime, tmp_path):
    _, coordinator, journals = runtime
    root = tmp_path / "files"
    root.mkdir()
    source = root / "source.txt"
    target = root / "target.txt"
    artifact = root / "leftover.tmp"
    source.write_bytes(b"original")
    artifact.write_bytes(b"unneeded")
    observer = WindowsPathObserver(capture_identity=True)
    entry_id = uuid4()
    source_observation = observer(str(source))
    entry = TransactionEntry(
        entry_id,
        str(source),
        str(target),
        source_observation,
        observer(str(target)),
        temporary_path=str(artifact),
    )
    journal = journals.prepare(TransactionRecord.prepared(OperationType.REPACK, (entry,)))
    job = coordinator.reserve(OperationType.REPACK)
    coordinator.begin_modification(job.job_id, journal)
    journal.checkpoint(replace(entry, stage="output_verified", outcome=EntryOutcome.SUCCEEDED))
    target.write_bytes(b"output")
    verified = WindowsPathObserver(capture_identity=True)(str(target))

    with pytest.raises(ValueError, match="Temporary artifacts must be absent"):
        journal.finalize(
            (
                EntryVerification(
                    entry_id, VerifiedOutcome.DESTINATION, str(target), verified, (str(artifact),)
                ),
            )
        )
    assert artifact.read_bytes() == b"unneeded"
    assert source.read_bytes() == b"original"
    journal.checkpoint(recovery_required="artifact intentionally retained by test")
    coordinator.finish(job.job_id)


def test_trash_clear_removes_recognized_current_and_legacy_batches(runtime):
    app, coordinator, journals = runtime
    trash = app / "trash"
    batch = trash / f"20261126T000000Z-{'a' * 32}"
    previous_batch = trash / "20260101_010203"
    nested = batch / "nested"
    nested.mkdir(parents=True)
    previous_batch.mkdir()
    contents = nested / "original.txt"
    previous_contents = previous_batch / "older.txt"
    contents.write_bytes(b"recoverable user file")
    previous_contents.write_bytes(b"older recoverable user file")
    unrecognized = trash / "not-a-batch"
    unrecognized.mkdir()
    unknown_file = unrecognized / "leave.txt"
    unknown_file.write_bytes(b"outside the managed batch format")
    loose_file = trash / "loose.txt"
    loose_file.write_bytes(b"outside any batch")
    service = TrashClearService(coordinator, journals)

    summary = service.inspect()
    unauthorized = service.clear()
    result = service.clear(confirmed=summary)

    assert summary.eligible_count == result.inspection.eligible_count == 2
    assert summary.eligible_size_bytes == len(b"recoverable user file") + len(
        b"older recoverable user file"
    )
    assert "Explicit authorization" in (unauthorized.cleanup.detail or "")
    assert not contents.exists() and not previous_contents.exists()
    assert not nested.exists() and not batch.exists() and not previous_batch.exists()
    assert trash.is_dir()
    assert unknown_file.read_bytes() == b"outside the managed batch format"
    assert loose_file.read_bytes() == b"outside any batch"
    assert any(item.reason == "outside_recognized_batch" for item in summary.items)


def test_manual_trash_clear_preserves_new_entries_inside_and_outside_confirmed_batch(runtime):
    app, coordinator, journals = runtime
    batch = app / "trash" / "20261126_010203"
    batch.mkdir(parents=True)
    confirmed = batch / "confirmed.txt"
    confirmed.write_bytes(b"confirmed")
    service = TrashClearService(coordinator, journals)
    inspection = service.inspect()

    new_file = batch / "new.txt"
    new_file.write_bytes(b"new file")
    new_directory = batch / "new-directory"
    new_directory.mkdir()
    new_batch = batch.parent / "20261126_010204"
    new_batch.mkdir()

    result = service.clear(confirmed=inspection)

    assert result.cleanup.deleted_paths == (str(confirmed),)
    assert not confirmed.exists()
    assert new_file.read_bytes() == b"new file"
    assert new_directory.is_dir() and new_batch.is_dir()
    assert any(
        item.path == str(batch)
        and item.status is CleanupStatus.SKIPPED
        and item.reason == "directory_not_empty"
        for item in result.cleanup.items
    )


def test_manual_trash_clear_skips_replaced_and_missing_candidates(runtime, tmp_path):
    app, coordinator, journals = runtime
    batch = app / "trash" / "20261126_010203"
    batch.mkdir(parents=True)
    replaced = batch / "replaced.txt"
    missing = batch / "missing.txt"
    for path in (replaced, missing):
        path.write_bytes(b"confirmed")
    service = TrashClearService(coordinator, journals)
    inspection = service.inspect()

    original = replaced.rename(tmp_path / "original.txt")
    missing.rename(tmp_path / "moved.txt")
    replaced.write_bytes(b"replacement")
    result = service.clear(confirmed=inspection)

    assert not result.cleanup.deleted_paths
    assert original.read_bytes() == b"confirmed"
    assert replaced.read_bytes() == b"replacement"
    assert {(item.path, item.reason) for item in result.cleanup.items} >= {
        (str(replaced), "entry_changed"),
        (str(missing), "entry_missing"),
    }


def test_manual_trash_clear_skips_candidates_under_a_replaced_root(runtime):
    app, coordinator, journals = runtime
    trash = app / "trash"
    batch = trash / "20261126_010203"
    batch.mkdir(parents=True)
    contents = batch / "confirmed.txt"
    contents.write_bytes(b"confirmed")
    service = TrashClearService(coordinator, journals)
    inspection = service.inspect()

    parked = trash.rename(app / "parked-trash")
    trash.mkdir()
    (parked / batch.name).rename(batch)
    result = service.clear(confirmed=inspection)

    assert not result.cleanup.deleted_paths
    assert contents.read_bytes() == b"confirmed"
    assert {(item.path, item.reason) for item in result.cleanup.items} == {
        (str(contents), "trash_changed"),
        (str(batch), "trash_changed"),
    }


def test_trash_clear_preserves_unresolved_recovery_file_and_containing_batch(runtime):
    app, coordinator, journals = runtime
    batch = app / "trash" / f"20261126T000000Z-{'b' * 32}"
    batch.mkdir(parents=True)
    protected = batch / "recover.txt"
    sibling = batch / "safe.txt"
    protected.write_bytes(b"recovery dependency")
    sibling.write_bytes(b"ordinary managed Trash content")
    service = TrashClearService(coordinator, journals)
    confirmed = service.inspect()
    assert confirmed.eligible_count == 2
    assert confirmed.eligible_directory_count == 1
    observer = WindowsPathObserver(capture_identity=True, allow_managed_trash=True)
    row = TransactionEntry(
        uuid4(),
        str(protected),
        str(batch / "destination.txt"),
        observer(str(protected)),
        observer(str(batch / "destination.txt")),
    )
    journal = journals.prepare(TransactionRecord.prepared(OperationType.TRASH, (row,)))
    journal.start()
    journal.checkpoint(recovery_required="test unresolved Trash copy")

    summary = service.inspect()
    result = service.clear(confirmed=confirmed)

    assert any(
        item.path == str(protected) and item.reason == "recovery_dependent"
        for item in summary.items
    )
    assert protected.read_bytes() == b"recovery dependency"
    assert not sibling.exists()
    assert batch.is_dir()
    assert any(
        item.path == str(protected)
        and item.status is CleanupStatus.SKIPPED
        and str(journal.record.transaction_id) in (item.reason or "")
        for item in result.cleanup.items
    )
    assert any(
        item.path == str(sibling) and item.status is CleanupStatus.DELETED
        for item in result.cleanup.items
    )


def test_missing_managed_trash_inspection_does_not_create_its_directory(
    runtime, tmp_path, monkeypatch
):
    _, coordinator, journals = runtime
    missing = tmp_path / "not-created-trash"
    monkeypatch.setattr(cleanup_module, "managed_trash_path", lambda: missing)

    service = TrashClearService(coordinator, journals)
    summary = service.inspect()
    result = service.clear(confirmed=summary)

    assert summary.detail == "Managed Trash does not exist"
    assert summary.eligible_count == 0
    assert result.inspection.detail == "Managed Trash does not exist"
    assert not missing.exists()


def test_existing_empty_managed_trash_has_no_eligible_content(runtime, tmp_path, monkeypatch):
    _, coordinator, journals = runtime
    empty = tmp_path / "empty-trash"
    empty.mkdir()
    monkeypatch.setattr(cleanup_module, "managed_trash_path", lambda: empty)

    service = TrashClearService(coordinator, journals)
    summary = service.inspect()
    result = service.clear(confirmed=summary)

    assert summary.detail is None
    assert summary.eligible_count == 0
    assert result.inspection.eligible_count == 0
    assert result.cleanup.detail == "No eligible Trash items"
    assert empty.is_dir() and tuple(empty.iterdir()) == ()


def test_trash_clear_does_not_follow_junction_outside_managed_batch(runtime, tmp_path):
    app, coordinator, journals = runtime
    batch = app / "trash" / f"20261126T000000Z-{'c' * 32}"
    outside = tmp_path / "outside"
    batch.mkdir(parents=True)
    outside.mkdir()
    target = outside / "keep.txt"
    target.write_bytes(b"outside Trash")
    junction = batch / "linked"
    linked = subprocess.run(
        f'cmd.exe /d /c mklink /J "{junction}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
    )
    if linked.returncode:
        pytest.skip(f"The test environment could not create a junction: {linked.stderr}")
    safe = batch / "safe.txt"
    safe.write_bytes(b"inside Trash")

    service = TrashClearService(coordinator, journals)
    result = service.clear(confirmed=service.inspect())

    assert not safe.exists()
    assert junction.is_dir()
    assert target.read_bytes() == b"outside Trash"
    assert any(item.reason == "linked_entry" for item in result.inspection.items)


def test_trash_clear_reports_deletion_failure_and_keeps_failed_file(
    runtime, monkeypatch
):
    app, coordinator, journals = runtime
    batch = app / "trash" / f"20261126T000000Z-{'d' * 32}"
    batch.mkdir(parents=True)
    blocked = batch / "blocked.txt"
    deleted = batch / "deleted.txt"
    blocked.write_bytes(b"keep after deletion error")
    deleted.write_bytes(b"remove")
    remove = cleanup_module.remove_file

    def fail_blocked(observer, path, expected):
        if path == str(blocked):
            raise PermissionError("injected delete denial")
        return remove(observer, path, expected)

    monkeypatch.setattr(cleanup_module, "remove_file", fail_blocked)
    service = TrashClearService(coordinator, journals)
    result = service.clear(confirmed=service.inspect())

    assert blocked.read_bytes() == b"keep after deletion error"
    assert not deleted.exists()
    assert batch.is_dir()
    assert any(
        item.path == str(blocked) and item.status is CleanupStatus.FAILED
        for item in result.cleanup.items
    )
    assert result.cleanup.failures


def test_trash_clear_preserves_locked_content_and_reports_failure(runtime):
    app, coordinator, journals = runtime
    batch = app / "trash" / f"20261126T000000Z-{'e' * 32}"
    batch.mkdir(parents=True)
    contents = batch / "locked.txt"
    contents.write_bytes(b"keep")
    observer = WindowsPathObserver(capture_identity=True)
    handle = observer._open(str(contents), 0x80000000, 0x01, None, 3, 0, None)
    assert handle not in (None, -1, (1 << 64) - 1)
    try:
        service = TrashClearService(coordinator, journals)
        result = service.clear(confirmed=service.inspect())
    finally:
        observer._close(handle)

    assert result.inspection.eligible_count == 1
    assert any(
        item.path == str(contents) and item.status is CleanupStatus.FAILED
        for item in result.cleanup.items
    )
    assert contents.read_bytes() == b"keep"


def test_trash_inspection_reports_unavailable_location(runtime, monkeypatch):
    _, coordinator, journals = runtime
    monkeypatch.setattr(cleanup_module, "managed_trash_path", lambda: None)

    summary = TrashClearService(coordinator, journals).inspect()

    assert summary.eligible_count == 0
    assert "unavailable" in (summary.detail or "")


def test_auto_clear_trash_requires_normal_exit_and_idle_coordinator(runtime):
    app, coordinator, journals = runtime
    service = TrashClearService(coordinator, journals)
    batch = app / "trash" / f"20261126T000000Z-{'f' * 32}"
    batch.mkdir(parents=True)
    contents = batch / "eligible.txt"
    contents.write_bytes(b"clear on normal exit")
    default = service.clear(auto_clear_enabled=False, normal_exit=True)
    startup = service.clear(auto_clear_enabled=True, normal_exit=False)
    assert contents.exists()
    normal = service.clear(auto_clear_enabled=True, normal_exit=True)
    assert not contents.exists()
    busy_batch = app / "trash" / f"20261126T000001Z-{'1' * 32}"
    busy_batch.mkdir()
    busy_contents = busy_batch / "preserve.txt"
    busy_contents.write_bytes(b"active operation")
    job = coordinator.reserve(OperationType.RENAME)
    busy = service.clear(auto_clear_enabled=True, normal_exit=True)
    coordinator.finish(job.job_id)

    assert "disabled" in (default.cleanup.detail or "")
    assert "Explicit authorization" in (startup.cleanup.detail or "")
    assert any(
        item.path == str(contents) and item.status is CleanupStatus.DELETED
        for item in normal.cleanup.items
    )
    assert "during another" in (busy.cleanup.detail or "")
    assert busy_contents.read_bytes() == b"active operation"
