"""Windows path exclusions shared by discovery and destination planning."""

from pathlib import PureWindowsPath

CLUTTER_FILENAMES = frozenset({"thumbs.db", ".ds_store", "desktop.ini"})
_PROTECTED_DIRECTORIES = frozenset({"$recycle.bin", "system volume information"})


def is_protected_system_path(path: str) -> bool:
    return any(part.casefold() in _PROTECTED_DIRECTORIES for part in PureWindowsPath(path).parts)
