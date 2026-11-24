"""Bounded, read-only filesystem discovery for collection imports."""

from __future__ import annotations

import os
import queue
import stat
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Iterable, Iterator
from uuid import UUID

from tidyfiles.collection import (
    CollectionEntry,
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportDiagnostic,
    ImportMode,
    ImportSessionClosedError,
    WorkingCollection,
    normalize_windows_path,
)

_CLUTTER_FILES = {"thumbs.db", ".ds_store", "desktop.ini"}
_PROTECTED_DIRECTORIES = {"$recycle.bin", "system volume information"}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WAKE = object()


class DiscoveryState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CANCELLING = "cancelling"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DiscoveryDisposition(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_LISTED = "already_listed"
    EXCLUDED = "excluded"
    INACCESSIBLE = "inaccessible"


@dataclass(frozen=True, slots=True)
class DiscoveryCounters:
    visited_entries: int = 0
    candidate_files: int = 0
    accepted: int = 0
    already_listed: int = 0
    excluded: int = 0
    inaccessible: int = 0


@dataclass(frozen=True, slots=True)
class DiscoveryItem:
    session_id: UUID
    path: str
    disposition: DiscoveryDisposition
    reason: str | None = None
    entry: CollectionEntry | None = None
    entry_type: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    session_id: UUID
    state: DiscoveryState
    counters: DiscoveryCounters
    diagnostics: tuple[ImportDiagnostic, ...]
    duration_seconds: float | None
    time_to_first_result_seconds: float | None
    cancellation_latency_seconds: float | None
    queue_capacity: int
    peak_queue_occupancy: int
    worker_count: int


@dataclass(frozen=True, slots=True)
class _Record:
    path: str
    disposition: DiscoveryDisposition
    reason: str | None = None
    metadata: FileMetadataSnapshot | None = None
    filesystem_identity: FileSystemIdentitySnapshot | None = None
    entry_type: str | None = None
    coverage_error: bool = False
    revoke_root_authorization: bool = False


class FilesystemDiscovery:
    """Creates one bounded import task at a time; managed paths are explicit."""

    def __init__(
        self,
        collection: WorkingCollection,
        *,
        managed_paths: Iterable[str | os.PathLike[str]] = (),
        queue_capacity: int = 128,
        batch_size: int = 64,
    ) -> None:
        if queue_capacity < 1 or batch_size < 1:
            raise ValueError("queue_capacity and batch_size must be positive")
        self.collection = collection
        self.managed_paths = tuple(
            normalize_windows_path(os.path.abspath(os.fspath(path)))
            for path in managed_paths
        )
        self.queue_capacity = queue_capacity
        self.batch_size = batch_size
        self._task: DiscoverySession | None = None

    def begin_files(self, paths: Iterable[str | os.PathLike[str]]) -> DiscoverySession:
        if self._task is not None and not self._task.is_terminal:
            raise RuntimeError("A discovery session is already active")
        selected_paths = tuple(os.fspath(path) for path in paths)
        if any(not isinstance(path, str) for path in selected_paths):
            raise TypeError("selected paths must resolve to strings")
        if any(not path for path in selected_paths):
            raise ValueError("selected paths must not be empty")
        session_id = self.collection.begin_import(ImportMode.ADD_FILES)
        self._task = DiscoverySession(
            self,
            session_id,
            ImportMode.ADD_FILES,
            selected_paths,
            None,
        )
        return self._task

    def begin_folder(self, root: str | os.PathLike[str]) -> DiscoverySession:
        if self._task is not None and not self._task.is_terminal:
            raise RuntimeError("A discovery session is already active")
        selected_root = os.fspath(root)
        if not isinstance(selected_root, str):
            raise TypeError("selected root must resolve to a string")
        if not selected_root:
            raise ValueError("selected root must not be empty")
        parent_traversal = self._has_parent_segment(selected_root)
        root_path = os.path.abspath(selected_root)
        session_id = self.collection.begin_import(
            ImportMode.ADD_FOLDER, folder_root=root_path
        )
        self._task = DiscoverySession(
            self,
            session_id,
            ImportMode.ADD_FOLDER,
            (),
            root_path,
            parent_traversal=parent_traversal,
        )
        return self._task

    @staticmethod
    def _has_parent_segment(path: str) -> bool:
        return ".." in PureWindowsPath(path).parts


class DiscoverySession:
    """Background producer with consumer-owned collection updates and rollback."""

    def __init__(
        self,
        service: FilesystemDiscovery,
        session_id: UUID,
        mode: ImportMode,
        paths: tuple[str, ...],
        root: str | None,
        *,
        parent_traversal: bool = False,
    ) -> None:
        self.service = service
        self.collection = service.collection
        self.session_id = session_id
        self.mode = mode
        self.paths = paths
        self.root = root
        self.parent_traversal = parent_traversal
        self._queue: queue.Queue[_Record | object] = queue.Queue(service.queue_capacity)
        self._cancel = threading.Event()
        self._producer_done = threading.Event()
        self._producer_waiting = threading.Event()
        self._lock = threading.RLock()
        self._metrics_lock = threading.Lock()
        self._state = DiscoveryState.PENDING
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._first_result_at: float | None = None
        self._cancelled_at: float | None = None
        self._peak_queue_occupancy = 0
        self._visited_entries = 0
        self._candidate_files = 0
        self._accepted = 0
        self._already_listed = 0
        self._excluded = 0
        self._inaccessible_count = 0
        self._diagnostics: list[ImportDiagnostic] = []
        self._fatal_error: ImportDiagnostic | None = None

    @property
    def state(self) -> DiscoveryState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in {
            DiscoveryState.COMPLETE,
            DiscoveryState.INCOMPLETE,
            DiscoveryState.CANCELLED,
            DiscoveryState.FAILED,
        }

    @property
    def producer_waiting(self) -> bool:
        return self._producer_waiting.is_set()

    @property
    def counters(self) -> DiscoveryCounters:
        with self._metrics_lock:
            return DiscoveryCounters(
                self._visited_entries,
                self._candidate_files,
                self._accepted,
                self._already_listed,
                self._excluded,
                self._inaccessible_count,
            )

    @property
    def report(self) -> DiscoveryReport:
        finished = self._finished_at
        duration = (
            finished - self._started_at
            if finished is not None and self._started_at is not None
            else None
        )
        first = (
            self._first_result_at - self._started_at
            if self._first_result_at is not None and self._started_at is not None
            else None
        )
        cancellation_latency = (
            finished - self._cancelled_at
            if finished is not None and self._cancelled_at is not None
            else None
        )
        return DiscoveryReport(
            self.session_id,
            self._state,
            self.counters,
            tuple(self._diagnostics),
            duration,
            first,
            cancellation_latency,
            self.service.queue_capacity,
            self._peak_queue_occupancy,
            1 if self._thread is not None else 0,
        )

    def start(self) -> None:
        with self._lock:
            if self._state is not DiscoveryState.PENDING:
                raise RuntimeError("Discovery session has already been started or cancelled")
            self._started_at = time.monotonic()
            self._state = DiscoveryState.ACTIVE
            self._thread = threading.Thread(
                target=self._produce,
                name=f"TidyFiles-discovery-{self.session_id}",
                daemon=True,
            )
            try:
                self._thread.start()
            except RuntimeError as error:
                self._fatal_error = ImportDiagnostic(
                    self.root or "<selected files>",
                    f"Could not start discovery worker: {error}",
                )
                self._producer_done.set()
                self._advance_terminal_state()

    def cancel(self) -> None:
        with self._lock:
            if self.is_terminal:
                return
            if self._cancel.is_set():
                return
            self._cancel.set()
            self._cancelled_at = time.monotonic()
            if self._state is DiscoveryState.PENDING:
                self.collection.cancel_import(self.session_id)
                self._state = DiscoveryState.CANCELLED
                self._finished_at = self._cancelled_at
                try:
                    self._queue.put_nowait(_WAKE)
                except queue.Full:
                    pass
                return
            self._state = DiscoveryState.CANCELLING
            try:
                self._queue.put_nowait(_WAKE)
            except queue.Full:
                pass

    def consume_batch(
        self, *, limit: int | None = None, timeout: float = 0
    ) -> tuple[DiscoveryItem, ...]:
        batch_limit = self.service.batch_size if limit is None else limit
        if batch_limit < 1 or timeout < 0:
            raise ValueError("limit must be positive and timeout must not be negative")
        output: list[DiscoveryItem] = []
        deadline = time.monotonic() + timeout
        while len(output) < batch_limit:
            try:
                item = self._queue.get(
                    timeout=max(0, deadline - time.monotonic()) if not output and timeout else 0
                )
            except queue.Empty:
                break
            if item is _WAKE:
                if self._cancel.is_set():
                    break
                continue
            assert isinstance(item, _Record)
            with self._lock:
                if self._cancel.is_set():
                    continue
                if item.revoke_root_authorization:
                    self.collection.discard_folder_root_authorization(self.session_id)
                if item.coverage_error:
                    self._diagnostics.append(
                        ImportDiagnostic(item.path, item.reason or "Discovery failed")
                    )
                entry: CollectionEntry | None = None
                disposition = item.disposition
                if disposition is DiscoveryDisposition.ACCEPTED:
                    try:
                        registered = self.collection.register_path(
                            self.session_id,
                            item.path,
                            metadata=item.metadata,
                            filesystem_identity=item.filesystem_identity,
                        )
                    except ImportSessionClosedError:
                        self._fatal_error = ImportDiagnostic(
                            item.path, "Import session closed before discovery result was consumed"
                        )
                        continue
                    entry = registered.entry
                    disposition = (
                        DiscoveryDisposition.ACCEPTED
                        if registered.added
                        else DiscoveryDisposition.ALREADY_LISTED
                    )
                    with self._metrics_lock:
                        if registered.added:
                            self._accepted += 1
                        else:
                            self._already_listed += 1
                output.append(
                    DiscoveryItem(
                        self.session_id,
                        item.path,
                        disposition,
                        item.reason,
                        entry,
                        item.entry_type,
                    )
                )
        self._advance_terminal_state()
        return tuple(output)

    def wait_until_terminal(self, timeout: float = 10) -> DiscoveryReport:
        deadline = time.monotonic() + timeout
        while not self.is_terminal and time.monotonic() < deadline:
            self.consume_batch(timeout=min(0.05, max(0, deadline - time.monotonic())))
        if not self.is_terminal:
            raise TimeoutError("Discovery did not reach a terminal state before timeout")
        if self._thread is not None:
            self._thread.join(timeout=0)
        return self.report

    def _advance_terminal_state(self) -> None:
        if not self._producer_done.is_set() or not self._queue.empty():
            if self._producer_done.is_set() and self._state is DiscoveryState.ACTIVE:
                self._state = DiscoveryState.FINALIZING
            return
        with self._lock:
            if self.is_terminal or not self._producer_done.is_set() or not self._queue.empty():
                return
            if self._cancel.is_set():
                self._cancel_collection_session()
                return
            if self._fatal_error is not None:
                self._diagnostics.append(self._fatal_error)
                self.collection.fail_import(self.session_id, self._diagnostics)
                self._state = DiscoveryState.FAILED
            else:
                incomplete = bool(self._diagnostics)
                self.collection.finalize_import(
                    self.session_id,
                    incomplete=incomplete,
                    diagnostics=self._diagnostics,
                )
                self._state = (
                    DiscoveryState.INCOMPLETE if incomplete else DiscoveryState.COMPLETE
                )
            self._finished_at = time.monotonic()

    def _cancel_collection_session(self) -> None:
        if self.is_terminal:
            return
        try:
            self.collection.cancel_import(self.session_id)
        except ImportSessionClosedError:
            pass
        self._state = DiscoveryState.CANCELLED
        self._finished_at = time.monotonic()

    def _produce(self) -> None:
        try:
            records = (
                self._walk_folder(self.root or "")
                if self.mode is ImportMode.ADD_FOLDER
                else self._walk_files()
            )
            for record in records:
                if self._cancel.is_set() or not self._publish(record):
                    break
        except Exception as error:
            self._fatal_error = ImportDiagnostic(
                self.root or "<selected files>", f"Discovery worker failed: {error}"
            )
        finally:
            if self._cancel.is_set():
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
            else:
                if self._state is DiscoveryState.ACTIVE:
                    self._state = DiscoveryState.FINALIZING
            try:
                self._queue.put_nowait(_WAKE)
            except queue.Full:
                pass
            self._producer_done.set()

    def _publish(self, record: _Record) -> bool:
        while not self._cancel.is_set():
            try:
                self._queue.put(record, timeout=0.05)
            except queue.Full:
                self._producer_waiting.set()
                continue
            self._producer_waiting.clear()
            now = time.monotonic()
            with self._metrics_lock:
                if record.disposition in (
                    DiscoveryDisposition.ACCEPTED,
                    DiscoveryDisposition.ALREADY_LISTED,
                ):
                    self._candidate_files += 1
                elif record.disposition is DiscoveryDisposition.EXCLUDED:
                    self._excluded += 1
                elif record.disposition is DiscoveryDisposition.INACCESSIBLE:
                    self._inaccessible_count += 1
                if self._first_result_at is None:
                    self._first_result_at = now
                self._peak_queue_occupancy = max(self._peak_queue_occupancy, self._queue.qsize())
            return True
        self._producer_waiting.clear()
        return False

    def _walk_files(self) -> Iterator[_Record]:
        for selected in self.paths:
            if self._cancel.is_set():
                return
            if self.service._has_parent_segment(selected):
                self._visit()
                yield _Record(
                    os.path.abspath(selected),
                    DiscoveryDisposition.EXCLUDED,
                    "parent_traversal",
                    entry_type="file",
                )
                continue
            path = os.path.abspath(selected)
            self._visit()
            protected_reason = self._protected_reason(path)
            if protected_reason:
                yield _Record(
                    path,
                    DiscoveryDisposition.EXCLUDED,
                    protected_reason,
                    entry_type="file",
                )
                continue
            try:
                chain_error = self._inspect_ancestors(path)
                if chain_error:
                    disposition, reason = chain_error
                    yield _Record(
                        path,
                        disposition,
                        reason,
                        entry_type=(
                            "reparse_point"
                            if reason in {"reparse_point", "linked_ancestor"}
                            else "file"
                        ),
                        coverage_error=disposition is DiscoveryDisposition.INACCESSIBLE,
                    )
                    continue
                info = os.lstat(path)
            except OSError as error:
                yield self._inaccessible(path, error)
                continue
            record = self._classify_file(path, info)
            if record is not None:
                yield record

    def _walk_folder(self, root: str) -> Iterator[_Record]:
        self._visit()
        if self.parent_traversal:
            yield _Record(
                root,
                DiscoveryDisposition.EXCLUDED,
                "parent_traversal",
                entry_type="directory",
                revoke_root_authorization=True,
            )
            return
        protected_reason = self._protected_reason(root)
        if protected_reason:
            yield _Record(
                root,
                DiscoveryDisposition.EXCLUDED,
                protected_reason,
                entry_type="directory",
                revoke_root_authorization=True,
            )
            return
        try:
            chain_error = self._inspect_ancestors(root)
            if chain_error:
                disposition, reason = chain_error
                yield _Record(
                    root,
                    disposition,
                    reason,
                    entry_type=(
                        "reparse_point"
                        if reason in {"reparse_point", "linked_ancestor"}
                        else "directory"
                    ),
                    coverage_error=disposition is DiscoveryDisposition.INACCESSIBLE,
                    revoke_root_authorization=True,
                )
                return
            root_info = os.lstat(root)
        except OSError as error:
            yield self._inaccessible(root, error, revoke_root_authorization=True)
            return
        if self._is_reparse(root_info):
            yield _Record(
                root,
                DiscoveryDisposition.EXCLUDED,
                "reparse_point",
                entry_type="reparse_point",
                revoke_root_authorization=True,
            )
            return
        if not stat.S_ISDIR(root_info.st_mode):
            yield _Record(
                root,
                DiscoveryDisposition.EXCLUDED,
                "not_a_directory",
                entry_type=self._entry_type(root_info.st_mode),
                revoke_root_authorization=True,
            )
            return
        try:
            first = os.scandir(root)
        except OSError as error:
            yield self._inaccessible(root, error, revoke_root_authorization=True)
            return

        # Keep only one iterator per path depth; never collect a directory's files first.
        stack: list[tuple[str, os.ScandirIterator[str]]] = [(root, first)]
        try:
            while stack and not self._cancel.is_set():
                directory, iterator = stack[-1]
                try:
                    item = next(iterator)
                except StopIteration:
                    iterator.close()
                    stack.pop()
                    continue
                except OSError as error:
                    iterator.close()
                    stack.pop()
                    yield self._inaccessible(directory, error)
                    continue
                path = item.path
                self._visit()
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError as error:
                    yield self._inaccessible(path, error)
                    continue
                if self._is_reparse(info):
                    yield _Record(
                        path,
                        DiscoveryDisposition.EXCLUDED,
                        "reparse_point",
                        entry_type="reparse_point",
                    )
                    continue
                if stat.S_ISDIR(info.st_mode):
                    reason = self._protected_reason(path)
                    if reason:
                        yield _Record(
                            path,
                            DiscoveryDisposition.EXCLUDED,
                            reason,
                            entry_type="directory",
                        )
                        continue
                    try:
                        child_iterator = os.scandir(path)
                    except OSError as error:
                        yield self._inaccessible(path, error)
                        continue
                    stack.append((path, child_iterator))
                elif stat.S_ISREG(info.st_mode):
                    record = self._classify_file(path, info)
                    if record is not None:
                        yield record
                else:
                    yield _Record(
                        path,
                        DiscoveryDisposition.EXCLUDED,
                        "non_regular_entry",
                        entry_type=self._entry_type(info.st_mode),
                    )
        finally:
            for _, iterator in reversed(stack):
                iterator.close()

    def _classify_file(self, path: str, info: os.stat_result) -> _Record | None:
        if self._is_reparse(info):
            return _Record(
                path,
                DiscoveryDisposition.EXCLUDED,
                "reparse_point",
                entry_type="reparse_point",
            )
        if not stat.S_ISREG(info.st_mode):
            return _Record(
                path,
                DiscoveryDisposition.EXCLUDED,
                "non_regular_entry",
                entry_type=self._entry_type(info.st_mode),
            )
        reason = self._protected_reason(path)
        if reason:
            return _Record(path, DiscoveryDisposition.EXCLUDED, reason, entry_type="file")
        if os.path.basename(path).casefold() in _CLUTTER_FILES:
            return _Record(path, DiscoveryDisposition.EXCLUDED, "clutter_file", entry_type="file")
        identity_warning = None
        if os.name == "nt" and getattr(info, "st_ino", 0) == 0:
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as error:
                identity_warning = (
                    "filesystem_identity_unavailable: "
                    f"{type(error).__name__}: {error.strerror or str(error)}"
                )
            if identity_warning is None and self._is_reparse(info):
                return _Record(
                    path,
                    DiscoveryDisposition.EXCLUDED,
                    "reparse_point",
                    entry_type="reparse_point",
                )
            if identity_warning is None and not stat.S_ISREG(info.st_mode):
                return _Record(
                    path,
                    DiscoveryDisposition.EXCLUDED,
                    "non_regular_entry",
                    entry_type=self._entry_type(info.st_mode),
                )
        metadata = FileMetadataSnapshot(
            size_bytes=(
                info.st_size
                if isinstance(getattr(info, "st_size", None), int) and info.st_size >= 0
                else None
            ),
            modified_time_ns=getattr(info, "st_mtime_ns", None),
            created_time_ns=self._created_time_ns(info),
            attributes=getattr(info, "st_file_attributes", None),
        )
        device_id = getattr(info, "st_dev", None)
        file_id = getattr(info, "st_ino", None)
        identity = (
            FileSystemIdentitySnapshot(device_id, file_id)
            if device_id is not None and file_id not in (None, 0)
            else None
        )
        if identity is None and identity_warning is None:
            identity_warning = "filesystem_identity_unavailable"
        return _Record(
            path,
            DiscoveryDisposition.ACCEPTED,
            reason=identity_warning,
            metadata=metadata,
            filesystem_identity=identity,
            entry_type="regular_file",
        )

    def _inspect_ancestors(
        self, path: str
    ) -> tuple[DiscoveryDisposition, str] | None:
        drive, tail = os.path.splitdrive(path)
        current = drive + os.sep if drive else os.sep
        try:
            root_info = os.lstat(current)
        except OSError as error:
            return DiscoveryDisposition.INACCESSIBLE, self._error_reason(error)
        if self._is_reparse(root_info):
            return DiscoveryDisposition.EXCLUDED, "linked_ancestor"
        parts = [part for part in tail.split(os.sep) if part][:-1]
        for part in parts:
            current = os.path.join(current, part)
            reason = self._protected_reason(current)
            if reason:
                return DiscoveryDisposition.EXCLUDED, reason
            try:
                info = os.lstat(current)
            except OSError as error:
                return DiscoveryDisposition.INACCESSIBLE, self._error_reason(error)
            if self._is_reparse(info):
                return DiscoveryDisposition.EXCLUDED, "linked_ancestor"
            if not stat.S_ISDIR(info.st_mode):
                return DiscoveryDisposition.INACCESSIBLE, "ancestor_not_a_directory"
        return None

    def _protected_reason(self, path: str) -> str | None:
        key = normalize_windows_path(path)
        if any(self._is_within(key, root) for root in self.service.managed_paths):
            return "managed_location"
        if any(
            part.casefold() in _PROTECTED_DIRECTORIES
            for part in PureWindowsPath(path).parts
        ):
            return "protected_system_directory"
        return None

    @staticmethod
    def _is_within(path_key: str, root_key: str) -> bool:
        try:
            return os.path.commonpath((path_key, root_key)) == root_key
        except ValueError:
            return False

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        if stat.S_ISLNK(info.st_mode):
            return True
        attributes = getattr(info, "st_file_attributes", None)
        return attributes is not None and bool(attributes & _REPARSE_POINT)

    @staticmethod
    def _created_time_ns(info: os.stat_result) -> int | None:
        created = getattr(info, "st_birthtime_ns", None)
        if created is not None:
            return created
        return getattr(info, "st_ctime_ns", None) if os.name == "nt" else None

    @staticmethod
    def _entry_type(mode: int) -> str:
        if stat.S_ISDIR(mode):
            return "directory"
        if stat.S_ISREG(mode):
            return "regular_file"
        if stat.S_ISFIFO(mode):
            return "fifo"
        if stat.S_ISSOCK(mode):
            return "socket"
        if stat.S_ISCHR(mode):
            return "character_device"
        if stat.S_ISBLK(mode):
            return "block_device"
        return "unknown"

    @staticmethod
    def _error_reason(error: OSError) -> str:
        return f"{type(error).__name__}: {error.strerror or str(error)}"

    def _inaccessible(
        self, path: str, error: OSError, *, revoke_root_authorization: bool = False
    ) -> _Record:
        return _Record(
            path,
            DiscoveryDisposition.INACCESSIBLE,
            self._error_reason(error),
            entry_type="unknown",
            coverage_error=True,
            revoke_root_authorization=revoke_root_authorization,
        )

    def _visit(self) -> None:
        with self._metrics_lock:
            self._visited_entries += 1
