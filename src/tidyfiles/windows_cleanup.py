"""Identity checked, nonrecursive Windows deletion for cleanup services."""

from __future__ import annotations

import ctypes
import ntpath
from contextlib import ExitStack
from ctypes import wintypes
from functools import lru_cache

from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
    path_key,
)
from tidyfiles.windows_rename import (
    _DIRECTORY_ACCESS,
    _DIRECTORY_SHARE,
    _check_open_source,
    _handle_metadata,
    _handle_name,
    _IoStatus,
    _open_parent,
    _open_relative,
    _raise_status,
)

_DELETE = 0x00010000
_READ_ATTRIBUTES = 0x80
_FILE_DISPOSITION_INFO_EX = 21
_FILE_DIRECTORY_INFORMATION = 1
_STATUS_NO_MORE_FILES = 0x80000006


def _set_disposition(handle: int) -> None:
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    call = library.SetFileInformationByHandle
    call.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    call.restype = wintypes.BOOL
    flags = wintypes.DWORD(0x01)
    if not call(handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)):
        raise ctypes.WinError(ctypes.get_last_error())


@lru_cache(maxsize=1)
def _query_directory_call():
    query = ctypes.WinDLL("ntdll").NtQueryDirectoryFile
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(_IoStatus),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.BOOLEAN,
        wintypes.LPVOID,
        wintypes.BOOLEAN,
    ]
    query.restype = ctypes.c_long
    return query


def directory_entries(
    observer: WindowsPathObserver, path: str, expected: PathObservation
) -> tuple[tuple[str, int], ...]:
    """Enumerate a verified directory through a no-reparse handle."""
    if (
        expected.occupancy is not PathOccupancy.DIRECTORY
        or expected.actual_parent is None
        or expected.actual_name is None
        or expected.object_metadata is None
    ):
        raise OSError("Directory enumeration requires its observed identity")
    directory_tail = ntpath.join(expected.actual_parent, expected.actual_name)
    if path_key(ntpath.splitdrive(ntpath.normpath(path))[1]) != path_key(directory_tail):
        raise OSError("The directory path differs from its observed location")
    parent = PathObservation(
        PathOccupancy.VACANT,
        volume_key=expected.volume_key,
        actual_parent=directory_tail,
        volume_guid=expected.volume_guid,
        parent_identity=expected.object_metadata.identity,
    )
    with ExitStack() as stack:
        handle = _open_parent(
            observer,
            stack,
            ntpath.join(path, "__tidyfiles_enumeration_probe__"),
            parent,
            directory_access=_DIRECTORY_ACCESS | 0x01,
        )
        if _handle_metadata(observer, handle, directory=True).identity != (
            expected.object_metadata.identity
        ):
            raise OSError("The directory changed before enumeration")
        entries = []
        restart = True
        while True:
            status_block = _IoStatus()
            buffer = ctypes.create_string_buffer(65536)
            status = _query_directory_call()(
                handle,
                None,
                None,
                None,
                ctypes.byref(status_block),
                buffer,
                ctypes.sizeof(buffer),
                _FILE_DIRECTORY_INFORMATION,
                False,
                None,
                restart,
            )
            restart = False
            status_code = status & 0xFFFFFFFF
            if status_code == _STATUS_NO_MORE_FILES:
                break
            if status_code:
                _raise_status(status)
            length = status_block.information
            if not length:
                break
            offset = 0
            while offset < length:
                if offset + 64 > length:
                    raise OSError("The directory returned malformed entry data")
                next_offset = wintypes.DWORD.from_buffer(buffer, offset).value
                attributes = wintypes.DWORD.from_buffer(buffer, offset + 56).value
                name_length = wintypes.DWORD.from_buffer(buffer, offset + 60).value
                if name_length % 2 or offset + 64 + name_length > length:
                    raise OSError("The directory returned malformed entry data")
                name = ctypes.string_at(
                    ctypes.addressof(buffer) + offset + 64, name_length
                ).decode("utf-16-le", errors="surrogatepass")
                if name not in {".", ".."}:
                    entries.append((name, attributes))
                if not next_offset:
                    break
                if next_offset < 64 + name_length or offset + next_offset > length:
                    raise OSError("The directory returned malformed entry data")
                offset += next_offset
        return tuple(entries)


def remove_file(observer: WindowsPathObserver, path: str, expected: PathObservation) -> None:
    if expected.occupancy is not PathOccupancy.FILE or expected.actual_name is None:
        raise OSError("Cleanup requires the observed regular file entry")
    with ExitStack() as stack:
        parent = _open_parent(observer, stack, path, expected)
        handle, status = _open_relative(
            parent, expected.actual_name, _DELETE | _READ_ATTRIBUTES, 0x01
        )
        if handle is None:
            _raise_status(status)
        stack.callback(observer._close, handle)
        _check_open_source(observer, handle, expected)
        _set_disposition(handle)


def remove_empty_directory(
    observer: WindowsPathObserver, path: str, expected: PathObservation
) -> None:
    if expected.occupancy is not PathOccupancy.DIRECTORY or expected.actual_name is None:
        raise OSError("Cleanup requires the observed directory entry")
    with ExitStack() as stack:
        parent = _open_parent(observer, stack, path, expected)
        handle, status = _open_relative(
            parent,
            expected.actual_name,
            _DELETE | _DIRECTORY_ACCESS,
            _DIRECTORY_SHARE,
            directory=True,
        )
        if handle is None:
            _raise_status(status)
        stack.callback(observer._close, handle)
        metadata = _handle_metadata(observer, handle, directory=True)
        if metadata != expected.object_metadata or _handle_name(observer, handle) != ntpath.join(
            expected.actual_parent or "", expected.actual_name
        ):
            raise OSError("The directory changed after cleanup inspection")
        _set_disposition(handle)
