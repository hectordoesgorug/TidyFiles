"""Handle-bound Windows directory creation, copy verification, and source removal."""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import stat
import struct
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from tidyfiles.destination_observation import (
    _FILETIME_UNIX_EPOCH,
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
    WindowsPathObserver,
    _BasicInfo,
)
from tidyfiles.filesystem_paths import is_protected_system_path
from tidyfiles.windows_rename import (
    _DIRECTORY_ACCESS,
    _DIRECTORY_SHARE,
    _SOURCE_SHARE,
    _STATUS_NAME_NOT_FOUND,
    _check_open_source,
    _handle_metadata,
    _IoStatus,
    _open_parent,
    _open_relative,
    _raise_status,
)

_COPY_BUFFER = 1024 * 1024
_COPY_ATTRIBUTES = (
    stat.FILE_ATTRIBUTE_ARCHIVE
    | stat.FILE_ATTRIBUTE_HIDDEN
    | stat.FILE_ATTRIBUTE_SYSTEM
    | stat.FILE_ATTRIBUTE_NORMAL
    | stat.FILE_ATTRIBUTE_NOT_CONTENT_INDEXED
)
_COPY_SOURCE_ACCESS = 0x30089  # DELETE, READ_CONTROL, READ_ATTRIBUTES, READ_EA, READ_DATA
_COPY_TARGET_ACCESS = (
    0xE0183  # READ_CONTROL, WRITE_DAC, WRITE_OWNER, read/write data and attributes
)
_FILE_STREAM_INFO = 7
_FILE_EA_INFORMATION = 7
_FILE_DISPOSITION_INFO_EX = 21
_SE_FILE_OBJECT = 1
_OWNER_GROUP_DACL_AND_LABEL = 0x17
_PROTECTED_DACL = 0x80000004


def _directory(parent: int, name: str, *, create: bool) -> tuple[int | None, int]:
    return _open_relative(
        parent, name, _DIRECTORY_ACCESS, _DIRECTORY_SHARE, directory=True, create=create
    )


@contextmanager
def create_batch(root: Path, name: str) -> Iterator[Path]:
    """Create the batch relative to verified directory handles and pin its ancestors."""
    if is_protected_system_path(str(root)):
        raise OSError("The managed Trash location is protected")
    observer = WindowsPathObserver(capture_identity=True, allow_managed_trash=True)
    probe = str(root.parent / name)
    expected = observer(probe)
    if expected.occupancy is not PathOccupancy.VACANT or expected.read_only_volume:
        raise OSError(f"The application directory is unsuitable for Trash: {expected.reason}")
    with ExitStack() as stack:
        app = _open_parent(observer, stack, probe, expected)
        trash, status = _directory(app, root.name, create=False)
        if status & 0xFFFFFFFF == _STATUS_NAME_NOT_FOUND:
            trash, status = _directory(app, root.name, create=True)
        if trash is None:
            _raise_status(status)
        stack.callback(observer._close, trash)
        _handle_metadata(observer, trash, directory=True)
        batch, status = _directory(trash, name, create=True)
        if batch is None:
            _raise_status(status)
        stack.callback(observer._close, batch)
        _handle_metadata(observer, batch, directory=True)
        yield root / name


@lru_cache(maxsize=1)
def _copy_calls():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    free = kernel.LocalFree
    free.argtypes = [wintypes.HLOCAL]
    free.restype = wintypes.HLOCAL
    query = ctypes.WinDLL("ntdll").NtQueryInformationFile
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatus),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    query.restype = ctypes.c_long
    security = ctypes.WinDLL("advapi32")
    get_security = security.GetSecurityInfo
    pointer = ctypes.POINTER(ctypes.c_void_p)
    get_security.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD] + [pointer] * 5
    get_security.restype = wintypes.DWORD
    set_security = security.SetSecurityInfo
    set_security.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        *([ctypes.c_void_p] * 4),
    ]
    set_security.restype = wintypes.DWORD
    sid_length = security.GetLengthSid
    sid_length.argtypes = [ctypes.c_void_p]
    sid_length.restype = wintypes.DWORD
    return set_info, query, get_security, set_security, sid_length, free


@contextmanager
def _security(handle: int):
    _, _, get, _, sid_length, free = _copy_calls()
    owner, group, dacl, label, descriptor = (ctypes.c_void_p() for _ in range(5))
    status = get(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_GROUP_DACL_AND_LABEL,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(label),
        ctypes.byref(descriptor),
    )
    if status:
        raise ctypes.WinError(status)
    try:
        if not owner or not group:
            raise OSError("File owner or group is unverifiable")
        acl = _access_rules(dacl.value) if dacl else None
        values = (
            ctypes.string_at(owner, sid_length(owner)),
            ctypes.string_at(group, sid_length(group)),
            acl,
            _access_rules(label.value) if label else (),
        )
        yield (owner, group, dacl), values
    finally:
        free(descriptor)


def _access_rules(address: int) -> tuple[bytes, ...]:
    size = ctypes.c_ushort.from_address(address + 2).value
    data = ctypes.string_at(address, size)
    count = struct.unpack_from("<H", data, 4)[0]
    offset = 8
    rules = []
    for _ in range(count):
        kind, flags, length = struct.unpack_from("<BBH", data, offset)
        # Pinning a copied DACL can clear the inherited marker without changing the rule.
        rules.append(bytes((kind, flags & ~0x10)) + data[offset + 2 : offset + length])
        offset += length
    return tuple(rules)


def _copy_security(source: int, target: int) -> None:
    with _security(source) as ((owner, group, dacl), expected), _security(target) as (_, current):
        if expected[3] != current[3]:
            raise OSError("The Trash copy cannot preserve the original integrity label")
        flags = (1 if expected[0] != current[0] else 0) | (2 if expected[1] != current[1] else 0)
        status = _copy_calls()[3](
            target, _SE_FILE_OBJECT, flags | _PROTECTED_DACL, owner, group, dacl, None
        )
        if status:
            raise ctypes.WinError(status)
    _check_security(source, target)


def _check_security(source: int, target: int) -> None:
    with _security(source) as (_, original), _security(target) as (_, copied):
        if original != copied:
            raise OSError("The Trash copy could not preserve the owner, group and access rules")


def _check_ordinary_data(
    observer: WindowsPathObserver, handle: int, expected: WindowsObjectObservation
) -> None:
    if expected.created_time_ns is None or expected.attributes & ~_COPY_ATTRIBUTES:
        raise OSError("File attributes are unsupported for a verified cross-volume Trash copy")
    data = ctypes.create_string_buffer(4096)
    if observer._file_info(handle, _FILE_STREAM_INFO, data, ctypes.sizeof(data)):
        offset, length, size = struct.unpack_from("<IIq", data)
        if (
            offset
            or data.raw[24 : 24 + length] != "::$DATA".encode("utf-16-le")
            or size != expected.size_bytes
        ):
            raise OSError("Alternate streams or an unverifiable data stream block the Trash copy")
    elif ctypes.get_last_error() != 38 or expected.size_bytes:
        raise ctypes.WinError(ctypes.get_last_error())
    ea_size = wintypes.ULONG()
    status = _copy_calls()[1](
        handle,
        ctypes.byref(_IoStatus()),
        ctypes.byref(ea_size),
        ctypes.sizeof(ea_size),
        _FILE_EA_INFORMATION,
    )
    if status:
        _raise_status(status)
    if ea_size.value:
        raise OSError("Extended attributes block the cross-volume Trash copy")


def _set_information(handle: int, kind: int, data) -> None:
    if not _copy_calls()[0](handle, kind, ctypes.byref(data), ctypes.sizeof(data)):
        raise ctypes.WinError(ctypes.get_last_error())


def _write_data(source: BinaryIO, target: BinaryIO) -> None:
    while chunk := source.read(_COPY_BUFFER):
        if target.write(chunk) != len(chunk):
            raise OSError("The Trash copy write was incomplete")
    target.flush()


def _compare_data(source: BinaryIO, target: BinaryIO, expected_size: int) -> str:
    source.seek(0)
    target.seek(0)
    digest = hashlib.sha256()
    count = 0
    while True:
        original = source.read(_COPY_BUFFER)
        if original != target.read(_COPY_BUFFER):
            raise OSError("Trash copy content differs from the original")
        if not original:
            break
        digest.update(original)
        count += len(original)
    if count != expected_size:
        raise OSError("Trash copy size differs from the authorized size")
    return digest.hexdigest()


def _remove_original(handle: int) -> None:
    # The POSIX disposition removes the selected link when this open handle closes.
    _set_information(handle, _FILE_DISPOSITION_INFO_EX, wintypes.DWORD(0x03))


def _stream(observer: WindowsPathObserver, handle: int, writable: bool) -> BinaryIO:
    import msvcrt

    try:
        flags = os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY)
        fd = msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        observer._close(handle)
        raise
    try:
        return os.fdopen(fd, "r+b" if writable else "rb")
    except BaseException:
        os.close(fd)
        raise


def copy_across_volumes(
    observer: WindowsPathObserver,
    source_path: str,
    source: PathObservation,
    target_path: str,
    target: PathObservation,
    before_removal: Callable[[PathObservation, str], None],
    *,
    surviving_link: tuple[str, PathObservation] | None = None,
) -> WindowsObjectObservation | None:
    """Create, flush and verify a copy, then unlink only the bound source upon authorization."""
    metadata = source.object_metadata
    if (
        metadata is None
        or source.actual_name is None
        or not source.volume_guid
        or not target.volume_guid
        or source.volume_guid == target.volume_guid
        or target.occupancy is not PathOccupancy.VACANT
    ):
        raise OSError("Cross-volume copy requires verified source and destination entries")
    with ExitStack() as stack:
        parent = _open_parent(observer, stack, source_path, source)
        folder = _open_parent(observer, stack, target_path, target)
        source_handle, status = _open_relative(
            parent, source.actual_name, _COPY_SOURCE_ACCESS, _SOURCE_SHARE
        )
        if source_handle is None:
            _raise_status(status)
        original = stack.enter_context(_stream(observer, source_handle, False))
        _check_open_source(observer, source_handle, source)
        _check_ordinary_data(observer, source_handle, metadata)
        link = None
        if surviving_link is not None:
            path, expected_link = surviving_link
            assert expected_link.actual_name is not None
            link_parent = _open_parent(observer, stack, path, expected_link)
            link, status = _open_relative(link_parent, expected_link.actual_name, 0x80, 0x07)
            if link is None:
                _raise_status(status)
            stack.callback(observer._close, link)
            _check_open_source(observer, link, expected_link)
        name = ntpath.basename(target_path)
        target_handle, status = _open_relative(
            folder, name, _COPY_TARGET_ACCESS, _SOURCE_SHARE, create=True
        )
        if target_handle is None:
            _raise_status(status)
        copied = stack.enter_context(_stream(observer, target_handle, True))
        _copy_security(source_handle, target_handle)
        _write_data(original, copied)
        assert metadata.created_time_ns is not None
        _set_information(
            target_handle,
            0,
            _BasicInfo(
                created=metadata.created_time_ns // 100 + _FILETIME_UNIX_EPOCH,
                written=metadata.modified_time_ns // 100 + _FILETIME_UNIX_EPOCH,
                attributes=metadata.attributes,
            ),
        )
        os.fsync(copied.fileno())
        digest = _compare_data(original, copied, metadata.size_bytes)
        installed = _handle_metadata(observer, target_handle)
        _check_ordinary_data(observer, target_handle, installed)
        expected = replace(
            target, occupancy=PathOccupancy.FILE, actual_name=name, object_metadata=installed
        )

        def recheck() -> None:
            fresh = WindowsPathObserver(capture_identity=True, allow_managed_trash=True)
            if fresh(source_path) != source or fresh(target_path) != expected:
                raise OSError("Source or Trash copy changed before source removal")
            _check_open_source(observer, source_handle, source)
            _check_ordinary_data(observer, source_handle, metadata)
            _check_security(source_handle, target_handle)

        recheck()
        before_removal(expected, digest)
        recheck()
        _remove_original(source_handle)
        original.close()
        return _handle_metadata(observer, link) if link is not None else None
