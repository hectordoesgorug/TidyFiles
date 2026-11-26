"""Read-only recovery inspection and overlap protection for modifying services."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tidyfiles.collection import FileSystemIdentitySnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
    is_within,
    path_key,
)
from tidyfiles.transaction_journal import (
    JournalStore,
    RecoveryLocation,
    RecoveryStatus,
)
from tidyfiles.transaction_record import (
    EntryOutcome,
    OperationType,
    TransactionEntry,
    TransactionState,
    VerifiedOutcome,
)


@dataclass(frozen=True, slots=True)
class RecoveryPathObservation:
    path: str
    role: str
    recorded_stage: str
    recorded_outcome: EntryOutcome
    expected: PathObservation | None
    current: PathObservation
    matches_recorded_identity: bool | None


@dataclass(frozen=True, slots=True)
class RecoveryEntryInspection:
    entry_id: UUID
    source_path: str
    destination_path: str
    recorded_stage: str
    recorded_outcome: EntryOutcome
    paths: tuple[RecoveryPathObservation, ...]
    detail: str | None


@dataclass(frozen=True, slots=True)
class RecoveryTransactionInspection:
    transaction_id: UUID
    operation: OperationType | None
    state: TransactionState | None
    journal_status: RecoveryStatus
    journal_locations: tuple[RecoveryLocation, ...]
    entries: tuple[RecoveryEntryInspection, ...]
    detail: str | None


@dataclass(frozen=True, slots=True)
class RecoveryInspection:
    transactions: tuple[RecoveryTransactionInspection, ...]
    storage_issues: tuple[RecoveryLocation, ...]

    def has_incomplete_discovery(self) -> bool:
        return bool(self.storage_issues) or any(
            transaction.journal_status
            in {RecoveryStatus.DAMAGED, RecoveryStatus.MISSING, RecoveryStatus.INACCESSIBLE}
            for transaction in self.transactions
        )


@dataclass(frozen=True, slots=True)
class _Dependency:
    transaction_id: UUID
    path: str
    identity: tuple[str | None, FileSystemIdentitySnapshot] | None


class RecoveryConflict(OSError):
    """The operation overlaps unresolved recovery evidence or its scope is uncertain."""


class RecoveryInspector:
    def __init__(self, journals: JournalStore) -> None:
        self.journals = journals

    def inspect(self) -> RecoveryInspection:
        discovery = self.journals.discover()
        observer = WindowsPathObserver(
            self.journals.managed_paths,
            capture_identity=True,
            allow_managed_trash=True,
        )
        transactions = []
        issues = list(discovery.storage_issues)
        for transaction in discovery.transactions:
            records = tuple(
                item.record for item in transaction.locations if item.record is not None
            )
            if records and any(record != records[0] for record in records[1:]):
                issues.append(
                    RecoveryLocation(
                        transaction.locations[0].path,
                        RecoveryStatus.DAMAGED,
                        detail="Journal copies for one transaction disagree.",
                    )
                )
            location = next(
                (
                    item
                    for item in transaction.locations
                    if item.record is not None
                    and item.status in {RecoveryStatus.UNRESOLVED, RecoveryStatus.CLEANUP_PENDING}
                ),
                transaction.locations[0],
            )
            record = location.record
            if record is None:
                statuses = {item.status for item in transaction.locations}
                status = next(
                    (
                        item
                        for item in (
                            RecoveryStatus.DAMAGED,
                            RecoveryStatus.MISSING,
                            RecoveryStatus.INACCESSIBLE,
                        )
                        if item in statuses
                    ),
                    location.status,
                )
                transactions.append(
                    RecoveryTransactionInspection(
                        transaction.transaction_id,
                        None,
                        None,
                        status,
                        transaction.locations,
                        (),
                        location.detail,
                    )
                )
                continue
            entries = tuple(self._entry(entry, observer) for entry in record.entries)
            statuses = {item.status for item in transaction.locations}
            status = (
                RecoveryStatus.UNRESOLVED
                if RecoveryStatus.UNRESOLVED in statuses
                else RecoveryStatus.CLEANUP_PENDING
                if RecoveryStatus.CLEANUP_PENDING in statuses
                else next(
                    (
                        item
                        for item in (
                            RecoveryStatus.DAMAGED,
                            RecoveryStatus.MISSING,
                            RecoveryStatus.INACCESSIBLE,
                        )
                        if item in statuses
                    ),
                    location.status,
                )
            )
            transactions.append(
                RecoveryTransactionInspection(
                    record.transaction_id,
                    record.operation,
                    record.state,
                    status,
                    transaction.locations,
                    entries,
                    location.detail or record.failure,
                )
            )
        return RecoveryInspection(tuple(transactions), tuple(issues))

    @staticmethod
    def _entry(
        entry: TransactionEntry,
        observer: WindowsPathObserver,
    ) -> RecoveryEntryInspection:
        paths: list[tuple[str, str, PathObservation | None]] = [
            (entry.source_path, "source", entry.source_observation),
            (entry.destination_path, "intended_destination", entry.destination_observation),
        ]
        if entry.staging_path is not None:
            paths.append((entry.staging_path, "staging", None))
        if entry.temporary_path is not None:
            paths.append((entry.temporary_path, "temporary", None))
        if entry.verification is not None:
            paths.append(
                (
                    entry.verification.path,
                    "verified_trash_copy"
                    if entry.verification.outcome is VerifiedOutcome.TRASH_COPY
                    else "recorded_verified_outcome",
                    entry.verification.observation,
                )
            )
            paths.extend(
                (path, "resolved_artifact", None)
                for path in entry.verification.resolved_artifact_paths
            )
        unique = {}
        for path, role, expected in paths:
            key = path_key(path)
            previous = unique.get(key)
            if (
                previous is None
                or (expected is not None and expected.object_metadata is not None)
                or role == "verified_trash_copy"
            ):
                unique[key] = (path, role, expected)
        observations = []
        for path, role, expected in unique.values():
            current = observer(path)
            matches = None
            if expected is not None and expected.object_metadata is not None:
                metadata = current.object_metadata
                matches = bool(
                    current.occupancy is PathOccupancy.FILE
                    and metadata is not None
                    and metadata.identity == expected.object_metadata.identity
                    and current.volume_guid == expected.volume_guid
                    and current.parent_identity == expected.parent_identity
                    and current.actual_parent == expected.actual_parent
                    and current.actual_name == expected.actual_name
                )
            observations.append(
                RecoveryPathObservation(
                    path, role, entry.stage, entry.outcome, expected, current, matches
                )
            )
        notes = []
        if entry.outcome is EntryOutcome.RECOVERY_REQUIRED:
            notes.append(entry.detail or "The transaction records an unresolved entry.")
        if (
            entry.verification is not None
            and entry.verification.outcome is VerifiedOutcome.TRASH_COPY
        ):
            notes.append("The Trash copy has a distinct identity from the original source.")
        if any(path.current.occupancy is PathOccupancy.UNVERIFIABLE for path in observations):
            notes.append("One or more recorded paths cannot be observed.")
        if any(
            path.matches_recorded_identity is False and path.current.occupancy is PathOccupancy.FILE
            for path in observations
        ):
            notes.append("One or more observed files differ from the recorded identity.")
        return RecoveryEntryInspection(
            entry.entry_id,
            entry.source_path,
            entry.destination_path,
            entry.stage,
            entry.outcome,
            tuple(observations),
            " ".join(notes) or None,
        )


class RecoveryGuard:
    """Reject only overlaps with known unresolved dependencies, or uncertain scopes."""

    def __init__(self, journals: JournalStore) -> None:
        self.journals = journals

    def check(
        self,
        paths: tuple[str, ...],
        observations: tuple[PathObservation, ...] = (),
        *,
        ignore_transaction_id: UUID | None = None,
        inspection: RecoveryInspection | None = None,
    ) -> None:
        if inspection is None:
            inspection = RecoveryInspector(self.journals).inspect()
        if inspection.has_incomplete_discovery():
            raise RecoveryConflict(
                "Recovery evidence is incomplete, so independence cannot be verified."
            )
        candidate_keys = tuple(path_key(path) for path in paths)
        candidate_ids = {
            (observation.volume_guid, observation.object_metadata.identity)
            for observation in observations
            if observation.object_metadata is not None
        }
        dependencies = self._dependencies(inspection)
        for dependency in dependencies:
            if dependency.transaction_id == ignore_transaction_id:
                continue
            if dependency.identity is not None and dependency.identity in candidate_ids:
                raise RecoveryConflict(
                    f"The operation uses a filesystem object needed by transaction "
                    f"{dependency.transaction_id}: {dependency.path}"
                )
            protected = path_key(dependency.path)
            if any(
                is_within(candidate, protected) or is_within(protected, candidate)
                for candidate in candidate_keys
            ):
                raise RecoveryConflict(
                    f"The operation overlaps recovery path {dependency.path} "
                    f"from transaction {dependency.transaction_id}"
                )

    @staticmethod
    def _dependencies(inspection: RecoveryInspection) -> tuple[_Dependency, ...]:
        values = {}
        for transaction in inspection.transactions:
            if transaction.journal_status is not RecoveryStatus.UNRESOLVED:
                continue
            for entry in transaction.entries:
                for item in entry.paths:
                    expected = item.expected
                    identity = None
                    if expected is not None and expected.object_metadata is not None:
                        identity = (
                            expected.volume_guid,
                            expected.object_metadata.identity,
                        )
                    key = path_key(item.path)
                    values.setdefault(
                        (transaction.transaction_id, key),
                        _Dependency(transaction.transaction_id, item.path, identity),
                    )
        return tuple(values.values())
