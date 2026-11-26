"""Read-only Windows metadata for destination planning."""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import stat
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum

from tidyfiles.collection import FileSystemIdentitySnapshot, normalize_windows_path
from tidyfiles.storage import managed_trash_path

# shortcut: ordinary Win32 paths only, extend when preflight can verify long-path support.
_MAX_WIN32_PATH = 259
_LOCAL_VOLUME = re.compile(r"\\Device\\HarddiskVolume[0-9]+", re.IGNORECASE)
_POSSIBLE_SHORT_NAME = re.compile(r"[^.]{0,6}~[0-9]+\b(?:\.[^.]{1,3})?", re.ASCII)
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_UNSAFE_ATTRIBUTES = stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DEVICE
_FILE_CASE_SENSITIVE_SEARCH = 0x00000001
_FILE_CASE_PRESERVED_NAMES = 0x00000002
_FILE_READ_ONLY_VOLUME = 0x00080000
_FILE_CASE_SENSITIVE_INFO = 23
_FILE_BASIC_INFO = 0
_FILE_STANDARD_INFO = 1
_FILE_ID_INFO = 18
_FILE_TYPE_DISK = 1
_FILETIME_UNIX_EPOCH = 116444736000000000
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_ALL = 0x07
_OPEN_EXISTING = 3
_OPEN_DIRECTORY_NOFOLLOW = 0x02200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
_ERROR_FILE_NOT_FOUND = 2


class PathOccupancy(StrEnum):
    VACANT = "vacant"
    FILE = "file"
    DIRECTORY = "directory"
    UNSAFE = "unsafe"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class WindowsObjectObservation:
    """FileIdInfo identity with handle-observed metadata, excluding access time."""

    identity: FileSystemIdentitySnapshot
    size_bytes: int
    modified_time_ns: int
    created_time_ns: int | None
    change_time_ns: int
    attributes: int


@dataclass(frozen=True, slots=True)
class PathObservation:
    occupancy: PathOccupancy
    actual_name: str | None = None
    volume_key: str | None = None
    reason: str | None = None
    read_only_volume: bool = False
    preserves_case: bool = True
    actual_parent: str | None = None
    volume_guid: str | None = None
    parent_identity: FileSystemIdentitySnapshot | None = None
    object_metadata: WindowsObjectObservation | None = None


@dataclass(frozen=True, slots=True)
class _Volume:
    key: str
    component_limit: int
    flags: int
    guid: str | None = None


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("serial", ctypes.c_uint64), ("identifier", ctypes.c_ubyte * 16)]


class _BasicInfo(ctypes.Structure):
    _fields_ = [
        ("created", ctypes.c_int64), ("accessed", ctypes.c_int64),
        ("written", ctypes.c_int64), ("changed", ctypes.c_int64),
        ("attributes", wintypes.DWORD),
    ]


class _StandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocation", ctypes.c_int64), ("size", ctypes.c_int64), ("links", wintypes.DWORD),
        ("delete_pending", ctypes.c_ubyte), ("directory", ctypes.c_ubyte),
    ]


def path_key(path: str) -> str:
    """Conservatively group Windows case variants without normalizing Unicode."""
    return normalize_windows_path(path).casefold().upper()


def is_within(path: str, root: str) -> bool:
    """Compare already normalized Windows keys, including drive roots."""
    return path == root or path.startswith(root.rstrip("\\") + "\\")


def literal_windows_path(path: str, *, allow_root: bool = False) -> str | None:
    """Accept ordinary absolute drive paths without Win32 normalization aliases."""
    if not isinstance(path, str):
        return None
    path = path.replace("/", "\\")
    drive, tail = ntpath.splitdrive(path)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
        return None
    if allow_root and tail == "\\":
        return path
    for part in tail[1:].split("\\"):
        if not part or part in {".", ".."} or ntpath.isreserved(part):
            return None
    return path


def _unverifiable(reason: str) -> PathObservation:
    return PathObservation(PathOccupancy.UNVERIFIABLE, reason=reason)


class WindowsPathObserver:
    """One plan's literal path probes with call-local ancestor and volume observations."""

    def __init__(
        self,
        managed_paths: tuple[str, ...] = (),
        *,
        capture_identity: bool = False,
        allow_managed_trash: bool = False,
    ) -> None:
        self._capture_identity = capture_identity
        trash = None if allow_managed_trash else managed_trash_path()
        self._managed_paths = (*managed_paths, str(trash)) if trash is not None else managed_paths
        self._managed_keys: tuple[tuple[str, str], ...] | None = None
        self._volumes: dict[str, _Volume | PathObservation] = {}
        self._parents: dict[str, str | PathObservation] = {}
        self._directory_ids: dict[str, FileSystemIdentitySnapshot] = {}
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._find_first = kernel.FindFirstFileW
        self._find_first.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.WIN32_FIND_DATAW)]
        self._find_first.restype = wintypes.HANDLE
        self._find_close = kernel.FindClose
        self._find_close.argtypes = [wintypes.HANDLE]
        self._find_close.restype = wintypes.BOOL
        self._query_device = kernel.QueryDosDeviceW
        self._query_device.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        self._query_device.restype = wintypes.DWORD
        self._volume_info = kernel.GetVolumeInformationW
        self._volume_info.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPDWORD,
            wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPWSTR, wintypes.DWORD,
        ]
        self._volume_info.restype = wintypes.BOOL
        self._open = kernel.CreateFileW
        self._open.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._open.restype = wintypes.HANDLE
        self._file_info = kernel.GetFileInformationByHandleEx
        self._file_info.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        self._file_info.restype = wintypes.BOOL
        self._close = kernel.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL
        if capture_identity:
            self._file_type = kernel.GetFileType
            self._file_type.argtypes = [wintypes.HANDLE]
            self._file_type.restype = wintypes.DWORD
            self._volume_name = kernel.GetVolumeNameForVolumeMountPointW
            self._volume_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            self._volume_name.restype = wintypes.BOOL

    def __call__(self, path: str) -> PathObservation:
        if os.name != "nt":
            return _unverifiable("windows_observer_unavailable")
        literal = literal_windows_path(path)
        if literal is None:
            return _unverifiable("unsupported_path")
        try:
            if len(literal.encode("utf-16-le")) // 2 > _MAX_WIN32_PATH:
                return _unverifiable("unsupported_long_path")
        except UnicodeEncodeError:
            return _unverifiable("invalid_path_unicode")
        drive, tail = ntpath.splitdrive(literal)
        volume = self._volume(drive)
        if isinstance(volume, PathObservation):
            return volume
        managed = self._check_managed_paths(volume, tail)
        if managed is not None:
            return managed
        parent_path = ntpath.dirname(literal)
        parent = self._parent(parent_path, volume)
        if isinstance(parent, PathObservation):
            return parent
        name = ntpath.basename(literal)
        if len(name.encode("utf-16-le")) // 2 > volume.component_limit:
            return PathObservation(PathOccupancy.UNSAFE, reason="volume_component_limit")
        found = self._find(literal)
        parent_identity = self._directory_ids.get(parent_path)
        metadata = None
        if isinstance(found, PathObservation):
            if found.occupancy is not PathOccupancy.VACANT:
                return found
            if _looks_like_short_name(name):
                return _unverifiable("possible_short_name_alias")
            occupancy = PathOccupancy.VACANT
            actual_name = None
        else:
            attributes, actual_name = found
            if attributes & (_UNSAFE_ATTRIBUTES | stat.FILE_ATTRIBUTE_DIRECTORY):
                return PathObservation(PathOccupancy.UNSAFE, reason="directory_or_unsafe_entry")
            occupancy = PathOccupancy.FILE
            if self._capture_identity:
                metadata = self._inspect_handle(literal, directory=False, check_case=False)
                if isinstance(metadata, PathObservation):
                    return metadata
                assert metadata is not None and parent_identity is not None
                if metadata.identity.volume_id != parent_identity.volume_id:
                    return _unverifiable("object_volume_changed")
        return PathObservation(
            occupancy,
            actual_name,
            volume.key,
            read_only_volume=bool(volume.flags & _FILE_READ_ONLY_VOLUME),
            preserves_case=bool(volume.flags & _FILE_CASE_PRESERVED_NAMES),
            actual_parent=parent,
            volume_guid=volume.guid,
            parent_identity=parent_identity,
            object_metadata=metadata,
        )

    def observe_directory(self, path: str) -> PathObservation:
        """Capture a directory's own identity for guarded empty-directory cleanup."""
        if os.name != "nt":
            return _unverifiable("windows_observer_unavailable")
        literal = literal_windows_path(path)
        if literal is None:
            return _unverifiable("unsupported_path")
        try:
            if len(literal.encode("utf-16-le")) // 2 > _MAX_WIN32_PATH:
                return _unverifiable("unsupported_long_path")
        except UnicodeEncodeError:
            return _unverifiable("invalid_path_unicode")
        drive, _ = ntpath.splitdrive(literal)
        volume = self._volume(drive)
        if isinstance(volume, PathObservation):
            return volume
        managed = self._check_managed_paths(volume, ntpath.splitdrive(literal)[1])
        if managed is not None:
            return managed
        found = self._find(literal)
        if isinstance(found, PathObservation):
            return found
        attributes, name = found
        if attributes & _UNSAFE_ATTRIBUTES:
            return PathObservation(PathOccupancy.UNSAFE, reason="directory_or_unsafe_entry")
        if not attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
            return PathObservation(PathOccupancy.UNSAFE, reason="not_a_directory")
        if len(name.encode("utf-16-le")) // 2 > volume.component_limit:
            return PathObservation(PathOccupancy.UNSAFE, reason="volume_component_limit")
        parent_path = ntpath.dirname(literal)
        parent = self._parent(parent_path, volume)
        if isinstance(parent, PathObservation):
            return parent
        metadata = self._inspect_handle(literal, directory=True, check_case=False)
        if isinstance(metadata, PathObservation):
            return metadata
        if metadata is None:
            return _unverifiable("directory_identity_unavailable")
        parent_identity = self._directory_ids.get(parent_path)
        if parent_identity is None and parent_path != literal:
            return _unverifiable("directory_parent_identity_unavailable")
        if parent_identity is not None and metadata.identity.volume_id != parent_identity.volume_id:
            return _unverifiable("directory_volume_changed")
        return PathObservation(
            PathOccupancy.DIRECTORY,
            name,
            volume.key,
            actual_parent=parent,
            volume_guid=volume.guid,
            parent_identity=parent_identity,
            object_metadata=metadata,
        )

    def _volume(self, drive: str) -> _Volume | PathObservation:
        drive = drive.upper()
        if drive not in self._volumes:
            self._volumes[drive] = self._inspect_volume(drive)
        return self._volumes[drive]

    def _inspect_volume(self, drive: str) -> _Volume | PathObservation:
        mapping = ctypes.create_unicode_buffer(4096)
        if not self._query_device(drive, mapping, len(mapping)):
            return _unverifiable(f"drive_query_error_{ctypes.get_last_error()}")
        if not _LOCAL_VOLUME.fullmatch(mapping.value):
            return _unverifiable("unsupported_drive_mapping")
        maximum = wintypes.DWORD()
        flags = wintypes.DWORD()
        if not self._volume_info(
            drive + "\\", None, 0, None, ctypes.byref(maximum), ctypes.byref(flags), None, 0
        ):
            return _unverifiable(f"volume_query_error_{ctypes.get_last_error()}")
        if not maximum.value:
            return _unverifiable("volume_component_limit_unavailable")
        guid = None
        if self._capture_identity:
            volume_name = ctypes.create_unicode_buffer(64)
            if not self._volume_name(drive + "\\", volume_name, len(volume_name)):
                return _unverifiable(f"volume_name_error_{ctypes.get_last_error()}")
            mapping_again = ctypes.create_unicode_buffer(4096)
            if (
                not self._query_device(drive, mapping_again, len(mapping_again))
                or mapping_again.value != mapping.value
            ):
                return _unverifiable("drive_mapping_changed")
            guid = volume_name.value.lower()
        return _Volume(mapping.value.upper(), maximum.value, flags.value, guid)

    def _check_managed_paths(self, volume: _Volume, tail: str) -> PathObservation | None:
        if not self._managed_paths:
            return None
        if self._managed_keys is None:
            keys = []
            for root in self._managed_paths:
                literal = literal_windows_path(root, allow_root=True)
                if literal is None:
                    return _unverifiable("managed_path_unverifiable")
                drive, root_tail = ntpath.splitdrive(literal)
                if any(_looks_like_short_name(part) for part in root_tail.split("\\")):
                    return _unverifiable("managed_path_unverifiable")
                managed_volume = self._volume(drive)
                if isinstance(managed_volume, PathObservation):
                    return _unverifiable("managed_volume_unverifiable")
                keys.append((managed_volume.key, path_key(root_tail)))
            self._managed_keys = tuple(keys)
        key = path_key(tail)
        if any(
            volume.key == managed_volume and is_within(key, root)
            for managed_volume, root in self._managed_keys
        ):
            return PathObservation(PathOccupancy.UNSAFE, reason="managed_location")
        return None

    def _parent(self, path: str, volume: _Volume) -> str | PathObservation:
        if path not in self._parents:
            self._parents[path] = self._inspect_parent(path, volume)
        return self._parents[path]

    def _inspect_parent(self, path: str, volume: _Volume) -> str | PathObservation:
        ancestor = ntpath.dirname(path)
        actual_path = "\\"
        if ancestor != path:
            parent = self._parent(ancestor, volume)
            if isinstance(parent, PathObservation):
                return parent
            found = self._find(path)
            if isinstance(found, PathObservation):
                if found.occupancy is PathOccupancy.VACANT:
                    return _unverifiable("parent_missing")
                return found
            attributes, name = found
            if attributes & _UNSAFE_ATTRIBUTES or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                return PathObservation(PathOccupancy.UNSAFE, reason="unsafe_parent")
            actual_path = ntpath.join(parent, name)
        check_case = bool(volume.flags & _FILE_CASE_SENSITIVE_SEARCH)
        if check_case or self._capture_identity:
            result = self._inspect_handle(path, directory=True, check_case=check_case)
            if isinstance(result, PathObservation):
                return result
            if result is not None:
                ancestor_id = self._directory_ids.get(ancestor)
                if ancestor_id and ancestor_id.volume_id != result.identity.volume_id:
                    return _unverifiable("parent_volume_changed")
                self._directory_ids[path] = result.identity
        return actual_path

    def _inspect_handle(
        self, path: str, *, directory: bool, check_case: bool
    ) -> WindowsObjectObservation | PathObservation | None:
        handle = self._open(
            path, _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
            _OPEN_DIRECTORY_NOFOLLOW, None,
        )
        if handle == _INVALID_HANDLE:
            return _unverifiable(f"metadata_open_error_{ctypes.get_last_error()}")
        try:
            metadata = None
            if self._capture_identity:
                metadata = self._object_metadata(handle, directory)
                if isinstance(metadata, PathObservation):
                    return metadata
            if check_case:
                flags = wintypes.DWORD()
                if not self._file_info(
                    handle, _FILE_CASE_SENSITIVE_INFO, ctypes.byref(flags), ctypes.sizeof(flags)
                ):
                    return _unverifiable(f"directory_case_query_error_{ctypes.get_last_error()}")
                if flags.value:
                    return _unverifiable("case_sensitive_directory")
            return metadata
        finally:
            self._close(handle)

    def _object_metadata(
        self, handle: int, directory: bool
    ) -> WindowsObjectObservation | PathObservation:
        if self._file_type(handle) != _FILE_TYPE_DISK:
            return PathObservation(PathOccupancy.UNSAFE, reason="non_disk_object")
        basic, standard, identity = _BasicInfo(), _StandardInfo(), _FileIdInfo()
        for code, buffer in (
            (_FILE_BASIC_INFO, basic), (_FILE_STANDARD_INFO, standard), (_FILE_ID_INFO, identity)
        ):
            if not self._file_info(handle, code, ctypes.byref(buffer), ctypes.sizeof(buffer)):
                return _unverifiable(f"handle_query_{code}_error_{ctypes.get_last_error()}")
        if basic.attributes & _UNSAFE_ATTRIBUTES or bool(standard.directory) != directory:
            return PathObservation(PathOccupancy.UNSAFE, reason="unsafe_handle_object")
        file_id = int.from_bytes(bytes(identity.identifier), "little")
        if not file_id or standard.delete_pending or not basic.written or not basic.changed:
            return _unverifiable("incomplete_handle_metadata")
        return WindowsObjectObservation(
            FileSystemIdentitySnapshot(identity.serial, file_id),
            standard.size,
            (basic.written - _FILETIME_UNIX_EPOCH) * 100,
            (basic.created - _FILETIME_UNIX_EPOCH) * 100 if basic.created else None,
            (basic.changed - _FILETIME_UNIX_EPOCH) * 100,
            basic.attributes,
        )

    def _find(self, path: str) -> tuple[int, str] | PathObservation:
        data = wintypes.WIN32_FIND_DATAW()
        handle = self._find_first(path, ctypes.byref(data))
        if handle == _INVALID_HANDLE:
            error = ctypes.get_last_error()
            if error == _ERROR_FILE_NOT_FOUND:
                return PathObservation(PathOccupancy.VACANT)
            return _unverifiable(f"path_query_error_{error}")
        self._find_close(handle)
        if path_key(data.cFileName) != path_key(ntpath.basename(path)):
            return PathObservation(PathOccupancy.UNSAFE, reason="path_alias")
        return data.dwFileAttributes, data.cFileName


def _looks_like_short_name(name: str) -> bool:
    return len(ntpath.splitext(name)[0]) <= 8 and bool(_POSSIBLE_SHORT_NAME.fullmatch(name))
