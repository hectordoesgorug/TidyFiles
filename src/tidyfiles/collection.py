"""Shared in-memory working collection and import-session lifecycle."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Sequence
from uuid import UUID, uuid4

from tidyfiles.sorting import (
    SortCriterion,
    SortDirection,
    SortPreferences,
    sort_entries,
)


class ImportMode(StrEnum):
    ADD_FILES = "add_files"
    ADD_FOLDER = "add_folder"


class ImportSessionState(StrEnum):
    OPEN = "open"
    FINALIZED = "finalized"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class OrderingMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class CollectionError(ValueError):
    """An invalid collection or import-session operation."""


class ImportSessionClosedError(CollectionError):
    """An operation targeted an import session that is no longer open."""


@dataclass(frozen=True, slots=True)
class ImportDiagnostic:
    path: str
    reason: str


def normalize_windows_path(path: str | os.PathLike[str]) -> str:
    """Return a lexical Windows comparison key without accessing the filesystem."""
    value = os.fspath(path)
    if not isinstance(value, str):
        raise TypeError("path must resolve to a string")
    if not value:
        raise ValueError("path must not be empty")
    return ntpath.normcase(ntpath.normpath(value))


def _path_name(path: str) -> str:
    return PureWindowsPath(path).name


@dataclass(frozen=True, slots=True)
class FileMetadataSnapshot:
    size_bytes: int | None = None
    modified_time_ns: int | None = None
    created_time_ns: int | None = None
    physical_file_count: int | None = None
    physical_directory_count: int | None = None
    attributes: int | None = None


@dataclass(frozen=True, slots=True)
class FileSystemIdentitySnapshot:
    """Optional filesystem object identity; it never defines path deduplication."""

    volume_id: int | str
    file_id: int | str


@dataclass(frozen=True, slots=True)
class CollectionEntry:
    entry_id: UUID
    source_path: str
    normalized_path_key: str
    original_filename: str
    source_extension: str
    import_root_path: str | None
    import_root_name: str | None
    parent_path: str | None
    parent_name: str | None
    parent_identity: str | None
    metadata: FileMetadataSnapshot | None
    filesystem_identity: FileSystemIdentitySnapshot | None
    import_session_id: UUID
    is_finalized: bool

    @property
    def original_stem(self) -> str:
        return ntpath.splitext(self.original_filename)[0]


@dataclass(frozen=True, slots=True)
class ImportRootRecord:
    root_id: UUID
    path: str
    normalized_path_key: str
    name: str
    import_session_id: UUID


@dataclass(frozen=True, slots=True)
class ImportSessionSnapshot:
    session_id: UUID
    mode: ImportMode
    state: ImportSessionState
    entry_ids: tuple[UUID, ...]
    folder_root_ids: tuple[UUID, ...]
    previous_order: tuple[UUID, ...]
    diagnostics: tuple[ImportDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    entry: CollectionEntry
    added: bool


@dataclass(frozen=True, slots=True)
class PreparedImportOrder:
    collection_id: UUID
    session_id: UUID
    revision: int
    entry_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CollectionCapabilities:
    is_importing: bool
    can_reorder: bool
    can_remove: bool
    can_clear_all: bool
    can_sort: bool


@dataclass(frozen=True, slots=True)
class CollectionSnapshot:
    collection_id: UUID
    revision: int
    entries: tuple[CollectionEntry, ...]
    import_sessions: tuple[ImportSessionSnapshot, ...]
    folder_roots: tuple[ImportRootRecord, ...]
    ordering_mode: OrderingMode
    sort_preferences: SortPreferences
    capabilities: CollectionCapabilities

    @property
    def finalized_entries(self) -> tuple[CollectionEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_finalized)

    @property
    def incomplete_imports(self) -> tuple[ImportSessionSnapshot, ...]:
        return tuple(
            session
            for session in self.import_sessions
            if session.state is ImportSessionState.INCOMPLETE
        )

    @property
    def has_incomplete_imports(self) -> bool:
        return bool(self.incomplete_imports)


class WorkingCollection:
    """Mutable collection owner exposing immutable ordered snapshots."""

    def __init__(self, sort_preferences: SortPreferences | None = None) -> None:
        self._collection_id = uuid4()
        self._entries: dict[UUID, CollectionEntry] = {}
        self._entry_order: list[UUID] = []
        self._path_entries: dict[str, UUID] = {}
        self._sessions: dict[UUID, ImportSessionSnapshot] = {}
        self._session_entries: dict[UUID, list[UUID]] = {}
        self._folder_roots: dict[UUID, ImportRootRecord] = {}
        self._open_session_id: UUID | None = None
        self._revision = 0
        self._ordering_mode = OrderingMode.AUTOMATIC
        if sort_preferences is not None and not isinstance(sort_preferences, SortPreferences):
            raise TypeError("sort_preferences must be SortPreferences")
        self._sort_preferences = sort_preferences or SortPreferences()

    @property
    def collection_id(self) -> UUID:
        return self._collection_id

    @property
    def ordering_mode(self) -> OrderingMode:
        return self._ordering_mode

    @property
    def sort_preferences(self) -> SortPreferences:
        return self._sort_preferences

    @property
    def capabilities(self) -> CollectionCapabilities:
        importing = self._open_session_id is not None
        allowed = not importing
        return CollectionCapabilities(importing, allowed, allowed, allowed, allowed)

    def begin_import(
        self,
        mode: ImportMode,
        *,
        folder_root: str | os.PathLike[str] | None = None,
    ) -> UUID:
        if not isinstance(mode, ImportMode):
            raise TypeError("mode must be an ImportMode")
        if self._open_session_id is not None:
            raise CollectionError("An import session is already open")
        if mode is ImportMode.ADD_FOLDER and folder_root is None:
            raise ValueError("Add Folder requires its selected folder root")
        if mode is ImportMode.ADD_FILES and folder_root is not None:
            raise ValueError("Add Files does not authorize a folder root")

        session_id = uuid4()
        root_ids: tuple[UUID, ...] = ()
        if folder_root is not None:
            root_path = os.fspath(folder_root)
            root_key = normalize_windows_path(root_path)
            root_id = uuid4()
            root_name = _path_name(root_path)
            self._folder_roots[root_id] = ImportRootRecord(
                root_id, root_path, root_key, root_name, session_id
            )
            root_ids = (root_id,)

        self._sessions[session_id] = ImportSessionSnapshot(
            session_id,
            mode,
            ImportSessionState.OPEN,
            (),
            root_ids,
            tuple(self._entry_order),
        )
        self._session_entries[session_id] = []
        self._open_session_id = session_id
        self._revision += 1
        return session_id

    def register_path(
        self,
        session_id: UUID,
        source_path: str | os.PathLike[str],
        *,
        metadata: FileMetadataSnapshot | None = None,
        filesystem_identity: FileSystemIdentitySnapshot | None = None,
    ) -> RegistrationResult:
        session = self._require_open_session(session_id)
        path = os.fspath(source_path)
        path_key = normalize_windows_path(path)
        existing_id = self._path_entries.get(path_key)
        if existing_id is not None:
            return RegistrationResult(self._entries[existing_id], added=False)

        filename = ntpath.basename(path.rstrip("\\/"))
        if not filename:
            raise ValueError("source_path must identify a file")
        parent = ntpath.dirname(path.rstrip("\\/")) or None
        parent_name = _path_name(parent) if parent is not None else None
        parent_identity = normalize_windows_path(parent) if parent is not None else None
        if session.mode is ImportMode.ADD_FOLDER:
            if not session.folder_root_ids:
                raise CollectionError("Add Folder root authorization was discarded")
            root = self._folder_roots[session.folder_root_ids[0]]
            import_root_path, import_root_name = root.path, root.name
        else:
            import_root_path, import_root_name = parent, parent_name

        entry_id = uuid4()
        entry = CollectionEntry(
            entry_id=entry_id,
            source_path=path,
            normalized_path_key=path_key,
            original_filename=filename,
            source_extension=ntpath.splitext(filename)[1],
            import_root_path=import_root_path,
            import_root_name=import_root_name,
            parent_path=parent,
            parent_name=parent_name,
            parent_identity=parent_identity,
            metadata=metadata,
            filesystem_identity=filesystem_identity,
            import_session_id=session_id,
            is_finalized=False,
        )
        self._entries[entry_id] = entry
        self._entry_order.append(entry_id)
        self._path_entries[path_key] = entry_id
        self._session_entries[session_id].append(entry_id)
        self._revision += 1
        return RegistrationResult(entry, added=True)

    def finalize_import(
        self,
        session_id: UUID,
        *,
        incomplete: bool = False,
        diagnostics: Sequence[ImportDiagnostic] = (),
        prepared_order: PreparedImportOrder | None = None,
    ) -> None:
        session = self._require_open_session(session_id)
        if prepared_order is not None:
            if (
                prepared_order.collection_id != self._collection_id
                or prepared_order.session_id != session_id
                or prepared_order.revision != self._revision
                or len(prepared_order.entry_ids) != len(self._entry_order)
                or set(prepared_order.entry_ids) != set(self._entry_order)
            ):
                raise CollectionError("Prepared import order is stale or incomplete")
        for entry_id in self._session_entries[session_id]:
            self._entries[entry_id] = replace(self._entries[entry_id], is_finalized=True)
        self._sessions[session_id] = replace(
            session,
            state=(ImportSessionState.INCOMPLETE if incomplete else ImportSessionState.FINALIZED),
            diagnostics=tuple(diagnostics),
        )
        new_entry_ids = self._session_entries[session_id]
        if prepared_order is not None:
            self._entry_order = list(prepared_order.entry_ids)
        elif self._ordering_mode is OrderingMode.AUTOMATIC:
            entries = tuple(self._entries[entry_id] for entry_id in self._entry_order)
            sorted_entries = sort_entries(entries, self._sort_preferences)
            self._entry_order = [entry.entry_id for entry in sorted_entries]
        else:
            added_entries = tuple(self._entries[entry_id] for entry_id in new_entry_ids)
            sorted_new = sort_entries(added_entries, self._sort_preferences)
            self._entry_order = [
                *session.previous_order,
                *(entry.entry_id for entry in sorted_new),
            ]
        self._open_session_id = None
        self._revision += 1

    def prepare_import_order(self, session_id: UUID) -> PreparedImportOrder:
        """Sort an immutable import snapshot for later revision-checked finalization."""
        snapshot = self.snapshot()
        session = next(
            (item for item in snapshot.import_sessions if item.session_id == session_id),
            None,
        )
        if session is None or session.state is not ImportSessionState.OPEN:
            raise ImportSessionClosedError("Import session is not open")
        entries = {entry.entry_id: entry for entry in snapshot.entries}
        if snapshot.ordering_mode is OrderingMode.AUTOMATIC:
            ordered = sort_entries(snapshot.entries, snapshot.sort_preferences)
            entry_ids = tuple(entry.entry_id for entry in ordered)
        else:
            new_entries = tuple(entries[entry_id] for entry_id in session.entry_ids)
            ordered_new = sort_entries(new_entries, snapshot.sort_preferences)
            entry_ids = (
                *session.previous_order,
                *(entry.entry_id for entry in ordered_new),
            )
        return PreparedImportOrder(
            snapshot.collection_id,
            session_id,
            snapshot.revision,
            entry_ids,
        )

    def cancel_import(self, session_id: UUID) -> None:
        session = self._require_open_session(session_id)
        self._rollback_session(session)
        self._sessions[session_id] = replace(
            session, state=ImportSessionState.CANCELLED
        )
        self._open_session_id = None
        self._revision += 1

    def fail_import(
        self, session_id: UUID, diagnostics: Sequence[ImportDiagnostic]
    ) -> None:
        session = self._require_open_session(session_id)
        self._rollback_session(session)
        self._sessions[session_id] = replace(
            session,
            state=ImportSessionState.FAILED,
            diagnostics=tuple(diagnostics),
        )
        self._open_session_id = None
        self._revision += 1

    def discard_folder_root_authorization(self, session_id: UUID) -> None:
        session = self._require_open_session(session_id)
        for root_id in session.folder_root_ids:
            self._folder_roots.pop(root_id)
        self._sessions[session_id] = replace(session, folder_root_ids=())
        self._revision += 1

    def _rollback_session(self, session: ImportSessionSnapshot) -> None:
        removed_ids = set(self._session_entries[session.session_id])
        for entry_id in removed_ids:
            entry = self._entries.pop(entry_id)
            self._path_entries.pop(entry.normalized_path_key)
        for root_id in session.folder_root_ids:
            self._folder_roots.pop(root_id)
        self._entry_order = list(session.previous_order)

    def set_order(self, entry_ids: Sequence[UUID]) -> bool:
        """Set a complete manual order; reject stale or partial sequences."""
        self._require_idle("reorder")
        order = tuple(entry_ids)
        if len(order) != len(self._entry_order) or set(order) != set(self._entry_order):
            raise ValueError("order must contain every collection entry exactly once")
        if len(set(order)) != len(order):
            raise ValueError("order must not contain duplicate entry identifiers")
        if order == tuple(self._entry_order):
            return False
        self._entry_order = list(order)
        self._ordering_mode = OrderingMode.MANUAL
        self._revision += 1
        return True

    def move_up(self, entry_ids: Sequence[UUID]) -> bool:
        selected = set(entry_ids)
        order = self._manual_order()
        for index in range(1, len(order)):
            if order[index] in selected and order[index - 1] not in selected:
                order[index - 1], order[index] = order[index], order[index - 1]
        return self._set_manual_order(order)

    def move_down(self, entry_ids: Sequence[UUID]) -> bool:
        selected = set(entry_ids)
        order = self._manual_order()
        for index in range(len(order) - 2, -1, -1):
            if order[index] in selected and order[index + 1] not in selected:
                order[index], order[index + 1] = order[index + 1], order[index]
        return self._set_manual_order(order)

    def move_to_top(self, entry_ids: Sequence[UUID]) -> bool:
        selected = set(entry_ids)
        order = self._manual_order()
        selected_order = [entry_id for entry_id in order if entry_id in selected]
        remaining = [entry_id for entry_id in order if entry_id not in selected]
        return self._set_manual_order([*selected_order, *remaining])

    def move_to_bottom(self, entry_ids: Sequence[UUID]) -> bool:
        selected = set(entry_ids)
        order = self._manual_order()
        selected_order = [entry_id for entry_id in order if entry_id in selected]
        remaining = [entry_id for entry_id in order if entry_id not in selected]
        return self._set_manual_order([*remaining, *selected_order])

    def apply_sorting(
        self,
        criterion: SortCriterion | None = None,
        direction: SortDirection | None = None,
    ) -> bool:
        """Apply explicit sorting to all entries and restore automatic mode."""
        self._require_idle("sort")
        preferences = SortPreferences(
            self._sort_preferences.criterion if criterion is None else criterion,
            self._sort_preferences.direction if direction is None else direction,
        )
        sorted_entries = sort_entries(
            tuple(self._entries[entry_id] for entry_id in self._entry_order), preferences
        )
        order = [entry.entry_id for entry in sorted_entries]
        changed = order != self._entry_order
        self._sort_preferences = preferences
        self._ordering_mode = OrderingMode.AUTOMATIC
        if changed:
            self._entry_order = order
            self._revision += 1
        return changed

    def remove_entries(self, entry_ids: Sequence[UUID]) -> tuple[CollectionEntry, ...]:
        """Remove selected entries from memory without touching source files."""
        self._require_idle("remove entries")
        selected = set(entry_ids)
        removed_ids = [entry_id for entry_id in self._entry_order if entry_id in selected]
        if not removed_ids:
            return ()
        removed = tuple(self._entries.pop(entry_id) for entry_id in removed_ids)
        for entry in removed:
            self._path_entries.pop(entry.normalized_path_key)
        removed_set = set(removed_ids)
        self._entry_order = [
            entry_id for entry_id in self._entry_order if entry_id not in removed_set
        ]
        self._revision += 1
        return removed

    def clear_all(self) -> bool:
        """Clear collection-owned in-memory state; retain sort preferences."""
        self._require_idle("clear all")
        if not (self._entries or self._sessions or self._folder_roots):
            return False
        self._entries.clear()
        self._entry_order.clear()
        self._path_entries.clear()
        self._sessions.clear()
        self._session_entries.clear()
        self._folder_roots.clear()
        self._ordering_mode = OrderingMode.AUTOMATIC
        self._revision += 1
        return True

    def snapshot(self) -> CollectionSnapshot:
        capabilities = self.capabilities
        return CollectionSnapshot(
            collection_id=self.collection_id,
            revision=self._revision,
            entries=tuple(self._entries[entry_id] for entry_id in self._entry_order),
            import_sessions=tuple(
                replace(
                    session,
                    entry_ids=tuple(self._session_entries[session.session_id]),
                )
                for session in self._sessions.values()
            ),
            folder_roots=tuple(self._folder_roots.values()),
            ordering_mode=self._ordering_mode,
            sort_preferences=self._sort_preferences,
            capabilities=capabilities,
        )

    def is_current(self, snapshot: CollectionSnapshot) -> bool:
        """Return whether a result snapshot still matches this collection revision."""
        return snapshot.collection_id == self._collection_id and snapshot.revision == self._revision

    def _manual_order(self) -> list[UUID]:
        self._require_idle("reorder")
        return list(self._entry_order)

    def _set_manual_order(self, order: list[UUID]) -> bool:
        if order == self._entry_order:
            return False
        self._entry_order = order
        self._ordering_mode = OrderingMode.MANUAL
        self._revision += 1
        return True

    def _require_idle(self, operation: str) -> None:
        if self._open_session_id is not None:
            raise CollectionError(f"Cannot {operation} while an import is active")

    def _require_open_session(self, session_id: UUID) -> ImportSessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None or session.state is not ImportSessionState.OPEN:
            raise ImportSessionClosedError("Import session is not open")
        if self._open_session_id != session_id:
            raise ImportSessionClosedError("Import session is no longer current")
        return session
