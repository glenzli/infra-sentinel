from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import CollectorContext  # noqa: E402
from infra_sentinel.resources.system.disk_health import (  # noqa: E402
    DiskHealthEvidence,
    DiskHealthMonitor,
    classify_disk_health,
)
from infra_sentinel.resources.system.collector import SystemResourceCollector  # noqa: E402
from infra_sentinel.resources.system.contract import (  # noqa: E402
    CPU_UTILIZATION, DISK_CAPACITY, DISK_HEALTH, DISK_PROCESS_ATTRIBUTION,
    DISK_THROUGHPUT,
    MEMORY_CAPACITY, MEMORY_COMPRESSION, MEMORY_PRESSURE, MEMORY_SWAP,
    THERMAL_PRESSURE, HostReading,
)
from infra_sentinel.resources.system.backends.linux import LinuxSystemBackend  # noqa: E402
from infra_sentinel.resources.system.backends.macos_process_io import app_identity  # noqa: E402
from infra_sentinel.resources.system.backends import create_system_backend  # noqa: E402
from infra_sentinel.resources.system.process_io import (  # noqa: E402
    ProcessIoBatch,
    ProcessIoCounter,
)


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


class FakeProcessIoBackend:
    platform = "macos"

    def __init__(self, batches: list[ProcessIoBatch]) -> None:
        self.batches = iter(batches)

    def read(self) -> ProcessIoBatch:
        return next(self.batches)


def process_batch(
    codex_read: int,
    codex_write: int,
    helper_read: int,
    helper_write: int,
    chrome_read: int,
    chrome_write: int,
) -> ProcessIoBatch:
    return ProcessIoBatch(
        counters=(
            ProcessIoCounter("10:1", "app:codex", "Codex", codex_read, codex_write),
            ProcessIoCounter("11:1", "app:codex", "Codex", helper_read, helper_write),
            ProcessIoCounter("12:1", "app:google-chrome", "Google Chrome", chrome_read, chrome_write),
        ),
        observed_processes=3,
        skipped_processes=1,
    )


class SystemBackendSelectionTests(unittest.TestCase):
    def test_loads_only_the_selected_platform_adapter(self) -> None:
        backend = object()
        module = SimpleNamespace(LinuxSystemBackend=lambda: backend)
        with patch("infra_sentinel.resources.system.backends.import_module", return_value=module) as loader:
            self.assertIs(create_system_backend("linux-custom"), backend)
        loader.assert_called_once_with("infra_sentinel.resources.system.backends.linux")

    def test_rejects_an_undeclared_platform(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            create_system_backend("plan9")


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
    def test_macos_helpers_are_grouped_under_the_parent_app(self) -> None:
        app = app_identity(
            "/Applications/Codex.app/Contents/Frameworks/Codex Helper.app/Contents/MacOS/Codex Helper",
            "Codex Helper",
        )
        process = app_identity("/usr/local/bin/python3", "Python")

        self.assertEqual(app, ("app:codex", "Codex"))
        self.assertEqual(process, ("process:python", "Python"))

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

    def test_process_io_is_live_in_memory_then_persisted_in_the_existing_rollup(self) -> None:
        collector = SystemResourceCollector(
            FakeBackend([
                reading(0, disk_read=1_000, disk_write=2_000),
                reading(150, disk_read=5_000, disk_write=8_000),
                reading(300, disk_read=11_000, disk_write=17_000),
            ]),
            process_io_backend=FakeProcessIoBackend([
                process_batch(100, 200, 50, 75, 80, 90),
                process_batch(1_100, 1_200, 550, 575, 280, 390),
                process_batch(2_100, 2_700, 1_050, 1_075, 680, 990),
            ]),
            persist_interval_seconds=300,
        )

        first = collector.collect(context(0))
        live = collector.collect(context(150))
        rolled = collector.collect(context(300))

        self.assertEqual(first.points, ())
        self.assertEqual(live.points, ())
        self.assertIn(DISK_PROCESS_ATTRIBUTION, live.snapshot["capabilities"])
        attribution = live.snapshot["disk"]["attribution"]
        self.assertTrue(attribution["ready"])
        self.assertEqual(attribution["apps"][0]["label"], "Codex")
        self.assertAlmostEqual(attribution["apps"][0]["read_bytes_per_second"], 10.0)
        self.assertNotIn("path", str(attribution).casefold())
        self.assertNotIn("pid", str(attribution).casefold())
        self.assertEqual(live.snapshot["privacy"], "aggregate-host-and-app-io-counters")

        app_points = {
            (point.metric, point.dimensions.get("app_id")): point
            for point in rolled.points
            if point.metric.startswith("system.disk.app.")
        }
        self.assertEqual(app_points[("system.disk.app.read_bytes", "app:codex")].value, 3_000)
        self.assertEqual(app_points[("system.disk.app.write_bytes", "app:codex")].value, 3_500)
        self.assertEqual(app_points[("system.disk.app.read_bytes", "app:google-chrome")].value, 600)
        coverage = next(point for point in rolled.points if point.metric == "system.disk.process_coverage_ratio")
        self.assertEqual(coverage.attribution_method, "inferred")
        self.assertEqual(coverage.confidence, "medium")
        self.assertTrue(coverage.estimated)

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

    def test_capacity_is_immediate_while_transient_pressure_requires_confirmation(self) -> None:
        collector = SystemResourceCollector(FakeBackend([
            reading(0, pressure="warning"),
            reading(5, pressure="normal"),
            reading(10, pressure="warning"),
            reading(15, pressure="warning"),
            reading(20, pressure="warning"),
            reading(25, pressure="normal", free=8, thermal="nominal"),
            reading(30, pressure="normal", free=50, thermal="nominal"),
            reading(35, pressure="normal", free=50, thermal="nominal"),
        ]), persist_interval_seconds=300)

        transient = collector.collect(context(0)).snapshot
        normal = collector.collect(context(5)).snapshot
        first = collector.collect(context(10)).snapshot
        second = collector.collect(context(15)).snapshot
        confirmed = collector.collect(context(20)).snapshot
        warning_disk = collector.collect(context(25)).snapshot
        recovery_pending = collector.collect(context(30)).snapshot
        recovered = collector.collect(context(35)).snapshot

        self.assertEqual(transient["status"], "healthy")
        self.assertEqual(transient["health_confirmation"]["consecutive"], 1)
        self.assertEqual(normal["status"], "healthy")
        self.assertEqual((first["status"], second["status"]), ("healthy", "healthy"))
        self.assertEqual(confirmed["status"], "warning")
        self.assertIn("memory_pressure_warning", confirmed["reasons"])
        self.assertEqual(warning_disk["status"], "warning")
        self.assertIn("disk_space_low", warning_disk["reasons"])
        self.assertEqual(recovery_pending["status"], "warning")
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(
            [item["type"] for item in collector.drain_transitions()],
            ["alert", "recovered"],
        )

    def test_single_low_disk_sample_is_immediate_but_recovery_is_confirmed(self) -> None:
        collector = SystemResourceCollector(FakeBackend([
            reading(0, free=8),
            reading(5, free=50),
            reading(10, free=50),
        ]))

        low = collector.collect(context(0)).snapshot
        pending = collector.collect(context(5)).snapshot
        recovered = collector.collect(context(10)).snapshot

        self.assertEqual(low["status"], "warning")
        self.assertEqual(low["reasons"], ["disk_space_low"])
        self.assertEqual(pending["status"], "warning")
        self.assertEqual(pending["health_confirmation"]["candidate_status"], "healthy")
        self.assertEqual(recovered["status"], "healthy")

    def test_missing_physical_io_is_partial_data_not_a_resource_alert(self) -> None:
        collector = SystemResourceCollector(FakeBackend([reading(0, io_available=False)]))

        result = collector.collect(context(0))

        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.snapshot["status"], "healthy")
        self.assertEqual(result.snapshot["quality"], "partial")
        self.assertEqual(collector.drain_transitions(), ())

    def test_disk_health_warning_requires_confirmation_before_host_attention(self) -> None:
        health = classify_disk_health(
            DiskHealthEvidence(nand_status="Ready", write_retries=2),
            "2026-08-11T12:00:00+08:00",
        )
        collector = SystemResourceCollector(FakeBackend([
            reading(0, disk_health=health),
            reading(5, disk_health=health),
            reading(10, disk_health=health),
        ]))

        first = collector.collect(context(0))
        second = collector.collect(context(5))
        result = collector.collect(context(10))

        self.assertEqual(first.snapshot["status"], "healthy")
        self.assertEqual(second.snapshot["status"], "healthy")
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
