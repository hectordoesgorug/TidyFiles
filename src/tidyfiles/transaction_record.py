"""Versioned records for reconstructing a transaction without a live collection."""

from __future__ import annotations

import ntpath
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from tidyfiles.collection import FileSystemIdentitySnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
)

if TYPE_CHECKING:
    from tidyfiles.execution_preflight import PreflightResult

SCHEMA_VERSION = 1


class OperationType(StrEnum):
    RENAME = "rename"
    REPACK = "repack"
    TRASH = "trash"


class TransactionState(StrEnum):
    PREPARED = "prepared"
    IN_PROGRESS = "in_progress"
    FINALIZED = "finalized"
    FAILED_SAFE = "failed_safe"
    RECOVERY_REQUIRED = "recovery_required"


class EntryOutcome(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class VerifiedOutcome(StrEnum):
    DESTINATION = "destination_verified"
    ORIGINAL = "original_verified_safe"


@dataclass(frozen=True, slots=True)
class EntryVerification:
    entry_id: UUID
    outcome: VerifiedOutcome
    path: str
    observation: PathObservation
    resolved_artifact_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransactionEntry:
    entry_id: UUID
    source_path: str
    destination_path: str
    source_observation: PathObservation
    destination_observation: PathObservation | None = None
    staging_path: str | None = None
    temporary_path: str | None = None
    stage: str = "prepared"
    outcome: EntryOutcome = EntryOutcome.PENDING
    detail: str | None = None
    verification: EntryVerification | None = None


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    transaction_id: UUID
    operation: OperationType
    created_at: datetime
    updated_at: datetime
    state: TransactionState
    entries: tuple[TransactionEntry, ...]
    failure: str | None = None

    @classmethod
    def prepared(
        cls, operation: OperationType, entries: tuple[TransactionEntry, ...]
    ) -> TransactionRecord:
        now = datetime.now(UTC)
        return cls(uuid4(), operation, now, now, TransactionState.PREPARED, tuple(entries))

    @classmethod
    def from_rename_preflight(cls, result: PreflightResult) -> TransactionRecord:
        """Retain the intended set and last observed identities from an accepted preflight."""
        from tidyfiles.execution_preflight import PreflightState

        plan = result.fresh_plan
        if (
            result.state is not PreflightState.PASSED
            or plan is None
            or plan.evaluation_request_id != result.request_id
            or tuple(row.entry_id for row in plan.entries) != result.intended_entry_ids
        ):
            raise ValueError("A passing preflight with the exact intended entries is required")
        entries = []
        for row in plan.entries:
            if row.destination_path is None or row.source_observation is None:
                raise ValueError("The intended entry lacks a path or source observation")
            entries.append(
                TransactionEntry(
                    row.entry_id,
                    row.source_path,
                    row.destination_path,
                    row.source_observation,
                    row.destination_observation,
                )
            )
        return cls.prepared(OperationType.RENAME, tuple(entries))


def _fields(value: object, *keys: str) -> dict:
    if not isinstance(value, dict) or value.keys() != set(keys):
        raise ValueError("Invalid record fields")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Expected nonempty text")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _path(value: object) -> str:
    path = _text(value)
    if not Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValueError("Expected an absolute path without parent traversal")
    return path


def _optional_path(value: object) -> str | None:
    return None if value is None else _path(value)


def _number(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Expected an integer")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("Expected a boolean")
    return value


def _identifier(value: object) -> UUID:
    return UUID(_text(value))


def _timestamp(value: object) -> datetime:
    timestamp = datetime.fromisoformat(_text(value))
    if timestamp.utcoffset() != UTC.utcoffset(None):
        raise ValueError("Expected a UTC timestamp")
    return timestamp


def _identity_json(identity: FileSystemIdentitySnapshot) -> dict:
    return {
        "volume_serial": f"{identity.volume_id:016x}",
        "file_id": f"{identity.file_id:032x}",
    }


def _hex_value(value: object, width: int) -> int:
    if not re.fullmatch(rf"[0-9a-f]{{{width}}}", _text(value)):
        raise ValueError("Invalid native identity encoding")
    return int(value, 16)


def _identity(value: object) -> FileSystemIdentitySnapshot:
    data = _fields(value, "volume_serial", "file_id")
    file_id = _hex_value(data["file_id"], 32)
    if file_id == 0:
        raise ValueError("Missing native object identity")
    return FileSystemIdentitySnapshot(_hex_value(data["volume_serial"], 16), file_id)


def _observation_json(observation: PathObservation | None) -> dict | None:
    if observation is None:
        return None
    metadata = observation.object_metadata
    return {
        "occupancy": observation.occupancy.value,
        "actual_name": observation.actual_name,
        "actual_parent": observation.actual_parent,
        "volume_key": observation.volume_key,
        "volume_guid": observation.volume_guid,
        "parent_identity": _identity_json(observation.parent_identity)
        if observation.parent_identity
        else None,
        "read_only_volume": observation.read_only_volume,
        "preserves_case": observation.preserves_case,
        "reason": observation.reason,
        "object": {
            "identity": _identity_json(metadata.identity),
            "size_bytes": metadata.size_bytes,
            "modified_time_ns": metadata.modified_time_ns,
            "created_time_ns": metadata.created_time_ns,
            "change_time_ns": metadata.change_time_ns,
            "attributes": metadata.attributes,
        }
        if metadata
        else None,
    }


def _observation(value: object) -> PathObservation:
    data = _fields(
        value,
        "occupancy",
        "actual_name",
        "actual_parent",
        "volume_key",
        "volume_guid",
        "parent_identity",
        "read_only_volume",
        "preserves_case",
        "reason",
        "object",
    )
    metadata = None
    if data["object"] is not None:
        obj = _fields(
            data["object"],
            "identity",
            "size_bytes",
            "modified_time_ns",
            "created_time_ns",
            "change_time_ns",
            "attributes",
        )
        metadata = WindowsObjectObservation(
            _identity(obj["identity"]),
            _number(obj["size_bytes"]),
            _number(obj["modified_time_ns"]),
            _number(obj["created_time_ns"]) if obj["created_time_ns"] is not None else None,
            _number(obj["change_time_ns"]),
            _number(obj["attributes"]),
        )
    return PathObservation(
        PathOccupancy(data["occupancy"]),
        _optional_text(data["actual_name"]),
        _optional_text(data["volume_key"]),
        _optional_text(data["reason"]),
        _boolean(data["read_only_volume"]),
        _boolean(data["preserves_case"]),
        _optional_text(data["actual_parent"]),
        _optional_text(data["volume_guid"]),
        _identity(data["parent_identity"]) if data["parent_identity"] is not None else None,
        metadata,
    )


def _verification_json(verification: EntryVerification | None) -> dict | None:
    if verification is None:
        return None
    return {
        "entry_id": str(verification.entry_id),
        "outcome": verification.outcome.value,
        "path": verification.path,
        "observation": _observation_json(verification.observation),
        "resolved_artifact_paths": list(verification.resolved_artifact_paths),
    }


def _verification(value: object) -> EntryVerification:
    data = _fields(value, "entry_id", "outcome", "path", "observation", "resolved_artifact_paths")
    paths = data["resolved_artifact_paths"]
    if not isinstance(paths, list):
        raise ValueError("Expected a list of resolved artifact paths")
    return EntryVerification(
        _identifier(data["entry_id"]),
        VerifiedOutcome(data["outcome"]),
        _path(data["path"]),
        _observation(data["observation"]),
        tuple(_path(path) for path in paths),
    )


def record_to_json(record: TransactionRecord) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": str(record.transaction_id),
        "operation": record.operation.value,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "state": record.state.value,
        "failure": record.failure,
        "entries": [
            {
                "entry_id": str(entry.entry_id),
                "source_path": entry.source_path,
                "destination_path": entry.destination_path,
                "source_observation": _observation_json(entry.source_observation),
                "destination_observation": _observation_json(entry.destination_observation),
                "staging_path": entry.staging_path,
                "temporary_path": entry.temporary_path,
                "stage": entry.stage,
                "outcome": entry.outcome.value,
                "detail": entry.detail,
                "verification": _verification_json(entry.verification),
            }
            for entry in record.entries
        ],
    }


def record_from_json(value: object) -> TransactionRecord:
    data = _fields(
        value,
        "schema_version",
        "transaction_id",
        "operation",
        "created_at",
        "updated_at",
        "state",
        "failure",
        "entries",
    )
    if _number(data["schema_version"]) != SCHEMA_VERSION:
        raise ValueError("Unsupported journal schema version")
    if not isinstance(data["entries"], list) or not data["entries"]:
        raise ValueError("A transaction requires an ordered, nonempty scope")
    entries = []
    for value in data["entries"]:
        row = _fields(
            value,
            "entry_id",
            "source_path",
            "destination_path",
            "source_observation",
            "destination_observation",
            "staging_path",
            "temporary_path",
            "stage",
            "outcome",
            "detail",
            "verification",
        )
        entry = TransactionEntry(
            _identifier(row["entry_id"]),
            _path(row["source_path"]),
            _path(row["destination_path"]),
            _observation(row["source_observation"]),
            _observation(row["destination_observation"])
            if row["destination_observation"] is not None
            else None,
            _optional_path(row["staging_path"]),
            _optional_path(row["temporary_path"]),
            _text(row["stage"]),
            EntryOutcome(row["outcome"]),
            _optional_text(row["detail"]),
            _verification(row["verification"]) if row["verification"] is not None else None,
        )
        if not _object_is_observed(entry.source_observation):
            raise ValueError("A source requires its native review identity and directory entry")
        entries.append(entry)
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise ValueError("Duplicate transaction entry ID")
    created, updated = _timestamp(data["created_at"]), _timestamp(data["updated_at"])
    if updated < created:
        raise ValueError("An update predates transaction creation")
    record = TransactionRecord(
        _identifier(data["transaction_id"]),
        OperationType(data["operation"]),
        created,
        updated,
        TransactionState(data["state"]),
        tuple(entries),
        _optional_text(data["failure"]),
    )
    terminal = record.state in {TransactionState.FINALIZED, TransactionState.FAILED_SAFE}
    for entry in record.entries:
        if record.state is TransactionState.PREPARED and (
            entry.stage != "prepared" or entry.outcome is not EntryOutcome.PENDING
        ):
            raise ValueError("Prepared journal contains an active entry")
        if (
            entry.outcome is EntryOutcome.RECOVERY_REQUIRED
            and record.state is not TransactionState.RECOVERY_REQUIRED
        ):
            raise ValueError("Entry recovery requirement must also block the transaction")
        if (entry.verification is not None) != terminal:
            raise ValueError("Finalization evidence does not match transaction state")
        if entry.verification is not None:
            validate_verification(entry, entry.verification, record.operation)
            expected = (
                EntryOutcome.FAILED
                if entry.verification.outcome is VerifiedOutcome.ORIGINAL
                else EntryOutcome.SUCCEEDED
            )
            if entry.outcome is not expected:
                raise ValueError("Entry outcome contradicts its verification")
    if terminal and (
        (record.state is TransactionState.FAILED_SAFE)
        != any(
            entry.verification.outcome is VerifiedOutcome.ORIGINAL
            for entry in record.entries
            if entry.verification is not None
        )
    ):
        raise ValueError("Transaction state contradicts verified outcomes")
    return record


def _object_is_observed(observation: PathObservation) -> bool:
    forbidden = (
        stat.FILE_ATTRIBUTE_DIRECTORY
        | stat.FILE_ATTRIBUTE_REPARSE_POINT
        | stat.FILE_ATTRIBUTE_DEVICE
    )
    return bool(
        observation.occupancy is PathOccupancy.FILE
        and observation.actual_name
        and observation.actual_parent
        and observation.volume_guid
        and observation.volume_key
        and observation.parent_identity
        and observation.object_metadata
        and observation.object_metadata.size_bytes >= 0
        and not observation.object_metadata.attributes & forbidden
        and observation.parent_identity.volume_id == observation.object_metadata.identity.volume_id
    )


def validate_verification(
    entry: TransactionEntry, evidence: EntryVerification, operation: OperationType
) -> None:
    original = evidence.outcome is VerifiedOutcome.ORIGINAL
    expected = entry.source_path if original else entry.destination_path
    artifacts = {path for path in (entry.staging_path, entry.temporary_path) if path is not None}
    observed = evidence.observation
    if (
        evidence.entry_id != entry.entry_id
        or evidence.path != expected
        or set(evidence.resolved_artifact_paths) != artifacts
        or not _object_is_observed(observed)
    ):
        raise ValueError("Verification must cover the intended entry and all recorded artifacts")
    baseline = entry.source_observation if original else entry.destination_observation
    expected_name = ntpath.basename(expected)
    if original or entry.source_path == entry.destination_path:
        expected_name = entry.source_observation.actual_name
    if observed.actual_name != expected_name or (
        baseline is not None
        and (
            observed.parent_identity != baseline.parent_identity
            or observed.volume_guid != baseline.volume_guid
            or observed.actual_parent != baseline.actual_parent
        )
    ):
        raise ValueError("Verified directory entry does not match the intended location")
    if original or operation is OperationType.RENAME:
        source, verified = entry.source_observation.object_metadata, observed.object_metadata
        assert source is not None and verified is not None
        if (
            source.identity != verified.identity
            or source.size_bytes != verified.size_bytes
            or source.modified_time_ns != verified.modified_time_ns
            or source.created_time_ns != verified.created_time_ns
            or source.attributes != verified.attributes
        ):
            raise ValueError("Verified object differs from the original")
