"""Adapts existing collectors into the first generic Infra Sentinel projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from infra_sentinel.app.protocol import PROJECTION_SCHEMA
from infra_sentinel.resources.ai.contract import AI_USAGE_SNAPSHOT_SCHEMA, window_tokens
from infra_sentinel.core.collectors import CollectorRun
from infra_sentinel.core.model import MetricPoint, SourceStatus
from infra_sentinel.core.registry import DEFAULT_SOURCE_REGISTRY


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


def _network_status(level: str, remote: dict[str, Any], sources: Iterable[SourceStatus]) -> str:
    if level == "critical":
        return "critical"
    if level == "warning":
        return "warning"
    if remote.get("enabled") and remote.get("status") == "error":
        return "degraded"
    # Waiting/baseline is normal startup coverage, not a confirmed failure.
    # The source remains visibly offline in its own row, but must not turn the
    # whole Network resource into a fault before a first aligned sample exists.
    if any(source.enabled and source.status not in {"ok", "waiting", "baseline"} for source in sources):
        return "degraded"
    return "healthy"


def _overall_status(base: str, facilities: dict[str, Any]) -> str:
    facility_status = str(facilities.get("status") or "disabled")
    rank = {"healthy": 0, "starting": 0, "disabled": 0, "warning": 1, "degraded": 1, "critical": 2}
    return facility_status if rank.get(facility_status, 1) > rank.get(base, 1) else base


def _overall_with_upstream(base: str, upstream: dict[str, Any]) -> str:
    """Let confirmed upstream incidents affect health, but not read failures."""
    upstream_status = str(upstream.get("status") or "degraded")
    if upstream_status not in {"warning", "critical"}:
        return base
    rank = {"healthy": 0, "degraded": 1, "warning": 2, "critical": 3}
    return upstream_status if rank.get(upstream_status, 0) > rank.get(base, 0) else base


def _highest_status(*statuses: str) -> str:
    rank = {"healthy": 0, "starting": 0, "disabled": 0, "degraded": 1, "warning": 2, "critical": 3}
    return max(statuses, key=lambda status: rank.get(status, 1), default="healthy")


def _system_resource(
    runs: Iterable[CollectorRun],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, SourceStatus | None]:
    for run in runs:
        if run.capability.resource_id != "system" or not isinstance(run.snapshot, dict):
            continue
        snapshot = run.snapshot
        if not snapshot.get("available"):
            continue
        disk = snapshot.get("disk") if isinstance(snapshot.get("disk"), dict) else {}
        cpu = snapshot.get("cpu") if isinstance(snapshot.get("cpu"), dict) else {}
        capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), list) else []
        has_disk_capacity = "disk.capacity" in capabilities
        primary_metric = "system.disk.free_bytes" if has_disk_capacity else "system.cpu.percent"
        primary_value = _number(disk.get("free_bytes")) if has_disk_capacity else _number(cpu.get("percent"))
        primary_unit = "bytes" if has_disk_capacity else "percent"
        health = str(snapshot.get("status") or "degraded")
        source_status = "error" if run.status == "error" else "degraded" if run.status == "degraded" else "ok"
        platform = str(snapshot.get("platform") or "host")
        source_label = {"macos": "This Mac", "windows": "This Windows PC", "linux": "This Linux host"}.get(platform, "Local host")
        return snapshot, {
            "id": "system",
            "category": "runtime",
            "status": health,
            "enabled": True,
            "primary_metric": primary_metric,
            "primary_value": primary_value,
            "primary_unit": primary_unit,
            "primary_source_id": run.capability.source_id,
            "source_count": 1,
            "online_source_count": 1 if run.status in {"ok", "degraded"} else 0,
        }, SourceStatus(
            id=run.capability.source_id,
            kind=run.capability.source_kind,
            resource_id="system",
            enabled=True,
            status=source_status,
            label=source_label,
            updated_at=snapshot.get("observed_at") if isinstance(snapshot.get("observed_at"), str) else None,
        )
    return None, None, None


def _upstream_resource(upstream: dict[str, Any]) -> tuple[dict[str, Any], list[SourceStatus]]:
    items = upstream.get("items") if isinstance(upstream.get("items"), list) else []
    sources = [SourceStatus(
        id=f"upstream:{item.get('id', 'unknown')}",
        kind="upstream.statuspage",
        resource_id="upstream_status",
        enabled=True,
        status="ok" if item.get("available") else "error",
        label=str(item.get("label") or item.get("id") or "Upstream"),
        updated_at=item.get("observed_at") if isinstance(item.get("observed_at"), str) else None,
    ) for item in items if isinstance(item, dict)]
    return {
        "id": "upstream_status",
        "category": "dependency",
        "status": str(upstream.get("status") or "degraded"),
        "enabled": True,
        "primary_metric": "upstream.providers.healthy",
        "primary_value": _number(upstream.get("healthy")),
        "primary_unit": "providers",
        "primary_source_id": "upstream_status.aggregate",
        "source_count": len(sources),
        "online_source_count": sum(source.status == "ok" for source in sources),
    }, sources


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
        "category": "usage",
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
    facilities: dict[str, Any] | None = None,
    upstream_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a generic overview without changing existing network accounting."""

    timestamp = str(sample.get("timestamp") or "")
    facility_state = facilities if isinstance(facilities, dict) else {
        "schema": "infra.discovery.registration@20260812.1",
        "status": "disabled", "total": 0, "healthy": 0, "attention": 0, "items": [],
    }
    upstream_state = upstream_status if isinstance(upstream_status, dict) else {
        "schema": "20260811.1", "status": "degraded", "total": 0, "healthy": 0,
        "attention": 0, "unknown": 0, "items": [],
    }
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
    network_alert = {
        # The resource level also includes per-host daily guards.  Keep the
        # sliding-window diagnostic independent so the UI never attributes a
        # daily billing warning to the realtime traffic window.
        "level": str(session.get("traffic_alert_level") or "none"),
        "windows": {
            "warning": session.get("alert_windows", {}).get("warning", {})
            if isinstance(session.get("alert_windows"), dict) else {},
            "critical": session.get("alert_windows", {}).get("critical", {})
            if isinstance(session.get("alert_windows"), dict) else {},
        },
        "window_seconds": session.get("alert_window_seconds", {})
        if isinstance(session.get("alert_window_seconds"), dict) else {},
        "threshold_bytes": session.get("alert_threshold_bytes", {})
        if isinstance(session.get("alert_threshold_bytes"), dict) else {},
    }
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
    system_snapshot, system_resource, system_source = _system_resource(collector_runs)
    if system_source:
        sources.append(system_source)
    upstream_resource, upstream_sources = _upstream_resource(upstream_state)
    sources.extend(upstream_sources)
    source_dicts = [source.as_dict() for source in sources]
    network_sources = [source for source in sources if source.resource_id == "network" and source.enabled]
    online_network_sources = [source for source in network_sources if source.status == "ok"]
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
        "category": "usage",
        "status": _network_status(alert_level, remote, network_sources),
        "enabled": True,
        "primary_metric": "network.billable_bytes" if billing_available else "network.local_bytes",
        "primary_value": primary_total,
        "primary_unit": "bytes",
        "primary_source_id": primary_source,
        "source_count": len(network_sources),
        "online_source_count": len(online_network_sources),
    }]
    if system_resource:
        resources.append(system_resource)
        metrics.append(MetricPoint(
            observed_at=timestamp,
            metric=str(system_resource["primary_metric"]),
            instrument="gauge",
            value=system_resource["primary_value"],
            unit=str(system_resource["primary_unit"]),
            source_id=system_resource["primary_source_id"],
            resource_id="system",
        ).as_dict())
    if upstream_resource["source_count"]:
        resources.append(upstream_resource)
        metrics.append(MetricPoint(
            observed_at=timestamp,
            metric="upstream.providers.healthy",
            instrument="gauge",
            value=upstream_resource["primary_value"],
            unit="providers",
            source_id="upstream_status.aggregate",
            resource_id="upstream_status",
        ).as_dict())
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
    base_status = _overall_with_upstream(_network_status(alert_level, remote, network_sources), upstream_state)
    if system_resource:
        base_status = _highest_status(base_status, str(system_resource["status"]))
    system_alerts = 1 if system_resource and system_resource["status"] in {"warning", "critical"} else 0
    return {
        "schema": PROJECTION_SCHEMA,
        "product": {"id": "infra-sentinel", "mode": "infra"},
        "overall": {
            "status": _overall_status(base_status, facility_state),
            "active_alerts": active_daily_guards + (0 if network_alert["level"] == "none" else 1)
            + _number(facility_state.get("attention")) + _number(upstream_state.get("attention")) + system_alerts,
        },
        "resources": resources,
        "sources": source_dicts,
        "metrics": metrics,
        "capabilities": DEFAULT_SOURCE_REGISTRY.capabilities(),
        "collectors": [run.as_dict() for run in collector_runs],
        "facilities": facility_state,
        "network_diagnostics": {
            "traffic_alert": network_alert,
            "daily_usage_guards": daily_usage_guards,
        },
        "upstream_status": upstream_state,
        "ai_usage": {"schema": AI_USAGE_SNAPSHOT_SCHEMA, "sources": ai_snapshots, "aggregate": ai_aggregate} if ai_resource else {},
        "system": system_snapshot or {},
    }
