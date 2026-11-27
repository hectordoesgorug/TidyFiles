"""Read-only exact duplicate analysis over a finalized collection snapshot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO
from uuid import UUID

import xxhash

from tidyfiles.collection import CollectionEntry, CollectionSnapshot
from tidyfiles.destination_observation import PathObservation, PathOccupancy, WindowsPathObserver
from tidyfiles.filesystem_paths import CLUTTER_FILENAMES, is_protected_system_path
from tidyfiles.windows_rename import _check_open_source, _open_parent, _open_relative, _raise_status
from tidyfiles.windows_trash import _stream

_CHUNK_SIZE = 1024 * 1024
_READ_ACCESS = 0x81  # FILE_READ_DATA | FILE_READ_ATTRIBUTES
_SHARE_READ = 0x01


class HashAlgorithm(StrEnum):
    XXHASH64 = "xxhash64"
    XXHASH128 = "xxhash128"


class DuplicateAnalysisState(StrEnum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class DuplicateAnalysisStage(StrEnum):
    INSPECTING = "inspecting"
    HASHING = "hashing"
    COMPARING = "comparing"
    REVALIDATING = "revalidating"


@dataclass(frozen=True, slots=True)
class DuplicateProgress:
    stage: DuplicateAnalysisStage
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    keeper_id: UUID
    redundant_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class DuplicateFailure:
    entry_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class DuplicateAnalysisResult:
    collection_id: UUID
    revision: int
    state: DuplicateAnalysisState
    groups: tuple[DuplicateGroup, ...] = ()
    analyzed_ids: tuple[UUID, ...] = ()
    failures: tuple[DuplicateFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class _Source:
    entry: CollectionEntry
    observation: PathObservation
    size: int

    @property
    def entry_id(self) -> UUID:
        return self.entry.entry_id


@dataclass(frozen=True, slots=True)
class _Evidence:
    observation: PathObservation
    digest: bytes
    partition: int | None


@dataclass(slots=True)
class _Partition:
    members: list[_Source] = field(default_factory=list)
    previous: int | None = None


class _Cancelled(Exception):
    pass


class _FileFailure(Exception):
    def __init__(self, entry_id: UUID, error: OSError) -> None:
        self.entry_id = entry_id
        self.reason = f"{type(error).__name__}: {error}"
        super().__init__(self.reason)


def _observe(observer: WindowsPathObserver, entry: CollectionEntry) -> _Source:
    path = entry.source_path
    if is_protected_system_path(path) or entry.original_filename.casefold() in CLUTTER_FILENAMES:
        raise OSError("Excluded discovery path")
    observation = observer(path)
    current = observation.object_metadata
    if observation.occupancy is PathOccupancy.VACANT:
        raise FileNotFoundError(path)
    if observation.occupancy is not PathOccupancy.FILE or current is None:
        raise OSError(observation.reason or "Source identity unavailable")
    if entry.filesystem_identity is None:
        raise OSError("Discovery identity unavailable")
    if entry.filesystem_identity != current.identity:
        raise OSError("Source identity changed since discovery")
    baseline = entry.metadata
    if baseline is not None and any(
        old is not None and old != new
        for old, new in (
            (baseline.size_bytes, current.size_bytes),
            (baseline.modified_time_ns, current.modified_time_ns),
            (baseline.created_time_ns, current.created_time_ns),
            (baseline.attributes, current.attributes),
        )
    ):
        raise OSError("Source metadata changed since discovery")
    return _Source(entry, observation, current.size_bytes)


@contextmanager
def _read_source(observer: WindowsPathObserver, source: _Source) -> Iterator[BinaryIO]:
    path, expected = source.entry.source_path, source.observation
    try:
        with ExitStack() as stack:
            stream, handle = _open_read_path(observer, stack, path, expected, _SHARE_READ)
            yield stream
            _check_open_source(observer, handle, expected)
    except OSError as error:
        raise _FileFailure(source.entry_id, error) from error


def _open_read_path(
    observer: WindowsPathObserver, stack: ExitStack, path: str, expected: PathObservation,
    share: int,
) -> tuple[BinaryIO, int]:
    assert expected.actual_name is not None
    parent = _open_parent(observer, stack, path, expected)
    handle, status = _open_relative(parent, expected.actual_name, _READ_ACCESS, share)
    if handle is None:
        _raise_status(status)
    stream = stack.enter_context(_stream(observer, handle, False))
    _check_open_source(observer, handle, expected)
    return stream, handle


def _equal_chunks(size: int, first: Callable[[int], bytes], second: Callable[[int], bytes]) -> bool:
    while size:
        count = min(size, _CHUNK_SIZE)
        left, right = first(count), second(count)
        if len(left) != count or left != right:
            return False
        size -= count
    return True


@contextmanager
def hold_exact_duplicate(
    observer: WindowsPathObserver,
    path: str,
    source: PathObservation,
    keeper_path: str,
    keeper: PathObservation,
) -> Iterator[None]:
    """Confirm current bytes and hold read handles through the Trash transfer."""
    original, retained = source.object_metadata, keeper.object_metadata
    if original is None or retained is None or original.size_bytes != retained.size_bytes:
        raise OSError("Duplicate and keeper no longer have the same verified size")
    shared_object = original.identity == retained.identity
    share_delete = 0x04
    with ExitStack() as stack:
        first, source_handle = _open_read_path(
            observer, stack, path, source, _SHARE_READ | share_delete
        )
        second, keeper_handle = _open_read_path(
            observer, stack, keeper_path, keeper,
            _SHARE_READ | share_delete if shared_object else _SHARE_READ,
        )
        if not _equal_chunks(original.size_bytes, first.read, second.read):
            raise OSError("Duplicate and keeper contents differ")
        _check_open_source(observer, source_handle, source)
        _check_open_source(observer, keeper_handle, keeper)
        yield


def _read_chunk(
    stream: BinaryIO, source: _Source, check: Callable[[], None], exact_size: int | None = None
) -> bytes:
    check()
    try:
        chunk = stream.read(_CHUNK_SIZE if exact_size is None else exact_size)
        if exact_size is not None and len(chunk) != exact_size:
            raise OSError("Incomplete comparison read")
        return chunk
    except OSError as error:
        raise _FileFailure(source.entry_id, error) from error


def _hash_source(
    observer: WindowsPathObserver,
    source: _Source,
    algorithm: HashAlgorithm,
    check: Callable[[], None],
) -> bytes:
    check()
    digest = xxhash.xxh64() if algorithm is HashAlgorithm.XXHASH64 else xxhash.xxh128()
    length = 0
    with _read_source(observer, source) as stream:
        while chunk := _read_chunk(stream, source, check):
            digest.update(chunk)
            length += len(chunk)
        if length != source.size:
            raise OSError("Read size differs from inspected size")
    return digest.digest()


def _matches(
    observer: WindowsPathObserver, left: _Source, right: _Source, check: Callable[[], None]
) -> bool:
    check()
    with _read_source(observer, left) as first, _read_source(observer, right) as second:
        return _equal_chunks(
            left.size,
            lambda size: _read_chunk(first, left, check, size),
            lambda size: _read_chunk(second, right, check, size),
        )


def _hash_candidates(
    observer: WindowsPathObserver,
    candidates: list[_Source],
    algorithm: HashAlgorithm,
    workers: int,
    check: Callable[[], None],
    progress: Callable[[int], None],
    hashes: dict[UUID, bytes],
    failures: dict[UUID, str],
) -> None:
    if not candidates:
        return
    remaining = iter(candidates)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="TidyFiles-hash") as pool:
        pending: dict[Future[bytes], UUID] = {}

        def submit(source: _Source) -> None:
            check()
            pending[pool.submit(_hash_source, observer, source, algorithm, check)] = source.entry_id

        try:
            for _ in range(min(workers, len(candidates))):
                submit(next(remaining))
            completed = 0
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    check()
                    entry_id = pending.pop(future)
                    try:
                        hashes[entry_id] = future.result()
                    except _FileFailure as error:
                        failures[entry_id] = error.reason
                    completed += 1
                    progress(completed)
                    if (source := next(remaining, None)) is not None:
                        submit(source)
        finally:
            for future in pending:
                future.cancel()


def _partition_source(
    observer: WindowsPathObserver,
    source: _Source,
    previous: int | None,
    partitions: list[_Partition],
    failures: dict[UUID, str],
    check: Callable[[], None],
) -> None:
    for partition in partitions:
        while partition.members:
            check()
            try:
                if previous is not None and partition.previous is not None:
                    # Both labels came from the same fully partitioned hash bucket.
                    match = previous == partition.previous
                else:
                    match = _matches(observer, partition.members[0], source, check)
            except _FileFailure as error:
                failures[error.entry_id] = error.reason
                if error.entry_id == source.entry_id:
                    return
                partition.members.pop(0)
                continue
            if match:
                partition.members.append(source)
                if previous is not None:
                    partition.previous = previous
                return
            break
    partitions.append(_Partition([source], previous))


class DuplicateAnalyzer:
    """Session-scoped analysis and cache. Call serially from a background worker."""

    def __init__(self, *, managed_paths: tuple[str, ...] = ()) -> None:
        self._managed_paths = managed_paths
        self._cache: dict[UUID, _Evidence] = {}
        self._cache_scope: tuple[UUID, HashAlgorithm] | None = None

    def analyze(
        self,
        snapshot: CollectionSnapshot,
        *,
        algorithm: HashAlgorithm = HashAlgorithm.XXHASH128,
        workers: int = 4,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[DuplicateProgress], None] | None = None,
    ) -> DuplicateAnalysisResult:
        """Analyze one revision. Progress callbacks run on the calling thread."""
        algorithm = HashAlgorithm(algorithm)
        if type(workers) is not int or workers < 1:
            raise ValueError("Hash worker count must be a positive integer")
        if snapshot.capabilities.is_importing:
            raise ValueError("Duplicate analysis requires a finalized collection snapshot")

        def check() -> None:
            if cancel_check is not None and cancel_check():
                raise _Cancelled

        def progress(stage: DuplicateAnalysisStage, completed: int, total: int) -> None:
            check()
            if progress_callback is not None:
                progress_callback(DuplicateProgress(stage, completed, total))
            check()

        observer = WindowsPathObserver(self._managed_paths, capture_identity=True)
        scope = (snapshot.collection_id, algorithm)
        cache = self._cache if scope == self._cache_scope else {}
        valid_cache: dict[UUID, _Evidence] = {}
        failures: dict[UUID, str] = {}
        sources: list[_Source] = []
        sizes: dict[int, int] = defaultdict(int)
        entries = snapshot.entries
        try:
            progress(DuplicateAnalysisStage.INSPECTING, 0, len(entries))
            for count, entry in enumerate(entries, 1):
                try:
                    source = _observe(observer, entry)
                    with _read_source(observer, source):
                        pass  # Verify readable access even when size filtering skips content.
                except (OSError, _FileFailure) as error:
                    failures[entry.entry_id] = str(error)
                else:
                    sources.append(source)
                    sizes[source.size] += 1
                    cached = cache.get(entry.entry_id)
                    if cached is not None and cached.observation == source.observation:
                        valid_cache[entry.entry_id] = cached
                progress(DuplicateAnalysisStage.INSPECTING, count, len(entries))

            candidates = [source for source in sources if sizes[source.size] > 1]
            hashes = {entry_id: evidence.digest for entry_id, evidence in valid_cache.items()}
            to_hash = [source for source in candidates if source.entry_id not in hashes]
            progress(DuplicateAnalysisStage.HASHING, 0, len(to_hash))
            _hash_candidates(
                observer, to_hash, algorithm, workers, check,
                lambda done: progress(DuplicateAnalysisStage.HASHING, done, len(to_hash)),
                hashes, failures,
            )

            buckets: dict[tuple[int, bytes], list[_Partition]] = defaultdict(list)
            progress(DuplicateAnalysisStage.COMPARING, 0, len(candidates))
            for count, source in enumerate(candidates, 1):
                entry_id = source.entry_id
                if entry_id in hashes:
                    cached = valid_cache.get(entry_id)
                    _partition_source(
                        observer, source, cached.partition if cached is not None else None,
                        buckets[source.size, hashes[entry_id]], failures, check,
                    )
                progress(DuplicateAnalysisStage.COMPARING, count, len(candidates))

            fresh = WindowsPathObserver(self._managed_paths, capture_identity=True)
            progress(DuplicateAnalysisStage.REVALIDATING, 0, len(sources))
            for count, source in enumerate(sources, 1):
                if source.entry_id not in failures:
                    try:
                        if _observe(fresh, source.entry).observation != source.observation:
                            raise OSError("Source changed during analysis")
                    except OSError as error:
                        failures[source.entry_id] = str(error)
                progress(DuplicateAnalysisStage.REVALIDATING, count, len(sources))

            groups: list[DuplicateGroup] = []
            labels: dict[UUID, int] = {}
            for partitions in buckets.values():
                for partition in partitions:
                    members = tuple(
                        member.entry_id for member in partition.members
                        if member.entry_id not in failures
                    )
                    label = len(labels)
                    labels.update(dict.fromkeys(members, label))
                    if len(members) > 1:
                        groups.append(DuplicateGroup(members[0], members[1:]))
            order = {entry.entry_id: index for index, entry in enumerate(entries)}
            groups.sort(key=lambda group: order[group.keeper_id])
            analyzed = tuple(
                source.entry_id for source in sources if source.entry_id not in failures
            )
            next_cache = {
                source.entry_id: _Evidence(
                    source.observation, hashes[source.entry_id], labels.get(source.entry_id)
                )
                for source in sources
                if source.entry_id in hashes and source.entry_id not in failures
            }
            diagnostics = tuple(
                DuplicateFailure(entry.entry_id, failures[entry.entry_id])
                for entry in entries if entry.entry_id in failures
            )
            check()
        except _Cancelled:
            return DuplicateAnalysisResult(
                snapshot.collection_id, snapshot.revision, DuplicateAnalysisState.CANCELLED
            )

        self._cache_scope, self._cache = scope, next_cache
        return DuplicateAnalysisResult(
            snapshot.collection_id, snapshot.revision, DuplicateAnalysisState.COMPLETE,
            tuple(groups), analyzed, diagnostics,
        )
