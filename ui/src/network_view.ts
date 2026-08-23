import "./network_view.css";
import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatBytes, number } from "./format";
import { tr } from "./i18n";
import { TimeBucketBarBucket, TimeBucketBarSeries, renderTimeBucketBarChart } from "./time_bucket_bar_chart";
import { renderDailyActivityCalendar } from "./daily_activity_calendar";
import { NetworkAnalysisData, NetworkAnalysisSnapshot, NetworkHistoryVisual, NetworkTimeRange, NetworkViewMode, networkPathTotals } from "./network_analysis";
import { AttentionDiagnostic, renderAttentionDiagnostics } from "./attention_diagnostics";

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
  const visual = snapshot.mode === "billing" && snapshot.range === "recorded" ? `<div class="network-history-picker" role="group" aria-label="${tr("History visualization", "历史图表")}"><button type="button" class="network-history-visual${snapshot.historyVisual === "bars" ? " is-active" : ""}" data-network-history-visual="bars">${tr("Bars", "柱状")}</button><button type="button" class="network-history-visual${snapshot.historyVisual === "calendar" ? " is-active" : ""}" data-network-history-visual="calendar">${tr("Activity", "活动日历")}</button></div>` : "";
  return `<section class="network-analysis-toolbar">
    <div class="network-mode-tabs" role="tablist" aria-label="${tr("Network observation", "网络观测维度")}">${modes.map(([mode, label, detail]) => `<button type="button" role="tab" aria-selected="${mode === snapshot.mode}" class="network-mode-tab${mode === snapshot.mode ? " is-active" : ""}" data-network-mode="${mode}"><strong>${label}</strong><small>${detail}</small></button>`).join("")}</div>
    <div class="network-range-picker"><span>${tr("Time range", "时间范围")}</span><div role="group" aria-label="${tr("Network time range", "网络时间范围")}">${ranges.map(([range, label]) => `<button type="button" class="network-range${range === snapshot.range ? " is-active" : ""}" data-network-range="${range}">${label}</button>`).join("")}</div>${visual}</div>
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

function minutes(value: unknown): string {
  const seconds = number(value);
  return seconds >= 60 && seconds % 60 === 0
    ? tr(`${seconds / 60} min`, `${seconds / 60} 分钟`)
    : tr(`${seconds} sec`, `${seconds} 秒`);
}

function networkDiagnostics(
  projection: AgentProjection,
  resource: ResourceProjection,
  sources: SourceProjection[],
): AttentionDiagnostic[] {
  const diagnostics: AttentionDiagnostic[] = [];
  const projected = projection.infra.network_diagnostics;
  const traffic = projected?.traffic_alert;
  const trafficLevel = String(traffic?.level ?? "none");
  if (trafficLevel === "warning" || trafficLevel === "critical") {
    const windows = traffic?.windows ?? {};
    const values = windows[trafficLevel] ?? {};
    const up = number(values.up_bytes);
    const down = number(values.down_bytes);
    const threshold = number(traffic?.threshold_bytes?.[trafficLevel]);
    const duration = minutes(traffic?.window_seconds?.[trafficLevel]);
    const critical = trafficLevel === "critical";
    diagnostics.push({
      id: `traffic-${trafficLevel}`,
      level: trafficLevel,
      subject: tr("Realtime traffic", "实时流量"),
      title: critical ? tr("Combined traffic crossed the critical line", "合计流量超过严重线") : tr("One direction crossed the notice line", "单向流量超过提醒线"),
      current: critical
        ? tr(`${duration} combined ${formatBytes(up + down)} (up ${formatBytes(up)} · down ${formatBytes(down)})`, `${duration}合计 ${formatBytes(up + down)}（上行 ${formatBytes(up)} · 下行 ${formatBytes(down)}）`)
        : tr(`${duration}: up ${formatBytes(up)} · down ${formatBytes(down)}`, `${duration}：上行 ${formatBytes(up)} · 下行 ${formatBytes(down)}`),
      basis: critical
        ? tr(`combined > ${formatBytes(threshold)}`, `合计 > ${formatBytes(threshold)}`)
        : tr(`either direction > ${formatBytes(threshold)}`, `任一方向 > ${formatBytes(threshold)}`),
      action: tr("Inspect current attribution and active transfers; the alert clears automatically after the window returns below the line.", "查看当前流量归因与活跃传输；窗口回落到阈值内后会自动恢复。"),
    });
  }

  const guards = projected?.daily_usage_guards ?? asArray(asRecord(projection.vps).daily_usage_guards);
  for (const guard of guards) {
    const level = String(guard.level ?? "none");
    if (level !== "warning" && level !== "critical") continue;
    diagnostics.push({
      id: `daily-${String(guard.source_id ?? guard.label ?? diagnostics.length)}`,
      level,
      subject: String(guard.label ?? guard.source_id ?? "VPS"),
      title: level === "critical" ? tr("Today's billable traffic crossed the critical line", "今日计费流量超过严重线") : tr("Today's billable traffic crossed the notice line", "今日计费流量超过提醒线"),
      current: tr(`today ${formatBytes(guard.usage_bytes)}`, `今日 ${formatBytes(guard.usage_bytes)}`),
      basis: dailyUsageThresholds(guard),
      action: tr("Check this host's traffic composition and billing direction; the daily state resets at the next local day.", "检查该主机的流量构成与计费方向；每日状态会在下一个本地自然日重置。"),
    });
  }

  const collectors = projection.infra.collectors ?? [];
  const collectorBySource = new Map(collectors.map((collector) => [collector.capability.source_id, collector]));
  for (const source of sources) {
    if (!source.enabled || ["ok", "waiting", "baseline", "disabled"].includes(source.status)) continue;
    const collector = collectorBySource.get(source.id);
    diagnostics.push({
      id: `source-${source.id}`,
      level: "degraded",
      subject: source.label,
      title: tr("Collector is not returning usable data", "采集器未返回可用数据"),
      current: tr(`status ${source.status}${collector?.error_kind ? ` · ${collector.error_kind}` : ""}`, `状态 ${source.status}${collector?.error_kind ? ` · ${collector.error_kind}` : ""}`),
      basis: tr("Three consecutive collection failures are required before this source is marked unavailable.", "连续 3 次采集失败后，才会将该来源标记为不可用。"),
      action: tr("Check the source process and its configured connection; Sentinel will keep retrying automatically.", "检查来源进程及其连接配置；Sentinel 会继续自动重试。"),
    });
  }
  if (!diagnostics.length && ["warning", "critical", "degraded"].includes(String(resource.status))) {
    diagnostics.push({
      id: "network-unresolved",
      level: resource.status === "critical" ? "critical" : resource.status === "warning" ? "warning" : "degraded",
      subject: tr("Network", "网络"),
      title: tr("The current status has no matching diagnostic evidence", "当前状态缺少对应的诊断证据"),
      current: tr(`resource status ${resource.status}`, `资源状态 ${resource.status}`),
      action: tr("Refresh once; if this remains, inspect collector sources below.", "刷新一次；若仍存在，请检查下方采集数据源。"),
    });
  }
  return diagnostics;
}

function billingHistory(analysis: NetworkAnalysisData, remoteServers: Record<string, unknown>[], range: NetworkTimeRange, visual: NetworkHistoryVisual): string {
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
  const series: TimeBucketBarSeries[] = visible.map((id, index) => ({
    id,
    label: hostLabels.get(id.replace(/^vps:/, "")) ?? id.replace(/^vps:/, ""),
    color: TRAFFIC_COLORS[index],
  }));
  if (hasOther) series.push({ id: "__other_vps__", label: tr("Other VPS", "其他 VPS"), color: "#7b8794" });
  const buckets: TimeBucketBarBucket[] = [...valuesByBucket.entries()].sort(([left], [right]) => left - right).map(([epoch, values]) => {
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
  if (range === "recorded" && visual === "calendar") {
    return renderDailyActivityCalendar(series, buckets, {
      title: tr("Daily VPS billing activity", "VPS 每日账单活动"),
      detail: tr("Latest year of recorded history", "最近一年的已记录历史"),
      ariaLabel: tr("Daily VPS billable traffic activity", "VPS 每日计费流量活动"),
      formatValue: formatBytes,
      endEpoch: Math.max(...buckets.map((bucket) => bucket.epoch), Date.now() / 1_000),
      footnote: tr("Each cell is one day. Darker cells mean relatively higher billable traffic; hover or focus a cell for its host breakdown. Daily notice and critical levels remain independent per host.", "每格代表一天；颜色越深表示该日相对账单流量越高，悬停或点选可查看主机构成。每日提醒与严重阈值仍由每台主机独立判断。"),
    });
  }
  return renderTimeBucketBarChart(series, buckets, {
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
  visual: NetworkHistoryVisual,
): string {
  const recordedByHost = totalsBySource(analysis.vpsPoints);
  return `<section class="network-view-panel">
    <div class="network-view-heading"><div><p>${tr("Selected range", "所选范围")}</p><strong>${formatBytes(total(analysis.vpsPoints))}</strong></div><span>${rangeLabel(range)} · ${tr("billable VPS traffic", "VPS 计费流量")}</span></div>
    ${billingHistory(analysis, remoteServers, range, visual)}
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

function xrayClientTotals(
  points: Record<string, unknown>[],
  remoteServers: Record<string, unknown>[],
): Array<{ label: string; total: number }> {
  const hostLabels = new Map(remoteServers.map((server) => [
    `xray:${String(server.id ?? "")}`,
    String(server.label ?? server.id ?? "VPS"),
  ]));
  const totals = new Map<string, { label: string; total: number }>();
  for (const point of points) {
    const sourceId = String(point.source_id ?? "xray:unknown");
    const dimensions = asRecord(point.dimensions);
    const client = String(dimensions.client ?? "unknown");
    const key = `${sourceId}\u0000${client}`;
    const row = totals.get(key) ?? {
      label: `${hostLabels.get(sourceId) ?? sourceId.replace(/^xray:/, "")} / ${client}`,
      total: 0,
    };
    row.total += number(point.value);
    totals.set(key, row);
  }
  return [...totals.values()]
    .filter((row) => row.total > 0)
    .sort((left, right) => right.total - left.total || left.label.localeCompare(right.label));
}

function renderAttributionView(
  analysis: NetworkAnalysisData,
  remoteServers: Record<string, unknown>[],
  range: NetworkTimeRange,
): string {
  const services = serviceTotals(analysis.servicePoints);
  const attributed = [...services.values()].filter((row) => !row.unattributed).sort((left, right) => right.total - left.total);
  const localTotal = total(analysis.localPoints);
  const attributedTotal = attributed.reduce((sum, row) => sum + row.total, 0);
  const unattributed = Math.max(0, localTotal - attributedTotal);
  const coverage = localTotal > 0 ? attributedTotal / localTotal : 0;
  const visible = attributed.slice(0, 8);
  const maximum = Math.max(...visible.map((row) => row.total), 1);
  const xrayClients = xrayClientTotals(analysis.xrayPoints, remoteServers);
  return `<section class="network-view-panel">
    <div class="network-view-heading"><div><p>${tr("Attributed local traffic", "本机已归因流量")}</p><strong>${formatBytes(attributedTotal)}</strong></div><span>${rangeLabel(range)} · ${tr(`${(coverage * 100).toFixed(1)}% coverage`, `覆盖率 ${(coverage * 100).toFixed(1)}%`)}</span></div>
    <div class="network-attribution-grid">
      <article class="detail-panel traffic-total-chart"><div class="detail-panel__heading"><h3>${tr("Service attribution", "服务流量归因")}</h3><span>Mihomo</span></div><div class="traffic-total-bars">${visible.map((row, index) => `<div class="traffic-total-bar"><div><span><i class="chart-dot" style="background:${TRAFFIC_COLORS[index % TRAFFIC_COLORS.length]}"></i>${escapeHtml(row.label)}</span><strong>${formatBytes(row.total)}</strong></div><p><i style="background:${TRAFFIC_COLORS[index % TRAFFIC_COLORS.length]};width:${Math.max(1, (row.total / maximum) * 100)}%"></i></p></div>`).join("") || `<div class="chart-empty">${tr("Waiting for attributed service samples.", "等待已归因的服务采样。")}</div>`}</div><p class="panel-footnote">${tr(`Local total ${formatBytes(localTotal)} · unattributed ${formatBytes(unattributed)}`, `本机总量 ${formatBytes(localTotal)} · 未归因 ${formatBytes(unattributed)}`)}</p></article>
      <article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Xray client traffic", "Xray 客户端流量")}</h3><span>${rangeLabel(range)} · ${xrayClients.length}</span></div><ul class="traffic-list">${xrayClients.slice(0, 10).map((client) => `<li><span>${escapeHtml(client.label)}</span><strong>${formatBytes(client.total)}</strong></li>`).join("") || `<li class="empty">${tr("No Xray client traffic in this range.", "所选范围内暂无 Xray 客户端流量。")}</li>`}</ul><p class="panel-footnote">${tr("Xray traffic is derived from recorded interval deltas and uses the same selected time range as the other network measurements.", "Xray 流量由已记录的间隔增量聚合，与其他网络指标使用同一所选时间范围。")}</p></article>
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
  const trend = asRecord(session.trend);
  const content = snapshot.loading && !snapshot.data.servicePoints.length && !snapshot.data.vpsPoints.length && !snapshot.data.xrayPoints.length
    ? loadingPanel(tr("Loading recorded network metrics…", "正在读取已记录的网络指标…"))
    : snapshot.error
      ? errorPanel(snapshot.error)
      : snapshot.mode === "billing"
        ? renderBillingView(snapshot.data, remoteServers, usageChecks, snapshot.range, snapshot.historyVisual)
        : snapshot.mode === "attribution"
          ? renderAttributionView(snapshot.data, remoteServers, snapshot.range)
          : renderEfficiencyView(snapshot.data, remoteServers, snapshot.range, trend);
  return `<section class="network-detail">${renderCurrentSummary(snapshot.data, usageChecks, snapshot.range, snapshot.loading, snapshot.ready)}${renderControls(snapshot)}${content}</section>`;
}

function sourceLabel(source: SourceProjection): string {
  const status = source.enabled ? source.status : "disabled";
  const labels: Record<string, string> = {
    ok: tr("online", "在线"),
    disabled: tr("disabled", "已停用"),
    waiting: tr("establishing baseline", "正在建立基线"),
    baseline: tr("establishing baseline", "正在建立基线"),
    error: tr("collection failed", "采集失败"),
  };
  const text = labels[status] ?? status;
  return `<li class="source-row"><span class="source-state source-state--${escapeHtml(status)}" aria-hidden="true"></span><span class="source-main"><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.kind)}</small></span><span class="source-status">${escapeHtml(text)}</span></li>`;
}

export function renderNetworkResourcePage(projection: AgentProjection, resource: ResourceProjection, sources: SourceProjection[], snapshot: NetworkAnalysisSnapshot): string {
  const diagnostics = renderAttentionDiagnostics(networkDiagnostics(projection, resource, sources));
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("Network", "网络")}</h2></div></div>${diagnostics}${renderNetworkDetail(projection, snapshot)}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map(sourceLabel).join("") || `<li class="empty">${tr("No configured sources", "尚未配置数据源")}</li>`}</ul></article></section>`;
}
