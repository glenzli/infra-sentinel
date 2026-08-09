"""Adapts existing collectors into the first generic Infra Sentinel projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ai_usage_contract import AI_USAGE_SNAPSHOT_SCHEMA, window_tokens
from infra_collectors import CollectorRun
from infra_model import MetricPoint, SourceStatus
from infra_registry import DEFAULT_SOURCE_REGISTRY


PROJECTION_SCHEMA = "20260809.3"


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


def _ai_usage_resource(runs: Iterable[CollectorRun]) -> tuple[dict[str, Any] | None, list[SourceStatus], list[dict[str, Any]]]:
    """Project all valid AI provider snapshots without provider-name branches."""
    snapshots: list[dict[str, Any]] = []
    for run in runs:
        snapshot = run.snapshot
        if run.capability.resource_id != "ai_usage" or not isinstance(snapshot, dict):
            continue
        if snapshot.get("schema") != AI_USAGE_SNAPSHOT_SCHEMA or not snapshot.get("available"):
            continue
        snapshots.append({**snapshot, "source_id": run.capability.source_id})
    if not snapshots:
        return None, [], []
    aggregate = _ai_usage_aggregate(snapshots)
    source_ids = [str(snapshot["source_id"]) for snapshot in snapshots]
    online = [snapshot for snapshot in snapshots if str(snapshot.get("status") or "waiting") == "ok"]
    status = "ok" if online else str(snapshots[0].get("status") or "waiting")
    today = aggregate["today"]
    resource = {
        "id": "ai_usage",
        "status": status,
        "enabled": True,
        "primary_metric": "ai.tokens.total",
        "primary_value": _number(today.get("tokens")),
        "primary_unit": "tokens",
        "primary_source_id": "ai_usage.aggregate",
        "source_count": len(snapshots),
        "online_source_count": len(online),
    }
    sources = [SourceStatus(
        id=str(snapshot["source_id"]),
        kind=f"ai.{snapshot['source_id']}",
        resource_id="ai_usage",
        enabled=True,
        status=str(snapshot.get("status") or _collector_status(str(snapshot["source_id"]), "waiting", runs)),
        label=str(snapshot.get("label") or snapshot["source_id"]),
        updated_at=snapshot.get("observed_at") if isinstance(snapshot.get("observed_at"), str) else None,
    ) for snapshot in snapshots]
    return resource, sources, snapshots


def _ai_usage_aggregate(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only windows explicitly made available by each provider."""
    windows: dict[str, dict[str, Any]] = {}
    for window in ("today", "cumulative"):
        values = {
            str(snapshot["source_id"]): value
            for snapshot in snapshots
            if (value := window_tokens(snapshot, window)) is not None
        }
        windows[window] = {
            "tokens": sum(values.values()),
            "sources": list(values),
            "source_count": len(values),
        }
    return {
        "schema": AI_USAGE_SNAPSHOT_SCHEMA,
        "today": windows["today"],
        "cumulative": windows["cumulative"],
        "label": "local-usage-rollup",
    }


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
    daily_usage_guards = remote.get("daily_usage_guards") if isinstance(remote.get("daily_usage_guards"), list) else []
    active_daily_guards = sum(
        1 for guard in daily_usage_guards
        if isinstance(guard, dict) and guard.get("level") in {"warning", "critical"}
    )
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
    ai_resource, ai_sources, ai_snapshots = _ai_usage_resource(collector_runs)
    ai_aggregate = _ai_usage_aggregate(ai_snapshots)
    sources.extend(ai_sources)
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
    resources = [{
        "id": "network",
        "status": _status_for(alert_level, remote, collector_runs),
        "enabled": True,
        "primary_metric": "network.billable_bytes" if billing_available else "network.local_bytes",
        "primary_value": primary_total,
        "primary_unit": "bytes",
        "primary_source_id": primary_source,
        "source_count": len(active_sources),
        "online_source_count": len(online_sources),
    }]
    if ai_resource:
        resources.append(ai_resource)
        metrics.append(MetricPoint(
            observed_at=timestamp,
            metric="ai.tokens.total",
            instrument="gauge",
            value=ai_resource["primary_value"],
            unit="tokens",
            source_id="ai_usage.aggregate",
            resource_id="ai_usage",
            dimensions={"window": "today"},
            attribution_method="exact",
            confidence="high",
        ).as_dict())
    return {
        "schema": PROJECTION_SCHEMA,
        "product": {"id": "infra-sentinel", "mode": "network"},
        "overall": {
            "status": _status_for(alert_level, remote, collector_runs),
            "active_alerts": active_daily_guards or (0 if alert_level == "none" else 1),
        },
        "resources": resources,
        "sources": source_dicts,
        "metrics": metrics,
        "capabilities": DEFAULT_SOURCE_REGISTRY.capabilities(),
        "collectors": [run.as_dict() for run in collector_runs],
        "ai_usage": {"schema": AI_USAGE_SNAPSHOT_SCHEMA, "sources": ai_snapshots, "aggregate": ai_aggregate} if ai_resource else {},
    }
