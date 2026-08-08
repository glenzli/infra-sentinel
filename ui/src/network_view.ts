import { AgentProjection } from "./bridge";
import { asArray, asRecord, formatBytes, number } from "./format";
import { tr } from "./i18n";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function rate(value: unknown): string {
  return `${formatBytes(value)}${tr("/min", "/分钟")}`;
}

function card(label: string, value: unknown, detail: string, tone: string): string {
  return `<article class="network-card network-card--${tone}"><p>${escapeHtml(label)}</p><strong>${formatBytes(value)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

function trendSvg(trend: Record<string, unknown>): string {
  const buckets = asArray(trend.buckets);
  if (buckets.length < 2) return `<div class="chart-empty">${tr("Waiting for enough realtime samples.", "等待足够的实时采样。")} </div>`;
  const values = buckets.map((bucket) => number(bucket.mihomo_total));
  const proxy = buckets.map((bucket) => number(bucket.proxy_observed));
  const peak = Math.max(1, ...values, ...proxy);
  const points = (series: number[]) => series.map((value, index) => `${(index / (series.length - 1)) * 100},${92 - (value / peak) * 82}`).join(" ");
  return `<svg class="traffic-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="${tr("Traffic rate trend", "流量速率趋势")}">
    <path class="chart-grid" d="M0 10H100M0 51H100M0 92H100" />
    <polyline class="chart-line chart-line--mihomo" points="${points(values)}" />
    <polyline class="chart-line chart-line--proxy" points="${points(proxy)}" />
  </svg>`;
}

export function renderNetworkDetail(projection: AgentProjection): string {
  const state = projection as unknown as Record<string, unknown>;
  const session = asRecord(state.session);
  const vps = asRecord(session.vps);
  const kernel = asRecord(session.kernel);
  const attribution = asRecord(session.attribution);
  const breakdown = asRecord(session.breakdown);
  const services = asArray(session.visible_services);
  const remoteServers = asArray(session.remote_servers);
  const users = asArray(asRecord(state.xray_stats).users);
  const trend = asRecord(session.trend);
  const multiplier = number(breakdown.observed_multiplier);
  const comparison = String(breakdown.comparison_status ?? "waiting");
  const estimate = comparison === "multiple_servers"
    ? tr("Per-host measurement is shown below; fleet multipliers are intentionally not merged.", "下方按主机展示实测值；不同主机的倍率不会被合并。")
    : multiplier > 0
      ? tr(`Observed billing multiplier ${multiplier.toFixed(2)}×`, `实测账单倍率 ${multiplier.toFixed(2)}×`)
      : tr("Waiting for matching VPS and Xray intervals.", "等待 VPS 与 Xray 的可匹配采样区间。");
  return `
    <section class="network-detail">
      <div class="network-card-grid">
        ${card(tr("VPS billable", "VPS 账单量"), vps.total_bytes, `${tr("In", "入")} ${formatBytes(vps.in_bytes)} · ${tr("Out", "出")} ${formatBytes(vps.out_bytes)}`, "orange")}
        ${card(tr("Mihomo local total", "Mihomo 本机总量"), kernel.total_bytes, `${tr("Up", "上行")} ${formatBytes(kernel.up_bytes)} · ${tr("Down", "下行")} ${formatBytes(kernel.down_bytes)}`, "purple")}
        ${card(tr("Observed proxy route", "已识别代理路径"), session.proxy_observed_total_bytes, `${tr("Unattributed", "未归因")} ${formatBytes(attribution.unattributed_bytes)}`, "blue")}
      </div>
      <p class="network-explanation">${escapeHtml(estimate)}</p>
      <div class="network-breakdown">
        <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Domain traffic attribution", "域名流量归因")}</h3><span>${tr("local", "本机")}</span></div>
          <ul class="traffic-list">${services.map((service) => `<li><span>${escapeHtml(service.label)}</span><strong>${formatBytes(service.total_bytes)}</strong></li>`).join("") || `<li class="empty">${tr("Waiting for domain observations.", "等待域名归因数据。")}</li>`}</ul>
          <p class="panel-footnote">${tr("Coverage", "归因覆盖率")} ${new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 }).format(number(attribution.coverage))}</p>
        </article>
        <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Remote host traffic", "远端主机流量")}</h3><span>${remoteServers.length}</span></div>
          <ul class="traffic-list">${remoteServers.map((server) => `<li><span>${escapeHtml(server.label)}</span><span class="traffic-list__values">${tr("Bill", "账单")} ${formatBytes(server.total_bytes)} · Xray ${formatBytes(server.xray_logical_bytes)}</span></li>`).join("") || `<li class="empty">${tr("No remote host configured.", "尚未配置远端主机。")}</li>`}</ul>
        </article>
      </div>
      ${users.length ? `<article class="xray-panel"><div class="detail-panel__heading"><h3>${tr("Xray users", "Xray 用户")}</h3><span>${formatBytes(asRecord(state.xray_stats).total_bytes)}</span></div><ul class="traffic-list traffic-list--columns">${users.slice(0, 8).map((user) => `<li><span>${escapeHtml(user.label)}</span><strong>${formatBytes(user.total_bytes)}</strong></li>`).join("")}</ul></article>` : ""}
      <section class="trend-panel"><div class="detail-panel__heading"><h3>${tr("Last 15 minutes — rate", "近 15 分钟速率趋势")}</h3><span>${tr("Unit", "单位")} · ${rate(trend.peak_bytes_per_minute)}</span></div>${trendSvg(trend)}<div class="chart-legend"><span><i class="chart-dot chart-dot--mihomo"></i>Mihomo</span><span><i class="chart-dot chart-dot--proxy"></i>${tr("Proxy route", "代理路径")}</span></div></section>
    </section>`;
}
