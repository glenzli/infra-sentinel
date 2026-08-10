import { FacilitiesProjection, FacilityMetric, FacilityProjection } from "./bridge";
import { formatBytes, formatDuration } from "./format";
import { tr } from "./i18n";

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
    stale: tr("Registration stale", "注册已过期"),
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
  };
  return labels[id] ?? id.split(".").slice(-2).join(" · ");
}

function metricValue(metric: FacilityMetric): string {
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
  const requested = (facility.snapshot?.headline_metrics ?? []).map((id) => byId.get(id)).filter((metric): metric is FacilityMetric => Boolean(metric));
  return (requested.length ? requested : metrics).slice(0, 3);
}

function consoleButton(facility: FacilityProjection, compact = false): string {
  if (!facility.console_url) return `<span class="facility-console facility-console--missing">${tr("No Console", "无 Console")}</span>`;
  return `<button class="${compact ? "facility-console" : "button button--subtle"}" type="button" data-console-url="${escapeHtml(facility.console_url)}">${tr("Open Console", "打开 Console")} ↗</button>`;
}

function compactCard(facility: FacilityProjection): string {
  const metrics = headlineMetrics(facility).map((metric) => `<span><small>${escapeHtml(metricLabel(metric.id))}</small><strong>${escapeHtml(metricValue(metric))}</strong></span>`).join("");
  return `<article class="facility-card facility-card--${escapeHtml(facility.status)}"><div class="facility-card__heading"><span><i class="source-state source-state--${escapeHtml(facility.status)}"></i><b>${escapeHtml(facility.label)}</b></span><em>${escapeHtml(statusLabel(facility.status))}</em></div><div class="facility-card__meta"><span>${escapeHtml(facility.kind)}</span><span>${escapeHtml(facility.protocol_version)}</span></div><div class="facility-card__metrics">${metrics || `<small>${tr("Waiting for the first observation", "等待首次观测")}</small>`}</div><div class="facility-card__footer"><span>${facility.observed_at ? new Date(facility.observed_at).toLocaleTimeString() : "—"}</span>${consoleButton(facility, true)}</div></article>`;
}

export function renderFacilityOverview(facilities?: FacilitiesProjection): string {
  if (!facilities) return "";
  if (!facilities.items.length && facilities.error_kind) {
    return `<section class="module-section facility-module"><div class="section-heading"><div><p class="eyebrow">${tr("FACILITIES", "运行设施")}</p><h2>${tr("Facility discovery", "设施发现")}</h2></div><button class="section-link" type="button" data-view="facilities">${tr("Needs attention", "需要关注")} →</button></div><p class="facility-page-note facility-page-note--warning">${tr("The local registration directory is unavailable. Resource sampling continues normally.", "本机注册目录当前不可用；资源采样仍在正常继续。")}</p></section>`;
  }
  if (!facilities.items.length) return "";
  return `<section class="module-section facility-module"><div class="section-heading"><div><p class="eyebrow">${tr("FACILITIES", "运行设施")}</p><h2>${tr("Facilities", "运行设施")}</h2></div><button class="section-link" type="button" data-view="facilities">${facilities.healthy} / ${facilities.total} ${tr("healthy", "正常")} · ${tr("Details", "详情")} →</button></div><div class="facility-grid">${facilities.items.map(compactCard).join("")}</div></section>`;
}

function facilityDetail(facility: FacilityProjection): string {
  const allMetrics = facility.snapshot?.metrics ?? [];
  const allIssues = facility.snapshot?.issues ?? [];
  const metrics = allMetrics.slice(0, 18);
  const issues = allIssues.slice(0, 8);
  const metricRows = metrics.map((metric) => `<li><span>${escapeHtml(metricLabel(metric.id))}<small>${escapeHtml(metric.id)}</small></span><strong>${escapeHtml(metricValue(metric))}</strong></li>`).join("");
  const issueRows = issues.map((issue) => `<li class="facility-issue facility-issue--${escapeHtml(issue.severity)}"><span>${escapeHtml(issue.code)}${issue.subject_id ? `<small>${escapeHtml(issue.subject_id)}</small>` : ""}</span><strong>${escapeHtml(issue.severity)}</strong></li>`).join("");
  const overflow = allMetrics.length > metrics.length || allIssues.length > issues.length
    ? `<p class="facility-detail__overflow">${tr("Additional diagnostics remain in the facility Console.", "更多诊断信息保留在设施 Console 中。")}</p>`
    : "";
  return `<article class="facility-detail"><header><div><span class="facility-detail__identity"><i class="source-state source-state--${escapeHtml(facility.status)}"></i><h3>${escapeHtml(facility.label)}</h3></span><p>${escapeHtml(facility.kind)} · ${escapeHtml(facility.instance_id)}</p></div><div><span class="pill pill--${escapeHtml(facility.status)}">${escapeHtml(statusLabel(facility.status))}</span>${consoleButton(facility)}</div></header><div class="facility-detail__facts"><span><small>${tr("Protocol", "协议")}</small><strong>${escapeHtml(facility.protocol)} · ${escapeHtml(facility.protocol_version)}</strong></span><span><small>${tr("Last observed", "最近观测")}</small><strong>${facility.observed_at ? new Date(facility.observed_at).toLocaleString() : "—"}</strong></span><span><small>${tr("Binding", "连接方式")}</small><strong>${escapeHtml(facility.binding)}</strong></span></div><div class="facility-detail__columns"><section><h4>${tr("Read-only metrics", "只读指标")}</h4><ul>${metricRows || `<li>${tr("Waiting for metrics", "等待指标")}</li>`}</ul></section><section><h4>${tr("Issues", "问题")}</h4><ul>${issueRows || `<li class="facility-detail__empty">${tr("No reported issues", "没有已报告问题")}</li>`}</ul></section></div>${overflow}</article>`;
}

export function renderFacilitiesPage(facilities?: FacilitiesProjection): string {
  if (!facilities?.items.length) return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("FACILITIES", "运行设施")}</p><h2>${facilities?.error_kind ? tr("Facility discovery unavailable", "设施发现不可用") : tr("No facilities discovered", "尚未发现运行设施")}</h2></div></div><p class="empty">${facilities?.error_kind ? tr("The Infra Discovery runtime directory could not be validated. Network and AI usage collection are not affected.", "无法验证 Infra Discovery 运行目录；网络和 AI 用量采集不受影响。") : tr("Facilities appear automatically after publishing a compatible Infra Discovery offer.", "设施发布兼容的 Infra Discovery offer 后会自动出现。")}</p></section>`;
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("FACILITIES", "运行设施")}</p><h2>${tr("Facility observation", "设施观测")}</h2></div><span class="section-heading__meta">${facilities.healthy} / ${facilities.total} ${tr("healthy", "正常")}</span></div><p class="facility-page-note">${tr("Sentinel shows bounded read-only observations. Use each facility Console for diagnosis and operations.", "Sentinel 仅展示有界的只读观测；诊断与操作请进入对应设施的 Console。")}</p><div class="facility-detail-list">${facilities.items.map(facilityDetail).join("")}</div></section>`;
}
