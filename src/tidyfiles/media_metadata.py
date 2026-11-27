"""Read-only image dimensions, stream metadata, and review thumbnails."""

from __future__ import annotations

import io
import json
import math
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from PIL import Image
from pillow_heif import register_heif_opener

from tidyfiles.collection import CollectionEntry
from tidyfiles.destination_observation import PathObservation, WindowsPathObserver
from tidyfiles.duplicate_analysis import _FileFailure, _observe, _read_source
from tidyfiles.media_tools import MediaTool, run_media_command

UNAVAILABLE = "— (unavailable)"
PENDING = "— (not probed)"


def media_category(entry: CollectionEntry) -> str:
    suffix = entry.source_extension.casefold()
    if suffix in {".heic", ".heif", ".raw"}:
        return "image"
    if suffix == ".ts":
        return "video"
    media_type, _ = mimetypes.guess_type(entry.original_filename, strict=False)
    category = media_type.partition("/")[0] if media_type else ""
    return category if category in {"image", "video", "audio"} else ""


@dataclass(frozen=True, slots=True)
class MediaCells:
    resolution: str = ""
    length: str = ""


def default_media_cells(category: str, label: str) -> MediaCells:
    return MediaCells(
        label if category in {"image", "video"} else "",
        label if category in {"audio", "video"} else "",
    )


def _duration(values: tuple[object, ...]) -> str:
    for value in values:
        if not isinstance(value, (str, int, float)):
            continue
        try:
            duration = float(value)
        except (ValueError, OverflowError):
            continue
        if math.isfinite(duration) and duration >= 0:
            hours, rest = divmod(int(duration), 3600)
            minutes, seconds = divmod(rest, 60)
            return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"
    return UNAVAILABLE


def _dimensions(width: object, height: object) -> str:
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        return f"{width}x{height}"
    return UNAVAILABLE


class MediaMetadataProbe:
    """Use one probe serially from the application's background metadata worker."""

    def __init__(self, managed_paths: tuple[str, ...] = ()) -> None:
        self._managed = managed_paths
        self._cache: dict[UUID, tuple[PathObservation, MediaTool | None, MediaCells]] = {}

    def read(
        self,
        entries: tuple[CollectionEntry, ...],
        probe: MediaTool | None,
        *,
        cancelled: Callable[[], bool],
        publish: Callable[[CollectionEntry, MediaCells], None],
    ) -> None:
        observer = WindowsPathObserver(self._managed, capture_identity=True)
        current: dict[UUID, tuple[PathObservation, MediaTool | None, MediaCells]] = {}
        if any(entry.source_extension.casefold() in {".heic", ".heif"} for entry in entries):
            register_heif_opener()
        for entry in entries:
            if cancelled():
                return
            category = media_category(entry)
            if not category:
                continue
            tool = probe if category in {"video", "audio"} else None
            fallback = default_media_cells(category, UNAVAILABLE)
            try:
                source = _observe(observer, entry)
                if category in {"video", "audio"} and (tool is None or not tool.is_current()):
                    publish(entry, fallback)
                    continue
                cached = self._cache.get(entry.entry_id)
                if cached is not None and cached[:2] == (source.observation, tool):
                    cells = cached[2]
                else:
                    with _read_source(observer, source) as stream:
                        if category == "image":
                            with Image.open(stream) as image:
                                cells = MediaCells(_dimensions(*image.size))
                        else:
                            assert tool is not None and tool.path is not None
                            payload = run_media_command(
                                tool.path,
                                ("-v", "error", "-select_streams",
                                 "V:0" if category == "video" else "a:0", "-show_entries",
                                 "stream=width,height,duration:format=duration", "-of", "json",
                                 entry.source_path),
                                timeout=15, cancel_check=cancelled,
                            )
                            data = json.loads(payload)
                            video_or_audio = data["streams"][0]
                            length = _duration((
                                data.get("format", {}).get("duration"),
                                video_or_audio.get("duration"),
                            ))
                            resolution = (
                                _dimensions(
                                    video_or_audio.get("width"), video_or_audio.get("height")
                                )
                                if category == "video" else ""
                            )
                            cells = MediaCells(resolution, length)
                    if tool is not None and not tool.is_current():
                        cells = fallback
            except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError,
                    _FileFailure, Image.DecompressionBombError):
                cells = fallback
            else:
                if cells != fallback:
                    current[entry.entry_id] = (source.observation, tool, cells)
            publish(entry, cells)
        if not cancelled():
            self._cache = current


def image_thumbnail(
    observer: WindowsPathObserver, entry: CollectionEntry, reviewed: PathObservation | None
) -> bytes | None:
    try:
        source = _observe(observer, entry)
        if source.observation != reviewed:
            return None
        with _read_source(observer, source) as stream, Image.open(stream) as image:
            image.thumbnail((50, 50), Image.Resampling.LANCZOS)
            with image.convert("RGBA") as rgba, io.BytesIO() as output:
                rgba.save(output, format="PNG")
                return output.getvalue()
    except (OSError, ValueError, _FileFailure, Image.DecompressionBombError):
        return None
