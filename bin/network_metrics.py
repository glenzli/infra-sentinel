"""Network collector adapter for the canonical Infra Sentinel metric model.

Only aggregate directions, routes, and configured user labels cross this
boundary. Hosts, URL paths, payloads, prompts, and connection identifiers do
not become stored metric dimensions.
"""

from __future__ import annotations

from typing import Any, Iterable

from infra_model import MetricPoint


NETWORK_METRIC_SCHEMA = "20260808.2"


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _point(
    timestamp: str,
    epoch: Any,
    metric: str,
    value: Any,
    source_id: str,
    *,
    dimensions: dict[str, str],
    attribution_method: str = "exact",
    confidence: str = "high",
) -> MetricPoint:
    return MetricPoint(
        observed_at=timestamp,
        observed_epoch=float(epoch),
        metric=metric,
        instrument="counter",
        value=_number(value),
        unit="bytes",
        source_id=source_id,
        resource_id="network",
        dimensions=dimensions,
        attribution_method=attribution_method,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
    )


def local_sample_metrics(sample: dict[str, Any]) -> list[MetricPoint]:
    """Convert one exact Mihomo interval into aggregate canonical metrics."""
    timestamp = str(sample.get("timestamp") or "")
    epoch = sample.get("epoch", 0)
    kernel = sample.get("kernel") if isinstance(sample.get("kernel"), dict) else {}
    points = [
        _point(timestamp, epoch, "network.bytes", kernel.get("up_bytes"), "local-mihomo", dimensions={"direction": "up"}),
        _point(timestamp, epoch, "network.bytes", kernel.get("down_bytes"), "local-mihomo", dimensions={"direction": "down"}),
    ]
    routes = sample.get("routes") if isinstance(sample.get("routes"), dict) else {}
    for route_id, traffic in routes.items():
        if not isinstance(traffic, dict):
            continue
        for direction in ("up", "down"):
            points.append(_point(timestamp, epoch, "network.route_bytes", traffic.get(f"{direction}_bytes"), "local-mihomo",
                                 dimensions={"route": str(route_id), "direction": direction},
                                 attribution_method="residual" if route_id == "unattributed" else "mapped",
                                 confidence="medium" if route_id == "unattributed" else "high"))
    return points


def vps_sample_metrics(server_id: str, sample: dict[str, Any]) -> list[MetricPoint]:
    timestamp = str(sample.get("timestamp") or "")
    epoch = sample.get("epoch", 0)
    return [
        _point(timestamp, epoch, "network.billable_bytes", sample.get("in_bytes"), f"vps:{server_id}", dimensions={"direction": "in"}),
        _point(timestamp, epoch, "network.billable_bytes", sample.get("out_bytes"), f"vps:{server_id}", dimensions={"direction": "out"}),
    ]


def xray_sample_metrics(server_id: str, sample: dict[str, Any]) -> list[MetricPoint]:
    timestamp = str(sample.get("timestamp") or "")
    epoch = sample.get("epoch", 0)
    users = sample.get("users") if isinstance(sample.get("users"), dict) else {}
    points: list[MetricPoint] = []
    for user_id, traffic in users.items():
        if not isinstance(traffic, dict):
            continue
        for direction in ("up", "down"):
            points.append(_point(timestamp, epoch, "network.logical_bytes", traffic.get(f"{direction}_bytes"), f"xray:{server_id}",
                                 dimensions={"client": str(user_id), "direction": direction}, attribution_method="exact"))
    return points


def remote_state_metrics(remote: dict[str, Any]) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    for server in remote.get("servers", []):
        if not isinstance(server, dict):
            continue
        server_id = str(server.get("id") or "remote")
        vps = server.get("vps") if isinstance(server.get("vps"), dict) else {}
        vps_sample = vps.get("last_sample") if isinstance(vps.get("last_sample"), dict) else None
        if vps_sample:
            points.extend(vps_sample_metrics(server_id, vps_sample))
        xray = server.get("xray_stats") if isinstance(server.get("xray_stats"), dict) else {}
        xray_sample = xray.get("last_sample") if isinstance(xray.get("last_sample"), dict) else None
        if xray_sample:
            points.extend(xray_sample_metrics(server_id, xray_sample))
    return points


def network_metrics(sample: dict[str, Any], remote: dict[str, Any]) -> Iterable[MetricPoint]:
    yield from local_sample_metrics(sample)
    yield from remote_state_metrics(remote)
