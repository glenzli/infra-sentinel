import "./ai_usage_view.css";
import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatTokens, number } from "./format";
import { currentLocale, tr } from "./i18n";
import { DailyBarBucket, DailyBarSeries, renderDailyBarChart } from "./daily_bar_chart";
import { AiAnalysisSnapshot, AiTimeRange, AiViewMode } from "./ai_analysis";
import { AnalysisTimeWindow } from "./analysis_time";
import { AttentionDiagnostic, renderAttentionDiagnostics } from "./attention_diagnostics";
import {
  ProjectedUsage, UsageInterval, aggregateByModel, aggregateBySource, completeDailyBuckets,
  completeRateEpochs, projectUsage,
} from "./ai_usage_series";

const MODEL_COLORS = ["#3178dc", "#9168c6", "#329260", "#c7792d", "#278d94", "#7b8794"];
const SOURCE_COLORS = ["#3178dc", "#9168c6", "#329260", "#c7792d"];

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function modelLabel(identifier: string): string {
  return identifier === "__unattributed__" ? tr("Missing model metadata", "缺少模型信息") : identifier;
}

function localized(value: unknown): string {
  const text = asRecord(value);
  return String(text[currentLocale() === "zh" ? "zh" : "en"] ?? text.en ?? text.zh ?? "");
}

function formatMetric(value: unknown, unit: unknown = "tokens"): string {
  if (unit === "count") return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(number(value));
  if (unit === "usd") return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(number(value));
  return formatTokens(value);
}

function rangeLabel(range: AiTimeRange): string {
  const labels: Record<AiTimeRange, string> = {
    today: tr("Today", "今日"),
    "7d": tr("Last 7 days", "近 7 天"),
    "30d": tr("Last 30 days", "近 30 天"),
    recorded: tr("All history", "全部历史"),
  };
  return labels[range];
}

function windowOf(source: Record<string, unknown>, window: "today" | "cumulative"): Record<string, unknown> {
  return asRecord(asRecord(source.usage)[window]);
}

function renderControls(snapshot: AiAnalysisSnapshot): string {
  const modes: Array<[AiViewMode, string, string]> = [
    ["overview", tr("Usage overview", "用量总览"), tr("Totals and source composition", "总量与来源构成")],
    ["models", tr("Model analysis", "模型分析"), tr("Model share and rate", "模型占比与速率")],
    ["activity", tr("Agent activity", "Agent 活动"), tr("Provider-specific diagnostics", "来源特有诊断")],
  ];
  const ranges: Array<[AiTimeRange, string]> = [
    ["today", tr("Today", "今日")], ["7d", tr("7 days", "7 天")],
    ["30d", tr("30 days", "30 天")], ["recorded", tr("All", "全部")],
  ];
  return `<section class="ai-analysis-toolbar"><div class="ai-mode-tabs" role="tablist" aria-label="${tr("AI usage observation", "AI 用量观测维度")}">${modes.map(([mode, label, detail]) => `<button type="button" role="tab" aria-selected="${mode === snapshot.mode}" class="ai-mode-tab${mode === snapshot.mode ? " is-active" : ""}" data-ai-mode="${mode}"><strong>${label}</strong><small>${detail}</small></button>`).join("")}</div>${snapshot.mode === "activity" ? `<span class="ai-current-context">${tr("Current provider snapshot", "当前来源快照")}</span>` : `<div class="ai-range-picker"><span>${tr("Time range", "时间范围")}</span><div role="group">${ranges.map(([range, label]) => `<button type="button" class="ai-range${range === snapshot.range ? " is-active" : ""}" data-ai-range="${range}">${label}</button>`).join("")}</div></div>`}</section>`;
}

function shortStartedAt(value: unknown): string {
  const date = new Date(String(value ?? ""));
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderCurrentSummary(providerSources: Record<string, unknown>[], sources: SourceProjection[]): string {
  const today = providerSources.reduce((sum, source) => sum + number(windowOf(source, "today").tokens), 0);
  const cumulative = providerSources.reduce((sum, source) => sum + number(windowOf(source, "cumulative").tokens), 0);
  const online = sources.filter((source) => source.enabled && source.status === "ok").length;
  const coverage = providerSources.map((source) => {
    const window = windowOf(source, "today");
    const started = shortStartedAt(window.started_at);
    const method = String(window.method ?? "");
    const sourceDetail = localized(window.detail);
    const detail = started
      ? [tr(`since ${started}`, `${started} 起`), sourceDetail].filter(Boolean).join(" · ")
      : method === "provider-day" ? tr("provider day", "供应商自然日") : sourceDetail;
    return `<span><i class="source-state source-state--${escapeHtml(String(source.status ?? "ok"))}"></i><strong>${escapeHtml(source.label ?? source.source_id)}</strong><small>${escapeHtml(detail)}</small></span>`;
  }).join("");
  return `<section class="ai-usage-summary"><article><p>${tr("Observed today", "今日已观测")}</p><strong>${formatTokens(today)}</strong><small>${tr("Available source windows combined", "合并当前可用来源窗口")}</small></article><article><p>${tr("Local history total", "本机历史总量")}</p><strong>${formatTokens(cumulative)}</strong><small>${tr("All readable local records · not billing", "全部可读本地记录 · 非账单")}</small></article><article><p>${tr("Collector coverage", "采集覆盖")}</p><strong>${online} / ${sources.length}</strong><small>${tr("sources online", "个来源在线")}</small></article><div class="ai-window-coverage"><b>${tr("Window coverage", "统计窗口")}</b>${coverage}</div></section>`;
}

function horizontalBars(
  title: string, detail: string, totals: Map<string, number>, colors: string[],
  labelFor: (identifier: string) => string = (identifier) => identifier,
): string {
  const ranked = [...totals.entries()].filter(([, value]) => value > 0).sort((left, right) => right[1] - left[1]).slice(0, 8);
  const maximum = Math.max(...ranked.map(([, value]) => value), 1);
  return `<article class="detail-panel ai-ranked-panel"><div class="detail-panel__heading"><h3>${escapeHtml(title)}</h3><span>${escapeHtml(detail)}</span></div><div class="ai-ranked-bars">${ranked.map(([label, value], index) => `<div class="ai-ranked-bar"><div><span><i class="chart-dot" style="background:${colors[index % colors.length]}"></i>${escapeHtml(labelFor(label))}</span><strong>${formatTokens(value)}</strong></div><p><i style="background:${colors[index % colors.length]};width:${Math.max(1, value / maximum * 100)}%"></i></p></div>`).join("") || `<div class="chart-empty">${tr("Waiting for recorded Token increments.", "等待已记录的 Token 增量。")}</div>`}</div></article>`;
}

function dailyHistory(
  intervals: UsageInterval[], dimension: "source" | "model", range: AiTimeRange,
  window: AnalysisTimeWindow, undatedTotal = 0,
): string {
  const totals = dimension === "source" ? aggregateBySource(intervals) : aggregateByModel(intervals);
  const visible = [...totals.entries()].sort((left, right) => right[1] - left[1]).slice(0, 5).map(([id]) => id);
  const colors = dimension === "source" ? SOURCE_COLORS : MODEL_COLORS;
  const series: DailyBarSeries[] = visible.map((id, index) => ({
    id, label: dimension === "model" ? modelLabel(id) : id, color: colors[index % colors.length],
  }));
  const hasOther = totals.size > visible.length;
  if (hasOther) series.push({ id: "__other__", label: tr("Other", "其他"), color: "#7b8794" });
  const days = new Map<number, Map<string, number>>();
  for (const interval of intervals) {
    const day = new Date(interval.epoch * 1_000);
    const epoch = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime() / 1_000;
    const values = days.get(epoch) ?? new Map<string, number>();
    const rows = dimension === "source" ? new Map([[interval.source, interval.total]]) : interval.models;
    for (const [id, value] of rows) {
      const target = visible.includes(id) ? id : "__other__";
      values.set(target, (values.get(target) ?? 0) + value);
    }
    days.set(epoch, values);
  }
  const buckets: DailyBarBucket[] = completeDailyBuckets(days, range, window).map(([epoch, values]) => ({ epoch, values }));
  return renderDailyBarChart(series, buckets, {
    title: dimension === "source" ? tr("Daily usage by Agent", "按 Agent 的每日用量") : tr("Daily usage by model", "按模型的每日用量"),
    detail: `${rangeLabel(range)}${days.size > 30 ? ` · ${tr("chart shows latest 30 days", "图表展示最近 30 天")}` : ""}`,
    ariaLabel: tr("Daily recorded Token usage", "每日已记录 Token 用量"), formatValue: formatTokens, mode: "stacked",
    footnote: undatedTotal > 0
      ? tr(
        `${formatTokens(undatedTotal)} is included in the range total but has no reliable calendar date, so it is not placed into a daily bar.`,
        `所选总量中另有 ${formatTokens(undatedTotal)} 无可靠日期，因此不强行放入每日柱状图。`,
      )
      : tr("Each bar is one recorded daily total. Colors are additive components of that total.", "每根柱是一个已记录的每日总量，颜色表示总量中的组成部分。"),
  });
}

function niceTokenAxisMaximum(value: number): number {
  if (value <= 0) return 1_000;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value * 1.12 / magnitude;
  return ([1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((step) => normalized <= step) ?? 10) * magnitude;
}

function rateTrend(intervals: UsageInterval[], dimension: "source" | "model", window: AnalysisTimeWindow): string {
  const seriesTotals = dimension === "source" ? aggregateBySource(intervals) : aggregateByModel(intervals);
  const seriesIds = [...seriesTotals.entries()].sort((left, right) => right[1] - left[1]).slice(0, 5).map(([id]) => id);
  const epochs = completeRateEpochs(window);
  if (epochs.length < 2) return `<article class="trend-panel"><div class="detail-panel__heading"><h3>${tr("Token consumption rate", "Token 消耗速率")}</h3><span>${tr("Token / min", "Token / 分钟")}</span></div><div class="chart-empty">${tr("Waiting for another recorded interval.", "等待下一个记录区间。")}</div></article>`;
  const values = new Map(seriesIds.map((id) => [id, new Map<number, number>()]));
  for (const interval of intervals) {
    const rows = dimension === "source" ? new Map([[interval.source, interval.total]]) : interval.models;
    for (const [id, value] of rows) {
      const target = values.get(id);
      if (target) target.set(interval.epoch, (target.get(interval.epoch) ?? 0) + value / (window.bucketSeconds / 60));
    }
  }
  const maximum = niceTokenAxisMaximum(Math.max(...seriesIds.flatMap((id) => epochs.map((epoch) => values.get(id)?.get(epoch) ?? 0)), 1));
  const start = epochs[0];
  const span = Math.max(1, epochs[epochs.length - 1] - start);
  const colors = dimension === "source" ? SOURCE_COLORS : MODEL_COLORS;
  const points = (id: string) => epochs.map((epoch) => `${(epoch - start) / span * 100},${92 - ((values.get(id)?.get(epoch) ?? 0) / maximum) * 82}`).join(" ");
  const startLabel = new Date(window.sinceEpoch * 1_000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `<article class="trend-panel"><div class="detail-panel__heading"><h3>${dimension === "source" ? tr("Agent Token rate", "Agent Token 速率") : tr("Model Token rate", "模型 Token 速率")}</h3><span>${tr("Token / min", "Token / 分钟")}</span></div><div class="traffic-chart-frame"><span class="chart-axis-label chart-axis-label--peak">${formatTokens(maximum)}</span><span class="chart-axis-label chart-axis-label--mid">${formatTokens(maximum / 2)}</span><span class="chart-axis-label chart-axis-label--zero">0</span><svg class="traffic-chart" viewBox="0 0 100 100" preserveAspectRatio="none"><path class="chart-grid chart-grid--reference" d="M0 10H100"/><path class="chart-grid" d="M0 51H100M0 92H100"/>${seriesIds.map((id, index) => `<polyline class="chart-line" style="stroke:${colors[index % colors.length]}" points="${points(id)}"/>`).join("")}</svg></div><div class="traffic-chart__timeline"><span>${escapeHtml(startLabel)}</span><span>${tr("Now", "现在")}</span></div><div class="chart-legend">${seriesIds.map((id, index) => `<span><i class="chart-dot" style="background:${colors[index % colors.length]}"></i>${escapeHtml(dimension === "model" ? modelLabel(id) : id)}</span>`).join("")}</div></article>`;
}

function renderOverview(usage: ProjectedUsage, range: AiTimeRange, window: AnalysisTimeWindow): string {
  const sources = usage.sourceTotals;
  const selectedTotal = [...sources.values()].reduce((sum, value) => sum + value, 0);
  return `<section class="ai-view-panel"><div class="ai-view-heading"><div><p>${tr("Selected range total", "所选时段总量")}</p><strong>${formatTokens(selectedTotal)}</strong></div><span>${rangeLabel(range)} · ${sources.size} ${tr("Agents", "个 Agent")}</span></div>${horizontalBars(tr("Usage by Agent", "按 Agent 的用量"), rangeLabel(range), sources, SOURCE_COLORS)}${range === "today" ? rateTrend(usage.intervals, "source", window) : dailyHistory(usage.intervals, "source", range, window, usage.undatedTotal)}</section>`;
}

function renderModels(usage: ProjectedUsage, range: AiTimeRange, window: AnalysisTimeWindow): string {
  const models = usage.modelTotals;
  const selectedTotal = [...models.values()].reduce((sum, value) => sum + value, 0);
  return `<section class="ai-view-panel"><div class="ai-view-heading"><div><p>${tr("Model total in range", "所选时段模型量")}</p><strong>${formatTokens(selectedTotal)}</strong></div><span>${rangeLabel(range)} · ${tr("mass-conserving", "总量守恒")}</span></div>${horizontalBars(tr("Model composition", "模型构成"), rangeLabel(range), models, MODEL_COLORS, modelLabel)}${range === "today" ? rateTrend(usage.intervals, "model", window) : dailyHistory(usage.intervals, "model", range, window, usage.undatedTotal)}</section>`;
}

function providerDetails(source: Record<string, unknown>): string {
  const today = windowOf(source, "today");
  const cumulative = windowOf(source, "cumulative");
  const groups = asArray(source.details);
  const started = shortStartedAt(today.started_at);
  const todaySource = [started ? tr(`since ${started}`, `${started} 起`) : "", localized(today.detail)].filter(Boolean).join(" · ");
  return `<details class="ai-provider-panel"><summary><span><strong>${escapeHtml(source.label ?? source.source_id)}</strong><small>${escapeHtml(String(source.collection_method ?? ""))}</small></span><span><small>${tr("Today", "今日")}</small><strong>${today.available ? formatTokens(today.tokens) : "—"}</strong></span><span><small>${tr("Cumulative", "累计")}</small><strong>${cumulative.available ? formatTokens(cumulative.tokens) : "—"}</strong></span><em>${escapeHtml(todaySource)}</em></summary><div class="ai-provider-body">${groups.map((group) => `<section><div class="detail-panel__heading"><h3>${escapeHtml(localized(group.title))}</h3><span>${escapeHtml(localized(group.badge))}</span></div><dl>${asArray(group.metrics).map((metric) => `<div><dt>${escapeHtml(localized(metric.label))}<small>${escapeHtml(localized(metric.detail))}</small></dt><dd>${formatMetric(metric.value, metric.unit)}</dd></div>`).join("")}</dl>${group.note ? `<p>${escapeHtml(localized(group.note))}</p>` : ""}</section>`).join("")}</div></details>`;
}

function renderActivity(providerSources: Record<string, unknown>[]): string {
  return `<section class="ai-view-panel"><div class="ai-view-heading"><div><p>${tr("Provider snapshots", "来源快照")}</p><strong>${providerSources.length}</strong></div><span>${tr("Current diagnostics · not additive", "当前诊断 · 不可与 Token 总量相加")}</span></div><div class="ai-provider-list">${providerSources.map(providerDetails).join("") || `<div class="chart-empty">${tr("No AI usage provider available.", "暂无可用的 AI 用量来源。")}</div>`}</div></section>`;
}

function analysisBody(snapshot: AiAnalysisSnapshot, providerSources: Record<string, unknown>[]): string {
  if (snapshot.mode === "activity") return renderActivity(providerSources);
  if (snapshot.loading && !snapshot.points.length) return `<article class="detail-panel ai-analysis-state"><span class="pulse"></span><p>${tr("Loading recorded Token usage…", "正在读取已记录的 Token 用量…")}</p></article>`;
  if (snapshot.error) return `<article class="detail-panel ai-analysis-state ai-analysis-state--error"><strong>${tr("Recorded metrics unavailable", "暂时无法读取历史指标")}</strong><p>${escapeHtml(snapshot.error)}</p></article>`;
  const usage = projectUsage(snapshot.points, providerSources, snapshot.range, snapshot.window);
  return snapshot.mode === "overview"
    ? renderOverview(usage, snapshot.range, snapshot.window)
    : renderModels(usage, snapshot.range, snapshot.window);
}

function sourceRow(source: SourceProjection): string {
  return `<li class="source-row"><span class="source-state source-state--${escapeHtml(source.status)}"></span><span class="source-main"><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.kind)}</small></span><span class="source-status">${escapeHtml(source.status)}</span></li>`;
}

function aiUsageDiagnostics(projection: AgentProjection, resource: ResourceProjection, sources: SourceProjection[]): AttentionDiagnostic[] {
  const diagnostics: AttentionDiagnostic[] = [];
  const collectors = projection.infra.collectors ?? [];
  const collectorBySource = new Map(collectors.map((collector) => [collector.capability.source_id, collector]));
  for (const source of sources) {
    if (!source.enabled || ["ok", "waiting", "baseline", "disabled"].includes(source.status)) continue;
    const collector = collectorBySource.get(source.id);
    diagnostics.push({
      id: source.id,
      level: "degraded",
      subject: source.label,
      title: tr("Usage metadata could not be collected", "无法采集用量元数据"),
      current: tr(`status ${source.status}${collector?.error_kind ? ` · ${collector.error_kind}` : ""}`, `状态 ${source.status}${collector?.error_kind ? ` · ${collector.error_kind}` : ""}`),
      basis: tr("Three consecutive collection failures are required before this source is marked unavailable.", "连续 3 次采集失败后，才会将该来源标记为不可用。"),
      action: tr("Confirm the local Agent and its metadata store are available; Sentinel never reads prompts or response bodies.", "确认本地 Agent 及其元数据存储可用；Sentinel 不会读取提示词或响应正文。"),
    });
  }
  if (!diagnostics.length && !["ok", "healthy", "waiting"].includes(String(resource.status))) {
    diagnostics.push({
      id: "ai-usage-status",
      level: "degraded",
      subject: tr("AI usage", "AI 用量"),
      title: tr("No provider currently has a usable usage snapshot", "当前没有来源提供可用的用量快照"),
      current: tr(`resource status ${resource.status}`, `资源状态 ${resource.status}`),
      action: tr("Check the collector rows below and refresh after the local Agent writes new metadata.", "检查下方采集来源，并在本地 Agent 写入新元数据后刷新。"),
    });
  }
  return diagnostics;
}

export function renderAiUsageResourcePage(projection: AgentProjection, resource: ResourceProjection, sources: SourceProjection[], snapshot: AiAnalysisSnapshot): string {
  const providerSources = asArray(asRecord(projection.infra.ai_usage).sources);
  const sourceNames = providerSources.map((source) => String(source.label ?? source.source_id ?? "")).filter(Boolean).join(" · ");
  const diagnostics = renderAttentionDiagnostics(aiUsageDiagnostics(projection, resource, sources));
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("AI usage", "AI 用量")}</h2></div><span class="section-heading__meta">${escapeHtml(sourceNames)}</span></div>${diagnostics}<section class="network-detail">${renderCurrentSummary(providerSources, sources)}${renderControls(snapshot)}${analysisBody(snapshot, providerSources)}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map(sourceRow).join("")}</ul></article></section></section>`;
}
