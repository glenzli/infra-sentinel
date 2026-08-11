"""Privacy-bounded attribution of host disk I/O to local applications.

Platform adapters expose cumulative per-process counters without paths or
arguments.  This owner converts them into interval deltas, groups helpers by a
stable application identity, and bounds both the live projection and durable
metric cardinality.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Iterable, Protocol


MAX_SAMPLE_APPS = 32
MAX_DISPLAY_APPS = 8
OTHER_APP_ID = "other-attributed"
OTHER_APP_LABEL = "Other attributed processes"


@dataclass
class _AppAccumulator:
    label: str
    read_bytes: int = 0
    write_bytes: int = 0
    process_count: int = 0


@dataclass(frozen=True)
class ProcessIoCounter:
    identity: str
    app_id: str
    app_label: str
    read_bytes: int
    write_bytes: int


@dataclass(frozen=True)
class ProcessIoBatch:
    counters: tuple[ProcessIoCounter, ...]
    observed_processes: int
    skipped_processes: int


class ProcessIoBackend(Protocol):
    platform: str

    def read(self) -> ProcessIoBatch:
        """Return cumulative process counters without paths or arguments."""


@dataclass(frozen=True)
class AppIoDelta:
    app_id: str
    app_label: str
    read_bytes: int
    write_bytes: int
    process_count: int

    @property
    def total_bytes(self) -> int:
        return self.read_bytes + self.write_bytes


@dataclass(frozen=True)
class ProcessIoInterval:
    available: bool
    ready: bool
    elapsed_seconds: float
    apps: tuple[AppIoDelta, ...]
    attributed_read_bytes: int
    attributed_write_bytes: int
    host_read_bytes: int
    host_write_bytes: int
    coverage_ratio: float | None
    observed_processes: int
    skipped_processes: int
    error_kind: str | None = None

    def as_dict(self, *, limit: int = MAX_DISPLAY_APPS) -> dict[str, object]:
        apps, other = _bounded_apps(self.apps, limit)
        materialized = list(apps)
        if other is not None:
            materialized.append(other)
        seconds = max(1.0, self.elapsed_seconds)
        return {
            "available": self.available,
            "ready": self.ready,
            "coverage_ratio": self.coverage_ratio,
            "attributed_read_bytes_per_second": self.attributed_read_bytes / seconds,
            "attributed_write_bytes_per_second": self.attributed_write_bytes / seconds,
            "unattributed_read_bytes_per_second": max(
                0, self.host_read_bytes - self.attributed_read_bytes
            ) / seconds,
            "unattributed_write_bytes_per_second": max(
                0, self.host_write_bytes - self.attributed_write_bytes
            ) / seconds,
            "observed_processes": self.observed_processes,
            "skipped_processes": self.skipped_processes,
            "apps": [
                {
                    "id": app.app_id,
                    "label": app.app_label,
                    "read_bytes_per_second": app.read_bytes / seconds,
                    "write_bytes_per_second": app.write_bytes / seconds,
                    "process_count": app.process_count,
                }
                for app in materialized
            ],
            **({"error_kind": self.error_kind} if self.error_kind else {}),
        }


def _bounded_apps(
    apps: Iterable[AppIoDelta],
    limit: int,
) -> tuple[tuple[AppIoDelta, ...], AppIoDelta | None]:
    ordered = sorted(
        (app for app in apps if app.total_bytes > 0),
        key=lambda app: (-app.total_bytes, app.app_label.casefold(), app.app_id),
    )
    selected = tuple(ordered[:max(0, limit)])
    remainder = ordered[max(0, limit):]
    if not remainder:
        return selected, None
    return selected, AppIoDelta(
        OTHER_APP_ID,
        OTHER_APP_LABEL,
        sum(app.read_bytes for app in remainder),
        sum(app.write_bytes for app in remainder),
        sum(app.process_count for app in remainder),
    )


def aggregate_process_io(
    intervals: Iterable[ProcessIoInterval],
    *,
    limit: int = MAX_DISPLAY_APPS,
) -> ProcessIoInterval | None:
    materialized = tuple(item for item in intervals if item.available and item.ready)
    if not materialized:
        return None
    grouped: dict[str, _AppAccumulator] = {}
    for interval in materialized:
        for app in interval.apps:
            current = grouped.setdefault(app.app_id, _AppAccumulator(app.app_label))
            current.read_bytes += app.read_bytes
            current.write_bytes += app.write_bytes
            current.process_count = max(current.process_count, app.process_count)
    apps = tuple(
        AppIoDelta(
            app_id,
            value.label,
            value.read_bytes,
            value.write_bytes,
            value.process_count,
        )
        for app_id, value in grouped.items()
    )
    bounded, other = _bounded_apps(apps, limit)
    if other is not None:
        bounded += (other,)
    host_read = sum(item.host_read_bytes for item in materialized)
    host_write = sum(item.host_write_bytes for item in materialized)
    attributed_read = sum(item.attributed_read_bytes for item in materialized)
    attributed_write = sum(item.attributed_write_bytes for item in materialized)
    host_total = host_read + host_write
    attributed_total = attributed_read + attributed_write
    coverage = min(1.0, attributed_total / host_total) if host_total > 0 else None
    return ProcessIoInterval(
        available=True,
        ready=True,
        elapsed_seconds=sum(item.elapsed_seconds for item in materialized),
        apps=bounded,
        attributed_read_bytes=attributed_read,
        attributed_write_bytes=attributed_write,
        host_read_bytes=host_read,
        host_write_bytes=host_write,
        coverage_ratio=coverage,
        observed_processes=max(item.observed_processes for item in materialized),
        skipped_processes=max(item.skipped_processes for item in materialized),
    )


class ProcessIoAttributor:
    """Convert platform cumulative counters into bounded interval attribution."""

    def __init__(self, backend: ProcessIoBackend) -> None:
        self.backend = backend
        self._previous: dict[str, ProcessIoCounter] = {}

    def sample(
        self,
        *,
        elapsed_seconds: float | None,
        host_read_bytes: int = 0,
        host_write_bytes: int = 0,
    ) -> ProcessIoInterval:
        try:
            batch = self.backend.read()
        except Exception as error:
            self._previous.clear()
            return ProcessIoInterval(
                available=False,
                ready=False,
                elapsed_seconds=max(0.0, elapsed_seconds or 0.0),
                apps=(),
                attributed_read_bytes=0,
                attributed_write_bytes=0,
                host_read_bytes=max(0, host_read_bytes),
                host_write_bytes=max(0, host_write_bytes),
                coverage_ratio=None,
                observed_processes=0,
                skipped_processes=0,
                error_kind=type(error).__name__,
            )

        current = {counter.identity: counter for counter in batch.counters}
        if elapsed_seconds is None or elapsed_seconds <= 0 or not self._previous:
            self._previous = current
            return ProcessIoInterval(
                available=True,
                ready=False,
                elapsed_seconds=max(0.0, elapsed_seconds or 0.0),
                apps=(),
                attributed_read_bytes=0,
                attributed_write_bytes=0,
                host_read_bytes=max(0, host_read_bytes),
                host_write_bytes=max(0, host_write_bytes),
                coverage_ratio=None,
                observed_processes=batch.observed_processes,
                skipped_processes=batch.skipped_processes,
            )

        counts: dict[str, int] = {}
        for counter in batch.counters:
            counts[counter.app_id] = counts.get(counter.app_id, 0) + 1
        grouped: dict[str, _AppAccumulator] = {}
        for identity, counter in current.items():
            previous = self._previous.get(identity)
            if previous is None:
                continue
            read_bytes = max(0, counter.read_bytes - previous.read_bytes)
            write_bytes = max(0, counter.write_bytes - previous.write_bytes)
            if read_bytes + write_bytes <= 0:
                continue
            value = grouped.setdefault(counter.app_id, _AppAccumulator(counter.app_label))
            value.read_bytes += read_bytes
            value.write_bytes += write_bytes
        self._previous = current
        apps = tuple(
            AppIoDelta(
                app_id,
                value.label,
                value.read_bytes,
                value.write_bytes,
                counts.get(app_id, 0),
            )
            for app_id, value in grouped.items()
        )
        bounded, other = _bounded_apps(apps, MAX_SAMPLE_APPS)
        if other is not None:
            bounded += (other,)
        attributed_read = sum(app.read_bytes for app in apps)
        attributed_write = sum(app.write_bytes for app in apps)
        host_total = max(0, host_read_bytes) + max(0, host_write_bytes)
        attributed_total = attributed_read + attributed_write
        coverage = min(1.0, attributed_total / host_total) if host_total > 0 else None
        return ProcessIoInterval(
            available=True,
            ready=True,
            elapsed_seconds=float(elapsed_seconds),
            apps=bounded,
            attributed_read_bytes=attributed_read,
            attributed_write_bytes=attributed_write,
            host_read_bytes=max(0, host_read_bytes),
            host_write_bytes=max(0, host_write_bytes),
            coverage_ratio=coverage,
            observed_processes=batch.observed_processes,
            skipped_processes=batch.skipped_processes,
        )


def create_process_io_backend(platform: str) -> ProcessIoBackend | None:
    if platform != "macos":
        return None
    module = import_module("infra_sentinel.resources.system.backends.macos_process_io")
    return module.MacOSProcessIoBackend()
