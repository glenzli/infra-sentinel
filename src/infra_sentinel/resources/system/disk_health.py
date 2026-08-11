"""Conservative, ultra-low-frequency disk health observation.

The monitor caches public aggregate device evidence for six hours.  It never
reads volume contents, file metadata, model names, or device serial numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


DEFAULT_DISK_HEALTH_INTERVAL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class DiskHealthEvidence:
    nand_status: str | None = None
    read_errors: int | None = None
    write_errors: int | None = None
    read_retries: int | None = None
    write_retries: int | None = None


@dataclass(frozen=True)
class DiskHealthSnapshot:
    state: str
    observed_at: str
    reason_codes: tuple[str, ...]
    read_errors: int | None = None
    write_errors: int | None = None
    read_retries: int | None = None
    write_retries: int | None = None
    interval_seconds: int = DEFAULT_DISK_HEALTH_INTERVAL_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "observed_at": self.observed_at,
            "reason_codes": list(self.reason_codes),
            "read_errors": self.read_errors,
            "write_errors": self.write_errors,
            "read_retries": self.read_retries,
            "write_retries": self.write_retries,
            "interval_seconds": self.interval_seconds,
        }


def classify_disk_health(
    evidence: DiskHealthEvidence,
    observed_at: str,
    *,
    interval_seconds: int = DEFAULT_DISK_HEALTH_INTERVAL_SECONDS,
) -> DiskHealthSnapshot:
    """Classify only signals that are explicit enough to avoid false health."""
    counters = (
        evidence.read_errors,
        evidence.write_errors,
        evidence.read_retries,
        evidence.write_retries,
    )
    status = (evidence.nand_status or "").strip().lower()
    if status in {"failed", "failure", "critical", "offline", "not ready"}:
        state, reasons = "critical", ("device_status_critical",)
    elif any(value is not None and value > 0 for value in counters[:2]):
        state, reasons = "warning", ("io_errors_recorded",)
    elif any(value is not None and value > 0 for value in counters[2:]):
        state, reasons = "warning", ("io_retries_recorded",)
    elif status in {"ready", "ok", "online", "verified"}:
        state, reasons = "healthy", ()
    elif status:
        state, reasons = "warning", ("device_status_unexpected",)
    else:
        state, reasons = "unknown", ("health_signal_unavailable",)
    return DiskHealthSnapshot(
        state=state,
        observed_at=observed_at,
        reason_codes=reasons,
        read_errors=evidence.read_errors,
        write_errors=evidence.write_errors,
        read_retries=evidence.read_retries,
        write_retries=evidence.write_retries,
        interval_seconds=max(1, int(interval_seconds)),
    )


class DiskHealthMonitor:
    """Run the native probe at startup and no more than once per interval."""

    def __init__(
        self,
        probe: Callable[[], DiskHealthEvidence],
        *,
        interval_seconds: int = DEFAULT_DISK_HEALTH_INTERVAL_SECONDS,
    ) -> None:
        self._probe = probe
        self.interval_seconds = max(1, int(interval_seconds))
        self._next_probe_epoch = 0.0
        self._snapshot: DiskHealthSnapshot | None = None

    def read(self, observed_at: str, epoch: float) -> DiskHealthSnapshot:
        if self._snapshot is not None and epoch < self._next_probe_epoch:
            return self._snapshot
        try:
            evidence = self._probe()
            snapshot = classify_disk_health(
                evidence,
                observed_at,
                interval_seconds=self.interval_seconds,
            )
        except Exception:
            snapshot = DiskHealthSnapshot(
                state="unknown",
                observed_at=observed_at,
                reason_codes=("health_probe_failed",),
                interval_seconds=self.interval_seconds,
            )
        self._snapshot = snapshot
        self._next_probe_epoch = float(epoch) + self.interval_seconds
        return snapshot
