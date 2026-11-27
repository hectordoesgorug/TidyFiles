"""Validated, per-mode Treeview preferences and safe window geometry."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MODES = ("Bulk Rename", "Duplicate Finder", "Repacker")
MODE_KEYS = {
    "Bulk Rename": "bulk_rename",
    "Duplicate Finder": "duplicate_finder",
    "Repacker": "repacker",
}

@dataclass(frozen=True, slots=True)
class Column:
    identifier: str
    label: str
    width: int
    minimum: int


COLUMNS = (
    Column("row_number", "Row", 50, 40),
    Column("filename", "Filename", 280, 120),
    Column("new_filename", "New filename", 280, 120),
    Column("size", "Size", 100, 70),
    Column("status", "Status", 130, 100),
    Column("duplicate_of", "Duplicate of", 230, 120),
    Column("resolution", "Resolution", 120, 95),
    Column("length", "Length", 100, 75),
    Column("extension", "Extension", 85, 60),
    Column("file_type", "File type", 100, 85),
    Column("date_created", "Date created", 160, 120),
    Column("date_modified", "Date modified", 160, 120),
    Column("parent_directory", "Parent directory", 200, 120),
    Column("import_root", "Import root", 200, 120),
    Column("full_path", "Full path", 400, 180),
)
COLUMN_BY_ID = {column.identifier: column for column in COLUMNS}
_OPTIONAL_COLUMNS = (
    "resolution",
    "length",
    "extension",
    "file_type",
    "date_created",
    "date_modified",
    "parent_directory",
    "import_root",
    "full_path",
)
MODE_COLUMNS = {
    "Bulk Rename": (
        "row_number",
        "filename",
        "new_filename",
        "size",
        "status",
        *_OPTIONAL_COLUMNS,
    ),
    "Duplicate Finder": (
        "row_number",
        "filename",
        "size",
        "duplicate_of",
        "status",
        *_OPTIONAL_COLUMNS,
    ),
    "Repacker": (
        "row_number",
        "filename",
        "new_filename",
        "size",
        "resolution",
        "length",
        "status",
        "extension",
        "file_type",
        "date_created",
        "date_modified",
        "parent_directory",
        "import_root",
        "full_path",
    ),
}
REQUIRED_COLUMNS = {
    "Bulk Rename": frozenset({"filename", "new_filename", "status"}),
    "Duplicate Finder": frozenset({"filename", "duplicate_of", "status"}),
    "Repacker": frozenset({"filename", "new_filename", "status"}),
}
DEFAULT_VISIBLE_COLUMNS = {
    "Bulk Rename": ("row_number", "filename", "new_filename", "size", "status"),
    "Duplicate Finder": ("row_number", "filename", "size", "duplicate_of", "status"),
    "Repacker": (
        "row_number",
        "filename",
        "new_filename",
        "size",
        "resolution",
        "length",
        "status",
    ),
}
_GEOMETRY_PATTERN = re.compile(r"^=?([0-9]+)x([0-9]+)([+-][0-9]+)([+-][0-9]+)$")


@dataclass(frozen=True, slots=True)
class ColumnPreferences:
    visible: tuple[str, ...]
    widths: dict[str, int]


def _mode(mode: str) -> str:
    if mode not in MODE_KEYS:
        raise ValueError(f"Unknown application mode: {mode}")
    return mode


def default_column_preferences(mode: str) -> ColumnPreferences:
    mode = _mode(mode)
    widths = {column.identifier: column.width for column in COLUMNS}
    if mode == "Duplicate Finder":
        widths["filename"] = 320
    return ColumnPreferences(
        DEFAULT_VISIBLE_COLUMNS[mode],
        widths,
    )


def get_column_preferences(config: Mapping[str, Any], mode: str) -> ColumnPreferences:
    """Return safe known preferences, adding required columns and defaults."""
    mode = _mode(mode)
    defaults = default_column_preferences(mode)
    settings = config.get("global_settings")
    stored = settings.get("column_preferences") if isinstance(settings, Mapping) else None
    mode_data = stored.get(MODE_KEYS[mode]) if isinstance(stored, Mapping) else None
    if not isinstance(mode_data, Mapping):
        return defaults

    available = MODE_COLUMNS[mode]
    required = REQUIRED_COLUMNS[mode]
    raw_visible = mode_data.get("visible")
    if isinstance(raw_visible, Sequence) and not isinstance(raw_visible, (str, bytes)):
        visible_ids = {
            identifier
            for identifier in raw_visible
            if isinstance(identifier, str) and identifier in available
        }
        visible_ids.update(required)
        visible = tuple(identifier for identifier in available if identifier in visible_ids)
    else:
        visible = defaults.visible

    widths = dict(defaults.widths)
    raw_widths = mode_data.get("widths")
    if isinstance(raw_widths, Mapping):
        for identifier, width in raw_widths.items():
            column = COLUMN_BY_ID.get(identifier) if isinstance(identifier, str) else None
            if (
                column is not None
                and identifier in available
                and type(width) is int
                and column.minimum <= width <= 2000
            ):
                widths[identifier] = width
    return ColumnPreferences(visible, widths)


def set_column_preferences(
    config: dict[str, Any],
    mode: str,
    visible: Sequence[str],
    widths: Mapping[str, int],
) -> None:
    """Store one mode's known visibility and valid widths without touching other settings."""
    mode = _mode(mode)
    if isinstance(visible, (str, bytes)):
        raise TypeError("visible must be a sequence of column identifiers")
    if not isinstance(widths, Mapping):
        raise TypeError("widths must be a mapping of column identifiers to pixels")
    settings = config.get("global_settings")
    if not isinstance(settings, dict):
        raise ValueError("Configuration global_settings must be an object")
    available = MODE_COLUMNS[mode]
    visible_ids = set(visible) | REQUIRED_COLUMNS[mode]
    normalized_visible = [identifier for identifier in available if identifier in visible_ids]
    normalized_widths = default_column_preferences(mode).widths
    for identifier, width in widths.items():
        column = COLUMN_BY_ID.get(identifier) if isinstance(identifier, str) else None
        if column is None or identifier not in available:
            continue
        if type(width) is int and column.minimum <= width <= 2000:
            normalized_widths[identifier] = width

    all_preferences = settings.setdefault("column_preferences", {})
    if not isinstance(all_preferences, dict):
        all_preferences = {}
        settings["column_preferences"] = all_preferences
    all_preferences[MODE_KEYS[mode]] = {
        "visible": normalized_visible,
        "widths": normalized_widths,
    }


def get_appearance_mode(config: Mapping[str, Any]) -> str:
    settings = config.get("global_settings")
    mode = settings.get("appearance_mode") if isinstance(settings, Mapping) else None
    return mode if isinstance(mode, str) and mode in {"System", "Light", "Dark"} else "System"


def restore_window_geometry(
    value: str | None,
    bounds: tuple[int, int, int, int],
    *,
    default_size: tuple[int, int] = (1000, 680),
    minimum_size: tuple[int, int] = (760, 520),
) -> str:
    """Parse and clamp saved Tk geometry to the current virtual-screen bounds."""
    left, top, screen_width, screen_height = bounds
    match = _GEOMETRY_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        width, height = default_size
        x, y = 100, 100
    else:
        width, height, x, y = int(match[1]), int(match[2]), int(match[3]), int(match[4])

    min_width = min(minimum_size[0], max(1, screen_width))
    min_height = min(minimum_size[1], max(1, screen_height))
    width = max(min_width, min(width, max(min_width, screen_width)))
    height = max(min_height, min(height, max(min_height, screen_height)))
    x = max(left, min(x, left + screen_width - width))
    y = max(top, min(y, top + screen_height - height))
    return f"{width}x{height}{x:+d}{y:+d}"
