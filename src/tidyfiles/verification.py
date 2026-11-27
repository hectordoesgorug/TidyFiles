"""Bounded, read-only integrity checks with file-specific corruption evidence."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from itertools import batched
from typing import BinaryIO
from uuid import UUID

from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from tidyfiles.collection import CollectionEntry, ExcludedCorruption
from tidyfiles.destination_observation import PathObservation, WindowsPathObserver
from tidyfiles.duplicate_analysis import _FileFailure, _observe, _read_source
from tidyfiles.media_tools import MediaCancelled, MediaTool, MediaToolError, run_media_command
from tidyfiles.perceptual_analysis import _IMAGE_EXTENSIONS, _VIDEO_EXTENSIONS

_IMAGE_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".bmp": "BMP",
    ".tiff": "TIFF", ".webp": "WEBP", ".heic": "HEIF", ".heif": "HEIF",
}
_ISO_VIDEO = {".mp4", ".mov", ".m4v", ".3gp"}
_IMAGE_DATA_ERRORS = (
    "truncated file read", "image file is truncated", "broken data stream when reading image file",
    "broken png file", "bad header checksum", "broken jpeg",
)


class VerificationStatus(StrEnum):
    VERIFIED = "Verified"
    CORRUPTED = "Corrupted"
    UNSUPPORTED = "Unsupported"
    INACCESSIBLE = "Inaccessible"
    INCOMPLETE = "Incomplete"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    entry: CollectionEntry
    status: VerificationStatus
    reason: str
    observation: PathObservation | None = None

    def corruption(self) -> ExcludedCorruption | None:
        if self.status is VerificationStatus.CORRUPTED and self.observation is not None:
            return ExcludedCorruption(self.entry, self.observation, self.reason)
        return None


def _matches_image(header: bytes, image_format: str) -> bool:
    return {
        "JPEG": header.startswith(b"\xff\xd8\xff"),
        "PNG": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "BMP": header.startswith(b"BM"),
        "TIFF": header.startswith((b"II*\x00", b"MM\x00*")),
        "WEBP": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        "HEIF": header[4:8] == b"ftyp" and header[8:12] in {b"heic", b"heix", b"mif1"},
    }.get(image_format, False)


def _image_check(
    stream: BinaryIO, header: bytes, extension: str,
) -> tuple[VerificationStatus, str]:
    image_format = _IMAGE_FORMATS.get(extension, "")
    Image.init()
    if image_format and image_format not in Image.OPEN:
        return VerificationStatus.UNSUPPORTED, f"{image_format} image decoder unavailable"
    if not header and image_format:
        return VerificationStatus.CORRUPTED, f"Empty file lacks the required {image_format} header"
    recognized = _matches_image(header, image_format)
    try:
        stream.seek(0)
        with Image.open(stream) as image:
            image.load()
    except UnidentifiedImageError as error:
        return (
            VerificationStatus.INCOMPLETE if recognized else VerificationStatus.UNSUPPORTED,
            str(error),
        )
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError) as error:
        return VerificationStatus.INCOMPLETE, str(error) or "Image resource limit reached"
    except ImportError as error:
        return VerificationStatus.UNSUPPORTED, str(error)
    except (TypeError, OverflowError, IndexError, struct.error) as error:
        return VerificationStatus.INCOMPLETE, str(error)
    except (EOFError, OSError, SyntaxError, ValueError, NotImplementedError) as error:
        reason = str(error)
        detail = reason.casefold()
        if recognized and any(marker in detail for marker in _IMAGE_DATA_ERRORS):
            return VerificationStatus.CORRUPTED, reason
        if isinstance(error, NotImplementedError) or any(
            marker in detail for marker in ("unsupported", "decoder not available", "not supported")
        ):
            return VerificationStatus.UNSUPPORTED, reason
        if isinstance(error, OSError):
            return VerificationStatus.INACCESSIBLE, reason
        return VerificationStatus.INCOMPLETE, reason
    return VerificationStatus.VERIFIED, "Pillow loaded the image"


def _video_check(
    entry: CollectionEntry, header: bytes, probe: MediaTool | None, cancelled: Callable[[], bool],
) -> tuple[VerificationStatus, str]:
    extension = entry.source_extension.casefold()
    iso_header = extension in _ISO_VIDEO and header[4:8] == b"ftyp"
    if probe is None or not probe.is_current() or probe.path is None:
        detail = "recognized ISO media header" if iso_header else "header read only"
        return VerificationStatus.UNSUPPORTED, f"Portable FFprobe unavailable ({detail})"
    if not header:
        return VerificationStatus.CORRUPTED, "Empty file lacks the required video container data"
    try:
        output = run_media_command(
            probe.path,
            ("-v", "error", "-show_entries", "format=format_name:stream=codec_type,codec_name",
             "-of", "json", entry.source_path),
            timeout=30, cancel_check=cancelled,
        )
        if not probe.is_current():
            return VerificationStatus.UNSUPPORTED, "Portable FFprobe changed during verification"
        data = json.loads(output)
        video = [row for row in data.get("streams", []) if row.get("codec_type") == "video"]
        if not video or any(row.get("codec_name") in (None, "", "unknown") for row in video):
            return VerificationStatus.UNSUPPORTED, "No supported video stream identified by FFprobe"
        if not data.get("format", {}).get("format_name"):
            return VerificationStatus.INCOMPLETE, "FFprobe did not identify the container"
    except MediaToolError as error:
        reason = str(error)
        detail = reason.casefold()
        if not probe.is_current():
            return VerificationStatus.UNSUPPORTED, "Portable FFprobe changed during verification"
        if any(marker in detail for marker in ("permission denied", "input/output error",
                                                "no such file or directory")):
            return VerificationStatus.INACCESSIBLE, reason
        if any(marker in detail for marker in ("unknown decoder", "decoder not found",
                                                "unsupported")):
            return VerificationStatus.UNSUPPORTED, reason
        if iso_header and "moov atom not found" in detail:
            return VerificationStatus.CORRUPTED, "ISO media container lacks its required moov atom"
        return VerificationStatus.INCOMPLETE, reason
    except (ValueError, AttributeError, TypeError, MemoryError) as error:
        return VerificationStatus.INCOMPLETE, str(error) or "FFprobe result unavailable"
    return VerificationStatus.VERIFIED, "FFprobe identified the container and video streams"


class IntegrityVerifier:
    def __init__(self, managed_paths: tuple[str, ...] = ()) -> None:
        self._managed = managed_paths

    def check(
        self, entry: CollectionEntry, probe: MediaTool | None = None, *,
        cancelled: Callable[[], bool] = lambda: False,
        expected: PathObservation | None = None,
    ) -> VerificationResult:
        observation = None
        try:
            if cancelled():
                raise MediaCancelled
            observer = WindowsPathObserver(self._managed, capture_identity=True)
            source = _observe(observer, entry)
            observation = source.observation
            if expected is not None and observation != expected:
                raise OSError("Output changed after rename")
            with _read_source(observer, source) as stream:
                size = min(512, source.size)
                header = stream.read(size)
                extension = entry.source_extension.casefold()
                if len(header) != size:
                    status, reason = VerificationStatus.INCOMPLETE, "Initial read was short"
                elif extension in _IMAGE_EXTENSIONS or extension == ".heif":
                    status, reason = _image_check(stream, header, extension)
                elif extension in _VIDEO_EXTENSIONS:
                    status, reason = _video_check(entry, header, probe, cancelled)
                else:
                    status, reason = VerificationStatus.VERIFIED, f"Read the initial {size} byte(s)"
            if cancelled():
                raise MediaCancelled
        except (OSError, _FileFailure) as error:
            status, reason = VerificationStatus.INACCESSIBLE, str(error)
        except MediaCancelled:
            status, reason = VerificationStatus.INCOMPLETE, "Verification canceled"
        except MemoryError:
            status, reason = VerificationStatus.INCOMPLETE, "Verification resource limit reached"
        return VerificationResult(entry, status, reason, observation)

    def verify(
        self, entries: tuple[CollectionEntry, ...], probe: MediaTool | None, *,
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[int, int, VerificationResult], None] | None = None,
        expected: Mapping[UUID, PathObservation] | None = None,
    ) -> tuple[VerificationResult, ...]:
        if any(entry.source_extension.casefold() in {".heic", ".heif"} for entry in entries):
            register_heif_opener()
        Image.init()
        results = []
        observations = expected or {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="TidyFiles-verify") as pool:
            for batch in batched(entries, 4):
                if cancelled():
                    break
                checks = pool.map(
                    lambda entry: self.check(
                        entry, probe, cancelled=cancelled, expected=observations.get(entry.entry_id)
                    ),
                    batch,
                )
                for result in checks:
                    results.append(result)
                    if progress is not None:
                        progress(len(results), len(entries), result)
        return tuple(results)
