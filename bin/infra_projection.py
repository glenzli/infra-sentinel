"""Adapts existing collectors into the first generic Infra Sentinel projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from infra_collectors import CollectorRun
from infra_model import MetricPoint, SourceStatus
from infra_registry import DEFAULT_SOURCE_REGISTRY


PROJECTION_SCHEMA = "20260808.1"


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _collector_status(source_id: str, configured_status: str, runs: Iterable[CollectorRun]) -> str:
    for run in runs:
        if run.capability.source_id == source_id and run.status == "error":
            return "error"
    return configured_status


def _status_for(level: str, remote: dict[str, Any], runs: Iterable[CollectorRun]) -> str:
    if level == "critical":
        return "critical"
    if level == "warning":
        return "warning"
    if any(run.status == "error" for run in runs):
        return "degraded"
    if remote.get("enabled") and remote.get("status") == "error":
        return "degraded"
    return "healthy"


def _remote_sources(remote: dict[str, Any], runs: Iterable[CollectorRun]) -> list[SourceStatus]:
    sources: list[SourceStatus] = []
    for server in remote.get("servers", []):
        if not isinstance(server, dict):
            continue
        server_id = str(server.get("id") or "remote")
        vps = server.get("vps") if isinstance(server.get("vps"), dict) else {}
        enabled = bool(vps.get("enabled"))
        sources.append(SourceStatus(
            id=f"vps:{server_id}",
            kind="network.linux-vps",
            resource_id="network",
            enabled=enabled,
            status=_collector_status(
                f"vps:{server_id}",
                str(vps.get("status") or ("waiting" if enabled else "disabled")),
                runs,
            ),
            label=str(server.get("label") or server_id),
            updated_at=vps.get("updated_at") if isinstance(vps.get("updated_at"), str) else None,
        ))
        xray = server.get("xray_stats")
        if isinstance(xray, dict) and xray.get("enabled"):
            sources.append(SourceStatus(
                id=f"xray:{server_id}",
                kind="network.xray",
                resource_id="network",
                enabled=True,
                status=_collector_status(f"xray:{server_id}", str(xray.get("status") or "waiting"), runs),
                label=str(server.get("label") or server_id),
                updated_at=xray.get("updated_at") if isinstance(xray.get("updated_at"), str) else None,
            ))
    return sources


def build_infra_projection(
    sample: dict[str, Any],
    session: dict[str, Any],
    remote: dict[str, Any],
    alert_level: str,
    collector_runs: tuple[CollectorRun, ...] = (),
) -> dict[str, Any]:
    """Produce a generic overview without changing existing network accounting."""

    timestamp = str(sample.get("timestamp") or "")
    kernel = session.get("kernel") if isinstance(session.get("kernel"), dict) else {}
    vps = session.get("vps") if isinstance(session.get("vps"), dict) else {}
    local_total = _number(kernel.get("total_bytes"))
    vps_total = _number(vps.get("total_bytes"))
    billing_available = bool(remote.get("enabled"))
    primary_total = vps_total if billing_available else local_total
    primary_source = "network.vps-billing" if billing_available else "local-mihomo"
    sources = [SourceStatus(
        id="local-mihomo",
        kind="network.mihomo",
        resource_id="network",
        enabled=True,
        status=_collector_status("local-mihomo", "ok", collector_runs),
        label="Mihomo",
        updated_at=timestamp or None,
    )]
    sources.extend(_remote_sources(remote, collector_runs))
    source_dicts = [source.as_dict() for source in sources]
    active_sources = [source for source in sources if source.enabled]
    online_sources = [source for source in active_sources if source.status == "ok"]
    metrics = [
        MetricPoint(
            observed_at=timestamp,
            metric="network.local_bytes",
            instrument="counter",
            value=local_total,
            unit="bytes",
            source_id="local-mihomo",
            resource_id="network",
        ).as_dict(),
    ]
    if billing_available:
        metrics.append(MetricPoint(
            observed_at=timestamp,
            metric="network.billable_bytes",
            instrument="counter",
            value=vps_total,
            unit="bytes",
            source_id="network.vps-billing",
            resource_id="network",
            dimensions={"scope": "fleet"},
        ).as_dict())
    return {
        "schema": PROJECTION_SCHEMA,
        "product": {"id": "infra-sentinel", "mode": "network"},
        "overall": {
            "status": _status_for(alert_level, remote, collector_runs),
            "active_alerts": 0 if alert_level == "none" else 1,
        },
        "resources": [{
            "id": "network",
            "status": _status_for(alert_level, remote, collector_runs),
            "enabled": True,
            "primary_metric": "network.billable_bytes" if billing_available else "network.local_bytes",
            "primary_value": primary_total,
            "primary_unit": "bytes",
            "primary_source_id": primary_source,
            "source_count": len(active_sources),
            "online_source_count": len(online_sources),
        }],
        "sources": source_dicts,
        "metrics": metrics,
        "capabilities": DEFAULT_SOURCE_REGISTRY.capabilities(),
        "collectors": [run.as_dict() for run in collector_runs],
    }
