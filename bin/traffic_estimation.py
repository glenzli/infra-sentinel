"""Empirical VPS billing estimates and minute-normalized local traffic trends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


TREND_WINDOW_MINUTES = 15


@dataclass(frozen=True)
class TrafficEstimationConfig:
    proxy_group: str | None
    vps_billing_legs: float = 2.0
    link_overhead_ratio: float = 0.20

    @property
    def effective_multiplier(self) -> float:
        return self.vps_billing_legs * (1.0 + self.link_overhead_ratio)


def estimate_traffic(
    proxy_external_bytes: int,
    project_bytes: int,
    vps_billable_bytes: int,
    vps_ready: bool,
    config: TrafficEstimationConfig,
) -> dict[str, Any]:
    """Split local and remote traffic using a conservative empirical ceiling."""
    multiplier = config.effective_multiplier
    local_other = max(0, proxy_external_bytes - project_bytes)
    local_vps_ceiling = int(round(proxy_external_bytes * multiplier))
    other_billable = max(0, vps_billable_bytes - local_vps_ceiling) if vps_ready else None
    other_logical = int(round(other_billable / multiplier)) if other_billable is not None else None
    return {
        "method": "ceiling_conservative",
        "vps_billing_legs": config.vps_billing_legs,
        "link_overhead_ratio": config.link_overhead_ratio,
        "effective_multiplier": multiplier,
        "project_total_bytes": project_bytes,
        "local_other_estimated_bytes": local_other,
        "local_vps_billable_ceiling_bytes": local_vps_ceiling,
        "other_devices_billable_estimated_bytes": other_billable,
        "other_devices_logical_estimated_bytes": other_logical,
    }


def minute_rate_trend(
    history: Iterable[dict[str, Any]],
    group_ids: Iterable[str],
    window_minutes: int = TREND_WINDOW_MINUTES,
) -> dict[str, Any]:
    """Aggregate uneven local samples into comparable bytes-per-minute rates."""
    points = [point for point in history if isinstance(point.get("epoch"), (int, float))]
    ids = tuple(group_ids)
    if not points:
        return {"unit": "bytes_per_minute", "window_minutes": window_minutes, "buckets": [], "peak_bytes_per_minute": 0}
    latest_epoch = max(float(point["epoch"]) for point in points)
    cutoff = latest_epoch - window_minutes * 60
    buckets: dict[int, dict[str, Any]] = {}
    for point in points:
        epoch = float(point["epoch"])
        if epoch < cutoff:
            continue
        bucket_epoch = int(epoch // 60) * 60
        bucket = buckets.setdefault(
            bucket_epoch,
            {
                "observed_seconds": 0.0,
                "groups": {group_id: 0 for group_id in ids},
                "proxy_external": 0,
            },
        )
        observed = max(0.0, float(point.get("observed_seconds", 0.0)))
        bucket["observed_seconds"] += observed
        raw_groups = point.get("groups", {})
        for group_id in ids:
            bucket["groups"][group_id] += int(raw_groups.get(group_id, 0))
        bucket["proxy_external"] += int(point.get("proxy_external", 0))

    normalized: list[dict[str, Any]] = []
    peak = 0
    for epoch, bucket in sorted(buckets.items()):
        observed = float(bucket["observed_seconds"])
        if observed <= 0:
            continue
        groups = {
            group_id: int(round(int(value) * 60.0 / observed))
            for group_id, value in bucket["groups"].items()
        }
        proxy_rate = int(round(int(bucket["proxy_external"]) * 60.0 / observed))
        peak = max([peak, proxy_rate, *groups.values()])
        normalized.append({
            "epoch": epoch,
            "groups": groups,
            "proxy_external": proxy_rate,
        })
    return {
        "unit": "bytes_per_minute",
        "window_minutes": window_minutes,
        "buckets": normalized,
        "peak_bytes_per_minute": peak,
    }
