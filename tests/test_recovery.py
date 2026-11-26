from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import tidyfiles.recovery as recovery_module
from tidyfiles.collection import FileSystemIdentitySnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
    path_key,
)
from tidyfiles.recovery import RecoveryConflict, RecoveryGuard, RecoveryInspector
from tidyfiles.transaction_journal import (
    JournalStore,
    RecoveryDiscovery,
    RecoveryLocation,
    RecoveryStatus,
    RecoveryTransaction,
)
from tidyfiles.transaction_record import (
    TRASH_COPY_BEFORE_REMOVE,
    EntryOutcome,
    EntryVerification,
    OperationType,
    TransactionEntry,
    TransactionRecord,
    TransactionState,
    VerifiedOutcome,
)


def _file(path: str, volume: int, file_id: int, name: str) -> PathObservation:
    identity = FileSystemIdentitySnapshot(volume, file_id)
    parent = FileSystemIdentitySnapshot(volume, volume + 100)
    return PathObservation(
        PathOccupancy.FILE,
        actual_name=name,
        actual_parent=r"\folder",
        volume_key=f"volume-{volume}",
        volume_guid=f"volume-guid-{volume}",
        parent_identity=parent,
        object_metadata=WindowsObjectObservation(identity, 12, 13, 14, 15, 32),
    )


class _MappedObserver:
    def __init__(self, values, *args, **kwargs):
        self.values = {path_key(path): observation for path, observation in values.items()}

    def __call__(self, path):
        return self.values.get(path_key(path), PathObservation(PathOccupancy.VACANT))


def _discovery(record, path: Path):
    location = RecoveryLocation(path, RecoveryStatus.UNRESOLVED, record)
    return RecoveryDiscovery((RecoveryTransaction(record.transaction_id, (location,)),), ())


def test_recovery_inspection_keeps_cross_volume_trash_copy_identity(monkeypatch, tmp_path):
    source_path = r"D:\source\original.txt"
    trash_path = r"E:\app\trash\batch\original.txt"
    source = _file(source_path, 1, 10, "original.txt")
    copied = _file(trash_path, 2, 20, "original.txt")
    destination = PathObservation(
        PathOccupancy.VACANT,
        actual_parent=copied.actual_parent,
        volume_key=copied.volume_key,
        volume_guid=copied.volume_guid,
        parent_identity=copied.parent_identity,
    )
    entry_id = uuid4()
    verification = EntryVerification(
        entry_id,
        VerifiedOutcome.TRASH_COPY,
        trash_path,
        copied,
        content_sha256="a" * 64,
    )
    entry = TransactionEntry(
        entry_id,
        source_path,
        trash_path,
        source,
        destination,
        stage=TRASH_COPY_BEFORE_REMOVE,
        outcome=EntryOutcome.PENDING,
        verification=verification,
    )
    record = TransactionRecord(
        uuid4(),
        OperationType.TRASH,
        datetime.now(UTC),
        datetime.now(UTC),
        TransactionState.RECOVERY_REQUIRED,
        (entry,),
        "source removal was interrupted",
    )
    journals = JournalStore(app_dir=tmp_path / "portable", local_app_data=tmp_path / "local")
    monkeypatch.setattr(journals, "discover", lambda: _discovery(record, tmp_path / "journal.json"))
    monkeypatch.setattr(
        recovery_module,
        "WindowsPathObserver",
        lambda *args, **kwargs: _MappedObserver(
            {source_path: PathObservation(PathOccupancy.VACANT), trash_path: copied}
        ),
    )

    report = RecoveryInspector(journals).inspect()
    copy_observation = next(
        item
        for item in report.transactions[0].entries[0].paths
        if item.role == "verified_trash_copy"
    )

    assert copy_observation.matches_recorded_identity is True
    observed_metadata = copy_observation.current.object_metadata
    source_metadata = source.object_metadata
    assert observed_metadata is not None and source_metadata is not None
    assert observed_metadata.identity != source_metadata.identity
    assert "different from the recorded identity" not in (
        report.transactions[0].entries[0].detail or ""
    )


def test_recovery_guard_blocks_direct_and_directory_dependencies_but_allows_siblings(
    monkeypatch, tmp_path
):
    source = r"C:\data\A.txt"
    destination = r"C:\data\B.txt"
    staging = r"C:\data\.stage-A.tmp"
    observation = _file(source, 1, 10, "A.txt")
    entry = TransactionEntry(uuid4(), source, destination, observation, None, staging_path=staging)
    record = TransactionRecord(
        uuid4(),
        OperationType.RENAME,
        datetime.now(UTC),
        datetime.now(UTC),
        TransactionState.RECOVERY_REQUIRED,
        (entry,),
        "interrupted",
    )
    journals = JournalStore(app_dir=tmp_path / "portable", local_app_data=tmp_path / "local")
    monkeypatch.setattr(journals, "discover", lambda: _discovery(record, tmp_path / "journal.json"))
    monkeypatch.setattr(
        recovery_module,
        "WindowsPathObserver",
        lambda *args, **kwargs: _MappedObserver({}),
    )
    guard = RecoveryGuard(journals)

    for path in (source, destination, staging, r"C:\data"):
        with pytest.raises(RecoveryConflict):
            guard.check((path,))
    guard.check((r"C:\data\unrelated.txt",))


def test_recovery_guard_blocks_when_discovery_scope_is_unverifiable(monkeypatch, tmp_path):
    journals = JournalStore(app_dir=tmp_path / "portable", local_app_data=tmp_path / "local")
    issue = RecoveryLocation(tmp_path / "recovery_index.json", RecoveryStatus.INACCESSIBLE)
    monkeypatch.setattr(journals, "discover", lambda: RecoveryDiscovery((), (issue,)))

    with pytest.raises(RecoveryConflict, match="independence cannot be verified"):
        RecoveryGuard(journals).check((r"D:\unrelated\file.txt",))


def test_recovery_guard_blocks_disagreeing_copies_of_one_transaction(monkeypatch, tmp_path):
    source = r"C:\data\A.txt"
    entry = TransactionEntry(uuid4(), source, r"C:\data\B.txt", _file(source, 1, 10, "A.txt"), None)
    record = TransactionRecord(
        uuid4(),
        OperationType.RENAME,
        datetime.now(UTC),
        datetime.now(UTC),
        TransactionState.RECOVERY_REQUIRED,
        (entry,),
        "interrupted",
    )
    first = RecoveryLocation(tmp_path / "first.json", RecoveryStatus.UNRESOLVED, record)
    second = RecoveryLocation(
        tmp_path / "second.json", RecoveryStatus.UNRESOLVED, replace(record, failure="different")
    )
    discovery = RecoveryDiscovery(
        (RecoveryTransaction(record.transaction_id, (first, second)),), ()
    )
    journals = JournalStore(app_dir=tmp_path / "portable", local_app_data=tmp_path / "local")
    monkeypatch.setattr(journals, "discover", lambda: discovery)
    monkeypatch.setattr(
        recovery_module, "WindowsPathObserver", lambda *args, **kwargs: _MappedObserver({})
    )

    report = RecoveryInspector(journals).inspect()

    assert report.has_incomplete_discovery()
    assert report.storage_issues[0].status is RecoveryStatus.DAMAGED
    with pytest.raises(RecoveryConflict, match="independence cannot be verified"):
        RecoveryGuard(journals).check((r"D:\unrelated\file.txt",))
