"""Handle-bound, non-replacing rename on the supported local Windows paths."""

from __future__ import annotations

import ctypes
import ntpath
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import replace
from functools import lru_cache
from typing import Iterator

from tidyfiles.destination_observation import (
    _FILE_CASE_SENSITIVE_INFO,
    _FILE_CASE_SENSITIVE_SEARCH,
    _FILE_READ_ATTRIBUTES,
    _FILETIME_UNIX_EPOCH,
    _INVALID_HANDLE,
    _OPEN_DIRECTORY_NOFOLLOW,
    _OPEN_EXISTING,
    PathObservation,
    WindowsObjectObservation,
    WindowsPathObserver,
    _BasicInfo,
)

_STATUS_NAME_NOT_FOUND = 0xC0000034
_DIRECTORY_ACCESS = _FILE_READ_ATTRIBUTES | 0x20  # FILE_TRAVERSE
_DIRECTORY_SHARE = 0x03  # READ | WRITE, deny delete while the chain is held
_SOURCE_ACCESS = _FILE_READ_ATTRIBUTES | 0x10000 | 0x100  # DELETE | FILE_WRITE_ATTRIBUTES
_SOURCE_SHARE = 0x01  # deny write and delete opens while validating and moving
_FILE_NAME_INFO = 2
_FILE_RENAME_INFORMATION = 10
_FILE_BASIC_INFORMATION = 4


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("maximum_length", ctypes.c_ushort),
        ("buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root", wintypes.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("descriptor", wintypes.LPVOID),
        ("quality_of_service", wintypes.LPVOID),
    ]


class _IoStatus(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.ULONG),
        ("root", wintypes.HANDLE),
        ("length", wintypes.ULONG),
        ("name", wintypes.WCHAR),
    ]


@lru_cache(maxsize=1)
def _native_calls():
    library = ctypes.WinDLL("ntdll")
    open_file = library.NtCreateFile
    open_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatus),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    open_file.restype = ctypes.c_long
    set_info = library.NtSetInformationFile
    set_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatus),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    set_info.restype = ctypes.c_long
    error_code = library.RtlNtStatusToDosError
    error_code.argtypes = [ctypes.c_long]
    error_code.restype = wintypes.ULONG
    return open_file, set_info, error_code


def _open_relative(
    parent: int,
    name: str,
    access: int,
    share: int,
    *,
    directory: bool = False,
    create: bool = False,
) -> tuple[int | None, int]:
    open_file, _, _ = _native_calls()
    contents = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le"))
    string = _UnicodeString(length, length + 2, ctypes.cast(contents, wintypes.LPWSTR))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent,
        ctypes.pointer(string),
        0x1040,
        None,
        None,  # OBJ_DONT_REPARSE | OBJ_CASE_INSENSITIVE
    )
    handle = wintypes.HANDLE()
    status = open_file(
        ctypes.byref(handle),
        access | 0x100000,
        ctypes.byref(attributes),
        ctypes.byref(_IoStatus()),
        None,
        0,
        share,
        2 if create else 1,  # FILE_CREATE | FILE_OPEN
        0x20 | (0x01 if directory else 0x40),
        None,
        0,
    )
    return handle.value if status == 0 else None, status


def _raise_status(status: int) -> None:
    _, _, error_code = _native_calls()
    raise ctypes.WinError(error_code(status))


def _handle_metadata(
    observer: WindowsPathObserver, handle: int, *, directory: bool = False
) -> WindowsObjectObservation:
    metadata = observer._object_metadata(handle, directory)
    if isinstance(metadata, PathObservation):
        raise OSError(metadata.reason or "Unverifiable open object")
    return metadata


def _handle_name(observer: WindowsPathObserver, handle: int) -> str:
    buffer = ctypes.create_string_buffer(1024)
    if not observer._file_info(handle, _FILE_NAME_INFO, buffer, ctypes.sizeof(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    length = wintypes.DWORD.from_buffer(buffer).value
    if length % 2 or length > ctypes.sizeof(buffer) - 4:
        raise OSError("Invalid open-object name")
    return buffer.raw[4 : 4 + length].decode("utf-16-le")


def _open_parent(
    observer: WindowsPathObserver, stack: ExitStack, path: str, expected: PathObservation
) -> int:
    drive, _ = ntpath.splitdrive(path)
    volume = observer._volume(drive)
    if isinstance(volume, PathObservation) or (
        volume.key != expected.volume_key or volume.guid != expected.volume_guid
    ):
        raise OSError(f"Volume changed: {path}")
    if expected.actual_parent is None or expected.parent_identity is None:
        raise OSError(f"Parent identity is unavailable: {path}")
    handle = observer._open(
        drive + "\\",
        _DIRECTORY_ACCESS,
        _DIRECTORY_SHARE,
        None,
        _OPEN_EXISTING,
        _OPEN_DIRECTORY_NOFOLLOW,
        None,
    )
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    stack.callback(observer._close, handle)
    parts = tuple(part for part in expected.actual_parent.split("\\") if part)
    parent_name = "\\"
    for index in range(len(parts) + 1):
        metadata = _handle_metadata(observer, handle, directory=True)
        if metadata.identity.volume_id != expected.parent_identity.volume_id:
            raise OSError(f"Parent volume changed: {path}")
        if volume.flags & _FILE_CASE_SENSITIVE_SEARCH:
            flags = wintypes.DWORD()
            if not observer._file_info(
                handle, _FILE_CASE_SENSITIVE_INFO, ctypes.byref(flags), ctypes.sizeof(flags)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if flags.value:
                raise OSError(f"Case-sensitive directory: {path}")
        if index == len(parts):
            if metadata.identity != expected.parent_identity:
                raise OSError(f"Parent identity changed: {path}")
            if parts and _handle_name(observer, handle) != parent_name:
                raise OSError(f"Parent name changed: {path}")
            return handle
        part = parts[index]
        parent_name = ntpath.join(parent_name, part)
        child, status = _open_relative(
            handle,
            part,
            _DIRECTORY_ACCESS,
            _DIRECTORY_SHARE,
            directory=True,
        )
        if child is None:
            _raise_status(status)
        handle = child
        stack.callback(observer._close, handle)
    raise AssertionError("Parent traversal did not reach the expected directory")


class BoundRename:
    """A source entry and a destination directory held across the journal checkpoint."""

    def __init__(
        self,
        observer: WindowsPathObserver,
        source: int,
        target: int,
        name: str,
        expected_source: PathObservation,
    ) -> None:
        self._observer = observer
        self._source = source
        self._target = target
        self._name = name
        self._expected = expected_source

    def check(self) -> None:
        _check_open_source(self._observer, self._source, self._expected)
        occupant, status = _open_relative(self._target, self._name, _FILE_READ_ATTRIBUTES, 0x07)
        if occupant is not None:
            self._observer._close(occupant)
            raise FileExistsError(f"Destination is occupied: {self._name}")
        if status & 0xFFFFFFFF != _STATUS_NAME_NOT_FOUND:
            _raise_status(status)

    def move(self) -> None:
        self.check()
        _, set_info, _ = _native_calls()
        name = self._name.encode("utf-16-le")
        size = max(ctypes.sizeof(_RenameInfo), _RenameInfo.name.offset + len(name) + 2)
        buffer = ctypes.create_string_buffer(size)
        info = _RenameInfo.from_buffer(buffer)
        info.flags = 0  # ReplaceIfExists is false.
        info.root = self._target
        info.length = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, name, len(name))
        status = set_info(
            self._source,
            ctypes.byref(_IoStatus()),
            buffer,
            size,
            _FILE_RENAME_INFORMATION,
        )
        if status != 0:
            _raise_status(status)
        expected = self._expected.object_metadata
        assert expected is not None
        actual = _handle_metadata(self._observer, self._source)
        if (
            replace(
                actual,
                change_time_ns=expected.change_time_ns,
                created_time_ns=expected.created_time_ns,
            )
            != expected
        ):
            raise OSError("The renamed object no longer matches its verified metadata")
        if actual.created_time_ns != expected.created_time_ns:
            if expected.created_time_ns is None:
                raise OSError("The original creation time is unavailable")
            self._restore_creation_time(expected.created_time_ns)

    def _restore_creation_time(self, creation_ns: int) -> None:
        # NTFS can inherit the creation time of a recently vacated destination name.
        _, set_info, _ = _native_calls()
        basic = _BasicInfo(created=creation_ns // 100 + _FILETIME_UNIX_EPOCH)
        status = set_info(
            self._source,
            ctypes.byref(_IoStatus()),
            ctypes.byref(basic),
            ctypes.sizeof(basic),
            _FILE_BASIC_INFORMATION,
        )
        if status != 0:
            _raise_status(status)


def _check_open_source(
    observer: WindowsPathObserver, handle: int, expected: PathObservation
) -> None:
    assert expected.actual_parent is not None and expected.actual_name is not None
    if _handle_metadata(observer, handle) != expected.object_metadata or _handle_name(
        observer, handle
    ) != ntpath.join(expected.actual_parent, expected.actual_name):
        raise OSError("The open source no longer matches the verified entry")


@contextmanager
def bind_rename(
    observer: WindowsPathObserver,
    source_path: str,
    source: PathObservation,
    destination: str,
    target: PathObservation,
) -> Iterator[BoundRename]:
    if (
        source.parent_identity is None
        or target.parent_identity is None
        or source.parent_identity.volume_id != target.parent_identity.volume_id
        or source.volume_guid != target.volume_guid
        or source.actual_name is None
    ):
        raise OSError("Rename requires verified same-volume directory entries")
    with ExitStack() as stack:
        source_parent = _open_parent(observer, stack, source_path, source)
        target_parent = (
            source_parent
            if source.parent_identity == target.parent_identity
            and source.actual_parent == target.actual_parent
            else _open_parent(observer, stack, destination, target)
        )
        handle, status = _open_relative(
            source_parent,
            source.actual_name,
            _SOURCE_ACCESS,
            _SOURCE_SHARE,
        )
        if handle is None:
            _raise_status(status)
        stack.callback(observer._close, handle)
        bound = BoundRename(observer, handle, target_parent, ntpath.basename(destination), source)
        bound.check()
        yield bound
