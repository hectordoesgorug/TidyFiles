"""Windows named-mutex protection for a single running TidyFiles instance."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

DEFAULT_MUTEX_NAME = r"Local\TidyFiles.SingleInstance"
ERROR_ALREADY_EXISTS = 183


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when entering a guard owned by another application instance."""


class InstanceGuardError(OSError):
    """Raised when a Windows mutex operation fails unexpectedly."""

    def __init__(self, operation: str, error_code: int | None) -> None:
        self.operation = operation
        self.error_code = error_code
        message = f"{operation} failed"
        if error_code is not None:
            message += f" with Windows error {error_code}"
        super().__init__(error_code, message)


def _load_kernel32():
    if os.name != "nt":
        raise InstanceGuardError("Windows mutex support is unavailable", None)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _last_error() -> int:
    return ctypes.get_last_error()


class SingleInstanceGuard:
    """Own a named mutex until closed; acquire returns False if one already exists."""

    def __init__(self, mutex_name: str = DEFAULT_MUTEX_NAME) -> None:
        if not mutex_name:
            raise ValueError("mutex_name must not be empty")
        self.mutex_name = mutex_name
        self._api = None
        self._handle = None
        self._owns_mutex = False
        self._owner_thread_id: int | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None and self._owns_mutex

    def acquire(self) -> bool:
        """Acquire the mutex, returning False only when another instance owns it."""
        if self._handle is not None:
            if self._owns_mutex:
                return True
            raise InstanceGuardError("Close the previous mutex handle before reacquiring", None)

        api = _load_kernel32()
        handle = api.CreateMutexW(None, True, self.mutex_name)
        error_code = _last_error()
        if not handle:
            raise InstanceGuardError("CreateMutexW", error_code)

        if error_code == ERROR_ALREADY_EXISTS:
            if not api.CloseHandle(handle):
                raise InstanceGuardError("CloseHandle", _last_error())
            return False

        self._api = api
        self._handle = handle
        self._owns_mutex = True
        self._owner_thread_id = threading.get_ident()
        return True

    def close(self) -> None:
        """Release ownership and close the handle; safe to call repeatedly."""
        if self._handle is None:
            return
        if self._owns_mutex and self._owner_thread_id != threading.get_ident():
            raise InstanceGuardError("ReleaseMutex must run on the acquiring thread", None)

        release_error: int | None = None
        if self._owns_mutex:
            if self._api.ReleaseMutex(self._handle):
                self._owns_mutex = False
            else:
                release_error = _last_error()

        if not self._api.CloseHandle(self._handle):
            raise InstanceGuardError("CloseHandle", _last_error())

        self._api = None
        self._handle = None
        self._owner_thread_id = None
        if release_error is not None:
            raise InstanceGuardError("ReleaseMutex", release_error)

    def __enter__(self) -> SingleInstanceGuard:
        if not self.acquire():
            raise InstanceAlreadyRunningError(
                f"Another TidyFiles instance owns mutex {self.mutex_name!r}."
            )
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
