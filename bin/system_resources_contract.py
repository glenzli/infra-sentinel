"""Platform-neutral contract for host resource backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from disk_health import DiskHealthSnapshot


CPU_UTILIZATION = "cpu.utilization"
MEMORY_CAPACITY = "memory.capacity"
MEMORY_PRESSURE = "memory.pressure"
MEMORY_COMPRESSION = "memory.compression"
MEMORY_SWAP = "memory.swap"
DISK_CAPACITY = "disk.capacity"
DISK_THROUGHPUT = "disk.throughput"
DISK_HEALTH = "disk.health"
THERMAL_PRESSURE = "thermal.pressure"


@dataclass(frozen=True)
class HostReading:
    observed_at: str
    epoch: float
    cpu_ticks: tuple[int, int, int, int]
    memory_total_bytes: int
    memory_available_bytes: int
    memory_compressed_bytes: int
    memory_pressure: str
    memory_pressure_exact: bool
    swap_used_bytes: int
    swapin_bytes: int
    swapout_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    disk_read_bytes: int
    disk_write_bytes: int
    disk_read_operations: int
    disk_write_operations: int
    physical_io_available: bool
    thermal_state: str | None
    disk_health: DiskHealthSnapshot | None = None


class SystemResourceBackend(Protocol):
    platform: str
    capabilities: tuple[str, ...]

    def read(self, observed_at: str, epoch: float) -> HostReading:
        """Read cumulative host counters and current gauges."""
