"""macOS host resource backend using public Mach, sysctl, and IOKit APIs."""

from __future__ import annotations

import ctypes
from ctypes import (
    POINTER, Structure, byref, c_bool, c_char_p, c_int32, c_int64, c_long,
    c_size_t, c_uint32, c_uint64, c_void_p, cast, create_string_buffer, sizeof,
)
import os
from pathlib import Path
import shutil
from typing import Any

from infra_sentinel.resources.system.disk_health import DiskHealthEvidence, DiskHealthMonitor
from infra_sentinel.resources.system.contract import (
    CPU_UTILIZATION, DISK_CAPACITY, DISK_HEALTH, DISK_THROUGHPUT,
    MEMORY_CAPACITY, MEMORY_COMPRESSION, MEMORY_PRESSURE, MEMORY_SWAP,
    THERMAL_PRESSURE, HostReading,
)


MEMORY_PRESSURE_NORMAL = 1
MEMORY_PRESSURE_WARNING = 2
MEMORY_PRESSURE_CRITICAL = 4
THERMAL_STATES = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}


class _HostCpuLoadInfo(Structure):
    _fields_ = [("cpu_ticks", c_uint32 * 4)]


class _VmStatistics64(Structure):
    _fields_ = [
        ("free_count", c_uint32), ("active_count", c_uint32),
        ("inactive_count", c_uint32), ("wire_count", c_uint32),
        ("zero_fill_count", c_uint64), ("reactivations", c_uint64),
        ("pageins", c_uint64), ("pageouts", c_uint64), ("faults", c_uint64),
        ("cow_faults", c_uint64), ("lookups", c_uint64), ("hits", c_uint64),
        ("purges", c_uint64), ("purgeable_count", c_uint32),
        ("speculative_count", c_uint32), ("decompressions", c_uint64),
        ("compressions", c_uint64), ("swapins", c_uint64),
        ("swapouts", c_uint64), ("compressor_page_count", c_uint32),
        ("throttled_count", c_uint32), ("external_page_count", c_uint32),
        ("internal_page_count", c_uint32),
        ("total_uncompressed_pages_in_compressor", c_uint64),
        ("swapped_count", c_uint64), ("total_tag_storage_pages", c_uint64),
        ("nontag_pageable_tag_storage_pages", c_uint64),
        ("nontag_wired_tag_storage_pages", c_uint64),
        ("free_tag_storage_pages", c_uint64),
        ("tag_storing_tag_storage_pages", c_uint64),
        ("total_tagged_pages", c_uint64), ("resident_tagged_pages", c_uint64),
        ("compressed_tagged_pages", c_uint64), ("tagged_compressions", c_uint64),
        ("tagged_decompressions", c_uint64),
        ("compressed_tag_storage_bytes", c_uint64),
    ]


class _SwapUsage(Structure):
    _fields_ = [
        ("total", c_uint64), ("available", c_uint64), ("used", c_uint64),
        ("page_size", c_uint32), ("encrypted", c_int32),
    ]


class _MacDiskCounters:
    _ENCODING_UTF8 = 0x08000100
    _CF_NUMBER_SINT64 = 4

    def __init__(self) -> None:
        self._io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._configure()
        self._statistics_key = self._string("Statistics")
        self._value_keys = {
            "read_bytes": self._string("Bytes (Read)"),
            "write_bytes": self._string("Bytes (Write)"),
            "read_operations": self._string("Operations (Read)"),
            "write_operations": self._string("Operations (Write)"),
        }
        self._health_value_keys = {
            "read_errors": self._string("Errors (Read)"),
            "write_errors": self._string("Errors (Write)"),
            "read_retries": self._string("Retries (Read)"),
            "write_retries": self._string("Retries (Write)"),
        }
        self._nand_status_key = self._string("AppleNANDStatus")

    def _configure(self) -> None:
        self._io.IOServiceMatching.argtypes = [c_char_p]
        self._io.IOServiceMatching.restype = c_void_p
        self._io.IOServiceGetMatchingServices.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
        self._io.IOServiceGetMatchingServices.restype = c_int32
        self._io.IOIteratorNext.argtypes = [c_uint32]
        self._io.IOIteratorNext.restype = c_uint32
        self._io.IORegistryEntryCreateCFProperty.argtypes = [c_uint32, c_void_p, c_void_p, c_uint32]
        self._io.IORegistryEntryCreateCFProperty.restype = c_void_p
        self._io.IOObjectRelease.argtypes = [c_uint32]
        self._io.IOObjectRelease.restype = c_int32
        self._cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        self._cf.CFStringCreateWithCString.restype = c_void_p
        self._cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
        self._cf.CFDictionaryGetValue.restype = c_void_p
        self._cf.CFNumberGetValue.argtypes = [c_void_p, c_int32, c_void_p]
        self._cf.CFNumberGetValue.restype = c_bool
        self._cf.CFGetTypeID.argtypes = [c_void_p]
        self._cf.CFGetTypeID.restype = c_size_t
        self._cf.CFDictionaryGetTypeID.restype = c_size_t
        self._cf.CFStringGetTypeID.restype = c_size_t
        self._cf.CFStringGetLength.argtypes = [c_void_p]
        self._cf.CFStringGetLength.restype = c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [c_long, c_uint32]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = c_long
        self._cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        self._cf.CFStringGetCString.restype = c_bool
        self._cf.CFRelease.argtypes = [c_void_p]

    def _string(self, value: str) -> c_void_p:
        result = self._cf.CFStringCreateWithCString(None, value.encode(), self._ENCODING_UTF8)
        if not result:
            raise RuntimeError("cannot create CoreFoundation key")
        return result

    def _statistics(self, keys: dict[str, c_void_p]) -> tuple[dict[str, int], bool]:
        iterator = c_uint32()
        matching = self._io.IOServiceMatching(b"IOBlockStorageDriver")
        if not matching or self._io.IOServiceGetMatchingServices(0, matching, byref(iterator)) != 0:
            raise RuntimeError("cannot enumerate block storage drivers")
        totals = {name: 0 for name in keys}
        seen = False
        try:
            while service := self._io.IOIteratorNext(iterator.value):
                try:
                    statistics = self._io.IORegistryEntryCreateCFProperty(service, self._statistics_key, None, 0)
                    if not statistics:
                        continue
                    try:
                        if self._cf.CFGetTypeID(statistics) != self._cf.CFDictionaryGetTypeID():
                            continue
                        seen = True
                        for name, key in keys.items():
                            number = self._cf.CFDictionaryGetValue(statistics, key)
                            value = c_int64()
                            if number and self._cf.CFNumberGetValue(number, self._CF_NUMBER_SINT64, byref(value)):
                                totals[name] += max(0, int(value.value))
                    finally:
                        self._cf.CFRelease(statistics)
                finally:
                    self._io.IOObjectRelease(service)
        finally:
            self._io.IOObjectRelease(iterator.value)
        return totals, seen

    def read(self) -> tuple[int, int, int, int]:
        totals, _ = self._statistics(self._value_keys)
        return totals["read_bytes"], totals["write_bytes"], totals["read_operations"], totals["write_operations"]

    def _cf_text(self, value: c_void_p) -> str | None:
        if not value or self._cf.CFGetTypeID(value) != self._cf.CFStringGetTypeID():
            return None
        length = self._cf.CFStringGetLength(value)
        capacity = self._cf.CFStringGetMaximumSizeForEncoding(length, self._ENCODING_UTF8) + 1
        buffer = create_string_buffer(max(1, capacity))
        if not self._cf.CFStringGetCString(value, buffer, capacity, self._ENCODING_UTF8):
            return None
        return buffer.value.decode("utf-8", errors="replace")

    def _nand_status(self) -> str | None:
        iterator = c_uint32()
        matching = self._io.IOServiceMatching(b"IONVMeController")
        if not matching or self._io.IOServiceGetMatchingServices(0, matching, byref(iterator)) != 0:
            return None
        statuses: list[str] = []
        try:
            while service := self._io.IOIteratorNext(iterator.value):
                try:
                    value = self._io.IORegistryEntryCreateCFProperty(service, self._nand_status_key, None, 0)
                    if not value:
                        continue
                    try:
                        if text := self._cf_text(value):
                            statuses.append(text)
                    finally:
                        self._cf.CFRelease(value)
                finally:
                    self._io.IOObjectRelease(service)
        finally:
            self._io.IOObjectRelease(iterator.value)
        return next((item for item in statuses if item.strip().lower() != "ready"), statuses[0] if statuses else None)

    def read_health(self) -> DiskHealthEvidence:
        totals, seen = self._statistics(self._health_value_keys)
        return DiskHealthEvidence(
            nand_status=self._nand_status(),
            read_errors=totals["read_errors"] if seen else None,
            write_errors=totals["write_errors"] if seen else None,
            read_retries=totals["read_retries"] if seen else None,
            write_retries=totals["write_retries"] if seen else None,
        )


class MacOSSystemBackend:
    platform = "macos"
    capabilities = (
        CPU_UTILIZATION, MEMORY_CAPACITY, MEMORY_PRESSURE, MEMORY_COMPRESSION,
        MEMORY_SWAP, DISK_CAPACITY, DISK_THROUGHPUT, DISK_HEALTH, THERMAL_PRESSURE,
    )
    _HOST_CPU_LOAD_INFO = 3
    _HOST_VM_INFO64 = 4

    def __init__(self, volume_path: Path | None = None) -> None:
        self._volume_path = volume_path or Path.home()
        self._page_size = int(os.sysconf("SC_PAGE_SIZE"))
        self._libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self._configure_libc()
        self._disk = _MacDiskCounters()
        self._disk_health = DiskHealthMonitor(self._disk.read_health)
        self._thermal_reader = self._configure_thermal_reader()

    def _configure_libc(self) -> None:
        self._libc.mach_host_self.restype = c_uint32
        self._libc.host_statistics.argtypes = [c_uint32, c_int32, POINTER(c_int32), POINTER(c_uint32)]
        self._libc.host_statistics.restype = c_int32
        self._libc.host_statistics64.argtypes = [c_uint32, c_int32, POINTER(c_int32), POINTER(c_uint32)]
        self._libc.host_statistics64.restype = c_int32
        self._libc.sysctlbyname.argtypes = [c_char_p, c_void_p, POINTER(c_size_t), c_void_p, c_size_t]
        self._libc.sysctlbyname.restype = c_int32

    @staticmethod
    def _configure_thermal_reader() -> Any:
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.argtypes = [c_char_p]
        objc.objc_getClass.restype = c_void_p
        objc.sel_registerName.argtypes = [c_char_p]
        objc.sel_registerName.restype = c_void_p
        send_id = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(("objc_msgSend", objc))
        send_integer = ctypes.CFUNCTYPE(c_long, c_void_p, c_void_p)(("objc_msgSend", objc))
        process_info = send_id(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
        selector = objc.sel_registerName(b"thermalState")
        return lambda: int(send_integer(process_info, selector))

    def _cpu_ticks(self) -> tuple[int, int, int, int]:
        value = _HostCpuLoadInfo()
        count = c_uint32(sizeof(value) // sizeof(c_int32))
        result = self._libc.host_statistics(self._libc.mach_host_self(), self._HOST_CPU_LOAD_INFO, cast(byref(value), POINTER(c_int32)), byref(count))
        if result != 0:
            raise RuntimeError(f"host CPU statistics failed: {result}")
        return tuple(int(item) for item in value.cpu_ticks)  # type: ignore[return-value]

    def _vm(self) -> _VmStatistics64:
        value = _VmStatistics64()
        count = c_uint32(sizeof(value) // sizeof(c_int32))
        result = self._libc.host_statistics64(self._libc.mach_host_self(), self._HOST_VM_INFO64, cast(byref(value), POINTER(c_int32)), byref(count))
        if result != 0:
            raise RuntimeError(f"host VM statistics failed: {result}")
        return value

    def _sysctl(self, name: str, value: Any) -> Any | None:
        size = c_size_t(sizeof(value))
        return value if self._libc.sysctlbyname(name.encode(), byref(value), byref(size), None, 0) == 0 else None

    def read(self, observed_at: str, epoch: float) -> HostReading:
        cpu_ticks = self._cpu_ticks()
        vm = self._vm()
        total_value = self._sysctl("hw.memsize", c_uint64())
        memory_total = int(total_value.value) if total_value is not None else int(os.sysconf("SC_PHYS_PAGES")) * self._page_size
        available_pages = int(vm.free_count + vm.inactive_count + vm.speculative_count + vm.purgeable_count)
        memory_available = min(memory_total, available_pages * self._page_size)
        pressure_value = self._sysctl("kern.memorystatus_vm_pressure_level", c_int32())
        pressure_levels = {MEMORY_PRESSURE_NORMAL: "normal", MEMORY_PRESSURE_WARNING: "warning", MEMORY_PRESSURE_CRITICAL: "critical"}
        if pressure_value is not None and int(pressure_value.value) in pressure_levels:
            pressure, pressure_exact = pressure_levels[int(pressure_value.value)], True
        else:
            ratio = memory_available / max(1, memory_total)
            pressure, pressure_exact = ("critical" if ratio <= .05 else "warning" if ratio <= .10 else "normal"), False
        swap_value = self._sysctl("vm.swapusage", _SwapUsage())
        usage = shutil.disk_usage(self._volume_path)
        try:
            disk_read, disk_write, read_ops, write_ops = self._disk.read()
            physical_io_available = True
        except Exception:
            disk_read = disk_write = read_ops = write_ops = 0
            physical_io_available = False
        try:
            thermal_state = THERMAL_STATES.get(self._thermal_reader())
        except Exception:
            thermal_state = None
        return HostReading(
            observed_at, float(epoch), cpu_ticks, memory_total, memory_available,
            int(vm.compressor_page_count) * self._page_size, pressure, pressure_exact,
            int(swap_value.used) if swap_value is not None else int(vm.swapped_count) * self._page_size,
            int(vm.swapins) * self._page_size, int(vm.swapouts) * self._page_size,
            int(usage.total), int(usage.free), disk_read, disk_write, read_ops, write_ops,
            physical_io_available, thermal_state, self._disk_health.read(observed_at, epoch),
        )
