"""Native process probes and non-interactive child-process options.

Keep OS process behavior here: resource adapters must not emulate POSIX
signals on Windows or create console windows while the desktop samples.
"""

from __future__ import annotations

import os
import subprocess


def background_subprocess_options() -> dict[str, int]:
    """Preserve redirected standard I/O without opening a Windows console."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def process_is_running(pid: int) -> bool:
    """Probe liveness without ever sending a terminating Windows signal.

    Inaccessible processes are conservatively treated as alive. The caller
    records the desktop PID explicitly because a one-file PyInstaller Agent
    has an intermediate bootstrap parent.
    """
    if os.name == "nt":
        return _windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _windows_process_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # SYNCHRONIZE grants only the right to wait; no termination permission.
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER is returned for a PID that no longer exists.
        return ctypes.get_last_error() != 87
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: exited
    finally:
        kernel.CloseHandle(handle)
