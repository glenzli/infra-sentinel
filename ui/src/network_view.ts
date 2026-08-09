import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatBytes, number } from "./format";
import { tr } from "./i18n";
import { AnalysisScope, renderAnalysisScopes } from "./analysis_scope";
import { DailyBarBucket, DailyBarSeries, renderDailyBarChart } from "./daily_bar_chart";

export type NetworkAnalysisData = {
  servicePoints: Record<string, unknown>[];
  localPoints: Record<string, unknown>[];
  vpsPoints: Record<string, unknown>[];
};

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function rate(value: unknown): string {
  return `${formatBytes(value)}${tr("/min", "/分钟")}`;
}

function card(label: string, value: unknown, detail: string, tone: string): string {
  return `<article class="network-card network-card--${tone}"><p>${escapeHtml(label)}</p><strong>${formatBytes(value)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

function dailyUsageThresholds(usage: Record<string, unknown>): string {
  return tr(
    `warning ${formatBytes(usage.warning_bytes)} · critical ${formatBytes(usage.critical_bytes)}`,
    `预警 ${formatBytes(usage.warning_bytes)} · 严重 ${formatBytes(usage.critical_bytes)}`,
  );
}

const TRAFFIC_COLORS = ["#1a73e8", "#1e8e3e", "#00838f", "#a142f4", "#e37400"];

function trafficTotalsChart(points: Record<string, unknown>[], scope: AnalysisScope, loading: boolean): string {
  if (loading) return `<section class="analysis-panel">${renderAnalysisScopes("network", scope)}<article class="detail-panel traffic-total-chart"><div class="chart-empty">${tr("Loading recorded network usage…", "正在读取已记录的网络用量…")}</div></article></section>`;
  const totals = new Map<string, number>();
  for (const point of points) {
    const dimensions = asRecord(point.dimensions);
    const label = String(dimensions.label || dimensions.service || "Unknown");
    if (label && label !== "Unattributed") totals.set(label, (totals.get(label) ?? 0) + number(point.value));
  }
  const ranked = [...totals.entries()].sort((left, right) => right[1] - left[1]).slice(0, 5);
  const maximum = Math.max(...ranked.map(([, total]) => total), 1);
  const title = scope === "cumulative" ? tr("Recorded domain traffic", "域名流量记录累计") : tr("Today's domain traffic", "当日域名流量汇总");
  const detail = scope === "cumulative" ? tr("local Mihomo history", "本机 Mihomo 历史") : tr("local Mihomo today", "本机 Mihomo 今日");
  return `<section class="analysis-panel">${renderAnalysisScopes("network", scope)}<article class="detail-panel traffic-total-chart"><div class="detail-panel__heading"><h3>${title}</h3><span>${detail}</span></div><div class="traffic-total-bars">${ranked.map(([label, total], index) => {
    const percentage = Math.max(1, Math.min(100, (total / maximum) * 100));
    return `<div class="traffic-total-bar"><div><span><i class="chart-dot" style="background:${TRAFFIC_COLORS[index]}"></i>${escapeHtml(label)}</span><strong>${formatBytes(total)}</strong></div><p><i style="background:${TRAFFIC_COLORS[index]};width:${percentage}%"></i></p></div>`;
  }).join("") || `<div class="chart-empty">${tr("Waiting for attributed service samples.", "等待已归因的服务采样。")}</div>`}</div><p class="panel-footnote">${tr("Only locally attributed Mihomo service traffic is grouped here. Unattributed traffic remains outside domain bars.", "这里只汇总本机 Mihomo 已归因的服务流量；未归因流量不会被分配到域名柱中。")}</p></article></section>`;
}

function dailyTrafficHistory(analysis: NetworkAnalysisData, remoteServers: Record<string, unknown>[], loading: boolean): string {
  if (loading) return `<section class="analysis-panel">${renderAnalysisScopes("network", "daily")}<article class="detail-panel traffic-total-chart"><div class="chart-empty">${tr("Loading recorded daily traffic…", "正在读取已记录的每日流量…")}</div></article></section>`;
  const hostLabels = new Map(remoteServers.map((server) => [String(server.id ?? ""), String(server.label ?? server.id ?? "VPS")]));
  const totals = new Map<string, number>();
  const valuesByDay = new Map<number, Map<string, number>>();
  const add = (point: Record<string, unknown>, sourceId: string) => {
    const epoch = number(point.observed_epoch);
    if (!epoch) return;
    const value = number(point.value);
    totals.set(sourceId, (totals.get(sourceId) ?? 0) + value);
    const day = valuesByDay.get(epoch) ?? new Map<string, number>();
    day.set(sourceId, (day.get(sourceId) ?? 0) + value);
    valuesByDay.set(epoch, day);
  };
  for (const point of analysis.localPoints) add(point, "local-mihomo");
  for (const point of analysis.vpsPoints) add(point, String(point.source_id || "vps:unknown"));
  if (!valuesByDay.size) return `<section class="analysis-panel">${renderAnalysisScopes("network", "daily")}<article class="detail-panel traffic-total-chart"><div class="chart-empty">${tr("Daily history begins when Infra Sentinel records local traffic.", "每日历史会从 Infra Sentinel 开始记录本机流量后出现。")}</div></article></section>`;
  const rankedVps = [...totals.entries()]
    .filter(([sourceId]) => sourceId !== "local-mihomo")
    .sort((left, right) => right[1] - left[1])
    .slice(0, 4)
    .map(([sourceId]) => sourceId);
  const hasOtherVps = [...totals.keys()].some((sourceId) => sourceId !== "local-mihomo" && !rankedVps.includes(sourceId));
  const ids = ["local-mihomo", ...rankedVps];
  const series: DailyBarSeries[] = ids.filter((id) => totals.has(id)).map((id, index) => ({
    id,
    label: id === "local-mihomo" ? "Mihomo" : hostLabels.get(id.replace(/^vps:/, "")) ?? id.replace(/^vps:/, ""),
    color: TRAFFIC_COLORS[index],
  }));
  if (hasOtherVps) series.push({ id: "__other_vps__", label: tr("Other VPS", "其他 VPS"), color: "#7b8794" });
  const dailyBuckets: DailyBarBucket[] = [...valuesByDay.entries()].sort(([left], [right]) => left - right).map(([epoch, values]) => {
    if (!hasOtherVps) return { epoch, values };
    const normalized = new Map(values);
    for (const [sourceId, value] of values) {
      if (sourceId !== "local-mihomo" && !rankedVps.includes(sourceId)) {
        normalized.delete(sourceId);
        normalized.set("__other_vps__", (normalized.get("__other_vps__") ?? 0) + value);
      }
    }
    return { epoch, values: normalized };
  });
  return `<section class="analysis-panel">${renderAnalysisScopes("network", "daily")}${renderDailyBarChart(series, dailyBuckets, {
    title: tr("Daily traffic by source", "每日流量来源"),
    detail: tr("Mihomo + VPS · last 30 days", "Mihomo + VPS · 近 30 天"),
    ariaLabel: tr("Daily traffic by local Mihomo and remote VPS", "按本机 Mihomo 与远端 VPS 的每日流量"),
    formatValue: formatBytes,
    footnote: tr("Mihomo and each VPS are separate measurement boundaries. Their bars are shown side by side and are never added into a misleading fleet total.", "Mihomo 与各 VPS 属于不同计量边界；柱按日并列展示，不会合并成误导性的总量。"),
  })}</section>`;
}

function niceAxisMaximum(value: number): number {
  if (value <= 0) return 1;
  const unit = value >= 1024 ** 3 ? 1024 ** 3 : value >= 1024 ** 2 ? 1024 ** 2 : value >= 1024 ? 1024 : 1;
  const target = (value / unit) * 1.1;
  const magnitude = 10 ** Math.floor(Math.log10(target));
  const normalized = target / magnitude;
  const steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10];
  return (steps.find((step) => normalized <= step) ?? 10) * magnitude * unit;
}

function trendSvg(trend: Record<string, unknown>): string {
  const buckets = asArray(trend.buckets);
  if (buckets.length < 2) return `<div class="chart-empty">${tr("Waiting for enough realtime samples.", "等待足够的实时采样。")} </div>`;
  const values = buckets.map((bucket) => number(bucket.mihomo_total));
  const proxy = buckets.map((bucket) => number(bucket.proxy_observed));
  const axisMaximum = niceAxisMaximum(Math.max(...values, ...proxy));
  const windowMinutes = number(trend.window_minutes) || 60;
  const points = (series: number[]) => series.map((value, index) => `${(index / (series.length - 1)) * 100},${92 - (value / axisMaximum) * 82}`).join(" ");
  return `<div class="traffic-chart-frame"><span class="chart-axis-label chart-axis-label--peak">${rate(axisMaximum)}</span><span class="chart-axis-label chart-axis-label--mid">${rate(axisMaximum / 2)}</span><span class="chart-axis-label chart-axis-label--zero">0</span><svg class="traffic-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="${tr("Traffic rate trend", "流量速率趋势")}">
    <path class="chart-grid chart-grid--reference" d="M0 10H100" />
    <path class="chart-grid" d="M0 51H100M0 92H100" />
    <polyline class="chart-line chart-line--mihomo" points="${points(values)}" />
    <polyline class="chart-line chart-line--proxy" points="${points(proxy)}" />
  </svg></div><div class="traffic-chart__timeline"><span>${tr(`${windowMinutes} min ago`, `${windowMinutes} 分钟前`)}</span><span>${tr("Now", "现在")}</span></div>`;
}

export function renderNetworkDetail(projection: AgentProjection, scope: AnalysisScope = "today", analysis: NetworkAnalysisData = { servicePoints: [], localPoints: [], vpsPoints: [] }, analysisLoading = false): string {
  const state = projection as unknown as Record<string, unknown>;
  const session = asRecord(state.session);
  const vps = asRecord(session.vps);
  const kernel = asRecord(session.kernel);
  const attribution = asRecord(session.attribution);
  const breakdown = asRecord(session.breakdown);
  const remoteServers = asArray(session.remote_servers);
  const remote = asRecord(state.vps);
  const usageChecks = new Map(asArray(remote.daily_usage_guards).map((usage) => [String(usage.source_id ?? ""), usage]));
  const users = asArray(asRecord(state.xray_stats).users);
  const trend = asRecord(session.trend);
  const trendWindowMinutes = number(trend.window_minutes) || 60;
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
      ${scope === "daily" ? dailyTrafficHistory(analysis, remoteServers, analysisLoading) : trafficTotalsChart(analysis.servicePoints, scope, analysisLoading)}
      <div class="network-breakdown">
        <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Remote host traffic", "远端主机流量")}</h3><span>${remoteServers.length}</span></div>
          <ul class="traffic-list">${remoteServers.map((server) => { const usage = usageChecks.get(String(server.id ?? "")); const level = String(usage?.level ?? "none"); return `<li class="${usage ? `traffic-list__host traffic-list__host--${escapeHtml(level)}` : ""}"><span>${escapeHtml(server.label)}</span><span class="traffic-list__values"><small>${tr("Bill", "账单")} ${formatBytes(server.total_bytes)} · Xray ${formatBytes(server.xray_logical_bytes)}</small>${usage ? `<strong class="daily-usage daily-usage--${escapeHtml(level)}">${formatBytes(usage.usage_bytes)} · ${dailyUsageThresholds(usage)}</strong>` : ""}</span></li>`; }).join("") || `<li class="empty">${tr("No remote host configured.", "尚未配置远端主机。")}</li>`}</ul>
        </article>
      </div>
      ${users.length ? `<article class="xray-panel"><div class="detail-panel__heading"><h3>${tr("Xray users", "Xray 用户")}</h3><span>${formatBytes(asRecord(state.xray_stats).total_bytes)}</span></div><ul class="traffic-list traffic-list--columns">${users.slice(0, 8).map((user) => `<li><span>${escapeHtml(user.label)}</span><strong>${formatBytes(user.total_bytes)}</strong></li>`).join("")}</ul></article>` : ""}
      <section class="trend-panel"><div class="detail-panel__heading"><h3>${tr(`Last ${trendWindowMinutes} minutes — rate`, `近 ${trendWindowMinutes} 分钟速率趋势`)}</h3><span>${tr("Per-minute rate", "每分钟速率")}</span></div>${trendSvg(trend)}<div class="chart-legend"><span><i class="chart-dot chart-dot--mihomo"></i>Mihomo</span><span><i class="chart-dot chart-dot--proxy"></i>${tr("Proxy route", "代理路径")}</span></div></section>
    </section>`;
}

function sourceLabel(source: SourceProjection): string {
  const status = source.enabled ? source.status : "disabled";
  const text = status === "ok" ? tr("online", "在线") : status === "disabled" ? tr("disabled", "已停用") : status;
  return `<li class="source-row"><span class="source-state source-state--${escapeHtml(status)}" aria-hidden="true"></span><span class="source-main"><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.kind)}</small></span><span class="source-status">${escapeHtml(text)}</span></li>`;
}

export function renderNetworkResourcePage(projection: AgentProjection, resource: ResourceProjection, sources: SourceProjection[], scope: AnalysisScope = "today", analysis: NetworkAnalysisData = { servicePoints: [], localPoints: [], vpsPoints: [] }, analysisLoading = false): string {
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("Network", "网络")}</h2></div></div>${renderNetworkDetail(projection, scope, analysis, analysisLoading)}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map(sourceLabel).join("") || `<li class="empty">${tr("No configured sources", "尚未配置数据源")}</li>`}</ul></article></section>`;
}
