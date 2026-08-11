from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_collectors import CollectorContext  # noqa: E402
from disk_health import (  # noqa: E402
    DiskHealthEvidence,
    DiskHealthMonitor,
    classify_disk_health,
)
from system_resources import HostReading, SystemResourceCollector  # noqa: E402
from system_resources_contract import (  # noqa: E402
    CPU_UTILIZATION, DISK_CAPACITY, DISK_HEALTH, DISK_THROUGHPUT,
    MEMORY_CAPACITY, MEMORY_COMPRESSION, MEMORY_PRESSURE, MEMORY_SWAP,
    THERMAL_PRESSURE,
)
from system_resources_linux import LinuxSystemBackend  # noqa: E402


class FakeBackend:
    platform = "macos"
    capabilities = (
        CPU_UTILIZATION, MEMORY_CAPACITY, MEMORY_PRESSURE, MEMORY_COMPRESSION,
        MEMORY_SWAP, DISK_CAPACITY, DISK_THROUGHPUT, DISK_HEALTH, THERMAL_PRESSURE,
    )

    def __init__(self, readings: list[HostReading]) -> None:
        self.readings = iter(readings)

    def read(self, observed_at: str, epoch: float) -> HostReading:
        return next(self.readings)


class LimitedBackend(FakeBackend):
    platform = "windows"
    capabilities = (CPU_UTILIZATION, MEMORY_CAPACITY, DISK_CAPACITY)


def reading(
    epoch: float,
    *,
    ticks: tuple[int, int, int, int] = (100, 100, 800, 0),
    pressure: str = "normal",
    free: int = 50,
    total: int = 100,
    disk_read: int = 1_000,
    disk_write: int = 2_000,
    read_ops: int = 10,
    write_ops: int = 20,
    swapin: int = 100,
    swapout: int = 200,
    thermal: str | None = "nominal",
    io_available: bool = True,
    disk_health=None,
) -> HostReading:
    return HostReading(
        observed_at=f"2026-08-11T{12 + int(epoch) // 3_600:02d}:{(int(epoch) // 60) % 60:02d}:00+08:00",
        epoch=epoch,
        cpu_ticks=ticks,
        memory_total_bytes=32 * 1024 ** 3,
        memory_available_bytes=16 * 1024 ** 3,
        memory_compressed_bytes=2 * 1024 ** 3,
        memory_pressure=pressure,
        memory_pressure_exact=True,
        swap_used_bytes=1024 ** 3,
        swapin_bytes=swapin,
        swapout_bytes=swapout,
        disk_total_bytes=total,
        disk_free_bytes=free,
        disk_read_bytes=disk_read,
        disk_write_bytes=disk_write,
        disk_read_operations=read_ops,
        disk_write_operations=write_ops,
        physical_io_available=io_available,
        thermal_state=thermal,
        disk_health=disk_health,
    )


def context(epoch: float) -> CollectorContext:
    return CollectorContext(
        {"epoch": epoch, "timestamp": f"2026-08-11T{12 + int(epoch) // 3_600:02d}:{(int(epoch) // 60) % 60:02d}:00+08:00"},
        {},
    )


class SystemResourceCollectorTests(unittest.TestCase):
    def test_disk_health_monitor_reads_at_startup_then_only_every_six_hours(self) -> None:
        calls = 0

        def probe() -> DiskHealthEvidence:
            nonlocal calls
            calls += 1
            return DiskHealthEvidence(nand_status="Ready", read_errors=0, write_errors=0)

        monitor = DiskHealthMonitor(probe)

        first = monitor.read("2026-08-11T12:00:00+08:00", 0)
        cached = monitor.read("2026-08-11T17:59:59+08:00", 21_599)
        refreshed = monitor.read("2026-08-11T18:00:00+08:00", 21_600)

        self.assertEqual(first.state, "healthy")
        self.assertIs(first, cached)
        self.assertEqual(refreshed.state, "healthy")
        self.assertEqual(calls, 2)

    def test_disk_health_classification_is_conservative(self) -> None:
        observed = "2026-08-11T12:00:00+08:00"

        self.assertEqual(classify_disk_health(DiskHealthEvidence(nand_status="Ready"), observed).state, "healthy")
        self.assertEqual(classify_disk_health(DiskHealthEvidence(nand_status="Ready", read_errors=1), observed).state, "warning")
        self.assertEqual(classify_disk_health(DiskHealthEvidence(nand_status="Failed"), observed).state, "critical")
        self.assertEqual(classify_disk_health(DiskHealthEvidence(), observed).state, "unknown")

    def test_live_snapshot_uses_counter_deltas_without_persisting_every_sample(self) -> None:
        collector = SystemResourceCollector(FakeBackend([
            reading(0),
            reading(
                5,
                ticks=(110, 120, 870, 0),
                disk_read=1_500,
                disk_write=3_000,
                read_ops=15,
                write_ops=30,
            ),
        ]), persist_interval_seconds=300)

        first = collector.collect(context(0))
        second = collector.collect(context(5))

        self.assertEqual(first.points, ())
        self.assertEqual(second.points, ())
        self.assertAlmostEqual(second.snapshot["cpu"]["percent"], 30.0)
        self.assertEqual(second.snapshot["disk"]["read_bytes_per_second"], 100.0)
        self.assertEqual(second.snapshot["disk"]["write_bytes_per_second"], 200.0)
        self.assertEqual(second.snapshot["privacy"], "aggregate-host-counters-only")
        self.assertEqual(second.snapshot["capabilities"], list(FakeBackend.capabilities))

    def test_five_minute_rollup_keeps_gauges_and_interval_counters_separate(self) -> None:
        collector = SystemResourceCollector(FakeBackend([
            reading(0),
            reading(150, ticks=(140, 120, 840, 0), disk_read=4_000, disk_write=8_000, read_ops=40, write_ops=80),
            reading(300, ticks=(180, 140, 880, 0), disk_read=10_000, disk_write=20_000, read_ops=100, write_ops=200),
        ]), persist_interval_seconds=300)

        collector.collect(context(0))
        collector.collect(context(150))
        result = collector.collect(context(300))
        points = {point.metric: point for point in result.points}

        self.assertEqual(points["system.disk.read_bytes"].instrument, "counter")
        self.assertEqual(points["system.disk.read_bytes"].value, 9_000)
        self.assertEqual(points["system.disk.write_bytes"].value, 18_000)
        self.assertEqual(points["system.disk.read_operations"].value, 90)
        self.assertEqual(points["system.cpu.percent"].instrument, "gauge")
        self.assertGreater(points["system.cpu.percent"].value, 0)
        self.assertEqual(points["system.disk.free_bytes"].value, 50)

    def test_unsupported_platform_metrics_are_not_persisted_as_zeroes(self) -> None:
        collector = SystemResourceCollector(LimitedBackend([
            reading(0),
            reading(300, ticks=(150, 130, 920, 0), disk_read=99_000, disk_write=88_000),
        ]), persist_interval_seconds=300)

        collector.collect(context(0))
        result = collector.collect(context(300))

        self.assertEqual({point.metric for point in result.points}, {
            "system.cpu.percent", "system.memory.available_bytes", "system.disk.free_bytes",
        })
        self.assertEqual(result.snapshot["capabilities"], list(LimitedBackend.capabilities))
        self.assertEqual(result.snapshot["quality"], "ok")

    def test_native_pressure_capacity_and_thermal_states_drive_health(self) -> None:
        collector = SystemResourceCollector(FakeBackend([
            reading(0, pressure="warning"),
            reading(5, pressure="critical"),
            reading(10, pressure="normal", free=8, thermal="nominal"),
            reading(15, pressure="normal", free=50, thermal="nominal"),
        ]), persist_interval_seconds=300)

        warning = collector.collect(context(0)).snapshot
        critical_memory = collector.collect(context(5)).snapshot
        warning_disk = collector.collect(context(10)).snapshot
        recovered = collector.collect(context(15)).snapshot

        self.assertEqual(warning["status"], "warning")
        self.assertIn("memory_pressure_warning", warning["reasons"])
        self.assertEqual(critical_memory["status"], "critical")
        self.assertEqual(warning_disk["status"], "warning")
        self.assertIn("disk_space_low", warning_disk["reasons"])
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(
            [item["type"] for item in collector.drain_transitions()],
            ["alert", "escalated", "deescalated", "recovered"],
        )

    def test_missing_physical_io_is_partial_data_not_a_resource_alert(self) -> None:
        collector = SystemResourceCollector(FakeBackend([reading(0, io_available=False)]))

        result = collector.collect(context(0))

        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.snapshot["status"], "healthy")
        self.assertEqual(result.snapshot["quality"], "partial")
        self.assertEqual(collector.drain_transitions(), ())

    def test_disk_health_warning_contributes_to_host_health(self) -> None:
        health = classify_disk_health(
            DiskHealthEvidence(nand_status="Ready", write_retries=2),
            "2026-08-11T12:00:00+08:00",
        )
        collector = SystemResourceCollector(FakeBackend([reading(0, disk_health=health)]))

        result = collector.collect(context(0))

        self.assertEqual(result.snapshot["status"], "warning")
        self.assertEqual(result.snapshot["disk"]["health"]["state"], "warning")
        self.assertIn("disk_health_warning", result.snapshot["reasons"])

    def test_long_sleep_restarts_the_history_window_instead_of_creating_a_rate_spike(self) -> None:
        collector = SystemResourceCollector(FakeBackend([
            reading(0),
            reading(3_600, disk_read=10_000_000, disk_write=20_000_000),
            reading(3_605, disk_read=10_000_500, disk_write=20_001_000),
        ]), persist_interval_seconds=300)

        collector.collect(context(0))
        after_sleep = collector.collect(context(3_600))
        resumed = collector.collect(context(3_605))

        self.assertEqual(after_sleep.points, ())
        self.assertEqual(resumed.points, ())
        self.assertEqual(resumed.snapshot["disk"]["read_bytes_per_second"], 100)

    def test_linux_backend_reads_procfs_sysfs_and_declares_only_supported_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            sys_root = root / "sys"
            volume = root / "volume"
            (sys_root / "block" / "sda").mkdir(parents=True)
            proc.mkdir()
            volume.mkdir()
            (proc / "stat").write_text("cpu 100 20 30 850 10 0 0 0\n", encoding="utf-8")
            (proc / "meminfo").write_text(
                "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 200 kB\nSwapFree: 120 kB\n",
                encoding="utf-8",
            )
            (proc / "vmstat").write_text("pswpin 3\npswpout 5\n", encoding="utf-8")
            (proc / "diskstats").write_text(
                "8 0 sda 10 0 20 0 30 0 40 0 0 0 0 0 0 0 0\n",
                encoding="utf-8",
            )
            backend = LinuxSystemBackend(volume, proc, sys_root)
            observed = backend.read("2026-08-11T12:00:00+08:00", 100.0)

        self.assertEqual(observed.cpu_ticks, (100, 30, 860, 20))
        self.assertEqual(observed.memory_total_bytes, 1_024_000)
        self.assertEqual(observed.memory_available_bytes, 409_600)
        self.assertEqual(observed.swap_used_bytes, 81_920)
        self.assertEqual(observed.swapin_bytes, 3 * int(os.sysconf("SC_PAGE_SIZE")))
        self.assertEqual(observed.disk_read_bytes, 20 * 512)
        self.assertEqual(observed.disk_write_bytes, 40 * 512)
        self.assertEqual(observed.memory_pressure, "unavailable")
        self.assertNotIn(MEMORY_PRESSURE, backend.capabilities)
        self.assertIn(DISK_THROUGHPUT, backend.capabilities)


if __name__ == "__main__":
    unittest.main()
