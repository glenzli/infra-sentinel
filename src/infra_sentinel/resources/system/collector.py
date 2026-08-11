"""Low-overhead local system resource observation.

The collector owns platform reads, counter deltas, five-minute rollups,
health evaluation, and transition state.  Optional process attribution retains
only bounded App labels and counters; it never records file names, process
arguments, paths, window titles, or user content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.core.model import MetricPoint
from infra_sentinel.resources.system.disk_health import DiskHealthSnapshot
from infra_sentinel.resources.system.contract import (
    CPU_UTILIZATION, DISK_CAPACITY, DISK_PROCESS_ATTRIBUTION, DISK_THROUGHPUT,
    MEMORY_CAPACITY, MEMORY_COMPRESSION, MEMORY_PRESSURE, MEMORY_SWAP, THERMAL_PRESSURE,
    HostReading, SystemResourceBackend,
)
from infra_sentinel.resources.system.backends import create_system_backend
from infra_sentinel.resources.system.process_io import (
    ProcessIoAttributor, ProcessIoBackend, ProcessIoInterval,
    aggregate_process_io, create_process_io_backend,
)


SYSTEM_RESOURCE_SCHEMA = "20260812.1"
SOURCE_ID = "local-system"
RESOURCE_ID = "system"
DEFAULT_PERSIST_INTERVAL_SECONDS = 300

@dataclass(frozen=True)
class _Interval:
    elapsed_seconds: float
    cpu_percent: float
    disk_read_bytes: int
    disk_write_bytes: int
    disk_read_operations: int
    disk_write_operations: int
    disk_read_rate: float
    disk_write_rate: float
    disk_read_iops: float
    disk_write_iops: float
    swapin_bytes: int
    swapout_bytes: int
    process_io: ProcessIoInterval | None


def _delta(current: int, previous: int, *, wraps_at: int | None = None) -> int:
    if current >= previous:
        return current - previous
    if wraps_at is not None:
        return wraps_at - previous + current
    return 0


def _cpu_percent(current: HostReading, previous: HostReading) -> float:
    deltas = [
        _delta(current.cpu_ticks[index], previous.cpu_ticks[index], wraps_at=2 ** 32)
        for index in range(4)
    ]
    total = sum(deltas)
    return 0.0 if total <= 0 else max(0.0, min(100.0, (deltas[0] + deltas[1] + deltas[3]) / total * 100))


def _health(reading: HostReading) -> tuple[str, list[str]]:
    reasons: list[str] = []
    level = "healthy"
    free_ratio = reading.disk_free_bytes / max(1, reading.disk_total_bytes)
    if reading.memory_pressure == "critical":
        reasons.append("memory_pressure_critical")
        level = "critical"
    elif reading.memory_pressure == "warning":
        reasons.append("memory_pressure_warning")
        level = "warning"
    if free_ratio <= 0.05:
        reasons.append("disk_space_critical")
        level = "critical"
    elif free_ratio <= 0.10:
        reasons.append("disk_space_low")
        if level == "healthy":
            level = "warning"
    if reading.thermal_state == "critical":
        reasons.append("thermal_pressure_critical")
        level = "critical"
    elif reading.thermal_state == "serious":
        reasons.append("thermal_pressure_serious")
        if level == "healthy":
            level = "warning"
    disk_health = reading.disk_health
    if disk_health is not None and disk_health.state == "critical":
        reasons.append("disk_health_critical")
        level = "critical"
    elif disk_health is not None and disk_health.state == "warning":
        reasons.append("disk_health_warning")
        if level == "healthy":
            level = "warning"
    return level, reasons


def _point(
    reading: HostReading,
    metric: str,
    instrument: str,
    value: int | float,
    unit: str,
) -> MetricPoint:
    return MetricPoint(
        observed_at=reading.observed_at,
        observed_epoch=reading.epoch,
        metric=metric,
        instrument=instrument,  # type: ignore[arg-type]
        value=value,
        unit=unit,
        source_id=SOURCE_ID,
        resource_id=RESOURCE_ID,
    )


def _app_point(
    reading: HostReading,
    metric: str,
    value: int,
    app_id: str,
    app_label: str,
) -> MetricPoint:
    return MetricPoint(
        observed_at=reading.observed_at,
        observed_epoch=reading.epoch,
        metric=metric,
        instrument="counter",
        value=value,
        unit="bytes",
        source_id=SOURCE_ID,
        resource_id=RESOURCE_ID,
        dimensions={"app_id": app_id, "app_label": app_label},
        attribution_method="mapped",
        confidence="medium",
    )


def _coverage_point(reading: HostReading, value: float) -> MetricPoint:
    return MetricPoint(
        observed_at=reading.observed_at,
        observed_epoch=reading.epoch,
        metric="system.disk.process_coverage_ratio",
        instrument="gauge",
        value=value,
        unit="ratio",
        source_id=SOURCE_ID,
        resource_id=RESOURCE_ID,
        attribution_method="inferred",
        confidence="medium",
        estimated=True,
    )


class SystemResourceCollector:
    """Own live sampling, bounded persistence, and host-risk transitions."""

    capability = CollectorCapability(
        id="system.host",
        source_id=SOURCE_ID,
        source_kind="system.host",
        resource_id=RESOURCE_ID,
        metrics=(
            "system.cpu.percent", "system.memory.pressure_level", "system.memory.available_bytes",
            "system.memory.compressed_bytes", "system.memory.swap_used_bytes",
            "system.memory.swapin_bytes", "system.memory.swapout_bytes",
            "system.disk.free_bytes", "system.disk.read_bytes", "system.disk.write_bytes",
            "system.disk.read_operations", "system.disk.write_operations", "system.thermal.level",
            "system.disk.app.read_bytes", "system.disk.app.write_bytes",
            "system.disk.process_coverage_ratio",
        ),
    )

    def __init__(
        self,
        backend: SystemResourceBackend | None = None,
        *,
        process_io_backend: ProcessIoBackend | None = None,
        persist_interval_seconds: int = DEFAULT_PERSIST_INTERVAL_SECONDS,
    ) -> None:
        owns_backend = backend is None
        self.backend = backend or create_system_backend()
        if process_io_backend is None and owns_backend:
            try:
                process_io_backend = create_process_io_backend(self.backend.platform)
            except Exception:
                process_io_backend = None
        self._process_io = ProcessIoAttributor(process_io_backend) if process_io_backend else None
        self._latest_process_io: ProcessIoInterval | None = None
        self.persist_interval_seconds = max(1, int(persist_interval_seconds))
        self._previous: HostReading | None = None
        self._window_started: float | None = None
        self._intervals: list[_Interval] = []
        self._last_status: str | None = None
        self._transitions: list[dict[str, Any]] = []

    @staticmethod
    def _observed(context: CollectorContext) -> tuple[str, float]:
        epoch = float(context.local_sample.get("epoch") or datetime.now().timestamp())
        observed_at = str(context.local_sample.get("timestamp") or datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds"))
        return observed_at, epoch

    def _interval(self, reading: HostReading) -> _Interval | None:
        previous = self._previous
        if previous is None:
            if self._process_io is not None:
                self._latest_process_io = self._process_io.sample(elapsed_seconds=None)
            return None
        seconds = reading.epoch - previous.epoch
        if seconds <= 0:
            return None
        if seconds > max(60, self.persist_interval_seconds):
            self._intervals.clear()
            self._window_started = reading.epoch
            if self._process_io is not None:
                self._latest_process_io = self._process_io.sample(elapsed_seconds=None)
            return None
        read_bytes = _delta(reading.disk_read_bytes, previous.disk_read_bytes)
        write_bytes = _delta(reading.disk_write_bytes, previous.disk_write_bytes)
        read_ops = _delta(reading.disk_read_operations, previous.disk_read_operations)
        write_ops = _delta(reading.disk_write_operations, previous.disk_write_operations)
        process_io = None
        if self._process_io is not None:
            process_io = self._process_io.sample(
                elapsed_seconds=seconds,
                host_read_bytes=read_bytes,
                host_write_bytes=write_bytes,
            )
            self._latest_process_io = process_io
        return _Interval(
            elapsed_seconds=seconds,
            cpu_percent=_cpu_percent(reading, previous),
            disk_read_bytes=read_bytes,
            disk_write_bytes=write_bytes,
            disk_read_operations=read_ops,
            disk_write_operations=write_ops,
            disk_read_rate=read_bytes / seconds,
            disk_write_rate=write_bytes / seconds,
            disk_read_iops=read_ops / seconds,
            disk_write_iops=write_ops / seconds,
            swapin_bytes=_delta(reading.swapin_bytes, previous.swapin_bytes),
            swapout_bytes=_delta(reading.swapout_bytes, previous.swapout_bytes),
            process_io=process_io if process_io is not None and process_io.ready else None,
        )

    def _transition(self, reading: HostReading, status: str, reasons: list[str]) -> None:
        previous = self._last_status
        self._last_status = status
        if previous == status or status == "degraded":
            return
        if previous is None:
            if status not in {"warning", "critical"}:
                return
            event_type = "alert"
        elif status == "healthy":
            event_type = "recovered"
        elif previous == "critical" and status == "warning":
            event_type = "deescalated"
        elif status == "critical":
            event_type = "escalated"
        else:
            event_type = "alert"
        self._transitions.append({
            "timestamp": reading.observed_at,
            "type": event_type,
            "level": "none" if status == "healthy" else status,
            "previous": previous or "unknown",
            "reasons": list(reasons),
            "disk_free_bytes": reading.disk_free_bytes,
            "memory_pressure": reading.memory_pressure,
            "thermal_state": reading.thermal_state or "unavailable",
        })

    def _points(self, reading: HostReading) -> tuple[MetricPoint, ...]:
        if self._window_started is None:
            self._window_started = reading.epoch
            return ()
        if reading.epoch - self._window_started < self.persist_interval_seconds or not self._intervals:
            return ()
        intervals = tuple(self._intervals)
        self._intervals.clear()
        self._window_started = reading.epoch
        pressure_level = {"normal": 0, "warning": 1, "critical": 2}.get(reading.memory_pressure, 0)
        thermal_level = {"nominal": 0, "fair": 1, "serious": 2, "critical": 3}.get(reading.thermal_state or "")
        capabilities = set(getattr(self.backend, "capabilities", (DISK_THROUGHPUT,)))
        if self._process_io is not None:
            capabilities.add(DISK_PROCESS_ATTRIBUTION)
        points: list[MetricPoint] = []
        if CPU_UTILIZATION in capabilities:
            points.append(_point(reading, "system.cpu.percent", "gauge", sum(item.cpu_percent for item in intervals) / len(intervals), "percent"))
        if MEMORY_PRESSURE in capabilities:
            points.append(_point(reading, "system.memory.pressure_level", "gauge", pressure_level, "level"))
        if MEMORY_CAPACITY in capabilities:
            points.append(_point(reading, "system.memory.available_bytes", "gauge", reading.memory_available_bytes, "bytes"))
        if MEMORY_COMPRESSION in capabilities:
            points.append(_point(reading, "system.memory.compressed_bytes", "gauge", reading.memory_compressed_bytes, "bytes"))
        if MEMORY_SWAP in capabilities:
            points.extend([
                _point(reading, "system.memory.swap_used_bytes", "gauge", reading.swap_used_bytes, "bytes"),
                _point(reading, "system.memory.swapin_bytes", "counter", sum(item.swapin_bytes for item in intervals), "bytes"),
                _point(reading, "system.memory.swapout_bytes", "counter", sum(item.swapout_bytes for item in intervals), "bytes"),
            ])
        if DISK_CAPACITY in capabilities:
            points.append(_point(reading, "system.disk.free_bytes", "gauge", reading.disk_free_bytes, "bytes"))
        if DISK_THROUGHPUT in capabilities:
            points.extend([
                _point(reading, "system.disk.read_bytes", "counter", sum(item.disk_read_bytes for item in intervals), "bytes"),
                _point(reading, "system.disk.write_bytes", "counter", sum(item.disk_write_bytes for item in intervals), "bytes"),
                _point(reading, "system.disk.read_operations", "counter", sum(item.disk_read_operations for item in intervals), "operations"),
                _point(reading, "system.disk.write_operations", "counter", sum(item.disk_write_operations for item in intervals), "operations"),
            ])
        if DISK_PROCESS_ATTRIBUTION in capabilities:
            process_window = aggregate_process_io(
                item.process_io for item in intervals if item.process_io is not None
            )
            if process_window is not None:
                for app in process_window.apps:
                    points.extend([
                        _app_point(
                            reading,
                            "system.disk.app.read_bytes",
                            app.read_bytes,
                            app.app_id,
                            app.app_label,
                        ),
                        _app_point(
                            reading,
                            "system.disk.app.write_bytes",
                            app.write_bytes,
                            app.app_id,
                            app.app_label,
                        ),
                    ])
                if process_window.coverage_ratio is not None:
                    points.append(_coverage_point(reading, process_window.coverage_ratio))
        if THERMAL_PRESSURE in capabilities and thermal_level is not None:
            points.append(_point(reading, "system.thermal.level", "gauge", thermal_level, "level"))
        return tuple(points)

    def collect(self, context: CollectorContext) -> Collection:
        observed_at, epoch = self._observed(context)
        reading = self.backend.read(observed_at, epoch)
        interval = self._interval(reading)
        if interval is not None:
            self._intervals.append(interval)
        status, reasons = _health(reading)
        self._transition(reading, status, reasons)
        points = self._points(reading)
        self._previous = reading
        capabilities = tuple(getattr(self.backend, "capabilities", (DISK_THROUGHPUT,)))
        if self._process_io is not None and DISK_PROCESS_ATTRIBUTION not in capabilities:
            capabilities += (DISK_PROCESS_ATTRIBUTION,)
        quality = "partial" if DISK_THROUGHPUT in capabilities and not reading.physical_io_available else "ok"
        current = interval or _Interval(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None)
        disk_snapshot: dict[str, Any] = {
            "total_bytes": reading.disk_total_bytes,
            "free_bytes": reading.disk_free_bytes,
            "used_percent": max(0.0, min(100.0, (1 - reading.disk_free_bytes / max(1, reading.disk_total_bytes)) * 100)),
            "read_bytes_per_second": current.disk_read_rate,
            "write_bytes_per_second": current.disk_write_rate,
            "read_iops": current.disk_read_iops,
            "write_iops": current.disk_write_iops,
            "physical_io_available": reading.physical_io_available,
            "health": (
                reading.disk_health.as_dict()
                if reading.disk_health is not None
                else DiskHealthSnapshot(
                    state="unknown",
                    observed_at=reading.observed_at,
                    reason_codes=("health_signal_unavailable",),
                ).as_dict()
            ),
        }
        if self._latest_process_io is not None:
            disk_snapshot["attribution"] = self._latest_process_io.as_dict()
        snapshot = {
            "schema": SYSTEM_RESOURCE_SCHEMA,
            "available": True,
            "platform": self.backend.platform,
            "capabilities": list(capabilities),
            "status": status,
            "quality": quality,
            "reasons": reasons,
            "observed_at": reading.observed_at,
            "observed_epoch": reading.epoch,
            "cpu": {"percent": current.cpu_percent},
            "memory": {
                "pressure": reading.memory_pressure,
                "pressure_exact": reading.memory_pressure_exact,
                "total_bytes": reading.memory_total_bytes,
                "available_bytes": reading.memory_available_bytes,
                "compressed_bytes": reading.memory_compressed_bytes,
                "swap_used_bytes": reading.swap_used_bytes,
                "swapin_bytes_per_second": current.swapin_bytes / current.elapsed_seconds,
                "swapout_bytes_per_second": current.swapout_bytes / current.elapsed_seconds,
            },
            "disk": disk_snapshot,
            "thermal": {"state": reading.thermal_state or "unavailable"},
            "persistence": {"interval_seconds": self.persist_interval_seconds},
            "privacy": (
                "aggregate-host-and-app-io-counters"
                if self._process_io is not None
                else "aggregate-host-counters-only"
            ),
        }
        return Collection(points=points, status="ok" if quality == "ok" else "degraded", snapshot=snapshot)

    def drain_transitions(self) -> tuple[dict[str, Any], ...]:
        transitions = tuple(self._transitions)
        self._transitions.clear()
        return transitions
