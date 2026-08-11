"""macOS per-process disk I/O counters using the public libproc API."""

from __future__ import annotations

import ctypes
from ctypes import Structure, byref, c_int, c_uint8, c_uint64, c_void_p, create_string_buffer
from pathlib import Path
import re

from infra_sentinel.resources.system.process_io import (
    ProcessIoBatch,
    ProcessIoCounter,
)


class _RUsageInfoV2(Structure):
    _fields_ = [
        ("ri_uuid", c_uint8 * 16),
        ("ri_user_time", c_uint64),
        ("ri_system_time", c_uint64),
        ("ri_pkg_idle_wkups", c_uint64),
        ("ri_interrupt_wkups", c_uint64),
        ("ri_pageins", c_uint64),
        ("ri_wired_size", c_uint64),
        ("ri_resident_size", c_uint64),
        ("ri_phys_footprint", c_uint64),
        ("ri_proc_start_abstime", c_uint64),
        ("ri_proc_exit_abstime", c_uint64),
        ("ri_child_user_time", c_uint64),
        ("ri_child_system_time", c_uint64),
        ("ri_child_pkg_idle_wkups", c_uint64),
        ("ri_child_interrupt_wkups", c_uint64),
        ("ri_child_pageins", c_uint64),
        ("ri_child_elapsed_abstime", c_uint64),
        ("ri_diskio_bytesread", c_uint64),
        ("ri_diskio_byteswritten", c_uint64),
    ]


def _token(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:64] or "unknown"


def app_identity(path: str, process_name: str) -> tuple[str, str]:
    """Map helpers inside one .app bundle to a shared privacy-safe identity."""
    for component in Path(path).parts:
        if component.casefold().endswith(".app") and len(component) > 4:
            label = component[:-4]
            return f"app:{_token(label)}", label[:96]
    label = (process_name or Path(path).name or "Unknown process")[:96]
    return f"process:{_token(label)}", label


class MacOSProcessIoBackend:
    platform = "macos"
    _RUSAGE_INFO_V2 = 2
    _PATH_BUFFER_SIZE = 4_096
    _NAME_BUFFER_SIZE = 1_024

    def __init__(self) -> None:
        self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._identities: dict[str, tuple[str, str]] = {}
        self._libproc.proc_listallpids.argtypes = [c_void_p, c_int]
        self._libproc.proc_listallpids.restype = c_int
        self._libproc.proc_pid_rusage.argtypes = [c_int, c_int, c_void_p]
        self._libproc.proc_pid_rusage.restype = c_int
        self._libproc.proc_pidpath.argtypes = [c_int, c_void_p, ctypes.c_uint32]
        self._libproc.proc_pidpath.restype = c_int
        self._libproc.proc_name.argtypes = [c_int, c_void_p, ctypes.c_uint32]
        self._libproc.proc_name.restype = c_int

    def _pids(self) -> tuple[int, ...]:
        count = self._libproc.proc_listallpids(None, 0)
        if count <= 0:
            raise RuntimeError("process enumeration is unavailable")
        capacity = count + 64
        values = (c_int * capacity)()
        observed = self._libproc.proc_listallpids(values, ctypes.sizeof(values))
        if observed < 0:
            raise RuntimeError("process enumeration failed")
        return tuple(int(pid) for pid in values[:observed] if pid > 0)

    def _text(self, pid: int, function: object, capacity: int) -> str:
        buffer = create_string_buffer(capacity)
        length = function(pid, buffer, capacity)  # type: ignore[operator]
        if length <= 0:
            return ""
        return buffer.value.decode("utf-8", errors="replace")

    def read(self) -> ProcessIoBatch:
        pids = self._pids()
        counters: list[ProcessIoCounter] = []
        identities: dict[str, tuple[str, str]] = {}
        skipped = 0
        for pid in pids:
            usage = _RUsageInfoV2()
            if self._libproc.proc_pid_rusage(pid, self._RUSAGE_INFO_V2, byref(usage)) != 0:
                skipped += 1
                continue
            identity = f"{pid}:{int(usage.ri_proc_start_abstime)}"
            app = self._identities.get(identity)
            if app is None:
                path = self._text(pid, self._libproc.proc_pidpath, self._PATH_BUFFER_SIZE)
                name = self._text(pid, self._libproc.proc_name, self._NAME_BUFFER_SIZE)
                app = app_identity(path, name)
            identities[identity] = app
            app_id, label = app
            counters.append(ProcessIoCounter(
                identity=identity,
                app_id=app_id,
                app_label=label,
                read_bytes=max(0, int(usage.ri_diskio_bytesread)),
                write_bytes=max(0, int(usage.ri_diskio_byteswritten)),
            ))
        self._identities = identities
        return ProcessIoBatch(tuple(counters), len(counters), skipped)
