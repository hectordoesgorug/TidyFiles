"""Collection-backed filename evaluation and read-only directory metadata."""

from __future__ import annotations

import os
import stat
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable
from uuid import UUID, uuid4

from tidyfiles.collection import (
    CollectionEntry,
    CollectionSnapshot,
    FileSystemIdentitySnapshot,
    normalize_windows_path,
)
from tidyfiles.filename_evaluator import (
    BatchEvaluationResult,
    EvaluationCancelledError,
    FileEvaluationContext,
    FileEvaluationResult,
    FilenameEvaluationError,
    evaluate_batch,
)
from tidyfiles.filename_format import (
    FilenameFormatParseError,
    TokenKind,
    TokenOccurrence,
    parse_format,
)
from tidyfiles.filename_validation import FilenameValidationError

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_COUNT_KINDS = {TokenKind.NUM_FILES, TokenKind.NUM_DIRS}


class EvaluationMode(StrEnum):
    BULK_RENAME = "bulk_rename"
    RENAME_AND_REPACK = "rename_and_repack"


class EvaluationState(StrEnum):
    NO_EVALUATION = "no_evaluation"
    PENDING = "pending"
    RUNNING = "running"
    VALID = "valid"
    INVALID = "invalid"
    CANCELLED = "cancelled"
    STALE = "stale"


class EvaluationStage(StrEnum):
    PARSING = "parsing"
    METADATA = "metadata"
    CONTEXTS = "contexts"
    EVALUATING = "evaluating"


class DiagnosticKind(StrEnum):
    PARSE = "parse"
    COLLECTION = "collection"
    METADATA = "metadata"
    TOKEN = "token_evaluation"
    VALIDATION = "filename_validation"
    CANCELLATION = "cancellation"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    mode: EvaluationMode = EvaluationMode.BULK_RENAME
    intended_output_extension: str | None = None
    max_component_length: int = 255

    def __post_init__(self) -> None:
        if not isinstance(self.mode, EvaluationMode):
            raise TypeError("mode must be an EvaluationMode")
        if self.intended_output_extension is not None and not isinstance(
            self.intended_output_extension, str
        ):
            raise TypeError("intended_output_extension must be a string or None")
        if self.mode is EvaluationMode.BULK_RENAME and self.intended_output_extension is not None:
            raise ValueError("An intended output extension requires rename-and-repack mode")
        if (
            not isinstance(self.max_component_length, int)
            or isinstance(self.max_component_length, bool)
            or self.max_component_length < 1
        ):
            raise ValueError("max_component_length must be a positive integer")

    @property
    def is_conversion(self) -> bool:
        return self.mode is EvaluationMode.RENAME_AND_REPACK


@dataclass(frozen=True, slots=True)
class EvaluationProgress:
    request_id: UUID
    stage: EvaluationStage
    completed: int
    total: int
    current_path: str | None = None


ProgressCallback = Callable[[EvaluationProgress], None]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class DirectoryMetadataFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DirectoryMetadataObservation:
    path: str
    normalized_path_key: str
    filesystem_identity: FileSystemIdentitySnapshot | None
    physical_file_count: int | None
    physical_directory_count: int | None
    failure: DirectoryMetadataFailure | None = None
    cache_hit: bool = False
    enumeration_attempted: bool = False

    @property
    def available(self) -> bool:
        return (
            self.failure is None
            and self.physical_file_count is not None
            and self.physical_directory_count is not None
        )


class _ObservationError(Exception):
    def __init__(self, failure: DirectoryMetadataFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


def _identity(info: os.stat_result) -> FileSystemIdentitySnapshot | None:
    device = getattr(info, "st_dev", None)
    file_id = getattr(info, "st_ino", None)
    if (
        isinstance(device, int)
        and not isinstance(device, bool)
        and isinstance(file_id, int)
        and not isinstance(file_id, bool)
        and file_id != 0
    ):
        return FileSystemIdentitySnapshot(device, file_id)
    return None


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _path_error(error: OSError) -> DirectoryMetadataFailure:
    if isinstance(error, FileNotFoundError):
        code = "missing_directory"
    elif isinstance(error, NotADirectoryError):
        code = "not_a_directory"
    elif isinstance(error, PermissionError):
        code = "inaccessible_directory"
    else:
        code = "filesystem_error"
    detail = error.strerror or str(error)
    return DirectoryMetadataFailure(code, f"{type(error).__name__}: {detail}")


def _absolute_directory(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str) or not value:
        raise ValueError("directory path must be a nonempty string")
    # abspath is lexical; it does not resolve symlinks or inspect directory contents.
    return os.path.abspath(value)


def _inspect_directory(path: str) -> tuple[str, FileSystemIdentitySnapshot | None]:
    """lstat every component so a static link or reparse path is refused."""
    drive, tail = os.path.splitdrive(path)
    current = drive + os.sep if drive else os.sep
    try:
        info = os.lstat(current)
    except OSError as error:
        raise _ObservationError(_path_error(error)) from error
    if _is_reparse(info):
        raise _ObservationError(
            DirectoryMetadataFailure("linked_path", "Directory path contains a linked root")
        )
    if not stat.S_ISDIR(info.st_mode):
        raise _ObservationError(
            DirectoryMetadataFailure(
                "not_a_directory", "A directory path component is not a directory"
            )
        )

    parts = [part for part in tail.split(os.sep) if part]
    for part in parts:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError as error:
            raise _ObservationError(_path_error(error)) from error
        if _is_reparse(info):
            raise _ObservationError(
                DirectoryMetadataFailure(
                    "linked_path", "Directory path contains a symlink or reparse point"
                )
            )
        if not stat.S_ISDIR(info.st_mode):
            raise _ObservationError(
                DirectoryMetadataFailure(
                    "not_a_directory", "A directory path component is not a directory"
                )
            )
    return current, _identity(info)


class DirectoryCountCache:
    """A bounded LRU keyed by lexical path plus available directory identity."""

    def __init__(self, *, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._observations: OrderedDict[
            tuple[str, FileSystemIdentitySnapshot], DirectoryMetadataObservation
        ] = OrderedDict()
        self._lock = threading.RLock()

    def invalidate(self, directory_path: str | os.PathLike[str] | None = None) -> None:
        """Drop cached observations for one lexical path, or clear the cache."""
        with self._lock:
            if directory_path is None:
                self._observations.clear()
                return
            key = normalize_windows_path(_absolute_directory(directory_path))
            for cache_key in tuple(self._observations):
                if cache_key[0] == key:
                    self._observations.pop(cache_key, None)

    def refresh(
        self,
        directory_path: str | os.PathLike[str],
        *,
        cancel_check: CancelCheck | None = None,
    ) -> DirectoryMetadataObservation:
        """Force a new observation, bypassing prior successes and failures."""
        return self.observe(directory_path, force=True, cancel_check=cancel_check)

    def observe(
        self,
        directory_path: str | os.PathLike[str],
        *,
        force: bool = False,
        cancel_check: CancelCheck | None = None,
    ) -> DirectoryMetadataObservation:
        path = _absolute_directory(directory_path)
        path_key = normalize_windows_path(path)
        with self._lock:
            if force:
                for cache_key in tuple(self._observations):
                    if cache_key[0] == path_key:
                        self._observations.pop(cache_key, None)
            self._check_cancel(cancel_check)
            try:
                actual_path, identity = _inspect_directory(path)
            except _ObservationError as error:
                return DirectoryMetadataObservation(path, path_key, None, None, None, error.failure)

            cache_key = (path_key, identity) if identity is not None else None
            if cache_key is not None and not force:
                cached = self._observations.pop(cache_key, None)
                if cached is not None:
                    self._observations[cache_key] = cached
                    return replace(cached, cache_hit=True, enumeration_attempted=False)

            observation = self._enumerate(
                actual_path, path_key, identity, cancel_check=cancel_check
            )
            if cache_key is not None:
                self._observations[cache_key] = observation
                while len(self._observations) > self.max_entries:
                    self._observations.popitem(last=False)
            return observation

    @staticmethod
    def _check_cancel(cancel_check: CancelCheck | None) -> None:
        if cancel_check is not None and cancel_check():
            raise EvaluationCancelledError("Filename evaluation was cancelled")

    def _enumerate(
        self,
        path: str,
        path_key: str,
        initial_identity: FileSystemIdentitySnapshot | None,
        *,
        cancel_check: CancelCheck | None,
    ) -> DirectoryMetadataObservation:
        files = directories = 0
        try:
            iterator = os.scandir(path)
        except OSError as error:
            return DirectoryMetadataObservation(
                path,
                path_key,
                initial_identity,
                None,
                None,
                _path_error(error),
                enumeration_attempted=True,
            )

        try:
            with iterator:
                while True:
                    self._check_cancel(cancel_check)
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    except OSError as error:
                        raise _ObservationError(
                            DirectoryMetadataFailure(
                                "partial_enumeration",
                                f"Directory enumeration was incomplete: {type(error).__name__}: "
                                f"{error.strerror or str(error)}",
                            )
                        ) from error
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise _ObservationError(
                            DirectoryMetadataFailure(
                                "partial_enumeration",
                                f"Entry metadata was incomplete: {type(error).__name__}: "
                                f"{error.strerror or str(error)}",
                            )
                        ) from error
                    if _is_reparse(info):
                        continue
                    if stat.S_ISREG(info.st_mode):
                        files += 1
                    elif stat.S_ISDIR(info.st_mode):
                        directories += 1
        except _ObservationError as error:
            return DirectoryMetadataObservation(
                path,
                path_key,
                initial_identity,
                None,
                None,
                error.failure,
                enumeration_attempted=True,
            )

        try:
            _, final_identity = _inspect_directory(path)
        except _ObservationError as error:
            return DirectoryMetadataObservation(
                path,
                path_key,
                initial_identity,
                None,
                None,
                DirectoryMetadataFailure(
                    "directory_changed_during_enumeration",
                    "Directory could not be revalidated after enumeration: "
                    f"{error.failure.message}",
                ),
                enumeration_attempted=True,
            )
        if initial_identity != final_identity and (
            initial_identity is not None or final_identity is not None
        ):
            return DirectoryMetadataObservation(
                path,
                path_key,
                initial_identity,
                None,
                None,
                DirectoryMetadataFailure(
                    "directory_replaced_during_enumeration",
                    "Directory identity changed during enumeration",
                ),
                enumeration_attempted=True,
            )
        return DirectoryMetadataObservation(
            path,
            path_key,
            initial_identity,
            files,
            directories,
            enumeration_attempted=True,
        )


@dataclass(frozen=True, slots=True)
class EvaluationDiagnostic:
    kind: DiagnosticKind
    code: str
    message: str
    entry_id: UUID | None = None
    source_path: str | None = None
    token: str | None = None
    position: int | None = None
    required_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryEvaluationResult:
    entry: CollectionEntry
    file_result: FileEvaluationResult | None
    metadata: DirectoryMetadataObservation | None

    @property
    def candidate_filename(self) -> str | None:
        return self.file_result.candidate_filename if self.file_result is not None else None

    @property
    def validated_filename(self) -> str | None:
        return self.file_result.validated_filename if self.file_result is not None else None

    @property
    def is_valid(self) -> bool:
        return (
            self.file_result is not None
            and self.file_result.error is None
            and self.file_result.validation_error is None
            and self.file_result.validated_filename is not None
        )


@dataclass(frozen=True, slots=True)
class FilenameEvaluationResult:
    collection_id: UUID
    collection_revision: int
    request_id: UUID
    expression: str | None
    settings: EvaluationSettings
    state: EvaluationState
    entries: tuple[EntryEvaluationResult, ...]
    diagnostics: tuple[EvaluationDiagnostic, ...]
    batch_result: BatchEvaluationResult | None
    distinct_parent_directories: int
    directory_enumerations: int
    directory_cache_hits: int

    @property
    def is_valid(self) -> bool:
        return self.state is EvaluationState.VALID


def evaluate_collection(
    snapshot: CollectionSnapshot,
    expression: str,
    *,
    settings: EvaluationSettings = EvaluationSettings(),
    request_id: UUID | None = None,
    metadata_cache: DirectoryCountCache | None = None,
    force_metadata_refresh: bool = False,
    cancel_check: CancelCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> FilenameEvaluationResult:
    """Adapt one immutable collection snapshot to the existing ordered engine."""
    request_id = request_id or uuid4()
    cache = metadata_cache or DirectoryCountCache()

    def progress(
        stage: EvaluationStage, completed: int, total: int, path: str | None = None
    ) -> None:
        if progress_callback is not None:
            progress_callback(EvaluationProgress(request_id, stage, completed, total, path))

    def cancelled() -> bool:
        return cancel_check is not None and cancel_check()

    def terminal(
        state: EvaluationState,
        diagnostic: EvaluationDiagnostic,
        *,
        entries: tuple[EntryEvaluationResult, ...] = (),
        batch: BatchEvaluationResult | None = None,
        parents: int = 0,
        enumerations: int = 0,
        cache_hits: int = 0,
    ) -> FilenameEvaluationResult:
        return FilenameEvaluationResult(
            snapshot.collection_id,
            snapshot.revision,
            request_id,
            expression if isinstance(expression, str) else None,
            settings,
            state,
            entries,
            (diagnostic,),
            batch,
            parents,
            enumerations,
            cache_hits,
        )

    try:
        progress(EvaluationStage.PARSING, 0, 1)
        parsed = parse_format(expression)
        progress(EvaluationStage.PARSING, 1, 1)
    except FilenameFormatParseError as error:
        return terminal(
            EvaluationState.INVALID,
            EvaluationDiagnostic(
                DiagnosticKind.PARSE,
                "invalid_expression",
                error.reason,
                token=error.token,
                position=error.position,
            ),
            entries=tuple(EntryEvaluationResult(entry, None, None) for entry in snapshot.entries),
        )

    if cancelled():
        return terminal(
            EvaluationState.CANCELLED,
            EvaluationDiagnostic(
                DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
            ),
        )
    if snapshot.capabilities.is_importing:
        return terminal(
            EvaluationState.INVALID,
            EvaluationDiagnostic(
                DiagnosticKind.COLLECTION,
                "collection_import_active",
                "Filename evaluation requires a finalized collection snapshot",
            ),
            entries=tuple(EntryEvaluationResult(entry, None, None) for entry in snapshot.entries),
        )

    required_tokens = tuple(
        part.raw
        for part in parsed.parts
        if isinstance(part, TokenOccurrence) and part.kind in _COUNT_KINDS
    )
    count_kinds = {
        part.kind
        for part in parsed.parts
        if isinstance(part, TokenOccurrence) and part.kind in _COUNT_KINDS
    }
    entry_metadata: dict[UUID, DirectoryMetadataObservation | None] = {}
    grouped: dict[str, tuple[str, list[CollectionEntry]]] = {}
    if count_kinds:
        for entry in snapshot.entries:
            if cancelled():
                return terminal(
                    EvaluationState.CANCELLED,
                    EvaluationDiagnostic(
                        DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
                    ),
                )
            if entry.parent_path is None:
                entry_metadata[entry.entry_id] = DirectoryMetadataObservation(
                    "",
                    "",
                    None,
                    None,
                    None,
                    DirectoryMetadataFailure(
                        "parent_unavailable", "Source parent directory is unavailable"
                    ),
                )
                continue
            absolute = _absolute_directory(entry.parent_path)
            key = normalize_windows_path(absolute)
            if key not in grouped:
                grouped[key] = (entry.parent_path, [])
            grouped[key][1].append(entry)

    enumerations = cache_hits = 0
    if count_kinds:
        total = len(grouped)
        progress(EvaluationStage.METADATA, 0, total)
        for index, (_key, (path, entries)) in enumerate(grouped.items(), start=1):
            if cancelled():
                return terminal(
                    EvaluationState.CANCELLED,
                    EvaluationDiagnostic(
                        DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
                    ),
                )
            try:
                observation = cache.observe(
                    path,
                    force=force_metadata_refresh,
                    cancel_check=cancel_check,
                )
            except EvaluationCancelledError:
                return terminal(
                    EvaluationState.CANCELLED,
                    EvaluationDiagnostic(
                        DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
                    ),
                    parents=len(grouped),
                    enumerations=enumerations,
                    cache_hits=cache_hits,
                )
            enumerations += observation.enumeration_attempted
            cache_hits += observation.cache_hit
            for entry in entries:
                entry_metadata[entry.entry_id] = observation
            progress(EvaluationStage.METADATA, index, total, path)

    contexts: list[FileEvaluationContext] = []
    diagnostics: list[EvaluationDiagnostic] = []
    total_entries = len(snapshot.entries)
    progress(EvaluationStage.CONTEXTS, 0, total_entries)
    for index, entry in enumerate(snapshot.entries, start=1):
        if cancelled():
            return terminal(
                EvaluationState.CANCELLED,
                EvaluationDiagnostic(
                    DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
                ),
                parents=len(grouped),
                enumerations=enumerations,
                cache_hits=cache_hits,
            )
        observation = entry_metadata.get(entry.entry_id)
        if observation is not None and observation.failure is not None:
            diagnostics.append(
                EvaluationDiagnostic(
                    DiagnosticKind.METADATA,
                    observation.failure.code,
                    observation.failure.message,
                    entry.entry_id,
                    entry.source_path,
                    required_tokens=required_tokens,
                )
            )
        contexts.append(
            FileEvaluationContext(
                original_stem=entry.original_stem,
                file_identifier=entry.source_path,
                import_root_name=entry.import_root_name,
                parent_name=entry.parent_name,
                parent_identity=entry.parent_identity,
                physical_file_count=(
                    observation.physical_file_count if observation is not None else None
                ),
                physical_directory_count=(
                    observation.physical_directory_count if observation is not None else None
                ),
                source_extension=entry.source_extension,
                intended_output_extension=(
                    settings.intended_output_extension if settings.is_conversion else None
                ),
                is_conversion=settings.is_conversion,
            )
        )
        if index % 256 == 0 or index == total_entries:
            progress(EvaluationStage.CONTEXTS, index, total_entries)

    if cancelled():
        return terminal(
            EvaluationState.CANCELLED,
            EvaluationDiagnostic(
                DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
            ),
            parents=len(grouped),
            enumerations=enumerations,
            cache_hits=cache_hits,
        )
    progress(EvaluationStage.EVALUATING, 0, total_entries)
    try:
        batch = evaluate_batch(
            parsed,
            tuple(contexts),
            max_component_length=settings.max_component_length,
            cancel_check=cancel_check,
            progress_callback=lambda complete, total: progress(
                EvaluationStage.EVALUATING, complete, total
            ),
        )
    except EvaluationCancelledError:
        return terminal(
            EvaluationState.CANCELLED,
            EvaluationDiagnostic(
                DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
            ),
            parents=len(grouped),
            enumerations=enumerations,
            cache_hits=cache_hits,
        )
    if cancelled():
        return terminal(
            EvaluationState.CANCELLED,
            EvaluationDiagnostic(
                DiagnosticKind.CANCELLATION, "cancelled", "Evaluation was cancelled"
            ),
            parents=len(grouped),
            enumerations=enumerations,
            cache_hits=cache_hits,
        )

    entry_results: list[EntryEvaluationResult] = []
    for entry, file_result in zip(snapshot.entries, batch.files, strict=True):
        observation = entry_metadata.get(entry.entry_id)
        entry_results.append(EntryEvaluationResult(entry, file_result, observation))
        if isinstance(file_result.error, FilenameEvaluationError):
            diagnostics.append(
                EvaluationDiagnostic(
                    DiagnosticKind.TOKEN,
                    "token_evaluation_failed",
                    file_result.error.reason,
                    entry.entry_id,
                    entry.source_path,
                    file_result.error.token,
                    file_result.error.position,
                )
            )
        if isinstance(file_result.validation_error, FilenameValidationError):
            diagnostics.append(
                EvaluationDiagnostic(
                    DiagnosticKind.VALIDATION,
                    "filename_validation_failed",
                    file_result.validation_error.reason,
                    entry.entry_id,
                    entry.source_path,
                )
            )
    if not snapshot.entries:
        diagnostics.append(
            EvaluationDiagnostic(
                DiagnosticKind.COLLECTION,
                "empty_collection",
                "Filename evaluation requires at least one collection entry",
            )
        )
    return FilenameEvaluationResult(
        snapshot.collection_id,
        snapshot.revision,
        request_id,
        expression if isinstance(expression, str) else None,
        settings,
        EvaluationState.VALID if batch.is_valid else EvaluationState.INVALID,
        tuple(entry_results),
        tuple(diagnostics),
        batch,
        len(grouped),
        enumerations,
        cache_hits,
    )


@dataclass(frozen=True, slots=True)
class CandidateChange:
    entry_id: UUID
    source_path: str
    previous_candidate: str | None
    current_candidate: str | None
    previous_validated_filename: str | None
    current_validated_filename: str | None


def compare_candidate_filenames(
    previous: FilenameEvaluationResult, current: FilenameEvaluationResult
) -> tuple[CandidateChange, ...]:
    """Return changed validated filenames by stable entry ID."""
    old = {item.entry.entry_id: item for item in previous.entries}
    new = {item.entry.entry_id: item for item in current.entries}
    changes = []
    for entry_id in dict.fromkeys((*old, *new)):
        before = old.get(entry_id)
        after = new.get(entry_id)
        old_candidate = before.candidate_filename if before is not None else None
        new_candidate = after.candidate_filename if after is not None else None
        old_validated = before.validated_filename if before is not None else None
        new_validated = after.validated_filename if after is not None else None
        if (
            before is None
            or after is None
            or (old_candidate, old_validated) != (new_candidate, new_validated)
        ):
            row = after if after is not None else before
            assert row is not None
            changes.append(
                CandidateChange(
                    entry_id,
                    row.entry.source_path,
                    old_candidate,
                    new_candidate,
                    old_validated,
                    new_validated,
                )
            )
    return tuple(changes)


@dataclass(slots=True)
class _Job:
    snapshot: CollectionSnapshot
    expression: str
    settings: EvaluationSettings
    request_id: UUID
    force_metadata_refresh: bool
    future: Future[FilenameEvaluationResult]
    cancel_event: threading.Event
    progress_callback: ProgressCallback | None
    cancel_reason: str | None = None


class EvaluationFuture(Future[FilenameEvaluationResult]):
    """Future carrying the request identity needed for cancellation and correlation."""

    def __init__(self, request_id: UUID) -> None:
        super().__init__()
        self.request_id = request_id


class FilenameEvaluationService:
    """One active evaluator and one replaceable pending request, off the GUI thread."""

    def __init__(self, *, cache_entries: int = 256) -> None:
        self.metadata_cache = DirectoryCountCache(max_entries=cache_entries)
        self._condition = threading.Condition()
        self._pending: _Job | None = None
        self._active: _Job | None = None
        self._latest_request_id: UUID | None = None
        self._latest_snapshot: CollectionSnapshot | None = None
        self._latest_expression: str | None = None
        self._latest_settings: EvaluationSettings | None = None
        self._latest_result: FilenameEvaluationResult | None = None
        self._state = EvaluationState.NO_EVALUATION
        self._closed = False
        self._worker = threading.Thread(
            target=self._work,
            name="TidyFiles-filename-evaluation",
            daemon=True,
        )
        self._worker.start()

    @property
    def worker_count(self) -> int:
        return 1

    @property
    def pending_limit(self) -> int:
        return 1

    def submit(
        self,
        snapshot: CollectionSnapshot,
        expression: str,
        *,
        settings: EvaluationSettings = EvaluationSettings(),
        force_metadata_refresh: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> EvaluationFuture:
        request_id = uuid4()
        future = EvaluationFuture(request_id)
        job = _Job(
            snapshot,
            expression,
            settings,
            request_id,
            force_metadata_refresh,
            future,
            threading.Event(),
            progress_callback,
        )
        displaced: _Job | None = None
        with self._condition:
            if self._closed:
                raise RuntimeError("Filename evaluation service is closed")
            if self._active is not None:
                self._active.cancel_reason = "superseded"
                self._active.cancel_event.set()
            if self._pending is not None:
                displaced = self._pending
                self._pending = None
            self._pending = job
            self._latest_request_id = request_id
            self._latest_snapshot = snapshot
            self._latest_expression = expression
            self._latest_settings = settings
            self._latest_result = None
            self._state = EvaluationState.PENDING
            self._condition.notify()
        if displaced is not None:
            displaced.future.set_result(
                _terminal_job_result(displaced, EvaluationState.STALE, "superseded")
            )
        return future

    def cancel(self, request_id: UUID | None = None) -> bool:
        pending: _Job | None = None
        canceled_result: FilenameEvaluationResult | None = None
        with self._condition:
            if request_id is not None and request_id != self._latest_request_id:
                return False
            if self._pending is not None and (
                request_id is None or self._pending.request_id == request_id
            ):
                pending = self._pending
                self._pending = None
                pending.cancel_reason = "cancelled"
                pending.cancel_event.set()
                self._state = EvaluationState.CANCELLED
                canceled_result = _terminal_job_result(
                    pending, EvaluationState.CANCELLED, "cancelled"
                )
                self._latest_result = canceled_result
                self._condition.notify_all()
            elif self._active is not None and (
                request_id is None or self._active.request_id == request_id
            ):
                self._active.cancel_reason = "cancelled"
                self._active.cancel_event.set()
                return True
            else:
                return False
        if pending is not None:
            assert canceled_result is not None
            pending.future.set_result(canceled_result)
        return True

    def lifecycle(
        self,
        snapshot: CollectionSnapshot | None = None,
        *,
        expression: str | None = None,
        settings: EvaluationSettings | None = None,
    ) -> EvaluationState:
        with self._condition:
            state = self._state
            requested = self._latest_snapshot
            requested_expression = self._latest_expression
            requested_settings = self._latest_settings
        if state is EvaluationState.NO_EVALUATION:
            return state
        if expression is not None and expression != requested_expression:
            return EvaluationState.STALE
        if settings is not None and settings != requested_settings:
            return EvaluationState.STALE
        if (
            snapshot is not None
            and requested is not None
            and not _matches_snapshot(requested, snapshot)
        ):
            return EvaluationState.STALE
        return state

    def current_result(
        self,
        snapshot: CollectionSnapshot,
        *,
        expression: str | None = None,
        settings: EvaluationSettings | None = None,
    ) -> FilenameEvaluationResult | None:
        with self._condition:
            result = self._latest_result
            latest_id = self._latest_request_id
        if (
            result is None
            or result.request_id != latest_id
            or result.state not in {EvaluationState.VALID, EvaluationState.INVALID}
            or not _matches_snapshot(result, snapshot)
            or (expression is not None and result.expression != expression)
            or (settings is not None and result.settings != settings)
        ):
            return None
        return result

    def is_current(
        self,
        result: FilenameEvaluationResult,
        snapshot: CollectionSnapshot,
        *,
        expression: str | None = None,
        settings: EvaluationSettings | None = None,
    ) -> bool:
        return self.current_result(snapshot, expression=expression, settings=settings) == result

    def is_current_revision(
        self,
        result: FilenameEvaluationResult,
        *,
        collection_id: UUID,
        revision: int,
        expression: str | None = None,
    ) -> bool:
        """Check a result against the latest request without copying a collection snapshot."""
        with self._condition:
            latest = self._latest_result
            latest_request_id = self._latest_request_id
        return bool(
            latest == result
            and result.request_id == latest_request_id
            and result.collection_id == collection_id
            and result.collection_revision == revision
            and (expression is None or result.expression == expression)
            and result.state in {EvaluationState.VALID, EvaluationState.INVALID}
        )

    def invalidate_metadata(self, directory_path: str | os.PathLike[str] | None = None) -> None:
        self.metadata_cache.invalidate(directory_path)
        pending: _Job | None = None
        stale: FilenameEvaluationResult | None = None
        with self._condition:
            if self._state is not EvaluationState.NO_EVALUATION:
                if self._active is not None:
                    self._active.cancel_reason = "superseded"
                    self._active.cancel_event.set()
                pending = self._pending
                self._pending = None
                if pending is not None:
                    stale = _terminal_job_result(
                        pending, EvaluationState.STALE, "metadata_invalidated"
                    )
                self._latest_result = None
                self._state = EvaluationState.STALE
                self._condition.notify_all()
        if pending is not None and stale is not None:
            pending.future.set_result(stale)

    def _work(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed and self._pending is None:
                    return
                job, self._pending = self._pending, None
                assert job is not None
                self._active = job
                if job.request_id == self._latest_request_id:
                    self._state = EvaluationState.RUNNING
            try:
                result = evaluate_collection(
                    job.snapshot,
                    job.expression,
                    settings=job.settings,
                    request_id=job.request_id,
                    metadata_cache=self.metadata_cache,
                    force_metadata_refresh=job.force_metadata_refresh,
                    cancel_check=job.cancel_event.is_set,
                    progress_callback=lambda update, active_job=job: self._progress(
                        active_job, update
                    ),
                )
            except Exception as error:
                result = FilenameEvaluationResult(
                    job.snapshot.collection_id,
                    job.snapshot.revision,
                    job.request_id,
                    job.expression,
                    job.settings,
                    EvaluationState.INVALID,
                    (),
                    (
                        EvaluationDiagnostic(
                            DiagnosticKind.COLLECTION,
                            "evaluation_failed",
                            f"{type(error).__name__}: {error}",
                        ),
                    ),
                    None,
                    0,
                    0,
                    0,
                )
            with self._condition:
                if job.cancel_reason == "cancelled":
                    result = _terminal_job_result(job, EvaluationState.CANCELLED, "cancelled")
                elif job.cancel_reason == "superseded" or job.request_id != self._latest_request_id:
                    result = _terminal_job_result(job, EvaluationState.STALE, "superseded")
                if job.request_id == self._latest_request_id:
                    self._state = result.state
                    self._latest_result = result
                self._active = None
                self._condition.notify_all()
            if not job.future.done():
                job.future.set_result(result)

    def _progress(self, job: _Job, update: EvaluationProgress) -> None:
        with self._condition:
            callback = job.progress_callback
            deliver = (
                job.request_id == self._latest_request_id
                and not job.cancel_event.is_set()
            )
        if deliver and callback is not None:
            callback(update)

    def close(self, *, wait: bool = True) -> None:
        pending: _Job | None = None
        pending_result: FilenameEvaluationResult | None = None
        with self._condition:
            if not self._closed:
                self._closed = True
                if self._active is not None:
                    self._active.cancel_reason = "cancelled"
                    self._active.cancel_event.set()
                pending = self._pending
                self._pending = None
                if pending is not None:
                    pending.cancel_reason = "cancelled"
                    pending.cancel_event.set()
                    if pending.request_id == self._latest_request_id:
                        self._state = EvaluationState.CANCELLED
                        pending_result = _terminal_job_result(
                            pending, EvaluationState.CANCELLED, "cancelled"
                        )
                        self._latest_result = pending_result
                self._condition.notify_all()
        if pending is not None:
            assert pending_result is not None
            pending.future.set_result(pending_result)
        if wait and threading.current_thread() is not self._worker:
            self._worker.join(timeout=1)

    def __enter__(self) -> FilenameEvaluationService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _matches_snapshot(
    result_or_snapshot: FilenameEvaluationResult | CollectionSnapshot,
    snapshot: CollectionSnapshot,
) -> bool:
    revision = (
        result_or_snapshot.revision
        if isinstance(result_or_snapshot, CollectionSnapshot)
        else result_or_snapshot.collection_revision
    )
    return (
        result_or_snapshot.collection_id == snapshot.collection_id
        and revision == snapshot.revision
        and not snapshot.capabilities.is_importing
    )


def _terminal_job_result(
    job: _Job, state: EvaluationState, reason: str
) -> FilenameEvaluationResult:
    kind = (
        DiagnosticKind.CANCELLATION if state is EvaluationState.CANCELLED else DiagnosticKind.STALE
    )
    code = reason
    message = (
        "Evaluation was cancelled"
        if state is EvaluationState.CANCELLED
        else "Evaluation was superseded"
    )
    return FilenameEvaluationResult(
        job.snapshot.collection_id,
        job.snapshot.revision,
        job.request_id,
        job.expression,
        job.settings,
        state,
        (),
        (EvaluationDiagnostic(kind, code, message),),
        None,
        0,
        0,
        0,
    )
