"""Shared in-memory working collection and import-session lifecycle."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Sequence
from uuid import UUID, uuid4


class ImportMode(StrEnum):
    ADD_FILES = "add_files"
    ADD_FOLDER = "add_folder"


class ImportSessionState(StrEnum):
    OPEN = "open"
    FINALIZED = "finalized"
    CANCELLED = "cancelled"


class CollectionError(ValueError):
    """An invalid collection or import-session operation."""


class ImportSessionClosedError(CollectionError):
    """An operation targeted an import session that is no longer open."""


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


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    entry: CollectionEntry
    added: bool


@dataclass(frozen=True, slots=True)
class CollectionSnapshot:
    collection_id: UUID
    revision: int
    entries: tuple[CollectionEntry, ...]
    import_sessions: tuple[ImportSessionSnapshot, ...]
    folder_roots: tuple[ImportRootRecord, ...]

    @property
    def finalized_entries(self) -> tuple[CollectionEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_finalized)


class WorkingCollection:
    """Mutable collection owner exposing immutable ordered snapshots."""

    def __init__(self) -> None:
        self._collection_id = uuid4()
        self._entries: dict[UUID, CollectionEntry] = {}
        self._entry_order: list[UUID] = []
        self._path_entries: dict[str, UUID] = {}
        self._sessions: dict[UUID, ImportSessionSnapshot] = {}
        self._folder_roots: dict[UUID, ImportRootRecord] = {}
        self._open_session_id: UUID | None = None
        self._revision = 0

    @property
    def collection_id(self) -> UUID:
        return self._collection_id

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
        self._sessions[session_id] = replace(
            session, entry_ids=(*session.entry_ids, entry_id)
        )
        self._revision += 1
        return RegistrationResult(entry, added=True)

    def finalize_import(self, session_id: UUID) -> None:
        session = self._require_open_session(session_id)
        for entry_id in session.entry_ids:
            self._entries[entry_id] = replace(self._entries[entry_id], is_finalized=True)
        self._sessions[session_id] = replace(
            session, state=ImportSessionState.FINALIZED
        )
        self._open_session_id = None
        self._revision += 1

    def cancel_import(self, session_id: UUID) -> None:
        session = self._require_open_session(session_id)
        for entry_id in session.entry_ids:
            entry = self._entries.pop(entry_id)
            self._path_entries.pop(entry.normalized_path_key)
            self._entry_order.remove(entry_id)
        for root_id in session.folder_root_ids:
            self._folder_roots.pop(root_id)
        self._entry_order = list(session.previous_order)
        self._sessions[session_id] = replace(
            session, state=ImportSessionState.CANCELLED
        )
        self._open_session_id = None
        self._revision += 1

    def set_order(self, entry_ids: Sequence[UUID]) -> None:
        order = tuple(entry_ids)
        if len(order) != len(self._entry_order) or set(order) != set(self._entry_order):
            raise ValueError("order must contain every collection entry exactly once")
        if len(set(order)) != len(order):
            raise ValueError("order must not contain duplicate entry identifiers")
        self._entry_order = list(order)
        self._revision += 1

    def snapshot(self) -> CollectionSnapshot:
        return CollectionSnapshot(
            collection_id=self.collection_id,
            revision=self._revision,
            entries=tuple(self._entries[entry_id] for entry_id in self._entry_order),
            import_sessions=tuple(self._sessions.values()),
            folder_roots=tuple(self._folder_roots.values()),
        )

    def _require_open_session(self, session_id: UUID) -> ImportSessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None or session.state is not ImportSessionState.OPEN:
            raise ImportSessionClosedError("Import session is not open")
        if self._open_session_id != session_id:
            raise ImportSessionClosedError("Import session is no longer current")
        return session
