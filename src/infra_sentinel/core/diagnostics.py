"""Pure, privacy-bounded diagnostic checks over the existing Agent Projection.

A check with no severity is evidence of recovery only when its observation
status is ``observed``. Missing, disabled, stale, and unknown facts never say
that an earlier condition recovered. No raw error, label, incident text, URL,
path, or arbitrary provider issue code crosses this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from typing import Any


DIAGNOSTIC_SCHEMA = "20261022.1"
CATEGORIES = frozenset({"source", "network", "system", "facility", "upstream"})
SEVERITIES = frozenset({"info", "warning", "critical"})
OBSERVATION_STATUSES = frozenset({"observed", "unknown", "disabled", "unobserved"})
MAX_NORMALIZED_CHECKS = 512
MAX_SOURCE_ITEMS = 128
FRESHNESS_SECONDS = 60

# Text is an owned vocabulary, never copied from a monitored resource.
# Entries are title, Chinese title, detail, Chinese detail.
CODE_TEXT = {
    "source.collection_failed": (
        "Source collection failed", "来源采集失败",
        "An existing source could not be collected.", "现有来源无法完成采集。",
    ),
    "network.traffic_high": (
        "High network traffic", "网络流量偏高",
        "The confirmed traffic window exceeds a configured limit.", "已确认的流量窗口超过配置阈值。",
    ),
    "network.daily_usage_high": (
        "High daily network usage", "每日网络用量偏高",
        "The existing daily billing guard is active.", "现有每日计费用量守卫处于告警状态。",
    ),
    "system.memory_pressure": (
        "Memory pressure", "内存压力",
        "The system collector confirmed elevated memory pressure.", "系统采集器已确认内存压力升高。",
    ),
    "system.disk_space": (
        "Low disk space", "磁盘空间不足",
        "The system collector confirmed low free disk capacity.", "系统采集器已确认磁盘剩余空间不足。",
    ),
    "system.thermal_pressure": (
        "Thermal pressure", "温度压力",
        "The system collector confirmed thermal pressure.", "系统采集器已确认温度压力。",
    ),
    "system.disk_health": (
        "Disk health attention", "磁盘健康需关注",
        "An existing native disk health signal needs attention.", "现有原生磁盘健康信号需要关注。",
    ),
    "system.disk_smart": (
        "Disk SMART attention", "磁盘 SMART 需关注",
        "An already available SMART health signal needs attention.", "已可用的 SMART 健康信号需要关注。",
    ),
    "facility.discovery_failed": (
        "Facility discovery failed", "设施发现失败",
        "The existing facility registration directory could not be read.", "现有设施注册目录无法读取。",
    ),
    "facility.health": (
        "Facility needs attention", "设施需关注",
        "The facility monitor confirmed a degraded or unavailable state.", "设施监视器已确认降级或不可用状态。",
    ),
    "facility.reported_issue": (
        "Facility reported an issue", "设施报告了问题",
        "The existing facility snapshot contains an issue.", "现有设施快照包含问题。",
    ),
    "dev_mesh.collection.failed": (
        "Observer collection failed", "Observer 采集失败",
        "The facility reported a collection failure.", "设施报告了采集失败。",
    ),
    "dev_mesh.collection.stale": (
        "Observer collection is stale", "Observer 采集已过期",
        "The facility reported stale collection evidence.", "设施报告了过期的采集证据。",
    ),
    "dev_mesh.workspace.unavailable": (
        "Observer workspace unavailable", "Observer 工作区不可用",
        "The facility reported an unavailable workspace.", "设施报告了不可用的工作区。",
    ),
    "dev_mesh.workspace.none_registered": (
        "Observer has no workspace", "Observer 尚无工作区",
        "The facility reported no registered workspace.", "设施报告尚无已登记的工作区。",
    ),
    "dev_mesh.integrity.issue": (
        "Observer integrity issue", "Observer 完整性问题",
        "The facility reported a coordination integrity issue.", "设施报告了协作状态完整性问题。",
    ),
    "dev_mesh.contention.stalled": (
        "Observer contention stalled", "Observer 争用停滞",
        "The facility reported stalled contention.", "设施报告了停滞的争用。",
    ),
    "dev_mesh.collection.backlog": (
        "Observer collection backlog", "Observer 采集积压",
        "The facility reported a collection backlog.", "设施报告了采集积压。",
    ),
    "upstream.service_incident": (
        "Upstream service incident", "上游服务异常",
        "The official status source reports a service incident.", "官方状态来源报告了服务异常。",
    ),
    "upstream.status_unavailable": (
        "Official status unavailable", "官方状态暂不可读",
        "The official status source could not be read; service health is unknown.",
        "官方状态来源无法读取，服务健康状态未知。",
    ),
}
DEV_MESH_CODES = frozenset(code for code in CODE_TEXT if code.startswith("dev_mesh."))
KNOWN_SOURCE_LABELS = {
    "local-mihomo": "Mihomo", "local-system": "Local system",
    "codex": "Codex", "opencode": "OpenCode", "dsh": "DeepSeek Harness",
    "infer-runtime": "Infer Runtime", "antigravity": "Antigravity",
    "facility-discovery": "Facility discovery",
    "upstream:openai": "OpenAI", "upstream:claude": "Claude",
    "upstream:deepseek": "DeepSeek", "upstream:moonshot": "Kimi / Moonshot",
    "upstream:cursor": "Cursor",
}
SAFE_SOURCE_LABELS = frozenset(KNOWN_SOURCE_LABELS.values()) | frozenset({
    "Observed source", "Remote network source", "Facility", "PCP", "Dev Mesh Observer", "Upstream service",
})


def iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def valid_epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        epoch = float(value)
    except (OverflowError, ValueError):
        return None
    # Bounds make timestamps portable across the supported desktop platforms.
    return epoch if math.isfinite(epoch) and 0 <= epoch <= 253402300799 else None


def _timestamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return valid_epoch(parsed.timestamp()) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identity(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 2048 else None


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def safe_source_id(value: str, namespace: str = "source") -> str:
    """Keep only product-owned IDs readable; hash user/provider identifiers."""
    if value in KNOWN_SOURCE_LABELS:
        return value
    prefix = value.split(":", 1)[0]
    kind = prefix if prefix in {"vps", "xray", "facility", "upstream"} else namespace
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"


def _source_label(source_id: str) -> str:
    if source_id in KNOWN_SOURCE_LABELS:
        return KNOWN_SOURCE_LABELS[source_id]
    if source_id.startswith(("vps:", "xray:")):
        return "Remote network source"
    if source_id.startswith("upstream:"):
        return "Upstream service"
    return "Observed source"


@dataclass(frozen=True)
class DiagnosticCheck:
    id: str
    category: str
    source_id: str
    source_label: str
    code: str
    severity: str | None
    observation_status: str
    observed_epoch: float | None
    freshness_seconds: int = FRESHNESS_SECONDS


@dataclass(frozen=True)
class DiagnosticObservation:
    observed_epoch: float
    checks: tuple[DiagnosticCheck, ...]
    truncated: bool = False


class _Checks:
    def __init__(self, now: float) -> None:
        self.now = now
        self.items: dict[str, DiagnosticCheck] = {}
        self.truncated = False

    def rows(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        if len(value) > MAX_SOURCE_ITEMS:
            self.truncated = True
        return [item for item in value[:MAX_SOURCE_ITEMS] if isinstance(item, dict)]

    def add(
        self, category: str, source_id: str, code: str, severity: str | None,
        status: str = "observed", *, observed_at: Any = None,
        freshness_seconds: int = FRESHNESS_SECONDS, label: str | None = None,
        discriminator: str = "",
    ) -> None:
        identity = "\x1f".join((category, source_id, code, discriminator))
        check_id = "diagnostic:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        if len(self.items) >= MAX_NORMALIZED_CHECKS and check_id not in self.items:
            self.truncated = True
            return
        observed_epoch = _timestamp_epoch(observed_at)
        # A caller without a source timestamp can still assert an observation
        # made by this sampling cycle (for example a confirmed collector error).
        if observed_at is not None and observed_epoch is None:
            status = "unknown"
        if observed_epoch is None and status == "observed":
            observed_epoch = self.now
        if observed_epoch is not None and (
            observed_epoch > self.now + FRESHNESS_SECONDS
            or self.now - observed_epoch > freshness_seconds
        ):
            status = "unknown"
        self.items[check_id] = DiagnosticCheck(
            check_id, category, source_id, label or _source_label(source_id), code,
            severity if status == "observed" else None, status, observed_epoch,
            freshness_seconds,
        )


def _availability(status: Any, enabled: Any = True) -> str:
    if enabled is False or status == "disabled":
        return "disabled"
    return "observed" if isinstance(status, str) and status in {"ok", "degraded", "healthy", "error"} else "unknown"


def _sources(infra: dict[str, Any], checks: _Checks) -> dict[str, dict[str, Any]]:
    sources = {
        raw_id: row for row in checks.rows(infra.get("sources"))
        if (raw_id := _identity(row.get("id"))) is not None
    }
    for run in checks.rows(infra.get("collectors")):
        capability = _object(run.get("capability"))
        raw_id = _identity(capability.get("source_id"))
        if raw_id is None:
            continue
        prior = sources.get(raw_id, {})
        if prior.get("enabled") is False:
            continue
        if raw_id not in sources or run.get("status") == "error":
            sources[raw_id] = {
                **prior,
                "id": raw_id, "enabled": True, "status": run.get("status"),
                "kind": capability.get("source_kind", prior.get("kind")),
                "checked_at": run.get("checked_at") if run.get("checked_at") is not None else prior.get("checked_at"),
            }
        elif run.get("checked_at") is not None:
            sources[raw_id] = {**prior, "checked_at": run["checked_at"]}
    for raw_id, row in sources.items():
        # The upstream owner below distinguishes page-read failures from
        # provider incidents, avoiding a duplicate generic source alert.
        if row.get("kind") == "upstream.statuspage" or raw_id.startswith("upstream:"):
            continue
        availability = _availability(row.get("status"), row.get("enabled"))
        failed = row.get("status") == "error"
        observed_at = row.get("checked_at") if failed else row.get("updated_at")
        if _timestamp_epoch(observed_at) is None and not (failed and raw_id == "local-mihomo" and observed_at is None):
            # Background collectors can cache errors. Their last successful
            # updated_at says nothing about when a failed attempt occurred.
            # Mihomo alone is synchronously collected by this sampling cycle.
            if availability == "observed":
                availability = "unknown"
        checks.add(
            "source", safe_source_id(raw_id), "source.collection_failed",
            "warning" if failed else None, availability, observed_at=observed_at,
            freshness_seconds=900 if raw_id.startswith(("vps:", "xray:")) or raw_id == "antigravity" else FRESHNESS_SECONDS,
        )
    return sources


def _network(infra: dict[str, Any], projection: dict[str, Any], sources: dict[str, dict[str, Any]], checks: _Checks) -> None:
    diagnostics = _object(infra.get("network_diagnostics"))
    traffic = _object(diagnostics.get("traffic_alert"))
    local = sources.get("local-mihomo", {})
    status = _availability(local.get("status"), local.get("enabled")) if local else "unobserved"
    if local.get("status") == "error" or projection.get("interval_kind", "realtime") != "realtime":
        status = "unknown"
    level = traffic.get("level") if isinstance(traffic.get("level"), str) else None
    if level not in {"none", "warning", "critical"}:
        status = "unknown" if traffic else "unobserved"
    candidate = _object(traffic.get("confirmation")).get("candidate_status")
    ranks = {"none": 0, "warning": 1, "critical": 2}
    if isinstance(candidate, str) and ranks.get(candidate, 0) > ranks.get(level or "none", 0):
        # A fresh in-memory stabilizer starts at none. Pending worsening is
        # not evidence that a journal condition from before restart cleared.
        status = "unknown"
    checks.add(
        "network", "local-mihomo", "network.traffic_high",
        level if level in {"warning", "critical"} else None, status,
        observed_at=local.get("updated_at"),
    )
    for guard in checks.rows(diagnostics.get("daily_usage_guards")):
        raw_id = _identity(guard.get("source_id"))
        if raw_id is None:
            continue
        source = sources.get(f"vps:{raw_id}", {})
        status = _availability(source.get("status"), source.get("enabled")) if source else "unobserved"
        if source.get("status") == "error":
            status = "unknown"
        level = guard.get("level") if isinstance(guard.get("level"), str) else None
        if level not in {"none", "warning", "critical"}:
            status = "unknown"
        checks.add(
            "network", safe_source_id(f"vps:{raw_id}"), "network.daily_usage_high",
            level if level in {"warning", "critical"} else None, status,
            observed_at=source.get("updated_at"), freshness_seconds=900,
            discriminator=_identity(guard.get("id")) or raw_id,
        )


_SYSTEM_REASONS = {
    "system.memory_pressure": ("memory_pressure_warning", "memory_pressure_critical"),
    "system.disk_space": ("disk_space_low", "disk_space_critical"),
    "system.thermal_pressure": ("thermal_pressure_serious", "thermal_pressure_critical"),
    "system.disk_health": ("disk_health_warning", "disk_health_critical"),
    "system.disk_smart": ("disk_smart_warning", "disk_smart_critical"),
}


def _system(infra: dict[str, Any], checks: _Checks) -> None:
    system = _object(infra.get("system"))
    capabilities = system.get("capabilities")
    capabilities = {value for value in capabilities if isinstance(value, str)} if isinstance(capabilities, list) else set()
    reasons = system.get("reasons")
    reasons = {value for value in reasons if isinstance(value, str)} if isinstance(reasons, list) else set()
    confirmation = _object(system.get("health_confirmation"))
    pending_reasons = confirmation.get("candidate_reasons")
    pending_reasons = {value for value in pending_reasons if isinstance(value, str)} if isinstance(pending_reasons, list) else set()
    memory, disk, thermal = (_object(system.get(key)) for key in ("memory", "disk", "thermal"))
    health, smart = (_object(disk.get(key)) for key in ("health", "smart"))
    disk_total = _nonnegative_number(disk.get("total_bytes"))
    known = {
        "system.memory_pressure": "memory.pressure" in capabilities and memory.get("pressure") in {"normal", "warning", "critical"},
        "system.disk_space": "disk.capacity" in capabilities and disk_total is not None and disk_total > 0
        and _nonnegative_number(disk.get("free_bytes")) is not None,
        "system.thermal_pressure": "thermal.pressure" in capabilities and thermal.get("state") in {"nominal", "fair", "serious", "critical"},
        "system.disk_health": health.get("state") in {"healthy", "warning", "critical"},
        "system.disk_smart": smart.get("state") in {"healthy", "warning", "critical"},
    }
    available = system.get("available") is True and system.get("status") in {"healthy", "warning", "critical"}
    for code, (warning, critical) in _SYSTEM_REASONS.items():
        severity = "critical" if critical in reasons else "warning" if warning in reasons else None
        status = "observed" if available and known[code] else "unknown" if system else "unobserved"
        # Keep confirmed severity during recovery hysteresis, but an absent
        # individual signal still leaves that family's coverage unknown.
        pending_severity = "critical" if critical in pending_reasons else "warning" if warning in pending_reasons else None
        ranks = {None: 0, "warning": 1, "critical": 2}
        if ranks[pending_severity] > ranks[severity]:
            status = "unknown"
        elif confirmation.get("candidate_status") in {"warning", "critical"} and not pending_reasons and severity is None:
            # Older snapshots may omit per-family pending reasons. They
            # cannot safely establish recovery for an unresolved family.
            status = "unknown"
        source = health if code == "system.disk_health" else smart if code == "system.disk_smart" else system
        interval = source.get("interval_seconds")
        age = min(86400, max(FRESHNESS_SECONDS, int(interval) * 3)) if type(interval) is int and interval > 0 else FRESHNESS_SECONDS
        checks.add(
            "system", "local-system", code, severity, status,
            observed_at=source.get("observed_at", system.get("observed_at")), freshness_seconds=age,
        )


def _facilities(infra: dict[str, Any], checks: _Checks) -> None:
    facilities = _object(infra.get("facilities"))
    checked_at = facilities.get("checked_at")
    discovery_status = "observed" if _timestamp_epoch(checked_at) is not None else "unknown" if facilities else "unobserved"
    if _object(facilities.get("confirmation")).get("candidate_status") == "error" and not facilities.get("error_kind"):
        discovery_status = "unknown"
    checks.add(
        "facility", "facility-discovery", "facility.discovery_failed",
        "warning" if facilities.get("error_kind") else None,
        discovery_status, observed_at=checked_at,
    )
    for facility in checks.rows(facilities.get("items")):
        raw_id = _identity(facility.get("id"))
        if raw_id is None:
            continue
        source_id = safe_source_id("facility:" + raw_id)
        label = {
            "pcp.runtime.observer": "PCP", "infer-runtime.status": "Infer Runtime",
            "dev-mesh.observer.status": "Dev Mesh Observer",
        }.get(facility.get("protocol"), "Facility")
        state = facility.get("status")
        severity = "critical" if state in {"unavailable", "unreachable"} else "warning" if state == "degraded" else None
        status = "observed" if state in {"healthy", "degraded", "unavailable", "unreachable"} else "unknown"
        snapshot = _object(facility.get("snapshot"))
        checked_at = facility.get("checked_at")
        evidence_at = checked_at if state == "unreachable" else facility.get("observed_at") or snapshot.get("captured_at")
        if _timestamp_epoch(evidence_at) is None:
            status = "unknown"
        attempt_epoch = _timestamp_epoch(checked_at)
        if checked_at is not None and (
            attempt_epoch is None or not -FRESHNESS_SECONDS <= checks.now - attempt_epoch <= FRESHNESS_SECONDS
        ):
            status = "unknown"
        pending = _object(facility.get("confirmation")).get("candidate_status")
        ranks = {"healthy": 0, "starting": 0, "stopping": 0, "degraded": 1, "unavailable": 2, "unreachable": 2}
        if isinstance(pending, str) and ranks.get(pending, 0) > ranks.get(state, 0):
            status = "unknown"
        checks.add(
            "facility", source_id, "facility.health", severity, status, label=label,
            # Only a transport failure is established by an attempt alone.
            # Successful reads also require a fresh provider snapshot; a
            # freshly read old healthy snapshot cannot establish recovery.
            observed_at=evidence_at,
        )
        raw_issues = snapshot.get("issues")
        issue_status = "observed" if status == "observed" and state != "unreachable" and isinstance(raw_issues, list) else "unknown"
        issue_observed_at = snapshot.get("captured_at", facility.get("observed_at"))
        if _timestamp_epoch(issue_observed_at) is None:
            issue_status = "unknown"
        if isinstance(raw_issues, list) and len(raw_issues) > MAX_SOURCE_ITEMS:
            issue_status = "unknown"
        issues: dict[str, str] = {}
        for issue in checks.rows(raw_issues):
            code = issue.get("code")
            code = code if isinstance(code, str) and code in DEV_MESH_CODES else "facility.reported_issue"
            issue_severity = issue.get("severity")
            if issue_severity not in SEVERITIES:
                issue_status = "unknown"
                continue
            if {"info": 0, "warning": 1, "critical": 2}[issue_severity] > {"info": 0, "warning": 1, "critical": 2}.get(issues.get(code, ""), -1):
                issues[code] = issue_severity
        for code in (*sorted(DEV_MESH_CODES), "facility.reported_issue"):
            checks.add(
                "facility", source_id, code, issues.get(code), issue_status, label=label,
                observed_at=issue_observed_at,
            )


def _upstream(infra: dict[str, Any], checks: _Checks) -> None:
    upstream = _object(infra.get("upstream_status"))
    for item in checks.rows(upstream.get("items")):
        raw_id = _identity(item.get("id"))
        if raw_id is None:
            continue
        source_id = safe_source_id("upstream:" + raw_id)
        state = item.get("status")
        readable = item.get("available") is True
        status = "observed" if readable and state in {"healthy", "warning", "critical"} else "unknown"
        checks.add(
            "upstream", source_id, "upstream.service_incident",
            state if state in {"warning", "critical"} else None, status,
            observed_at=item.get("observed_at"), freshness_seconds=900,
        )
        read_status = "observed" if readable or item.get("available") is False else "unknown"
        if item.get("error_kind") == "waiting":
            read_status = "unknown"
        checks.add(
            "upstream", source_id, "upstream.status_unavailable",
            "warning" if item.get("available") is False else None, read_status,
            observed_at=item.get("observed_at"), freshness_seconds=900,
        )


def normalize_diagnostics(projection: dict[str, Any], now_epoch: float) -> DiagnosticObservation:
    """Derive bounded checks without reading a source or mutating any state."""
    checks = _Checks(now_epoch)
    infra = _object(projection.get("infra")) if "infra" in projection else projection
    sources = _sources(infra, checks)
    _network(infra, projection, sources, checks)
    _system(infra, checks)
    _facilities(infra, checks)
    _upstream(infra, checks)
    return DiagnosticObservation(now_epoch, tuple(checks.items.values()), checks.truncated)
