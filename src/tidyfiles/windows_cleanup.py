"""Identity checked, nonrecursive Windows deletion for cleanup services."""

from __future__ import annotations

import ctypes
import ntpath
from contextlib import ExitStack
from ctypes import wintypes

from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
)
from tidyfiles.windows_rename import (
    _DIRECTORY_ACCESS,
    _DIRECTORY_SHARE,
    _check_open_source,
    _handle_metadata,
    _handle_name,
    _open_parent,
    _open_relative,
    _raise_status,
)

_DELETE = 0x00010000
_READ_ATTRIBUTES = 0x80
_FILE_DISPOSITION_INFO_EX = 21


def _set_disposition(handle: int) -> None:
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    call = library.SetFileInformationByHandle
    call.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    call.restype = wintypes.BOOL
    flags = wintypes.DWORD(0x01)
    if not call(handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)):
        raise ctypes.WinError(ctypes.get_last_error())


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
