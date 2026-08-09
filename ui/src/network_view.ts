import "./network_view.css";
import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatBytes, number } from "./format";
import { tr } from "./i18n";
import { DailyBarBucket, DailyBarSeries, renderDailyBarChart } from "./daily_bar_chart";
import { NetworkAnalysisData, NetworkAnalysisSnapshot, NetworkTimeRange, NetworkViewMode, networkPathTotals } from "./network_analysis";

const TRAFFIC_COLORS = ["#3178dc", "#2f9461", "#2e9298", "#9468c9", "#c77b2c"];

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function rate(value: unknown): string {
  return `${formatBytes(value)}${tr("/min", "/分钟")}`;
}

function total(points: Record<string, unknown>[]): number {
  return points.reduce((sum, point) => sum + number(point.value), 0);
}

function totalsBySource(points: Record<string, unknown>[]): Map<string, number> {
  const totals = new Map<string, number>();
  for (const point of points) {
    const source = String(point.source_id || "unknown");
    totals.set(source, (totals.get(source) ?? 0) + number(point.value));
  }
  return totals;
}

function rangeLabel(range: NetworkTimeRange): string {
  const labels: Record<NetworkTimeRange, string> = {
    today: tr("Today", "今日"),
    "7d": tr("Last 7 days", "近 7 天"),
    "30d": tr("Last 30 days", "近 30 天"),
    recorded: tr("Recorded history", "记录累计"),
  };
  return labels[range];
}

function renderControls(snapshot: NetworkAnalysisSnapshot): string {
  const modes: Array<[NetworkViewMode, string, string]> = [
    ["billing", tr("Billing usage", "账单用量"), tr("VPS billable traffic", "VPS 计费流量")],
    ["attribution", tr("Attribution", "流量归因"), tr("Local services and clients", "本地服务与客户端")],
    ["efficiency", tr("Link efficiency", "链路效率"), tr("Logical traffic versus billing", "逻辑流量与账单对照")],
  ];
  const ranges: Array<[NetworkTimeRange, string]> = [
    ["today", tr("Today", "今日")],
    ["7d", tr("7 days", "7 天")],
    ["30d", tr("30 days", "30 天")],
    ["recorded", tr("Recorded", "记录累计")],
  ];
  return `<section class="network-analysis-toolbar">
    <div class="network-mode-tabs" role="tablist" aria-label="${tr("Network observation", "网络观测维度")}">${modes.map(([mode, label, detail]) => `<button type="button" role="tab" aria-selected="${mode === snapshot.mode}" class="network-mode-tab${mode === snapshot.mode ? " is-active" : ""}" data-network-mode="${mode}"><strong>${label}</strong><small>${detail}</small></button>`).join("")}</div>
    <div class="network-range-picker"><span>${tr("Time range", "时间范围")}</span><div role="group" aria-label="${tr("Network time range", "网络时间范围")}">${ranges.map(([range, label]) => `<button type="button" class="network-range${range === snapshot.range ? " is-active" : ""}" data-network-range="${range}">${label}</button>`).join("")}</div></div>
  </section>`;
}

function loadingPanel(message: string): string {
  return `<article class="detail-panel network-analysis-state"><span class="pulse" aria-hidden="true"></span><p>${escapeHtml(message)}</p></article>`;
}

function errorPanel(message: string): string {
  return `<article class="detail-panel network-analysis-state network-analysis-state--error"><strong>${tr("Recorded metrics unavailable", "暂时无法读取历史指标")}</strong><p>${escapeHtml(message)}</p></article>`;
}

function dailyUsageThresholds(usage: Record<string, unknown>): string {
  return tr(
    `notice ${formatBytes(usage.warning_bytes)} · critical ${formatBytes(usage.critical_bytes)}`,
    `提醒 ${formatBytes(usage.warning_bytes)} · 严重 ${formatBytes(usage.critical_bytes)}`,
  );
}

function billingHistory(analysis: NetworkAnalysisData, remoteServers: Record<string, unknown>[], range: NetworkTimeRange): string {
  const hostLabels = new Map(remoteServers.map((server) => [String(server.id ?? ""), String(server.label ?? server.id ?? "VPS")]));
  const sourceTotals = totalsBySource(analysis.vpsPoints);
  const valuesByBucket = new Map<number, Map<string, number>>();
  for (const point of analysis.vpsPoints) {
    const epoch = number(point.observed_epoch);
    if (!epoch) continue;
    const sourceId = String(point.source_id || "vps:unknown");
    const values = valuesByBucket.get(epoch) ?? new Map<string, number>();
    values.set(sourceId, (values.get(sourceId) ?? 0) + number(point.value));
    valuesByBucket.set(epoch, values);
  }
  if (!valuesByBucket.size) return `<article class="detail-panel network-analysis-state"><p>${tr("Billing history begins after a complete remote sample interval is stored.", "保存首个完整远端采样区间后，这里会出现账单历史。")}</p></article>`;
  const ranked = [...sourceTotals.entries()].sort((left, right) => right[1] - left[1]);
  const visible = ranked.slice(0, 4).map(([id]) => id);
  const hasOther = ranked.length > visible.length;
  const series: DailyBarSeries[] = visible.map((id, index) => ({
    id,
    label: hostLabels.get(id.replace(/^vps:/, "")) ?? id.replace(/^vps:/, ""),
    color: TRAFFIC_COLORS[index],
  }));
  if (hasOther) series.push({ id: "__other_vps__", label: tr("Other VPS", "其他 VPS"), color: "#7b8794" });
  const buckets: DailyBarBucket[] = [...valuesByBucket.entries()].sort(([left], [right]) => left - right).map(([epoch, values]) => {
    if (!hasOther) return { epoch, values };
    const normalized = new Map(values);
    for (const [sourceId, value] of values) {
      if (!visible.includes(sourceId)) {
        normalized.delete(sourceId);
        normalized.set("__other_vps__", (normalized.get("__other_vps__") ?? 0) + value);
      }
    }
    return { epoch, values: normalized };
  });
  return renderDailyBarChart(series, buckets, {
    title: tr("VPS billable usage", "VPS 账单用量"),
    detail: `${rangeLabel(range)} · ${tr("all configured hosts", "全部已配置主机")}`,
    ariaLabel: tr("Billable traffic composition across configured VPS hosts", "已配置 VPS 主机的账单流量构成"),
    formatValue: formatBytes,
    mode: "stacked",
    footnote: tr("Colors are additive host components of the bill. Daily notice and critical levels remain independent per host.", "颜色表示总账单中各主机的组成；每日提醒与严重阈值仍由每台主机独立判断。"),
  });
}

function renderBillingView(
  analysis: NetworkAnalysisData,
  remoteServers: Record<string, unknown>[],
  usageChecks: Map<string, Record<string, unknown>>,
  range: NetworkTimeRange,
): string {
  const recordedByHost = totalsBySource(analysis.vpsPoints);
  return `<section class="network-view-panel">
    <div class="network-view-heading"><div><p>${tr("Selected range", "所选范围")}</p><strong>${formatBytes(total(analysis.vpsPoints))}</strong></div><span>${rangeLabel(range)} · ${tr("billable VPS traffic", "VPS 计费流量")}</span></div>
    ${billingHistory(analysis, remoteServers, range)}
    <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Host billing checks", "主机用量检测")}</h3><span>${remoteServers.length}</span></div><ul class="traffic-list network-host-list">${remoteServers.map((server) => {
      const id = String(server.id ?? "");
      const usage = usageChecks.get(id);
      const level = String(usage?.level ?? "none");
      const recorded = recordedByHost.get(`vps:${id}`) ?? 0;
      const direction = String(server.billing_mode ?? "both") === "outbound" ? tr("outbound billed", "仅出站计费") : tr("in + out billed", "入站 + 出站计费");
      return `<li class="network-host-row${level === "warning" || level === "critical" ? ` network-host-row--${escapeHtml(level)}` : ""}"><span class="network-host-name"><strong>${escapeHtml(server.label ?? id)}</strong><small>${escapeHtml(direction)}</small></span><span class="traffic-list__values"><strong>${rangeLabel(range)} ${formatBytes(recorded)}</strong>${usage ? `<small class="daily-usage daily-usage--${escapeHtml(level)}">${tr("Today", "今日")} ${formatBytes(usage.usage_bytes)} · ${dailyUsageThresholds(usage)}</small>` : `<small>${tr("Daily check disabled", "未启用每日检测")}</small>`}</span></li>`;
    }).join("") || `<li class="empty">${tr("No remote host configured.", "尚未配置远端主机。")}</li>`}</ul></article>
  </section>`;
}

function serviceTotals(points: Record<string, unknown>[]): Map<string, { label: string; total: number; unattributed: boolean }> {
  const totals = new Map<string, { label: string; total: number; unattributed: boolean }>();
  for (const point of points) {
    const dimensions = asRecord(point.dimensions);
    const id = String(dimensions.service || "unknown");
    const row = totals.get(id) ?? { label: String(dimensions.label || id), total: 0, unattributed: id === "unattributed" };
    row.total += number(point.value);
    totals.set(id, row);
  }
  return totals;
}

function renderAttributionView(analysis: NetworkAnalysisData, users: Record<string, unknown>[], range: NetworkTimeRange): string {
  const services = serviceTotals(analysis.servicePoints);
  const attributed = [...services.values()].filter((row) => !row.unattributed).sort((left, right) => right.total - left.total);
  const localTotal = total(analysis.localPoints);
  const attributedTotal = attributed.reduce((sum, row) => sum + row.total, 0);
  const unattributed = Math.max(0, localTotal - attributedTotal);
  const coverage = localTotal > 0 ? attributedTotal / localTotal : 0;
  const visible = attributed.slice(0, 8);
  const maximum = Math.max(...visible.map((row) => row.total), 1);
  return `<section class="network-view-panel">
    <div class="network-view-heading"><div><p>${tr("Attributed local traffic", "本机已归因流量")}</p><strong>${formatBytes(attributedTotal)}</strong></div><span>${rangeLabel(range)} · ${tr(`${(coverage * 100).toFixed(1)}% coverage`, `覆盖率 ${(coverage * 100).toFixed(1)}%`)}</span></div>
    <div class="network-attribution-grid">
      <article class="detail-panel traffic-total-chart"><div class="detail-panel__heading"><h3>${tr("Service attribution", "服务流量归因")}</h3><span>Mihomo</span></div><div class="traffic-total-bars">${visible.map((row, index) => `<div class="traffic-total-bar"><div><span><i class="chart-dot" style="background:${TRAFFIC_COLORS[index % TRAFFIC_COLORS.length]}"></i>${escapeHtml(row.label)}</span><strong>${formatBytes(row.total)}</strong></div><p><i style="background:${TRAFFIC_COLORS[index % TRAFFIC_COLORS.length]};width:${Math.max(1, (row.total / maximum) * 100)}%"></i></p></div>`).join("") || `<div class="chart-empty">${tr("Waiting for attributed service samples.", "等待已归因的服务采样。")}</div>`}</div><p class="panel-footnote">${tr(`Local total ${formatBytes(localTotal)} · unattributed ${formatBytes(unattributed)}`, `本机总量 ${formatBytes(localTotal)} · 未归因 ${formatBytes(unattributed)}`)}</p></article>
      <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Xray clients", "Xray 客户端")}</h3><span>${users.length}</span></div><ul class="traffic-list">${users.slice(0, 10).map((user) => `<li><span>${escapeHtml(user.label)}</span><strong>${formatBytes(user.total_bytes)}</strong></li>`).join("") || `<li class="empty">${tr("No Xray client counters available.", "暂无 Xray 客户端计数。")}</li>`}</ul><p class="panel-footnote">${tr("Client counters use the current Xray baseline; the time selector applies to local service attribution above.", "客户端计数采用当前 Xray 基线；时间范围仅作用于上方本地服务归因。")}</p></article>
    </div>
  </section>`;
}

function comparisonLabel(ratio: number, baseline: number): string {
  if (!ratio) return tr("Waiting for matching samples", "等待匹配采样");
  const uplift = (ratio / baseline - 1) * 100;
  return uplift >= 0
    ? tr(`${uplift.toFixed(1)}% above ${baseline.toFixed(0)}× baseline`, `高于 ${baseline.toFixed(0)}× 基线 ${uplift.toFixed(1)}%`)
    : tr(`${Math.abs(uplift).toFixed(1)}% below ${baseline.toFixed(0)}× baseline`, `低于 ${baseline.toFixed(0)}× 基线 ${Math.abs(uplift).toFixed(1)}%`);
}

function renderEfficiencyView(analysis: NetworkAnalysisData, remoteServers: Record<string, unknown>[], range: NetworkTimeRange, trend: Record<string, unknown>): string {
  const vpsTotals = totalsBySource(analysis.vpsPoints);
  const xrayTotals = totalsBySource(analysis.xrayPoints);
  const trendWindowMinutes = number(trend.window_minutes) || 60;
  return `<section class="network-view-panel">
    <div class="network-view-heading"><div><p>${tr("Comparable hosts", "可比较主机")}</p><strong>${remoteServers.filter((server) => (xrayTotals.get(`xray:${server.id}`) ?? 0) > 0).length} / ${remoteServers.length}</strong></div><span>${rangeLabel(range)} · ${tr("ratios remain per host", "倍率始终按主机独立计算")}</span></div>
    <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Billable versus logical traffic", "账单流量与逻辑流量")}</h3><span>${rangeLabel(range)}</span></div><div class="efficiency-table">${remoteServers.map((server) => {
      const id = String(server.id ?? "");
      const billed = vpsTotals.get(`vps:${id}`) ?? 0;
      const logical = xrayTotals.get(`xray:${id}`) ?? 0;
      const ratio = logical > 0 ? billed / logical : 0;
      const baseline = String(server.billing_mode ?? "both") === "outbound" ? 1 : 2;
      return `<div class="efficiency-row"><div><strong>${escapeHtml(server.label ?? id)}</strong><small>${String(server.billing_mode ?? "both") === "outbound" ? tr("one-leg billing", "单边计费") : tr("two-leg billing", "双边计费")}</small></div><dl><div><dt>${tr("Xray logical", "Xray 逻辑量")}</dt><dd>${formatBytes(logical)}</dd></div><div><dt>${tr("VPS billed", "VPS 账单量")}</dt><dd>${formatBytes(billed)}</dd></div><div><dt>${tr("Observed ratio", "实测倍率")}</dt><dd>${ratio ? `${ratio.toFixed(2)}×` : "—"}</dd></div></dl><span class="efficiency-row__status">${escapeHtml(comparisonLabel(ratio, baseline))}</span></div>`;
    }).join("") || `<div class="chart-empty">${tr("No comparable remote hosts.", "暂无可比较的远端主机。")}</div>`}</div><p class="panel-footnote">${tr("A multiplier is a relationship between one host's Xray logical traffic and that same host's billable interface traffic. It is never summed or averaged across hosts.", "倍率只描述同一主机的 Xray 逻辑流量与该主机计费网卡流量之间的关系；不会跨主机求和或平均。")}</p></article>
    <section class="trend-panel"><div class="detail-panel__heading"><h3>${tr(`Last ${trendWindowMinutes} minutes — local rate`, `近 ${trendWindowMinutes} 分钟本机速率`)}</h3><span>${tr("Per-minute rate", "每分钟速率")}</span></div>${trendSvg(trend)}<div class="chart-legend"><span><i class="chart-dot chart-dot--mihomo"></i>Mihomo</span><span><i class="chart-dot chart-dot--proxy"></i>${tr("Proxy route", "代理路径")}</span></div></section>
  </section>`;
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
  if (buckets.length < 2) return `<div class="chart-empty">${tr("Waiting for enough realtime samples.", "等待足够的实时采样。")}</div>`;
  const values = buckets.map((bucket) => number(bucket.mihomo_total));
  const proxy = buckets.map((bucket) => number(bucket.proxy_observed));
  const axisMaximum = niceAxisMaximum(Math.max(...values, ...proxy));
  const windowMinutes = number(trend.window_minutes) || 60;
  const points = (series: number[]) => series.map((value, index) => `${(index / (series.length - 1)) * 100},${92 - (value / axisMaximum) * 82}`).join(" ");
  return `<div class="traffic-chart-frame"><span class="chart-axis-label chart-axis-label--peak">${rate(axisMaximum)}</span><span class="chart-axis-label chart-axis-label--mid">${rate(axisMaximum / 2)}</span><span class="chart-axis-label chart-axis-label--zero">0</span><svg class="traffic-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="${tr("Traffic rate trend", "流量速率趋势")}"><path class="chart-grid chart-grid--reference" d="M0 10H100"/><path class="chart-grid" d="M0 51H100M0 92H100"/><polyline class="chart-line chart-line--mihomo" points="${points(values)}"/><polyline class="chart-line chart-line--proxy" points="${points(proxy)}"/></svg></div><div class="traffic-chart__timeline"><span>${tr(`${windowMinutes} min ago`, `${windowMinutes} 分钟前`)}</span><span>${tr("Now", "现在")}</span></div>`;
}

function renderCurrentSummary(
  analysis: NetworkAnalysisData,
  usageChecks: Map<string, Record<string, unknown>>,
  range: NetworkTimeRange,
  loading: boolean,
  ready: boolean,
): string {
  const { local, proxy, xray, billed } = networkPathTotals(analysis);
  const critical = [...usageChecks.values()].filter((usage) => usage.level === "critical").length;
  const warning = [...usageChecks.values()].filter((usage) => usage.level === "warning").length;
  const dailyStatus = range === "today";
  const status = loading
    ? tr("Loading selected range", "正在读取所选范围")
    : dailyStatus && critical
      ? tr(`${critical} critical host${critical === 1 ? "" : "s"}`, `${critical} 台主机严重`)
      : dailyStatus && warning
        ? tr(`${warning} host${warning === 1 ? "" : "s"} need attention`, `${warning} 台主机需关注`)
        : tr("Selected range loaded", "所选范围已对齐");
  const statusTone = dailyStatus && critical ? "critical" : dailyStatus && warning ? "warning" : "healthy";
  const value = (amount: number) => !ready ? "—" : formatBytes(amount);
  return `<section class="network-ledger-summary">
    <article class="network-ledger-primary"><p>${tr("VPS billing in range", "所选范围 VPS 账单量")}</p><strong>${value(billed)}</strong><small>${rangeLabel(range)} · ${tr("configured billing direction per host", "按每台主机配置的计费方向汇总")}</small><span class="network-ledger-status network-ledger-status--${statusTone}">${status}</span></article>
    <article class="network-path-card"><div class="detail-panel__heading"><h3>${tr("Observation path in range", "所选范围观测链路")}</h3><span>${rangeLabel(range)} · ${tr("same window · not additive", "同一时间范围 · 不可相加")}</span></div><div class="network-path"><span><small>Mihomo</small><strong>${value(local)}</strong></span><i>→</i><span><small>${tr("Proxy route", "代理路径")}</small><strong>${value(proxy)}</strong></span><i>→</i><span><small>Xray</small><strong>${value(xray)}</strong></span><i>→</i><span><small>${tr("VPS billed", "VPS 账单")}</small><strong>${value(billed)}</strong></span></div></article>
  </section>`;
}

export function renderNetworkDetail(projection: AgentProjection, snapshot: NetworkAnalysisSnapshot): string {
  const state = projection as unknown as Record<string, unknown>;
  const session = asRecord(state.session);
  const remoteServers = asArray(session.remote_servers);
  const usageChecks = new Map(asArray(asRecord(state.vps).daily_usage_guards).map((usage) => [String(usage.source_id ?? ""), usage]));
  const users = asArray(asRecord(state.xray_stats).users);
  const trend = asRecord(session.trend);
  const content = snapshot.loading && !snapshot.data.servicePoints.length && !snapshot.data.vpsPoints.length && !snapshot.data.xrayPoints.length
    ? loadingPanel(tr("Loading recorded network metrics…", "正在读取已记录的网络指标…"))
    : snapshot.error
      ? errorPanel(snapshot.error)
      : snapshot.mode === "billing"
        ? renderBillingView(snapshot.data, remoteServers, usageChecks, snapshot.range)
        : snapshot.mode === "attribution"
          ? renderAttributionView(snapshot.data, users, snapshot.range)
          : renderEfficiencyView(snapshot.data, remoteServers, snapshot.range, trend);
  return `<section class="network-detail">${renderCurrentSummary(snapshot.data, usageChecks, snapshot.range, snapshot.loading, snapshot.ready)}${renderControls(snapshot)}${content}</section>`;
}

function sourceLabel(source: SourceProjection): string {
  const status = source.enabled ? source.status : "disabled";
  const text = status === "ok" ? tr("online", "在线") : status === "disabled" ? tr("disabled", "已停用") : status;
  return `<li class="source-row"><span class="source-state source-state--${escapeHtml(status)}" aria-hidden="true"></span><span class="source-main"><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.kind)}</small></span><span class="source-status">${escapeHtml(text)}</span></li>`;
}

export function renderNetworkResourcePage(projection: AgentProjection, _resource: ResourceProjection, sources: SourceProjection[], snapshot: NetworkAnalysisSnapshot): string {
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("Network", "网络")}</h2></div></div>${renderNetworkDetail(projection, snapshot)}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map(sourceLabel).join("") || `<li class="empty">${tr("No configured sources", "尚未配置数据源")}</li>`}</ul></article></section>`;
}
