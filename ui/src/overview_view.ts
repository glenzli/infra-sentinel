import { AgentProjection, ResourceProjection } from "./bridge";
import { formatBytes, formatDuration } from "./format";
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

function resourceCard(resource: ResourceProjection): string {
  const canOpen = resource.id === "network";
  const tag = canOpen ? "button" : "article";
  const interaction = canOpen ? ` data-resource-id="${escapeHtml(resource.id)}"` : "";
  const type = canOpen ? ' type="button"' : "";
  return `<${tag} class="resource-card resource-card--${escapeHtml(resource.status)}"${type}${interaction}><span class="resource-card__state source-state source-state--${escapeHtml(resource.status)}" aria-hidden="true"></span><div class="resource-card__heading"><p>${escapeHtml(resourceLabel(resource))}</p><span class="pill pill--${escapeHtml(resource.status)}">${escapeHtml(statusLabel(resource.status))}</span></div><strong>${escapeHtml(resourceValue(resource))}</strong><small>${resource.online_source_count} / ${resource.source_count} ${tr("sources online", "个数据源在线")}</small>${canOpen ? `<span class="resource-card__open">${tr("Open details", "查看详情")} →</span>` : ""}</${tag}>`;
}

export function renderOverview(projection: AgentProjection): string {
  const { infra, session } = projection;
  const resources = infra.resources.filter((resource) => resource.enabled);
  const activeAlerts = infra.overall.active_alerts;
  const onlineSources = resources.reduce((total, resource) => total + resource.online_source_count, 0);
  const configuredSources = resources.reduce((total, resource) => total + resource.source_count, 0);
  return `<section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></section><section class="summary-grid" aria-label="${tr("Overview", "概览")}"><article class="summary-card summary-card--status"><p>${tr("Overall health", "整体健康")}</p><strong class="status-value status-value--${escapeHtml(infra.overall.status)}">${escapeHtml(statusLabel(infra.overall.status))}</strong><small>${activeAlerts ? tr(`${activeAlerts} active alert${activeAlerts === 1 ? "" : "s"}`, `当前 ${activeAlerts} 个活动告警`) : tr("No active alerts", "暂无活动告警")}</small></article><article class="summary-card"><p>${tr("Enabled resources", "已启用资源")}</p><strong>${resources.length}</strong><small>${resources.map(resourceLabel).join(" · ") || tr("None", "无")}</small></article><article class="summary-card"><p>${tr("Data sources", "数据源")}</p><strong>${onlineSources}<em> / ${configuredSources}</em></strong><small>${tr("online", "在线")}</small></article></section><section class="module-section"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCES", "资源模块")}</p><h2>${tr("Enabled resources", "已启用资源")}</h2></div></div><div class="resource-grid">${resources.map(resourceCard).join("") || `<p class="empty">${tr("No resource module is enabled.", "尚未启用资源模块。")}</p>`}</div></section>`;
}
