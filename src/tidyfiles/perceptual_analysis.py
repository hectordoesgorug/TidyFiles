"""Read-only perceptual hashing and reference-based probable duplicate analysis."""

from __future__ import annotations

import io
import json
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import batched
from pathlib import Path
from uuid import UUID

import imagehash
from PIL import Image
from pillow_heif import register_heif_opener

from tidyfiles.collection import CollectionEntry, CollectionSnapshot
from tidyfiles.config import PERCEPTUAL_THRESHOLDS, DuplicatePreferences
from tidyfiles.destination_observation import PathObservation, WindowsPathObserver
from tidyfiles.duplicate_analysis import _FileFailure, _observe, _read_source, _Source
from tidyfiles.media_tools import MediaCancelled, MediaToolError, MediaTools, run_media_command

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".raw", ".heic"}
_VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mkv", ".m4v",
    ".3gp", ".mpg", ".mpeg", ".ogv", ".ts",
}
SAMPLE_PERCENTAGES = (10, 30, 50, 70, 90)
_MEDIA_TIMEOUT = 15.0


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class PerceptualState(StrEnum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class FrameSample:
    percent: int
    seconds: float
    hash_value: int | None = None
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class PerceptualFile:
    entry: CollectionEntry
    kind: MediaKind
    size_bytes: int | None = None
    image_hash: int | None = None
    frames: tuple[FrameSample, ...] = ()
    problem: str | None = None
    observation: PathObservation | None = None


@dataclass(frozen=True, slots=True)
class ProbableMatch:
    file: PerceptualFile
    similarity: float


@dataclass(frozen=True, slots=True)
class ProbableGroup:
    reference_id: UUID
    members: tuple[ProbableMatch, ...]


@dataclass(frozen=True, slots=True)
class PerceptualResult:
    collection_id: UUID
    revision: int
    state: PerceptualState
    files: tuple[PerceptualFile, ...] = ()
    groups: tuple[ProbableGroup, ...] = ()


def _image_hash(image: Image.Image) -> int:
    if image.mode in {"L", "RGB"}:
        return int(str(imagehash.phash(image, hash_size=8)), 16)
    with image.convert("RGB") as converted:
        return int(str(imagehash.phash(converted, hash_size=8)), 16)


def _duration(probe: Path, path: str, cancel_check: Callable[[], bool] | None) -> float:
    output = run_media_command(
        probe,
        ("-v", "error", "-select_streams", "V:0", "-show_entries",
         "stream=duration:format=duration", "-of", "json", path),
        timeout=_MEDIA_TIMEOUT,
        cancel_check=cancel_check,
    )
    try:
        data = json.loads(output)
        stream = data["streams"][0]
        values = (stream.get("duration"), data.get("format", {}).get("duration"))
    except (ValueError, IndexError, KeyError, TypeError, AttributeError) as error:
        raise MediaToolError("FFprobe did not report a usable video stream") from error
    for value in values:
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    raise MediaToolError("Video duration is missing or invalid")


def _video_frames(
    tools: MediaTools, path: str, cancel_check: Callable[[], bool] | None
) -> tuple[FrameSample, ...]:
    ffmpeg, ffprobe = tools.ffmpeg.path, tools.ffprobe.path
    assert ffmpeg is not None and ffprobe is not None
    duration = _duration(ffprobe, path, cancel_check)
    samples = []
    for percent in SAMPLE_PERCENTAGES:
        seconds = duration * (percent / 100)
        timestamp = f"{seconds:.9f}"
        try:
            # Keep the last displayed frame available through its final frame interval.
            output = run_media_command(
                ffmpeg,
                ("-v", "error", "-nostdin", "-copyts", "-start_at_zero", "-ss", timestamp,
                 "-noaccurate_seek", "-i", path, "-map", "0:V:0",
                 "-vf", f"tpad=stop=1:stop_mode=clone,trim=start={timestamp}",
                 "-frames:v", "1", "-threads", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"),
                timeout=_MEDIA_TIMEOUT,
                cancel_check=cancel_check,
            )
            if not output:
                raise MediaToolError("FFmpeg returned no frame")
            with io.BytesIO(output) as stream, Image.open(stream) as image:
                hashed = _image_hash(image)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            samples.append(FrameSample(percent, seconds, problem=str(error)))
        else:
            samples.append(FrameSample(percent, seconds, hashed))
    return tuple(samples)


def _values(file: PerceptualFile) -> tuple[int, ...]:
    if file.problem is not None:
        return ()
    if file.kind is MediaKind.IMAGE:
        return () if file.image_hash is None else (file.image_hash,)
    if tuple(frame.percent for frame in file.frames) != SAMPLE_PERCENTAGES:
        return ()
    hashes = tuple(frame.hash_value for frame in file.frames if frame.hash_value is not None)
    return hashes if len(hashes) == len(SAMPLE_PERCENTAGES) else ()


def perceptual_similarity(first: PerceptualFile, second: PerceptualFile) -> float | None:
    """Compare like media with complete evidence at matching sample positions."""
    if first.kind is not second.kind:
        return None
    left, right = _values(first), _values(second)
    if not left or not right:
        return None
    differences = sum((a ^ b).bit_count() for a, b in zip(left, right, strict=True))
    bits = len(left) * 64
    return (bits - differences) / bits * 100


def probable_groups(
    files: tuple[PerceptualFile, ...],
    threshold: int,
    *,
    check: Callable[[], None] | None = None,
) -> tuple[ProbableGroup, ...]:
    if threshold not in PERCEPTUAL_THRESHOLDS:
        raise ValueError("Unsupported perceptual similarity threshold")
    remaining = tuple(file for file in files if _values(file))
    groups = []
    processed = set()
    for index, reference in enumerate(remaining):
        if check is not None:
            check()
        reference_id = reference.entry.entry_id
        if reference_id in processed:
            continue
        members = [ProbableMatch(reference, 100.0)]
        for candidate_index in range(index + 1, len(remaining)):
            if check is not None:
                check()
            candidate = remaining[candidate_index]
            entry_id = candidate.entry.entry_id
            if entry_id in processed:
                continue
            similarity = perceptual_similarity(reference, candidate)
            if similarity is not None and similarity >= threshold:
                members.append(ProbableMatch(candidate, similarity))
                processed.add(entry_id)
        if len(members) > 1:
            members.sort(key=lambda match: -(match.file.size_bytes or 0))
            groups.append(ProbableGroup(reference_id, tuple(members)))
    return tuple(groups)


def _hash_file(
    observer: WindowsPathObserver,
    source: _Source,
    kind: MediaKind,
    tools: MediaTools | None,
    cancel_check: Callable[[], bool] | None,
) -> PerceptualFile:
    if cancel_check is not None and cancel_check():
        raise MediaCancelled
    file = PerceptualFile(source.entry, kind, source.size, observation=source.observation)
    try:
        with _read_source(observer, source) as stream:
            if kind is MediaKind.IMAGE:
                with Image.open(stream) as image:
                    return replace(file, image_hash=_image_hash(image))
            if tools is None:
                raise MediaToolError("Video perceptual hashing requires local FFmpeg and FFprobe")
            frames = _video_frames(tools, source.entry.source_path, cancel_check)
            problem = "Incomplete video frame extraction" if any(
                frame.hash_value is None for frame in frames
            ) else None
            return replace(file, frames=frames, problem=problem)
    except (OSError, ValueError, Image.DecompressionBombError, _FileFailure) as error:
        return replace(file, problem=str(error))


@dataclass(frozen=True, slots=True)
class _Evidence:
    source: _Source
    tools: MediaTools | None
    file: PerceptualFile


class PerceptualAnalyzer:
    """Analyze active snapshots serially on a background caller's session."""

    def __init__(self, *, managed_paths: tuple[str, ...] = ()) -> None:
        self._managed_paths = managed_paths
        self._cache: dict[UUID, _Evidence] = {}
        self._collection_id: UUID | None = None

    def analyze(
        self,
        snapshot: CollectionSnapshot,
        preferences: DuplicatePreferences,
        *,
        tools: MediaTools | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> PerceptualResult:
        if not preferences.enabled or not preferences.perceptual_enabled:
            return PerceptualResult(
                snapshot.collection_id, snapshot.revision, PerceptualState.DISABLED
            )
        if snapshot.capabilities.is_importing:
            raise ValueError("Perceptual analysis requires a finalized collection snapshot")
        if preferences.perceptual_threshold not in PERCEPTUAL_THRESHOLDS:
            raise ValueError("Unsupported perceptual similarity threshold")
        if type(preferences.workers) is not int or not 1 <= preferences.workers <= 10:
            raise ValueError("Perceptual hashing requires 1 to 10 workers")

        def check() -> None:
            if cancel_check is not None and cancel_check():
                raise MediaCancelled

        entries = [
            (entry, MediaKind.IMAGE if extension in _IMAGE_EXTENSIONS else MediaKind.VIDEO)
            for entry in snapshot.entries
            if (extension := entry.source_extension.casefold())
            in _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS
        ]
        if any(entry.source_extension.casefold() == ".heic" for entry, _ in entries):
            register_heif_opener()
        verified_tools = tools if tools is not None and tools.is_current() else None
        observer = WindowsPathObserver(self._managed_paths, capture_identity=True)
        prior = self._cache if snapshot.collection_id == self._collection_id else {}
        sources: dict[UUID, _Source] = {}
        files: dict[UUID, PerceptualFile] = {}
        pending: list[tuple[_Source, MediaKind]] = []
        cache: dict[UUID, _Evidence] = {}

        def hash_one(task: tuple[_Source, MediaKind]) -> PerceptualFile:
            source, kind = task
            return _hash_file(observer, source, kind, verified_tools, cancel_check)

        try:
            check()
            for entry, kind in entries:
                check()
                entry_id = entry.entry_id
                try:
                    source = _observe(observer, entry)
                except OSError as error:
                    files[entry_id] = PerceptualFile(entry, kind, problem=str(error))
                    continue
                sources[entry_id] = source
                evidence = prior.get(entry_id)
                tool_key = verified_tools if kind is MediaKind.VIDEO else None
                if evidence and evidence.source == source and evidence.tools == tool_key:
                    files[entry_id] = evidence.file
                else:
                    pending.append((source, kind))
            if progress_callback is not None:
                progress_callback(len(files), len(entries))
            if pending:
                with ThreadPoolExecutor(max_workers=preferences.workers) as pool:
                    for batch in batched(pending, preferences.workers):
                        check()
                        for file in pool.map(hash_one, batch):
                            check()
                            files[file.entry.entry_id] = file
                            if progress_callback is not None:
                                progress_callback(len(files), len(entries))
            fresh = WindowsPathObserver(self._managed_paths, capture_identity=True)
            tools_changed = verified_tools is not None and not verified_tools.is_current()
            for entry_id, source in sources.items():
                check()
                file = files[entry_id]
                if not _values(file):
                    continue
                try:
                    if file.kind is MediaKind.VIDEO and tools_changed:
                        raise MediaToolError("Local media tools changed during analysis")
                    if _observe(fresh, source.entry) != source:
                        raise OSError("Source changed during perceptual analysis")
                except OSError as error:
                    files[entry_id] = replace(
                        file, image_hash=None, frames=(), problem=str(error), observation=None
                    )
                else:
                    tool_key = verified_tools if file.kind is MediaKind.VIDEO else None
                    cache[entry_id] = _Evidence(source, tool_key, file)
            ordered = tuple(files[entry.entry_id] for entry, _ in entries)
            groups = probable_groups(ordered, preferences.perceptual_threshold, check=check)
            check()
        except MediaCancelled:
            return PerceptualResult(
                snapshot.collection_id, snapshot.revision, PerceptualState.CANCELLED
            )

        self._collection_id, self._cache = snapshot.collection_id, cache
        return PerceptualResult(
            snapshot.collection_id, snapshot.revision, PerceptualState.COMPLETE, ordered, groups
        )
