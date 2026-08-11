"""Linux host resource backend using procfs and sysfs aggregate counters."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from system_resources_contract import (
    CPU_UTILIZATION, DISK_CAPACITY, DISK_THROUGHPUT, MEMORY_CAPACITY,
    MEMORY_SWAP, HostReading,
)


class LinuxSystemBackend:
    platform = "linux"
    capabilities = (CPU_UTILIZATION, MEMORY_CAPACITY, MEMORY_SWAP, DISK_CAPACITY, DISK_THROUGHPUT)

    def __init__(self, volume_path: Path | None = None, proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")) -> None:
        self._volume_path = volume_path or Path.home()
        self._proc = proc_root
        self._sys = sys_root
        self._page_size = int(os.sysconf("SC_PAGE_SIZE"))

    def _cpu_ticks(self) -> tuple[int, int, int, int]:
        fields = (self._proc / "stat").read_text(encoding="utf-8").splitlines()[0].split()
        if not fields or fields[0] != "cpu" or len(fields) < 5:
            raise RuntimeError("Linux CPU counters are unavailable")
        user, nice, system, idle = map(int, fields[1:5])
        iowait = int(fields[5]) if len(fields) > 5 else 0
        return user, system, idle + iowait, nice

    def _memory(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for line in (self._proc / "meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            token = raw.strip().split()[0] if raw.strip() else "0"
            values[key] = max(0, int(token)) * 1024
        return values

    def _swap_io(self) -> tuple[int, int]:
        values: dict[str, int] = {}
        for line in (self._proc / "vmstat").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in {"pswpin", "pswpout"}:
                values[parts[0]] = int(parts[1]) * self._page_size
        return values.get("pswpin", 0), values.get("pswpout", 0)

    def _disk(self) -> tuple[int, int, int, int]:
        devices = {path.name for path in (self._sys / "block").iterdir()}
        totals = [0, 0, 0, 0]
        for line in (self._proc / "diskstats").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 14 or parts[2] not in devices:
                continue
            totals[0] += int(parts[5]) * 512
            totals[1] += int(parts[9]) * 512
            totals[2] += int(parts[3])
            totals[3] += int(parts[7])
        return tuple(totals)  # type: ignore[return-value]

    def read(self, observed_at: str, epoch: float) -> HostReading:
        memory = self._memory()
        swapin, swapout = self._swap_io()
        usage = shutil.disk_usage(self._volume_path)
        try:
            disk_read, disk_write, read_ops, write_ops = self._disk()
            physical_io_available = True
        except (OSError, ValueError):
            disk_read = disk_write = read_ops = write_ops = 0
            physical_io_available = False
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable", memory.get("MemFree", 0))
        swap_total, swap_free = memory.get("SwapTotal", 0), memory.get("SwapFree", 0)
        return HostReading(
            observed_at, float(epoch), self._cpu_ticks(), total, available, 0,
            "unavailable", False, max(0, swap_total - swap_free), swapin, swapout,
            int(usage.total), int(usage.free), disk_read, disk_write, read_ops, write_ops,
            physical_io_available, None,
        )
