"""Collection sort preferences and deterministic, filesystem-free sort keys."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tidyfiles.collection import CollectionEntry


class SortCriterion(StrEnum):
    FILE_TYPE_AND_NAME = "file_type_name"
    NAME = "name"
    EXTENSION = "extension"
    SIZE = "size"
    DATE_CREATED = "date_created"
    DATE_MODIFIED = "date_modified"
    PATH = "path"


class SortDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True, slots=True)
class SortPreferences:
    criterion: SortCriterion = SortCriterion.FILE_TYPE_AND_NAME
    direction: SortDirection = SortDirection.ASCENDING

    def __post_init__(self) -> None:
        if not isinstance(self.criterion, SortCriterion):
            raise TypeError("criterion must be a SortCriterion")
        if not isinstance(self.direction, SortDirection):
            raise TypeError("direction must be a SortDirection")


_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".ico",
    ".svg", ".raw", ".psd", ".ai", ".heic",
}
_VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mkv", ".m4v",
    ".3gp", ".mpg", ".mpeg", ".ogv",
}
_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx",
    ".ppt", ".pptx", ".csv",
}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"}
_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}


def natural_sort_key(value: str) -> tuple[tuple[object, ...], ...]:
    """Compare decimal runs by value without converting long runs to integers."""
    parts: list[tuple[object, ...]] = []
    for part in re.split(r"(\d+)", value.casefold()):
        if not part:
            continue
        if part.isdecimal():
            digits = "".join(str(unicodedata.decimal(character)) for character in part)
            significant = digits.lstrip("0") or "0"
            parts.append((1, len(significant), significant))
        else:
            parts.append((0, part))
    return tuple(parts)


def _category(extension: str) -> int:
    suffix = extension.casefold()
    for category, extensions in enumerate(
        (
            _IMAGE_EXTENSIONS,
            _VIDEO_EXTENSIONS,
            _DOCUMENT_EXTENSIONS,
            _AUDIO_EXTENSIONS,
            _ARCHIVE_EXTENSIONS,
        )
    ):
        if suffix in extensions:
            return category
    return 5


def _primary_key(
    entry: CollectionEntry,
    criterion: SortCriterion,
    natural_name: tuple[object, ...],
    natural_path: tuple[object, ...],
) -> tuple[object, ...] | None:
    metadata = entry.metadata
    if criterion is SortCriterion.FILE_TYPE_AND_NAME:
        return (_category(entry.source_extension), natural_name)
    if criterion is SortCriterion.NAME:
        return (natural_name,)
    if criterion is SortCriterion.EXTENSION:
        return (entry.source_extension.casefold(),)
    if criterion is SortCriterion.PATH:
        return (natural_path,)
    if metadata is None:
        return None
    if criterion is SortCriterion.SIZE:
        return (metadata.size_bytes,) if metadata.size_bytes is not None else None
    if criterion is SortCriterion.DATE_CREATED:
        return (metadata.created_time_ns,) if metadata.created_time_ns is not None else None
    if criterion is SortCriterion.DATE_MODIFIED:
        return (metadata.modified_time_ns,) if metadata.modified_time_ns is not None else None
    raise AssertionError(f"Unhandled sort criterion: {criterion}")


def sort_entries(
    entries: tuple[CollectionEntry, ...] | list[CollectionEntry],
    preferences: SortPreferences,
) -> tuple[CollectionEntry, ...]:
    """Sort known primary values in the selected direction, then append missing values."""
    known: list[tuple[CollectionEntry, tuple[object, ...]]] = []
    missing: list[tuple[CollectionEntry, tuple[object, ...]]] = []
    for entry in entries:
        name = entry.original_filename
        path = entry.source_path
        natural_name = natural_sort_key(name)
        natural_path = natural_sort_key(path)
        tie_key = (
            natural_name,
            name.casefold(),
            name,
            natural_path,
            entry.normalized_path_key.casefold(),
            path.casefold(),
            path,
        )
        primary = _primary_key(entry, preferences.criterion, natural_name, natural_path)
        if primary is None:
            missing.append((entry, tie_key))
        else:
            known.append((entry, (primary, tie_key)))
    reverse = preferences.direction is SortDirection.DESCENDING
    known.sort(key=lambda pair: pair[1], reverse=reverse)
    missing.sort(key=lambda pair: pair[1])
    return tuple(entry for entry, _ in known) + tuple(entry for entry, _ in missing)
