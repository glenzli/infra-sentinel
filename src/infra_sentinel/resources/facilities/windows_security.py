"""Windows admission for Infra Discovery files and connected pipe peers.

Security is checked on the same handle used to read a manifest. Ancestor
handles exclude rename while the read is in progress; reparse points and
remote filesystems are rejected. This module is imported only on Windows.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes as W
from pathlib import Path


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_security = ctypes.WinDLL("advapi32", use_last_error=True)
_P = ctypes.c_void_p
_DWORD_P = ctypes.POINTER(W.DWORD)
_INVALID = _P(-1).value


def _declare(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


_create_file = _declare(_kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, _P, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
_close = _declare(_kernel, "CloseHandle", [W.HANDLE], W.BOOL)
_file_type = _declare(_kernel, "GetFileType", [W.HANDLE], W.DWORD)
_file_info = _declare(_kernel, "GetFileInformationByHandleEx", [W.HANDLE, ctypes.c_int, _P, W.DWORD], W.BOOL)
_drive_type = _declare(_kernel, "GetDriveTypeW", [W.LPCWSTR], W.UINT)
_read_file = _declare(_kernel, "ReadFile", [W.HANDLE, _P, W.DWORD, _DWORD_P, _P], W.BOOL)
_local_free = _declare(_kernel, "LocalFree", [_P], _P)
_current_process = _declare(_kernel, "GetCurrentProcess", [], W.HANDLE)
_open_process = _declare(_kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
_server_pid = _declare(_kernel, "GetNamedPipeServerProcessId", [W.HANDLE, _DWORD_P], W.BOOL)
_open_token = _declare(_security, "OpenProcessToken", [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)], W.BOOL)
_token_info = _declare(_security, "GetTokenInformation", [W.HANDLE, ctypes.c_int, _P, W.DWORD, _DWORD_P], W.BOOL)
_get_security = _declare(_security, "GetSecurityInfo", [W.HANDLE, ctypes.c_int, W.DWORD, ctypes.POINTER(_P), _P, ctypes.POINTER(_P), _P, ctypes.POINTER(_P)], W.DWORD)
_valid_sid = _declare(_security, "IsValidSid", [_P], W.BOOL)
_sid_length = _declare(_security, "GetLengthSid", [_P], W.DWORD)
_well_known_sid = _declare(_security, "IsWellKnownSid", [_P, ctypes.c_int], W.BOOL)
_valid_acl = _declare(_security, "IsValidAcl", [_P], W.BOOL)
_get_ace = _declare(_security, "GetAce", [_P, W.DWORD, ctypes.POINTER(_P)], W.BOOL)


class _AttributeTag(ctypes.Structure):
    _fields_ = [("attributes", W.DWORD), ("tag", W.DWORD)]


class _Acl(ctypes.Structure):
    _fields_ = [("revision", W.BYTE), ("reserved", W.BYTE), ("size", W.WORD),
                ("count", W.WORD), ("reserved2", W.WORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("kind", W.BYTE), ("flags", W.BYTE), ("size", W.WORD)]


def _sid_bytes(pointer: int) -> bytes:
    if not pointer or not _valid_sid(pointer):
        raise PermissionError("invalid Windows user SID")
    return ctypes.string_at(pointer, _sid_length(pointer))


def _process_user(process) -> bytes:
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
        return _sid_bytes(_P.from_buffer(buffer).value)
    finally:
        _close(token)


def current_user_sid() -> bytes:
    return _process_user(_current_process())


def _check_security(handle) -> None:
    owner, dacl, descriptor = _P(), _P(), _P()
    result = _get_security(handle, 1, 0x0001 | 0x0004, ctypes.byref(owner), None,
                           ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        user = current_user_sid()
        if _sid_bytes(owner.value) != user or not dacl.value or not _valid_acl(dacl):
            raise PermissionError("discovery object requires its current user as owner and a private DACL")
        acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
        for index in range(acl.count):
            pointer = _P()
            if not _get_ace(dacl, index, ctypes.byref(pointer)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(pointer, ctypes.POINTER(_AceHeader)).contents
            if header.flags & 0x08:  # INHERIT_ONLY_ACE does not apply to this object.
                continue
            if header.kind == 1:  # A deny ACE cannot widen access.
                continue
            if header.kind != 0 or header.size < 16:
                raise PermissionError("unsupported discovery allow ACE")
            sid = pointer.value + 8  # ACCESS_ALLOWED_ACE.SidStart
            value = _sid_bytes(sid)
            if len(value) + 8 > header.size:
                raise PermissionError("invalid discovery allow ACE")
            if value != user and not _well_known_sid(sid, 22) and not _well_known_sid(sid, 26):
                # LocalSystem and Builtin Administrators are the only exceptions.
                raise PermissionError("discovery object grants access to another user")
    finally:
        _local_free(descriptor)


def _local_path(path: Path) -> Path:
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":":
        raise PermissionError("discovery requires a local absolute drive path")
    if len(path.parts) < 2 or any(
        part in {".", ".."} or ":" in part or "\x00" in part or part.endswith((".", " "))
        for part in path.parts[1:]
    ):
        raise PermissionError("discovery path contains an alias or alternate stream")
    if _drive_type(path.anchor) not in {2, 3, 6}:  # removable, fixed, RAM disk
        raise PermissionError("discovery requires a local filesystem")
    return path


@contextmanager
def _open_object(path: Path, *, directory: bool, inspect_security: bool):
    access = (0x00020000 if inspect_security else 0) | (0x80 if directory else 0x80000000)
    # Keep every ancestor fixed until the checked object has been read.
    sharing = 1 | (2 if directory else 0)
    handle = _create_file(str(path), access, sharing, None, 3, 0x02200000, None)
    if handle == _INVALID:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _AttributeTag()
        if not _file_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (_file_type(handle) != 1 or attributes.attributes & 0x400
                or bool(attributes.attributes & 0x10) != directory):
            raise PermissionError("discovery rejects reparse points and unexpected object types")
        if inspect_security:
            _check_security(handle)
        yield handle
    finally:
        _close(handle)


@contextmanager
def _checked_object(path: Path, *, directory: bool):
    path = _local_path(path)
    with ExitStack() as stack:
        for ancestor in reversed(path.parents):
            stack.enter_context(_open_object(ancestor, directory=True, inspect_security=False))
        yield stack.enter_context(_open_object(path, directory=directory, inspect_security=True))


def validate_private_directory(path: Path) -> None:
    with _checked_object(path, directory=True):
        pass


def read_private_file(path: Path, maximum: int) -> bytes:
    with _checked_object(path, directory=False) as handle:
        buffer = ctypes.create_string_buffer(maximum + 1)
        count = W.DWORD()
        if not _read_file(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value > maximum:
            raise ValueError("registration exceeds 64 KiB")
        return buffer.raw[:count.value]


def validate_pipe_server(handle) -> None:
    """Bind admission to the connected server, before sending application bytes."""
    pid = W.DWORD()
    if not _server_pid(handle, ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    process = _open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _process_user(process) != current_user_sid():
            raise PermissionError("named-pipe server belongs to another user")
    finally:
        _close(process)
