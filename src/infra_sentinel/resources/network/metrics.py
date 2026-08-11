"""Network collector adapter for the canonical Infra Sentinel metric model.

Only aggregate directions, routes, and configured user labels cross this
boundary. Hosts, URL paths, payloads, prompts, and connection identifiers do
not become stored metric dimensions.
"""

from __future__ import annotations

from typing import Any, Iterable

from infra_sentinel.core.collectors import CallableCollector, CollectorCapability, CollectorContext, CollectorRegistry
from infra_sentinel.core.model import MetricPoint


NETWORK_METRIC_SCHEMA = "20260809.1"


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
    # Service ids are stable, bounded labels emitted by the local attribution
    # collector. They are stored without connection IDs, full URLs, or hosts.
    for service in sample.get("services", []):
        if not isinstance(service, dict):
            continue
        service_id = str(service.get("id") or "unknown")
        label = str(service.get("label") or service_id)
        for direction in ("up", "down"):
            points.append(_point(
                timestamp, epoch, "network.service_bytes", service.get(f"{direction}_bytes"), "local-mihomo",
                dimensions={"service": service_id, "label": label, "direction": direction},
                attribution_method="mapped" if service_id != "unattributed" else "residual",
                confidence="high" if service_id != "unattributed" else "medium",
            ))
    return points


def vps_sample_metrics(server_id: str, sample: dict[str, Any], billing_mode: str = "both") -> list[MetricPoint]:
    timestamp = str(sample.get("timestamp") or "")
    epoch = sample.get("epoch", 0)
    points = [_point(timestamp, epoch, "network.billable_bytes", sample.get("out_bytes"), f"vps:{server_id}", dimensions={"direction": "out"})]
    if billing_mode != "outbound":
        points.insert(0, _point(timestamp, epoch, "network.billable_bytes", sample.get("in_bytes"), f"vps:{server_id}", dimensions={"direction": "in"}))
    return points


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
            points.extend(vps_sample_metrics(server_id, vps_sample, str(server.get("billing_mode") or "both")))
        xray = server.get("xray_stats") if isinstance(server.get("xray_stats"), dict) else {}
        xray_sample = xray.get("last_sample") if isinstance(xray.get("last_sample"), dict) else None
        if xray_sample:
            points.extend(xray_sample_metrics(server_id, xray_sample))
    return points


def _server(remote: dict[str, Any], server_id: str) -> dict[str, Any] | None:
    for candidate in remote.get("servers", []):
        if isinstance(candidate, dict) and str(candidate.get("id") or "") == server_id:
            return candidate
    return None


def _vps_collector(server_id: str, billing_mode: str) -> CallableCollector:
    return CallableCollector(
        capability=CollectorCapability(
            id=f"network.vps:{server_id}",
            source_id=f"vps:{server_id}",
            source_kind="network.linux-vps",
            resource_id="network",
            metrics=("network.billable_bytes",),
        ),
        collect=lambda context: _vps_metrics_for_server(context, server_id, billing_mode),
    )


def _xray_collector(server_id: str) -> CallableCollector:
    return CallableCollector(
        capability=CollectorCapability(
            id=f"network.xray:{server_id}",
            source_id=f"xray:{server_id}",
            source_kind="network.xray",
            resource_id="network",
            metrics=("network.logical_bytes",),
        ),
        collect=lambda context: _xray_metrics_for_server(context, server_id),
    )


def _vps_metrics_for_server(context: CollectorContext, server_id: str, billing_mode: str) -> Iterable[MetricPoint]:
    server = _server(context.remote_state, server_id)
    vps = server.get("vps") if isinstance(server, dict) else None
    sample = vps.get("last_sample") if isinstance(vps, dict) else None
    return vps_sample_metrics(server_id, sample, billing_mode) if isinstance(sample, dict) else ()


def _xray_metrics_for_server(context: CollectorContext, server_id: str) -> Iterable[MetricPoint]:
    server = _server(context.remote_state, server_id)
    xray = server.get("xray_stats") if isinstance(server, dict) else None
    sample = xray.get("last_sample") if isinstance(xray, dict) else None
    return xray_sample_metrics(server_id, sample) if isinstance(sample, dict) else ()


def network_collector_registry(servers: Iterable[tuple[str, str]]) -> CollectorRegistry:
    """Register exact network adapters; one remote source cannot block another."""
    collectors = [CallableCollector(
        capability=CollectorCapability(
            id="network.mihomo",
            source_id="local-mihomo",
            source_kind="network.mihomo",
            resource_id="network",
            metrics=("network.bytes", "network.route_bytes"),
        ),
        collect=lambda context: local_sample_metrics(context.local_sample),
    )]
    configured = {str(server_id): str(billing_mode) for server_id, billing_mode in servers if str(server_id)}
    for server_id, billing_mode in configured.items():
        collectors.extend((_vps_collector(server_id, billing_mode), _xray_collector(server_id)))
    return CollectorRegistry(collectors)


def network_metrics(sample: dict[str, Any], remote: dict[str, Any]) -> Iterable[MetricPoint]:
    """Legacy functional facade kept for import and fixture equivalence checks."""
    yield from local_sample_metrics(sample)
    yield from remote_state_metrics(remote)
