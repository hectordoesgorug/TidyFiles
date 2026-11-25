"""Portable transaction journals and the independent LocalAppData recovery locator."""

from __future__ import annotations

import ctypes
import json
import os
import stat
import tempfile
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from threading import RLock
from uuid import UUID

from tidyfiles.collection import normalize_windows_path
from tidyfiles.storage import application_directory
from tidyfiles.transaction_record import (
    EntryOutcome,
    EntryVerification,
    TransactionEntry,
    TransactionRecord,
    TransactionState,
    VerifiedOutcome,
    record_from_json,
    record_to_json,
    validate_verification,
)

_LOCK = RLock()
_INDEX_VERSION = 1
_TERMINAL = {TransactionState.FINALIZED, TransactionState.FAILED_SAFE}
_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


class JournalError(OSError):
    """Persistence or cleanup failed. The caller must stop affected modifications."""


class RecoveryStatus(StrEnum):
    UNRESOLVED = "unresolved"
    CLEANUP_PENDING = "cleanup_pending"
    DAMAGED = "damaged"
    MISSING = "missing"
    INACCESSIBLE = "inaccessible"


@dataclass(frozen=True, slots=True)
class RecoveryLocation:
    path: Path
    status: RecoveryStatus
    record: TransactionRecord | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryTransaction:
    transaction_id: UUID
    locations: tuple[RecoveryLocation, ...]


@dataclass(frozen=True, slots=True)
class RecoveryDiscovery:
    transactions: tuple[RecoveryTransaction, ...]
    storage_issues: tuple[RecoveryLocation, ...]


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(path))


def _check_directory(path: Path) -> None:
    if not path.is_absolute() or path.drive.startswith(("\\\\.\\", "\\\\?\\")):
        raise JournalError(f"Unsupported journal directory: {path}")
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
            raise JournalError(f"Unsafe journal directory: {part}")


def _mkdir(path: Path) -> None:
    _check_directory(path.parent)
    path.mkdir(exist_ok=True)
    _check_directory(path)


def _regular_file(path: Path) -> None:
    _check_directory(path.parent)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _REPARSE
        or info.st_nlink != 1
    ):
        raise JournalError(f"Unsafe journal or locator file: {path}")


def _read(path: Path) -> bytes:
    _regular_file(path)
    return path.read_bytes()


def _unique_object(items: list[tuple[str, object]]) -> dict:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("Duplicate JSON field")
    return result


def _invalid_number(value: str) -> None:
    raise ValueError(f"Invalid JSON number {value}")


def _decode(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_number,
    )


def _encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


@lru_cache(maxsize=1)
def _windows_move():
    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    return move


def _publish(source: Path, target: Path, *, overwrite: bool) -> None:
    if os.name == "nt":
        flags = 0x8 | int(overwrite)  # MOVEFILE_WRITE_THROUGH | optional REPLACE_EXISTING
        if not _windows_move()(str(source), str(target), flags):
            raise ctypes.WinError(ctypes.get_last_error())
    elif overwrite:
        os.replace(source, target)
    else:
        os.link(source, target)


def _atomic_write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    _check_directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _publish(temporary, path, overwrite=overwrite)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # An unpublished temp file is never interpreted as a journal.
                pass


def _record_payload(record: TransactionRecord) -> bytes:
    data = record_to_json(record)
    if record_from_json(data) != record:
        raise ValueError("Transaction record is not in its canonical form")
    return _encode(data)


def _journal_id(path: Path) -> UUID:
    identifier = UUID(path.stem)
    if path.name != f"{identifier}.json":
        raise ValueError("Expected a transaction UUID filename")
    return identifier


def _load_record(path: Path, identifier: UUID) -> TransactionRecord:
    record = record_from_json(_decode(_read(path)))
    if record.transaction_id != identifier:
        raise ValueError("Journal ID does not match its registered location")
    return record


class JournalStore:
    """One app's journal locations. Create and write only while the instance guard is held."""

    def __init__(self, *, app_dir: Path | None = None, local_app_data: Path | None = None) -> None:
        preferred = application_directory() if app_dir is None else _absolute(app_dir)
        local = os.environ.get("LOCALAPPDATA") if local_app_data is None else local_app_data
        self._portable = preferred / "journals" if preferred is not None else None
        self._fallback = _absolute(local) / "TidyFiles" if local else None

    @property
    def locator_path(self) -> Path | None:
        return self._fallback / "recovery_index.json" if self._fallback is not None else None

    def _directories(self) -> tuple[Path, ...]:
        fallback = self._fallback / "journals" if self._fallback is not None else None
        return tuple(dict.fromkeys(path for path in (self._portable, fallback) if path is not None))

    def _locator(self) -> tuple[dict[UUID, Path], bool]:
        path = self.locator_path
        if path is None:
            raise JournalError("LOCALAPPDATA is required for the recovery locator")
        try:
            payload = _read(path)
        except FileNotFoundError:
            return {}, False
        data = _decode(payload)
        if (
            not isinstance(data, dict)
            or data.keys() != {"schema_version", "journals"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != _INDEX_VERSION
            or not isinstance(data["journals"], dict)
        ):
            raise ValueError("Unsupported or invalid recovery locator")
        entries = {}
        for key, value in data["journals"].items():
            identifier = UUID(key)
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("Invalid journal path in recovery locator")
            journal = Path(value)
            if (
                identifier in entries
                or key != str(identifier)
                or not journal.is_absolute()
                or journal.drive.startswith(("\\\\.\\", "\\\\?\\"))
                or ".." in journal.parts
                or journal.parent.name.casefold() != "journals"
                or _journal_id(journal) != identifier
            ):
                raise ValueError("Invalid journal registration")
            entries[identifier] = journal
        return entries, True

    def _write_locator(self, entries: dict[UUID, Path], *, overwrite: bool) -> None:
        path = self.locator_path
        assert path is not None
        data = {
            "schema_version": _INDEX_VERSION,
            "journals": {str(identifier): str(path) for identifier, path in entries.items()},
        }
        _atomic_write(path, _encode(data), overwrite=overwrite)

    def prepare(self, record: TransactionRecord) -> JournalSession:
        """Publish the prepared record and register it before returning a session."""
        if record.state is not TransactionState.PREPARED:
            raise ValueError("A new journal must be prepared")
        with _LOCK:
            try:
                payload = _record_payload(record)
                locator, exists = self._locator()
                if record.transaction_id in locator:
                    raise JournalError("Transaction ID is already registered")
                assert self._fallback is not None
                _mkdir(self._fallback)
                path = self._publish_initial(record.transaction_id, payload)
                locator[record.transaction_id] = path
                self._write_locator(locator, overwrite=exists)
                return JournalSession(self, path, record, payload)
            except (OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
                raise JournalError(
                    f"Cannot prepare transaction {record.transaction_id}: {error}"
                ) from error

    def _publish_initial(self, identifier: UUID, payload: bytes) -> Path:
        errors = []
        for directory in self._directories():
            path = directory / f"{identifier}.json"
            try:
                _mkdir(directory)
            except OSError as error:
                errors.append(f"{directory}: {error}")
                continue
            try:
                _atomic_write(path, payload, overwrite=False)
            except OSError as error:
                try:
                    path.lstat()
                except FileNotFoundError:
                    errors.append(f"{path}: {error}")
                    continue
                raise JournalError(
                    f"Initial journal path may already be published: {path}"
                ) from error
            return path
        raise JournalError("No writable journal location. " + " | ".join(errors))

    def discover(self) -> RecoveryDiscovery:
        """Inspect known locations without creating directories, repairing, or deleting anything."""
        with _LOCK:
            locations: dict[UUID, dict[str, Path]] = {}
            issues = []

            def add(identifier: UUID, path: Path) -> None:
                locations.setdefault(identifier, {}).setdefault(normalize_windows_path(path), path)

            locator_path = self.locator_path
            try:
                index, _ = self._locator()
            except (ValueError, RecursionError) as error:
                assert locator_path is not None
                issues.append(
                    RecoveryLocation(locator_path, RecoveryStatus.DAMAGED, detail=str(error))
                )
            except OSError as error:
                issues.append(
                    RecoveryLocation(
                        locator_path or Path("recovery_index.json"),
                        RecoveryStatus.INACCESSIBLE,
                        detail=str(error),
                    )
                )
            else:
                for identifier, path in index.items():
                    add(identifier, path)
            for directory in self._directories():
                try:
                    _check_directory(directory)
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            path = Path(entry.path)
                            if path.suffix.casefold() != ".json":
                                continue
                            try:
                                add(_journal_id(path), path)
                            except ValueError as error:
                                issues.append(
                                    RecoveryLocation(
                                        path,
                                        RecoveryStatus.DAMAGED,
                                        detail=str(error),
                                    )
                                )
                except FileNotFoundError:
                    continue
                except OSError as error:
                    issues.append(
                        RecoveryLocation(
                            directory,
                            RecoveryStatus.INACCESSIBLE,
                            detail=str(error),
                        )
                    )
            transactions = tuple(
                RecoveryTransaction(
                    identifier,
                    tuple(_inspect_record(path, identifier) for path in sorted(paths.values())),
                )
                for identifier, paths in sorted(locations.items())
            )
            return RecoveryDiscovery(
                transactions, tuple(sorted(issues, key=lambda item: item.path))
            )


def _inspect_record(path: Path, identifier: UUID) -> RecoveryLocation:
    try:
        record = _load_record(path, identifier)
    except FileNotFoundError as error:
        return RecoveryLocation(path, RecoveryStatus.MISSING, detail=str(error))
    except (ValueError, RecursionError) as error:
        return RecoveryLocation(path, RecoveryStatus.DAMAGED, detail=str(error))
    except OSError as error:
        return RecoveryLocation(path, RecoveryStatus.INACCESSIBLE, detail=str(error))
    status = (
        RecoveryStatus.CLEANUP_PENDING if record.state in _TERMINAL else RecoveryStatus.UNRESOLVED
    )
    return RecoveryLocation(path, status, record)


class JournalSession:
    """Pinned journal writer. A failed persistence boundary latches the session closed to edits."""

    def __init__(
        self, store: JournalStore, path: Path, record: TransactionRecord, payload: bytes
    ) -> None:
        self._store = store
        self._path = path
        self._record = record
        self._payload = payload
        self._failed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record(self) -> TransactionRecord:
        with _LOCK:
            return self._record

    @property
    def can_modify(self) -> bool:
        with _LOCK:
            return not self._failed and self._record.state is TransactionState.IN_PROGRESS

    def require_modification(self) -> None:
        if not self.can_modify:
            raise JournalError("The transaction lacks a healthy in-progress journal")

    def _check_persisted(self) -> dict[UUID, Path]:
        locator, _ = self._store._locator()
        if locator.get(self._record.transaction_id) != self._path:
            raise JournalError("The pinned journal is no longer registered")
        if _read(self._path) != self._payload:
            raise JournalError("The pinned journal changed outside this session")
        return locator

    def _persist(self, updated: TransactionRecord) -> None:
        if self._failed:
            raise JournalError("A failed journal session must be retained for inspection")
        try:
            self._check_persisted()
            if updated == self._record:
                return
            updated = replace(updated, updated_at=max(datetime.now(UTC), updated.updated_at))
            payload = _record_payload(updated)
            _atomic_write(self._path, payload, overwrite=True)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
            self._failed = True
            raise JournalError(f"Cannot persist checkpoint at {self._path}: {error}") from error
        self._payload = payload
        self._record = updated

    def start(self) -> None:
        """Persist the in-progress boundary before any source modification."""
        with _LOCK:
            if self._record.state is not TransactionState.PREPARED:
                raise ValueError("Only a prepared transaction can start")
            self._persist(replace(self._record, state=TransactionState.IN_PROGRESS))

    def checkpoint(
        self, *entries: TransactionEntry, recovery_required: str | None = None
    ) -> TransactionRecord:
        """Persist one boundary for the supplied rows. Any other changes remain out of scope."""
        with _LOCK:
            self.require_modification()
            updated = {entry.entry_id: entry for entry in entries}
            if len(updated) != len(entries):
                raise ValueError("Duplicate checkpoint entry ID")
            rows = []
            for old in self._record.entries:
                row = updated.pop(old.entry_id, old)
                if (
                    row.source_path != old.source_path
                    or row.destination_path != old.destination_path
                    or row.source_observation != old.source_observation
                    or row.destination_observation != old.destination_observation
                    or (old.staging_path is not None and row.staging_path != old.staging_path)
                    or (old.temporary_path is not None and row.temporary_path != old.temporary_path)
                ):
                    raise ValueError("Checkpoint changes the scope or discards a recorded artifact")
                rows.append(row)
            if updated:
                raise ValueError("Checkpoint refers to an entry outside the transaction")
            if recovery_required is None and any(
                row.outcome is EntryOutcome.RECOVERY_REQUIRED for row in rows
            ):
                recovery_required = "An entry requires recovery"
            state = (
                TransactionState.RECOVERY_REQUIRED
                if recovery_required is not None
                else TransactionState.IN_PROGRESS
            )
            self._persist(
                replace(
                    self._record,
                    entries=tuple(rows),
                    state=state,
                    failure=recovery_required,
                )
            )
            return self._record

    def finalize(self, evidence: tuple[EntryVerification, ...]) -> TransactionRecord:
        """Persist explicit per-entry verification before retiring the journal and locator entry."""
        with _LOCK:
            if self._record.state not in {
                TransactionState.IN_PROGRESS,
                TransactionState.RECOVERY_REQUIRED,
            }:
                raise ValueError("Only a started transaction can be finalized")
            if tuple(item.entry_id for item in evidence) != tuple(
                row.entry_id for row in self._record.entries
            ):
                raise ValueError("Verification must cover the complete ordered transaction scope")
            rows = []
            original_verified = False
            for row, item in zip(self._record.entries, evidence, strict=True):
                validate_verification(row, item, self._record.operation)
                original = item.outcome is VerifiedOutcome.ORIGINAL
                original_verified |= original
                rows.append(
                    replace(
                        row,
                        stage="verified",
                        outcome=EntryOutcome.FAILED if original else EntryOutcome.SUCCEEDED,
                        verification=item,
                    )
                )
            state = (
                TransactionState.FAILED_SAFE if original_verified else TransactionState.FINALIZED
            )
            self._persist(replace(self._record, entries=tuple(rows), state=state))
            self._retire()
            return self._record

    def _retire(self) -> None:
        # Keep the terminal journal until the locator update succeeds.
        try:
            locator = self._check_persisted()
            retained = dict(locator)
            del locator[self._record.transaction_id]
            self._store._write_locator(locator, overwrite=True)
        except (OSError, ValueError, RecursionError) as error:
            self._failed = True
            raise JournalError(f"Cannot retire locator entry for {self._path}: {error}") from error
        try:
            if _read(self._path) != self._payload:
                raise JournalError("The terminal journal changed before cleanup")
            self._path.unlink()
        except OSError as error:
            self._failed = True
            try:
                current, exists = self._store._locator()
                if not exists or current != locator:
                    raise JournalError("The locator changed before restoration")
                self._store._write_locator(retained, overwrite=True)
            except (OSError, ValueError, RecursionError) as restore_error:
                raise JournalError(
                    f"Journal cleanup failed at {self._path}: {error}. "
                    f"Locator restoration also failed: {restore_error}"
                ) from error
            raise JournalError(f"Journal cleanup failed at {self._path}: {error}") from error
