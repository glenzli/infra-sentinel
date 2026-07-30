"""Aligned VPS/Xray billing analysis and minute-normalized local traffic trends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sample_timing import DEFAULT_EXPECTED_INTERVAL_SECONDS, sample_is_realtime


TREND_WINDOW_MINUTES = 15
TYPICAL_TCP_FRAME_OVERHEAD_BYTES = 66
BILLING_MODES = ("both", "outbound")


@dataclass(frozen=True)
class TrafficEstimationConfig:
    billing_mode: str = "both"

    def __post_init__(self) -> None:
        if self.billing_mode not in BILLING_MODES:
            raise ValueError("billing_mode must be both or outbound")

    @property
    def vps_billing_legs(self) -> float:
        return 2.0 if self.billing_mode == "both" else 1.0

    def billable_bytes(self, incoming: int, outgoing: int) -> int:
        return incoming + outgoing if self.billing_mode == "both" else outgoing

    def billable_packets(self, incoming: int, outgoing: int) -> int:
        return incoming + outgoing if self.billing_mode == "both" else outgoing


def estimate_traffic(
    vps_billable_bytes: int,
    vps_packet_count: int,
    packet_covered_bytes: int,
    xray_logical_bytes: int,
    vps_ready: bool,
    xray_ready: bool,
    config: TrafficEstimationConfig,
) -> dict[str, Any]:
    """Measure bill expansion and estimate packet versus connection overhead."""
    empirical_ready = bool(vps_ready and xray_ready and xray_logical_bytes > 0)
    result: dict[str, Any] = {
        "method": "xray_empirical" if empirical_ready else "waiting_for_aligned_xray",
        "billing_mode": config.billing_mode,
        "vps_billing_legs": config.vps_billing_legs,
        "xray_logical_bytes": xray_logical_bytes,
        "empirical_ready": empirical_ready,
        "observed_multiplier": None,
        "ideal_billable_bytes": None,
        "billable_overhead_bytes": None,
        "billable_overhead_ratio": None,
        "billable_overhead_share": None,
        "packet_breakdown_ready": False,
        "average_packet_bytes": None,
        "packet_overhead_estimated_bytes": None,
        "packet_overhead_share_of_bill": None,
        "connection_overhead_estimated_bytes": None,
        "connection_overhead_share_of_bill": None,
    }
    if not empirical_ready:
        return result

    ideal_billable = int(round(xray_logical_bytes * config.vps_billing_legs))
    overhead_bytes = max(0, vps_billable_bytes - ideal_billable)
    overhead_share = overhead_bytes / vps_billable_bytes if vps_billable_bytes > 0 else 0.0
    result.update({
        "observed_multiplier": vps_billable_bytes / xray_logical_bytes,
        "ideal_billable_bytes": ideal_billable,
        "billable_overhead_bytes": overhead_bytes,
        "billable_overhead_ratio": overhead_bytes / ideal_billable if ideal_billable > 0 else 0.0,
        "billable_overhead_share": overhead_share,
    })

    if vps_packet_count <= 0 or packet_covered_bytes <= 0:
        return result

    average_packet_bytes = packet_covered_bytes / vps_packet_count
    sampled_packet_share = min(
        1.0,
        (vps_packet_count * TYPICAL_TCP_FRAME_OVERHEAD_BYTES) / packet_covered_bytes,
    )
    packet_share = min(overhead_share, sampled_packet_share)
    packet_bytes = min(overhead_bytes, int(round(vps_billable_bytes * packet_share)))
    connection_bytes = max(0, overhead_bytes - packet_bytes)
    result.update({
        "packet_breakdown_ready": True,
        "average_packet_bytes": average_packet_bytes,
        "packet_overhead_estimated_bytes": packet_bytes,
        "packet_overhead_share_of_bill": packet_bytes / vps_billable_bytes if vps_billable_bytes > 0 else 0.0,
        "connection_overhead_estimated_bytes": connection_bytes,
        "connection_overhead_share_of_bill": connection_bytes / vps_billable_bytes if vps_billable_bytes > 0 else 0.0,
    })
    return result


def minute_rate_trend(
    history: Iterable[dict[str, Any]],
    service_ids: Iterable[str],
    window_minutes: int = TREND_WINDOW_MINUTES,
    expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Aggregate uneven local samples into comparable bytes-per-minute rates."""
    points = [
        point
        for point in history
        if isinstance(point.get("epoch"), (int, float))
        and sample_is_realtime(point, expected_interval_seconds)
    ]
    ids = tuple(service_ids)
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
                "services": {service_id: 0 for service_id in ids},
                "mihomo_total": 0,
                "proxy_observed": 0,
                "unattributed": 0,
            },
        )
        observed = max(0.0, float(point.get("observed_seconds", 0.0)))
        bucket["observed_seconds"] += observed
        raw_services = point.get("services", {})
        for service_id in ids:
            bucket["services"][service_id] += int(raw_services.get(service_id, 0))
        bucket["mihomo_total"] += int(point.get("mihomo_total", 0))
        bucket["proxy_observed"] += int(point.get("proxy_observed", 0))
        bucket["unattributed"] += int(point.get("unattributed", 0))

    normalized: list[dict[str, Any]] = []
    peak = 0
    for epoch, bucket in sorted(buckets.items()):
        observed = float(bucket["observed_seconds"])
        if observed <= 0:
            continue
        services = {
            service_id: int(round(int(value) * 60.0 / observed))
            for service_id, value in bucket["services"].items()
        }
        mihomo_rate = int(round(int(bucket["mihomo_total"]) * 60.0 / observed))
        proxy_rate = int(round(int(bucket["proxy_observed"]) * 60.0 / observed))
        unattributed_rate = int(round(int(bucket["unattributed"]) * 60.0 / observed))
        peak = max([peak, mihomo_rate, proxy_rate, unattributed_rate, *services.values()])
        normalized.append({
            "epoch": epoch,
            "services": services,
            "mihomo_total": mihomo_rate,
            "proxy_observed": proxy_rate,
            "unattributed": unattributed_rate,
        })
    return {
        "unit": "bytes_per_minute",
        "window_minutes": window_minutes,
        "buckets": normalized,
        "peak_bytes_per_minute": peak,
    }
