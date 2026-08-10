import { ResourceProjection, UpstreamProviderProjection, UpstreamStatusProjection } from "./bridge";
import { tr } from "./i18n";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    healthy: tr("Operational", "正常"),
    warning: tr("Degraded", "性能异常"),
    critical: tr("Major outage", "严重故障"),
    degraded: tr("Status incomplete", "状态不完整"),
    unknown: tr("Unknown", "未知"),
  };
  return labels[status] ?? status;
}

function timestamp(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function providerLine(provider: UpstreamProviderProjection): string {
  return `<span class="upstream-overview__provider"><i class="source-state source-state--${escapeHtml(provider.status)}"></i><b>${escapeHtml(provider.label)}</b><em>${escapeHtml(statusLabel(provider.status))}</em></span>`;
}

export function renderUpstreamStatusCard(resource: ResourceProjection, snapshot?: UpstreamStatusProjection): string {
  const providers = snapshot?.items ?? [];
  const summary = snapshot
    ? `${snapshot.healthy} / ${snapshot.total} ${tr("operational", "正常")}`
    : tr("Waiting", "等待读取");
  const states = providers.map(providerLine).join("");
  return `<button class="resource-card resource-card--upstream-status resource-card--${escapeHtml(resource.status)}" type="button" data-resource-id="upstream_status">
    <div class="resource-card__heading"><span class="resource-card__identity"><span class="resource-card__state source-state source-state--${escapeHtml(resource.status)}" aria-hidden="true"></span><p>${tr("Upstream services", "上游服务状态")}</p></span><span class="pill pill--${escapeHtml(resource.status)}">${escapeHtml(summary)}</span></div>
    <div class="upstream-overview__providers">${states || `<span class="empty">${tr("Waiting for official status", "等待官方状态")}</span>`}</div>
    <div class="network-overview__footer upstream-overview__footer"><span>${tr("Official aggregate status", "官方聚合状态")}</span><span>${snapshot?.unknown ? tr(`${snapshot.unknown} unreadable`, `${snapshot.unknown} 项无法读取`) : tr("Low-frequency read-only checks", "低频只读检测")}</span><span>${tr("Details", "详情")} →</span></div>
  </button>`;
}

function incident(provider: UpstreamProviderProjection): string {
  const active = provider.incidents[0];
  if (!active) return `<p class="upstream-provider__quiet">${provider.available ? tr("No API incident reported", "未报告 API 事件") : tr("Official status could not be read", "无法读取官方状态")}</p>`;
  return `<div class="upstream-provider__incident upstream-provider__incident--${escapeHtml(active.level)}"><small>${tr("Active incident", "活动事件")} · ${escapeHtml(active.status)}</small><strong>${escapeHtml(active.name)}</strong><span>${active.updated_at ? timestamp(active.updated_at) : ""}</span></div>`;
}

function providerCard(provider: UpstreamProviderProjection): string {
  const components = provider.components.map((component) => `<li><span><i class="source-state source-state--${escapeHtml(component.level)}"></i><span class="upstream-provider__component-name">${escapeHtml(component.name)}${component.group ? `<small>${escapeHtml(component.group)}</small>` : ""}</span></span><em>${escapeHtml(statusLabel(component.level))}</em></li>`).join("");
  const componentScope = tr(`${provider.components.length} public status items`, `${provider.components.length} 个公开状态项`);
  return `<article class="upstream-provider upstream-provider--${escapeHtml(provider.status)}">
    <header><div><span class="source-state source-state--${escapeHtml(provider.status)}"></span><h3>${escapeHtml(provider.label)}</h3></div><span class="pill pill--${escapeHtml(provider.status)}">${escapeHtml(statusLabel(provider.status))}</span></header>
    ${incident(provider)}
    <div class="upstream-provider__components-heading"><span>${tr("Public components", "公开组件")}</span><em>${escapeHtml(componentScope)}</em></div>
    <ul>${components || `<li class="upstream-provider__empty">${tr("No component data", "没有组件数据")}</li>`}</ul>
    <footer><span>${tr("Official update", "官方更新")} · ${escapeHtml(timestamp(provider.official_updated_at))}</span><button class="facility-console" type="button" data-status-url="${escapeHtml(provider.status_url)}">${tr("Open status page", "打开状态页")} ↗</button></footer>
  </article>`;
}

export function renderUpstreamStatusResourcePage(resource: ResourceProjection, snapshot?: UpstreamStatusProjection): string {
  const providers = snapshot?.items ?? [];
  const summary = snapshot
    ? tr(
      `${snapshot.healthy} operational · ${snapshot.attention} need attention · ${snapshot.unknown} unknown`,
      `${snapshot.healthy} 项正常 · ${snapshot.attention} 项异常 · ${snapshot.unknown} 项未知`,
    )
    : tr("Waiting for the first official status read", "等待首次读取官方状态");
  return `<section class="resource-section resource-section--detail upstream-status-page">
    <div class="section-heading"><div><p class="eyebrow">UPSTREAM SERVICES</p><h2>${tr("Upstream services", "上游服务状态")}</h2></div><span class="pill pill--${escapeHtml(resource.status)}">${escapeHtml(statusLabel(resource.status))}</span></div>
    <div class="upstream-status-summary"><strong>${escapeHtml(summary)}</strong><span>${tr("Public provider status is diagnostic context, not a guarantee for a specific account, model, or region.", "官方状态用于辅助诊断，不代表特定账户、模型或地区一定可用。")}</span></div>
    <div class="upstream-provider-grid">${providers.map(providerCard).join("") || `<p class="empty">${tr("No upstream provider status is available.", "暂无上游服务状态。")}</p>`}</div>
    <p class="panel-footnote">${tr("Read-only checks run every five minutes. A read failure is shown as unknown and never treated as a provider outage.", "每 5 分钟进行一次只读检测；读取失败只显示为未知，不会被判定为供应商故障。")}</p>
  </section>`;
}
