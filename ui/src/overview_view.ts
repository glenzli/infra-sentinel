import { AgentProjection, ResourceProjection } from "./bridge";
import { asArray, asRecord, formatBytes, formatDuration, number } from "./format";
import { tr } from "./i18n";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function resourceLabel(resource: ResourceProjection): string {
  return resource.id === "network" ? tr("Network", "网络") : resource.id;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = { healthy: tr("Healthy", "正常"), warning: tr("Attention", "需关注"), critical: tr("Critical", "严重"), degraded: tr("Data source issue", "数据源异常") };
  return labels[status] ?? status;
}

function resourceValue(resource: ResourceProjection): string {
  return resource.primary_unit === "bytes" ? formatBytes(resource.primary_value) : String(resource.primary_value);
}

function sourceSummary(resource: ResourceProjection): string {
  return `${resource.online_source_count} / ${resource.source_count} ${tr("sources online", "个数据源在线")}`;
}

function dailyUsageThresholds(guard: Record<string, unknown>): string {
  return tr(
    `warning ${formatBytes(guard.warning_bytes)} · critical ${formatBytes(guard.critical_bytes)}`,
    `预警 ${formatBytes(guard.warning_bytes)} · 严重 ${formatBytes(guard.critical_bytes)}`,
  );
}

function networkCard(projection: AgentProjection, resource: ResourceProjection): string {
  const session = asRecord(projection.session);
  const remote = asRecord(projection.vps);
  const dailyUsage = asRecord(remote.cycle);
  const usageGuards = asArray(remote.daily_usage_guards);
  const kernel = asRecord(session.kernel);
  const attribution = asRecord(session.attribution);
  const remoteServers = asArray(session.remote_servers);
  const coverage = number(attribution.coverage);
  const coverageText = coverage > 0
    ? new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 }).format(coverage)
    : tr("waiting", "等待采样");
  const guards = usageGuards.slice(0, 3).map((guard) => `<span class="usage-guard usage-guard--${escapeHtml(String(guard.level ?? "none"))}"><i></i><b>${escapeHtml(guard.label)}</b><em>${formatBytes(guard.usage_bytes)} · ${dailyUsageThresholds(guard)}</em></span>`).join("");
  return `<button class="resource-card resource-card--network resource-card--${escapeHtml(resource.status)}" type="button" data-resource-id="network"><div class="resource-card__heading"><span class="resource-card__identity"><span class="resource-card__state source-state source-state--${escapeHtml(resource.status)}" aria-hidden="true"></span><p>${tr("Network", "网络")}</p></span></div><div class="network-overview__metrics"><span><small>${tr("Today — VPS total", "今日 VPS 总量")}</small><strong>${formatBytes(dailyUsage.total_bytes)}</strong></span><span><small>${tr("Local Mihomo", "本机 Mihomo")}</small><strong>${formatBytes(kernel.total_bytes)}</strong></span><span><small>${tr("Proxy route", "代理路径")}</small><strong>${formatBytes(session.proxy_observed_total_bytes)}</strong></span></div>${guards ? `<div class="usage-guards"><small>${tr("Daily usage checks", "每日用量检测")}</small><div>${guards}</div></div>` : ""}<div class="network-overview__footer"><span>${tr("Coverage", "归因覆盖率")} ${coverageText}</span><span>${remoteServers.length} ${tr("remote hosts", "台远端主机")} · ${sourceSummary(resource)}</span><span>${tr("Details", "详情")} →</span></div></button>`;
}

function resourceCard(resource: ResourceProjection): string {
  return `<article class="resource-card resource-card--${escapeHtml(resource.status)}"><div class="resource-card__heading"><span class="resource-card__identity"><span class="resource-card__state source-state source-state--${escapeHtml(resource.status)}" aria-hidden="true"></span><p>${escapeHtml(resourceLabel(resource))}</p></span><span class="pill pill--${escapeHtml(resource.status)}">${escapeHtml(statusLabel(resource.status))}</span></div><div class="resource-card__metric"><strong>${escapeHtml(resourceValue(resource))}</strong><small>${escapeHtml(sourceSummary(resource))}</small></div></article>`;
}

export function renderOverview(projection: AgentProjection): string {
  const { infra, session } = projection;
  const resources = infra.resources.filter((resource) => resource.enabled);
  const activeAlerts = infra.overall.active_alerts;
  const onlineSources = resources.reduce((total, resource) => total + resource.online_source_count, 0);
  const configuredSources = resources.reduce((total, resource) => total + resource.source_count, 0);
  return `<section class="overview-toolbar"><div class="overview-toolbar__session"><span>${tr("Session", "当前统计周期")}</span><strong>${formatDuration(session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></section><section class="summary-grid summary-grid--compact" aria-label="${tr("Overview", "概览")}"><article class="summary-card summary-card--status"><p>${tr("Overall health", "整体健康")}</p><strong class="status-value status-value--${escapeHtml(infra.overall.status)}">${escapeHtml(statusLabel(infra.overall.status))}</strong><small>${activeAlerts ? tr(`${activeAlerts} active alert${activeAlerts === 1 ? "" : "s"}`, `当前 ${activeAlerts} 个活动告警`) : tr("No active alerts", "暂无活动告警")}</small></article><article class="summary-card"><p>${tr("Enabled resources", "已启用资源")}</p><strong>${resources.length}</strong><small>${resources.map(resourceLabel).join(" · ") || tr("None", "无")}</small></article><article class="summary-card"><p>${tr("Data sources", "数据源")}</p><strong>${onlineSources}<em> / ${configuredSources}</em></strong><small>${tr("online", "在线")}</small></article></section><section class="module-section module-section--overview"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCES", "资源模块")}</p><h2>${tr("Resources", "资源模块")}</h2></div><span class="section-heading__meta">${resources.length} ${tr("enabled", "项已启用")}</span></div><div class="resource-grid resource-grid--compact">${resources.map((resource) => resource.id === "network" ? networkCard(projection, resource) : resourceCard(resource)).join("") || `<p class="empty">${tr("No resource module is enabled.", "尚未启用资源模块。")}</p>`}</div></section>`;
}
