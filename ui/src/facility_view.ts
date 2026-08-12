import { FacilitiesProjection, FacilityMetric, FacilityProjection } from "./bridge";
import { formatBytes, formatDuration } from "./format";
import { tr } from "./i18n";
import { AttentionDiagnostic, renderAttentionDiagnostics } from "./attention_diagnostics";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    healthy: tr("Healthy", "正常"),
    starting: tr("Starting", "启动中"),
    degraded: tr("Degraded", "降级"),
    unavailable: tr("Unavailable", "不可用"),
    unreachable: tr("Unreachable", "无法连接"),
    stopping: tr("Stopping", "正在停止"),
  };
  return labels[status] ?? status;
}

function metricLabel(id: string): string {
  const labels: Record<string, string> = {
    "runtime.uptime_seconds": tr("Uptime", "运行时间"),
    "process.uptime_seconds": tr("Uptime", "运行时间"),
    "requests.total": tr("Requests · 24h", "请求 · 24 小时"),
    "requests.failed": tr("Failed · 24h", "失败 · 24 小时"),
    "requests.denied": tr("Denied · 24h", "拒绝 · 24 小时"),
    "requests.latency.p95_ms": tr("P95 latency", "P95 延迟"),
    "requests.telemetry_coverage_ratio": tr("Telemetry coverage", "遥测覆盖"),
    "pcp.pages.current": tr("Pages", "页面"),
    "infer.runtime.uptime_seconds": tr("Uptime", "运行时间"),
    "infer.workload.active_attempts": tr("Active attempts", "活跃任务"),
    "infer.workload.queued_jobs": tr("Queued", "排队"),
    "infer.workload.submitted_total": tr("Submitted", "已提交"),
    "infer.workload.succeeded_total": tr("Succeeded", "成功"),
    "infer.workload.failed_total": tr("Failed", "失败"),
    "infer.workload.queue_rejected_total": tr("Rejected", "已拒绝"),
    "infer.providers.configured": tr("Providers configured", "已配置 Provider"),
    "infer.providers.available": tr("Providers available", "可用 Provider"),
    "infer.providers.circuit_open": tr("Circuits open", "熔断"),
    "infer.resources.pressure": tr("Pressure", "资源压力"),
    "infer.resources.free_memory_percent": tr("Free memory", "可用内存"),
    "infer.resources.resident_models": tr("Resident models", "驻留模型"),
    "infer.resources.active_model_reservations": tr("Model reservations", "模型预留"),
    "infer.budget.settled_usd": tr("Settled cost", "已结算费用"),
    "infer.budget.reserved_usd": tr("Reserved cost", "预留费用"),
    "infer.budget.global_usd_limit": tr("Global budget", "全局预算"),
    "infer.budget.active_reservations": tr("Budget reservations", "预算预留"),
    "workload.calls": tr("Calls", "调用"),
    "workload.calls_24h": tr("Calls · 24h", "调用 · 24 小时"),
    "workload.active": tr("Active", "活跃"),
    "workload.active_attempts": tr("Active attempts", "活跃任务"),
    "workload.queued": tr("Queued", "排队"),
    "reliability.failed": tr("Failed", "失败"),
    "reliability.failed_24h": tr("Failed · 24h", "失败 · 24 小时"),
    "reliability.denied_24h": tr("Denied · 24h", "拒绝 · 24 小时"),
    "latency.p95_ms": "P95",
    "telemetry.coverage_ratio": tr("Telemetry coverage", "遥测覆盖"),
    "storage.current_pages": tr("Pages", "页面"),
    "providers.available": tr("Providers available", "可用 Provider"),
    "providers.configured": tr("Providers configured", "已配置 Provider"),
    "providers.circuit_open": tr("Circuits open", "熔断"),
    "resource.pressure": tr("Pressure", "资源压力"),
    "resource.free_memory_percent": tr("Free memory", "可用内存"),
    "budget.remaining_ratio": tr("Budget remaining", "预算剩余"),
    "dev_mesh.workspaces.registered": tr("Registered workspaces", "已登记工作区"),
    "dev_mesh.workspaces.available": tr("Available workspaces", "可用工作区"),
    "dev_mesh.workspaces.unavailable": tr("Unavailable workspaces", "不可用工作区"),
    "dev_mesh.collection.pending_events": tr("Pending events", "待采集事件"),
    "dev_mesh.collection.last_success_age": tr("Last successful collection", "距上次采集成功"),
    "dev_mesh.collection.running": tr("Collection running", "采集正在运行"),
    "dev_mesh.integrity.issues": tr("Integrity issues", "完整性问题"),
    "dev_mesh.events.mirrored": tr("Mirrored events", "已镜像事件"),
    "dev_mesh.contentions.active": tr("Active contentions", "活跃竞争"),
    "dev_mesh.contentions.stalled": tr("Stalled contentions", "阻塞竞争"),
  };
  return labels[id] ?? id.split(".").slice(-2).join(" · ");
}

function issueLabel(code: string): string {
  const labels: Record<string, string> = {
    "dev_mesh.collection.failed": tr("Collection failed", "采集失败"),
    "dev_mesh.collection.stale": tr("Collection is stale", "采集已过期"),
    "dev_mesh.workspace.unavailable": tr("Workspace unavailable", "工作区不可用"),
    "dev_mesh.workspace.none_registered": tr("No workspace registered", "尚未登记工作区"),
    "dev_mesh.integrity.issue": tr("Integrity issue detected", "发现完整性问题"),
    "dev_mesh.contention.stalled": tr("Contention is stalled", "协作竞争已阻塞"),
    "dev_mesh.collection.backlog": tr("Collection backlog", "采集事件积压"),
  };
  return labels[code] ?? code;
}

function reasonLabel(code: string): string {
  const labels: Record<string, string> = {
    collection_failed: tr("Collection failed", "采集失败"),
    collection_stale: tr("Collection is stale", "采集已过期"),
    workspace_unavailable: tr("Workspace unavailable", "工作区不可用"),
    no_workspaces_registered: tr("No workspace registered", "尚未登记工作区"),
    integrity_issue: tr("Integrity issue detected", "发现完整性问题"),
    contention_stalled: tr("Contention is stalled", "协作竞争已阻塞"),
  };
  return labels[code] ?? code.replace(/[._-]+/g, " ");
}

function severityLabel(severity: string): string {
  const labels: Record<string, string> = {
    info: tr("Info", "提示"),
    warning: tr("Warning", "需关注"),
    critical: tr("Critical", "严重"),
  };
  return labels[severity] ?? severity;
}

function facilityKindLabel(kind: string): string {
  if (kind === "dev-mesh-observer") return tr("Shared-workspace coordination", "共享工作区协作");
  return kind;
}

function metricValue(metric: FacilityMetric): string {
  if (metric.id === "dev_mesh.collection.running" && typeof metric.value === "boolean") {
    return metric.value ? tr("Yes", "是") : tr("No", "否");
  }
  const numeric = typeof metric.value === "number" ? metric.value : Number(metric.value);
  if (metric.unit === "bytes" && Number.isFinite(numeric)) return formatBytes(numeric);
  if (metric.unit === "seconds" && Number.isFinite(numeric)) return formatDuration(numeric);
  if (metric.unit === "milliseconds" || metric.unit === "ms") return Number.isFinite(numeric) ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(numeric)} ms` : String(metric.value);
  if (metric.unit === "ratio" && Number.isFinite(numeric)) return new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 }).format(numeric);
  if (metric.unit === "percent" && Number.isFinite(numeric)) return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(numeric)}%`;
  if (metric.unit === "usd" && Number.isFinite(numeric)) return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(numeric);
  if (Number.isFinite(numeric) && typeof metric.value !== "boolean") return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(numeric);
  return String(metric.value);
}

function headlineMetrics(facility: FacilityProjection): FacilityMetric[] {
  const metrics = facility.snapshot?.metrics ?? [];
  const byId = new Map(metrics.map((metric) => [metric.id, metric]));
  const requested = (facility.snapshot?.headline_metrics ?? [])
    .map((id) => byId.get(id))
    .filter((metric): metric is FacilityMetric => Boolean(metric));
  return (requested.length ? requested : metrics).slice(0, 3);
}

function consoleControl(facility: FacilityProjection, compact = false): string {
  if (!facility.console_url) return `<span class="facility-console facility-console--missing">${tr("No Console", "无 Console")}</span>`;
  return `<button class="${compact ? "facility-console" : "button button--subtle"}" type="button" data-console-url="${escapeHtml(facility.console_url)}">${tr("Open Console", "打开 Console")} ↗</button>`;
}

function facilityDiagnostics(facility: FacilityProjection): AttentionDiagnostic[] {
  const diagnostics: AttentionDiagnostic[] = [];
  if (facility.error_kind) {
    diagnostics.push({
      id: "observation-error",
      level: "degraded",
      subject: facility.label,
      title: tr("The facility could not be observed", "无法读取该设施的观测快照"),
      current: facility.error_kind,
      basis: tr("The observation endpoint failed repeatedly before the public status changed.", "观测端点连续失败达到确认次数后，公开状态才会变化。"),
      action: facility.console_url
        ? tr("Confirm the facility process is running, then open its Console for deeper diagnostics. Sentinel keeps retrying.", "确认设施进程仍在运行，再打开其 Console 深入诊断；Sentinel 会继续重试。")
        : tr("Confirm the facility process and discovery registration are present. Sentinel keeps retrying.", "确认设施进程与发现注册仍然存在；Sentinel 会继续重试。"),
    });
  }
  for (const issue of facility.snapshot?.issues ?? []) {
    if (issue.severity === "info" && facility.status === "healthy") continue;
    diagnostics.push({
      id: `issue-${issue.code}-${issue.subject_id ?? "facility"}`,
      level: issue.severity === "critical" ? "critical" : issue.severity === "warning" ? "warning" : "info",
      subject: issue.subject_id && issue.subject_id !== "observer" ? issue.subject_id : facility.label,
      title: issueLabel(issue.code),
      current: tr(`reported ${severityLabel(issue.severity)} · ${new Date(issue.observed_at).toLocaleString()}`, `报告级别 ${severityLabel(issue.severity)} · ${new Date(issue.observed_at).toLocaleString()}`),
      basis: issue.code,
      action: facility.console_url
        ? tr("Open this facility's Console to inspect the owning workload and resolve it at the source.", "打开该设施的 Console，检查对应工作负载并从源头处理。")
        : tr("Inspect the owning facility; Sentinel only reads and reports this issue.", "检查对应设施；Sentinel 只读取并展示该问题。"),
    });
  }
  if (!diagnostics.length && !["healthy", "starting", "stopping"].includes(facility.status)) {
    const reasons = facility.snapshot?.status.reason_codes ?? [];
    diagnostics.push({
      id: "facility-status",
      level: facility.status === "unavailable" || facility.status === "unreachable" ? "critical" : "degraded",
      subject: facility.label,
      title: tr("The facility reported a degraded state", "设施报告了降级状态"),
      current: reasons.length ? reasons.map(reasonLabel).join(" · ") : statusLabel(facility.status),
      basis: reasons.length ? reasons.join(" · ") : tr("No structured issue was included in the current snapshot.", "当前快照未包含结构化问题。"),
      action: facility.console_url
        ? tr("Open the facility Console for the provider's full diagnostic context.", "打开设施 Console 查看提供方的完整诊断上下文。")
        : tr("Check that the facility and its observation endpoint are running.", "检查设施及其观测端点是否仍在运行。"),
    });
  }
  return diagnostics.slice(0, 8);
}

function facilityCard(facility: FacilityProjection): string {
  const metrics = headlineMetrics(facility)
    .map((metric) => `<span><small>${escapeHtml(metricLabel(metric.id))}</small><strong>${escapeHtml(metricValue(metric))}</strong></span>`)
    .join("");
  return `<article class="facility-card facility-card--interactive facility-card--${escapeHtml(facility.status)}" data-facility-id="${escapeHtml(facility.id)}" role="button" tabindex="0" aria-label="${escapeHtml(tr(`Open ${facility.label} details`, `打开 ${facility.label} 详情`))}">
    <div class="facility-card__heading"><span><i class="source-state source-state--${escapeHtml(facility.status)}"></i><b>${escapeHtml(facility.label)}</b></span><em>${escapeHtml(statusLabel(facility.status))}</em></div>
    <div class="facility-card__meta"><span>${escapeHtml(facilityKindLabel(facility.kind))} · ${escapeHtml(facility.instance_id)}</span><span>${escapeHtml(facility.protocol_version)}</span></div>
    <div class="facility-card__metrics">${metrics || `<small>${tr("Waiting for the first observation", "等待首次观测")}</small>`}</div>
    <div class="facility-card__footer"><span>${facility.observed_at ? new Date(facility.observed_at).toLocaleTimeString() : "—"}</span><div class="facility-card__actions">${consoleControl(facility, true)}<span class="facility-details-link">${tr("Details", "详情")} →</span></div></div>
  </article>`;
}

export function renderFacilityCards(facilities?: FacilitiesProjection): string {
  return facilities?.items.map(facilityCard).join("") ?? "";
}

export function renderFacilityDetailPage(facility?: FacilityProjection): string {
  if (!facility) {
    return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("FACILITY", "运行设施")}</p><h2>${tr("Facility no longer available", "设施已不可用")}</h2></div></div><p class="empty">${tr("The selected registration is no longer present. Return to the overview to choose another instance.", "所选注册已经消失，请返回概览选择其他实例。")}</p></section>`;
  }

  const metrics = facility.snapshot?.metrics ?? [];
  const issues = facility.snapshot?.issues ?? [];
  const metricRows = metrics.map((metric) => `<li><span>${escapeHtml(metricLabel(metric.id))}<small>${escapeHtml(metric.id)}</small></span><strong>${escapeHtml(metricValue(metric))}</strong></li>`).join("");
  const issueRows = issues.map((issue) => {
    const isDevMesh = issue.code.startsWith("dev_mesh.");
    const subject = issue.subject_id && (!isDevMesh || issue.subject_id !== "observer") ? ` · ${issue.subject_id}` : "";
    const label = isDevMesh ? issueLabel(issue.code) : issue.code;
    const severity = isDevMesh ? severityLabel(issue.severity) : issue.severity;
    return `<li class="facility-issue facility-issue--${escapeHtml(issue.severity)}"><span>${escapeHtml(label)}<small>${escapeHtml(`${issue.code}${subject}`)}</small></span><strong>${escapeHtml(severity)}</strong></li>`;
  }).join("");
  const headline = headlineMetrics(facility).map((metric) => `<span><small>${escapeHtml(metricLabel(metric.id))}</small><strong>${escapeHtml(metricValue(metric))}</strong></span>`).join("");
  const capturedAt = facility.snapshot?.captured_at ?? facility.observed_at;
  const diagnosticPanel = renderAttentionDiagnostics(facilityDiagnostics(facility));

  return `<section class="resource-section resource-section--detail facility-instance-page">
    <header class="facility-instance-header"><div><p class="eyebrow">${escapeHtml(facilityKindLabel(facility.kind))} · ${escapeHtml(facility.instance_id)}</p><span class="facility-detail__identity"><i class="source-state source-state--${escapeHtml(facility.status)}"></i><h2>${escapeHtml(facility.label)}</h2></span><p>${escapeHtml(facility.protocol)} · ${escapeHtml(facility.protocol_version)}</p></div><div><span class="pill pill--${escapeHtml(facility.status)}">${escapeHtml(statusLabel(facility.status))}</span>${consoleControl(facility)}</div></header>
    ${diagnosticPanel}
    ${headline ? `<div class="facility-instance-headlines">${headline}</div>` : ""}
    <div class="facility-detail__facts">
      <span><small>${tr("Last observed", "最近观测")}</small><strong>${capturedAt ? new Date(capturedAt).toLocaleString() : "—"}</strong></span>
      <span><small>${tr("Snapshot sequence", "快照序列")}</small><strong>${escapeHtml(facility.snapshot?.sequence ?? "—")}</strong></span>
      <span><small>${tr("Binding", "连接方式")}</small><strong>${escapeHtml(facility.binding)}</strong></span>
      <span><small>${tr("Generation", "运行代次")}</small><strong title="${escapeHtml(facility.generation)}">${escapeHtml(facility.generation)}</strong></span>
      <span><small>${tr("Observation schema", "观测协议")}</small><strong>${escapeHtml(facility.snapshot?.schema_version ?? facility.protocol_version)}</strong></span>
    </div>
    <div class="facility-detail__columns"><section><h3>${tr("Read-only metrics", "只读指标")}</h3><ul>${metricRows || `<li class="facility-detail__empty">${tr("Waiting for metrics", "等待指标")}</li>`}</ul></section><section><h3>${tr("Issues", "问题")}</h3><ul>${issueRows || `<li class="facility-detail__empty">${tr("No reported issues", "没有已报告问题")}</li>`}</ul></section></div>
    <p class="facility-detail__overflow">${tr("For operations and deeper diagnostics, open this facility's own Console.", "操作和更深入的诊断请进入该设施自己的 Console。")}</p>
  </section>`;
}
