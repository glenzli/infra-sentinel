"""Bounded native byte-stream transport for Infra local Windows named pipes.

This owner handles connection, peer admission and overlapped I/O lifetimes.
Provider adapters retain all JSON, framing, limits and completion semantics.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import math
import time

from infra_sentinel.resources.facilities.windows_security import validate_pipe_server


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_P = ctypes.c_void_p
_DWORD_P = ctypes.POINTER(W.DWORD)
_INVALID = _P(-1).value


class _Overlapped(ctypes.Structure):
    _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                ("offset", W.DWORD), ("offset_high", W.DWORD), ("event", W.HANDLE)]


def _declare(name, arguments, result):
    function = getattr(_kernel, name)
    function.argtypes = arguments
    function.restype = result
    return function


_create_file = _declare("CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, _P, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
_wait_pipe = _declare("WaitNamedPipeW", [W.LPCWSTR, W.DWORD], W.BOOL)
_pipe_info = _declare("GetNamedPipeInfo", [W.HANDLE, _DWORD_P, _DWORD_P, _DWORD_P, _DWORD_P], W.BOOL)
_create_event = _declare("CreateEventW", [_P, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE)
_close = _declare("CloseHandle", [W.HANDLE], W.BOOL)
_wait = _declare("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD)
_read = _declare("ReadFile", [W.HANDLE, _P, W.DWORD, _DWORD_P, ctypes.POINTER(_Overlapped)], W.BOOL)
_write = _declare("WriteFile", [W.HANDLE, _P, W.DWORD, _DWORD_P, ctypes.POINTER(_Overlapped)], W.BOOL)
_result = _declare("GetOverlappedResult", [W.HANDLE, ctypes.POINTER(_Overlapped), _DWORD_P, W.BOOL], W.BOOL)
_cancel = _declare("CancelIoEx", [W.HANDLE, ctypes.POINTER(_Overlapped)], W.BOOL)


class WindowsPipeConnection:
    def __init__(self, endpoint: str, timeout: float) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("named-pipe timeout must be positive and finite")
        self._deadline = time.monotonic() + timeout
        self._handle = None
        while True:
            self._remaining_ms()
            # Overlapped I/O and identification-only impersonation: the server
            # may inspect our SID but cannot act with our user permissions.
            handle = _create_file(endpoint, 0xC0000000, 0, None, 3, 0x40110000, None)
            if handle != _INVALID:
                self._handle = handle
                break
            error = ctypes.get_last_error()
            if error != 231:  # ERROR_PIPE_BUSY
                raise ctypes.WinError(error)
            if not _wait_pipe(endpoint, self._remaining_ms()):
                error = ctypes.get_last_error()
                if error == 121:  # ERROR_SEM_TIMEOUT
                    raise TimeoutError("named-pipe connection timed out")
                raise ctypes.WinError(error)
        try:
            flags = W.DWORD()
            if not _pipe_info(handle, ctypes.byref(flags), None, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            if flags.value & 4:  # PIPE_TYPE_MESSAGE
                raise PermissionError("Infra local binding requires a byte-stream pipe")
            validate_pipe_server(handle)
        except BaseException:
            self.close()
            raise

    def _remaining_ms(self) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("named-pipe request timed out")
        return min(0xFFFFFFFE, max(1, math.ceil(remaining * 1000)))

    def _transfer(self, buffer, size: int, *, writing: bool) -> int:
        if self._handle is None:
            raise OSError("named-pipe connection is closed")
        self._remaining_ms()
        event = _create_event(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        overlapped = _Overlapped(event=event)
        count = W.DWORD()
        pending = False
        try:
            operation = _write if writing else _read
            if operation(self._handle, buffer, size, ctypes.byref(count), ctypes.byref(overlapped)):
                return count.value
            error = ctypes.get_last_error()
            if error != 997:  # ERROR_IO_PENDING
                if not writing and error in {109, 232, 233}:
                    return 0  # Broken or disconnected pipe is stream EOF.
                raise ctypes.WinError(error)
            pending = True
            wait = _wait(event, self._remaining_ms())
            if wait == 258:
                raise TimeoutError("named-pipe request timed out")
            if wait != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            ok = _result(self._handle, ctypes.byref(overlapped), ctypes.byref(count), False)
            pending = False
            if not ok:
                error = ctypes.get_last_error()
                if not writing and error in {109, 232, 233}:
                    return 0
                raise ctypes.WinError(error)
            return count.value
        finally:
            if pending:
                # CancelIoEx requests cancellation; it does not complete it.
                # Keep buffer, event and OVERLAPPED alive until completion.
                _cancel(self._handle, ctypes.byref(overlapped))
                _result(self._handle, ctypes.byref(overlapped), ctypes.byref(count), True)
            _close(event)

    def sendall(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset:]
            buffer = ctypes.create_string_buffer(chunk)
            count = self._transfer(buffer, len(chunk), writing=True)
            if count == 0:
                raise BrokenPipeError("named pipe closed during request")
            offset += count

    def recv(self, maximum: int) -> bytes:
        buffer = ctypes.create_string_buffer(maximum)
        count = self._transfer(buffer, maximum, writing=False)
        return buffer.raw[:count]

    def close(self) -> None:
        if self._handle is not None:
            _close(self._handle)
            self._handle = None

    def __enter__(self) -> WindowsPipeConnection:
        return self

    def __exit__(self, *_) -> None:
        self.close()
