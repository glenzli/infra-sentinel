"""Native, private Windows facility fixtures; never shipped with the Agent."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import threading
import uuid


def make_private(path: Path, *, broad: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    from infra_sentinel.resources.facilities.windows_security import current_user_sid

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_sid = security.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    convert_sid.restype = W.BOOL
    convert_sd = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sd.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert_sd.restype = W.BOOL
    set_security = security.SetFileSecurityW
    set_security.argtypes = [W.LPCWSTR, W.DWORD, ctypes.c_void_p]
    set_security.restype = W.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    sid = ctypes.create_string_buffer(current_user_sid())
    text = W.LPWSTR()
    if not convert_sid(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.c_void_p()
    try:
        extra = "(A;;GR;;;WD)" if broad else ""
        sddl = f"O:{text.value}D:P(A;OICI;FA;;;{text.value})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA){extra}"
        if not convert_sd(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not set_security(str(path), 0x80000004, descriptor):  # protected DACL
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(text)
        if descriptor:
            kernel.LocalFree(descriptor)


class PipeServer:
    """One real byte-pipe connection with a bounded server lifetime."""

    def __init__(self, reply: bytes, *, hold: float = 0, chunks: int = 0, delay: float = 0,
                 message_mode: bool = False, before_reply=None):
        from infra_sentinel.resources.facilities import windows_pipe as pipe

        self.endpoint = rf"\\.\pipe\infra-protocol\test-{uuid.uuid4().hex}"
        self.reply, self.hold, self.chunks, self.delay = reply, hold, chunks, delay
        self.before_reply = before_reply
        self.request = b""
        self.error = None
        self.connected = threading.Event()
        self.admitted = threading.Event()
        self.release = threading.Event()
        self._pipe = pipe
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = self._kernel.CreateNamedPipeW
        create.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, ctypes.c_void_p]
        create.restype = W.HANDLE
        # The process default security descriptor is sufficient for this test
        # fixture; connected client SID admission is checked before the request.
        self._handle = create(self.endpoint, 3 | 0x40000000 | 0x00080000,
                              (4 if message_mode else 0) | 8, 1, 65536, 65536, 0, None)
        if self._handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        pipe = self._pipe
        event = pipe._create_event(None, True, False, None)
        overlapped = pipe._Overlapped(event=event)
        count = W.DWORD()
        pending = False
        connection = None
        try:
            connect = self._kernel.ConnectNamedPipe
            connect.argtypes = [W.HANDLE, ctypes.POINTER(pipe._Overlapped)]
            connect.restype = W.BOOL
            if not connect(self._handle, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                if error == 997:
                    pending = True
                    if pipe._wait(event, 5000) != 0:
                        raise TimeoutError("test pipe never connected")
                    ok = pipe._result(self._handle, ctypes.byref(overlapped), ctypes.byref(count), False)
                    pending = False
                    if not ok:
                        raise ctypes.WinError(ctypes.get_last_error())
                elif error != 535:  # ERROR_PIPE_CONNECTED
                    raise ctypes.WinError(error)
            self.connected.set()
            from infra_sentinel.resources.facilities import windows_security as security

            client_pid = self._kernel.GetNamedPipeClientProcessId
            client_pid.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
            client_pid.restype = W.BOOL
            pid = W.DWORD()
            if not client_pid(self._handle, ctypes.byref(pid)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = security._open_process(0x1000, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if security._process_user(process) != security.current_user_sid():
                    raise PermissionError("test provider rejected another user")
            finally:
                security._close(process)
            self.admitted.set()
            connection = pipe.WindowsPipeConnection.__new__(pipe.WindowsPipeConnection)
            import time
            connection._deadline = time.monotonic() + 6
            connection._handle = self._handle
            while b"\n" not in self.request:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                self.request += chunk
            if self.before_reply:
                self.before_reply()
            step = self.chunks or max(1, len(self.reply))
            for offset in range(0, len(self.reply), step):
                connection.sendall(self.reply[offset:offset + step])
                if self.delay and self.release.wait(self.delay):
                    break
            if self.hold:
                self.release.wait(self.hold)
        except (BrokenPipeError, ConnectionError):
            pass  # Expected when the client rejects a frame or reaches its deadline.
        except OSError as error:
            if getattr(error, "winerror", None) not in {109, 232, 233}:
                self.error = error
        except BaseException as error:
            self.error = error
        finally:
            if pending:
                pipe._cancel(self._handle, ctypes.byref(overlapped))
                pipe._result(self._handle, ctypes.byref(overlapped), ctypes.byref(count), True)
            pipe._close(event)
            if connection is not None:
                connection.close()
            else:
                pipe._close(self._handle)

    def close(self):
        self.release.set()
        self._thread.join(timeout=7)
        if self._thread.is_alive():
            raise AssertionError("native test server did not terminate")
        if self.error:
            raise self.error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
