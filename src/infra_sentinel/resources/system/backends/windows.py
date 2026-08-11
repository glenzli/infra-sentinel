"""Windows host resource backend using stable Win32 aggregate APIs."""

from __future__ import annotations

import ctypes
from ctypes import Structure, byref, c_uint32, c_uint64
from pathlib import Path
import shutil

from infra_sentinel.resources.system.contract import CPU_UTILIZATION, DISK_CAPACITY, MEMORY_CAPACITY, HostReading


class _FileTime(Structure):
    _fields_ = [("low", c_uint32), ("high", c_uint32)]

    @property
    def value(self) -> int:
        return (int(self.high) << 32) | int(self.low)


class _MemoryStatus(Structure):
    _fields_ = [
        ("length", c_uint32), ("memory_load", c_uint32),
        ("total_physical", c_uint64), ("available_physical", c_uint64),
        ("total_page_file", c_uint64), ("available_page_file", c_uint64),
        ("total_virtual", c_uint64), ("available_virtual", c_uint64),
        ("available_extended_virtual", c_uint64),
    ]


class WindowsSystemBackend:
    platform = "windows"
    capabilities = (CPU_UTILIZATION, MEMORY_CAPACITY, DISK_CAPACITY)

    def __init__(self, volume_path: Path | None = None) -> None:
        self._volume_path = volume_path or Path.home()
        self._kernel = ctypes.windll.kernel32
        self._kernel.GetSystemTimes.argtypes = [ctypes.POINTER(_FileTime)] * 3
        self._kernel.GetSystemTimes.restype = ctypes.c_int
        self._kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
        self._kernel.GlobalMemoryStatusEx.restype = ctypes.c_int

    def _cpu_ticks(self) -> tuple[int, int, int, int]:
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not self._kernel.GetSystemTimes(byref(idle), byref(kernel), byref(user)):
            raise RuntimeError("Windows CPU counters are unavailable")
        return user.value, max(0, kernel.value - idle.value), idle.value, 0

    def _memory(self) -> _MemoryStatus:
        memory = _MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if not self._kernel.GlobalMemoryStatusEx(byref(memory)):
            raise RuntimeError("Windows memory status is unavailable")
        return memory

    def read(self, observed_at: str, epoch: float) -> HostReading:
        memory = self._memory()
        usage = shutil.disk_usage(self._volume_path)
        return HostReading(
            observed_at, float(epoch), self._cpu_ticks(), int(memory.total_physical),
            int(memory.available_physical), 0, "unavailable", False,
            max(0, int(memory.total_page_file - memory.available_page_file)), 0, 0,
            int(usage.total), int(usage.free), 0, 0, 0, 0, False, None,
        )
