"""Windows user identity and creation of files private from their first byte."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import msvcrt
import os
from pathlib import Path

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_security = ctypes.WinDLL("advapi32", use_last_error=True)
_P = ctypes.c_void_p


def _declare(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


_close = _declare(_kernel, "CloseHandle", [W.HANDLE], W.BOOL)
_current_process = _declare(_kernel, "GetCurrentProcess", [], W.HANDLE)
_local_free = _declare(_kernel, "LocalFree", [_P], _P)
_open_token = _declare(_security, "OpenProcessToken", [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)], W.BOOL)
_token_info = _declare(_security, "GetTokenInformation", [W.HANDLE, ctypes.c_int, _P, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL)
_valid_sid = _declare(_security, "IsValidSid", [_P], W.BOOL)
_sid_length = _declare(_security, "GetLengthSid", [_P], W.DWORD)
_sid_text = _declare(_security, "ConvertSidToStringSidW", [_P, ctypes.POINTER(W.LPWSTR)], W.BOOL)
_descriptor = _declare(_security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [W.LPCWSTR, W.DWORD, ctypes.POINTER(_P), _P], W.BOOL)
_create_file = _declare(_kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, _P, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", _P), ("inherit", W.BOOL)]


def sid_bytes(pointer: int) -> bytes:
    if not pointer or not _valid_sid(pointer):
        raise PermissionError("invalid Windows user SID")
    return ctypes.string_at(pointer, _sid_length(pointer))


def process_user_sid(process) -> bytes:
    token = W.HANDLE()
    if not _open_token(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = W.DWORD()
        _token_info(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not ctypes.sizeof(_P) <= size.value <= 65536:
            raise PermissionError("invalid Windows token size")
        buffer = ctypes.create_string_buffer(size.value)
        if not _token_info(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return sid_bytes(_P.from_buffer(buffer).value)
    finally:
        _close(token)


def current_user_sid() -> bytes:
    return process_user_sid(_current_process())


def open_private_file(path: Path):
    """Create a new, non-inheriting file; never repair an existing object's ACL."""
    user = ctypes.create_string_buffer(current_user_sid())
    text, descriptor = W.LPWSTR(), _P()
    if not _sid_text(user, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sddl = f"O:{text.value}D:P(A;;FA;;;{text.value})(A;;FA;;;SY)(A;;FA;;;BA)"
        if not _descriptor(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        handle = _create_file(str(path), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == _P(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor_fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            _close(handle)
            raise
        try:
            return os.fdopen(descriptor_fd, "wb")
        except BaseException:
            os.close(descriptor_fd)
            raise
    finally:
        if descriptor.value:
            _local_free(descriptor)
        _local_free(ctypes.cast(text, _P))
