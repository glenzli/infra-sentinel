import "./ai_usage_view.css";
import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatTokens, number } from "./format";
import { currentLocale, tr } from "./i18n";
import { DailyBarBucket, DailyBarSeries, renderDailyBarChart } from "./daily_bar_chart";
import { renderDailyActivityCalendar } from "./daily_activity_calendar";
import { AiAnalysisSnapshot, AiHistoryVisual, AiTimeRange, AiViewMode } from "./ai_analysis";
import { AnalysisTimeWindow, localDayEpoch } from "./analysis_time";
import { AttentionDiagnostic, renderAttentionDiagnostics } from "./attention_diagnostics";
import {
  ProjectedUsage, UsageInterval, aggregateByModel, aggregateBySource, canonicalModelId, completeDailyBuckets,
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
  if (unit === "usd") {
    const amount = number(value);
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: amount > 0 && amount < 0.01 ? 4 : 2,
      maximumFractionDigits: amount > 0 && amount < 0.01 ? 4 : 2,
    }).format(amount);
  }
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
  const visual = snapshot.range === "recorded" ? `<div class="ai-history-picker" role="group" aria-label="${tr("History visualization", "历史图表")}"><button type="button" class="ai-history-visual${snapshot.historyVisual === "bars" ? " is-active" : ""}" data-ai-history-visual="bars">${tr("Bars", "柱状")}</button><button type="button" class="ai-history-visual${snapshot.historyVisual === "calendar" ? " is-active" : ""}" data-ai-history-visual="calendar">${tr("Activity", "活动日历")}</button></div>` : "";
  return `<section class="ai-analysis-toolbar"><div class="ai-mode-tabs" role="tablist" aria-label="${tr("AI usage observation", "AI 用量观测维度")}">${modes.map(([mode, label, detail]) => `<button type="button" role="tab" aria-selected="${mode === snapshot.mode}" class="ai-mode-tab${mode === snapshot.mode ? " is-active" : ""}" data-ai-mode="${mode}"><strong>${label}</strong><small>${detail}</small></button>`).join("")}</div>${snapshot.mode === "activity" ? `<span class="ai-current-context">${tr("Current provider snapshot", "当前来源快照")}</span>` : `<div class="ai-range-picker"><span>${tr("Time range", "时间范围")}</span><div role="group">${ranges.map(([range, label]) => `<button type="button" class="ai-range${range === snapshot.range ? " is-active" : ""}" data-ai-range="${range}">${label}</button>`).join("")}</div>${visual}</div>`}</section>`;
}

function shortStartedAt(value: unknown): string {
  const date = new Date(String(value ?? ""));
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function horizontalBars(
  title: string, detail: string, totals: Map<string, number>, colors: string[],
  labelFor: (identifier: string) => string = (identifier) => identifier,
): string {
  const ranked = [...totals.entries()].filter(([, value]) => value > 0).sort((left, right) => right[1] - left[1]).slice(0, 8);
  const maximum = Math.max(...ranked.map(([, value]) => value), 1);
  return `<article class="detail-panel ai-ranked-panel"><div class="detail-panel__heading"><h3>${escapeHtml(title)}</h3><span>${escapeHtml(detail)}</span></div><div class="ai-ranked-bars">${ranked.map(([label, value], index) => `<div class="ai-ranked-bar"><div><span><i class="chart-dot" style="background:${colors[index % colors.length]}"></i>${escapeHtml(labelFor(label))}</span><strong>${formatTokens(value)}</strong></div><p><i style="background:${colors[index % colors.length]};width:${Math.max(.2, value / maximum * 100)}%"></i></p></div>`).join("") || `<div class="chart-empty">${tr("Waiting for recorded Token increments.", "等待已记录的 Token 增量。")}</div>`}</div></article>`;
}

function dailyHistory(
  intervals: UsageInterval[], dimension: "source" | "model", range: AiTimeRange,
  window: AnalysisTimeWindow, visual: AiHistoryVisual, undatedTotal = 0,
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
  if (range === "recorded" && visual === "calendar") {
    return renderDailyActivityCalendar(series, [...days.entries()].map(([epoch, values]) => ({ epoch, values })), {
      title: dimension === "source" ? tr("Daily Agent activity", "按 Agent 的每日活动") : tr("Daily model activity", "按模型的每日活动"),
      detail: tr("Latest year of recorded history", "最近一年的已记录历史"),
      ariaLabel: tr("Daily recorded Token activity", "每日已记录 Token 活动"),
      formatValue: formatTokens,
      endEpoch: window.untilEpoch,
      footnote: undatedTotal > 0
        ? tr(`${formatTokens(undatedTotal)} is included in the range total but has no reliable calendar date, so it is not placed into the calendar.`, `所选总量中另有 ${formatTokens(undatedTotal)} 无可靠日期，因此不强行放入活动日历。`)
        : tr("Each cell is one day. Darker cells mean relatively higher daily usage; hover or focus a cell for its recorded breakdown.", "每格代表一天；颜色越深表示该日相对用量越高，悬停或点选可查看已记录的构成。"),
    });
  }
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

type RangeReference = {
  costUsd: number;
  pricedTokens: number;
  unpricedTokens: number;
  sources: string[];
};

type SampleRate = { costUsd: number; pricedTokens: number };

function referenceEpoch(day: unknown): number | undefined {
  const date = String(day ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return undefined;
  const epoch = new Date(`${date}T00:00:00`).getTime() / 1_000;
  return Number.isFinite(epoch) ? epoch : undefined;
}

function selectedCodexModels(usage: ProjectedUsage, providerSources: Record<string, unknown>[], range: AiTimeRange): Map<string, number> {
  if (range === "today" || range === "recorded") {
    const source = providerSources.find((candidate) => candidate.source_id === "codex");
    const window = range === "today" ? "today" : "cumulative";
    const models = new Map<string, number>();
    for (const model of asArray(source?.models)) {
      const id = canonicalModelId(model.id);
      const tokens = number(asRecord(model[window]).tokens);
      if (id && tokens > 0) models.set(id, tokens);
    }
    return models;
  }
  const models = new Map<string, number>();
  for (const interval of usage.intervals) {
    if (interval.source !== "codex") continue;
    for (const [rawId, tokens] of interval.models) {
      const id = canonicalModelId(rawId);
      models.set(id, (models.get(id) ?? 0) + tokens);
    }
  }
  return models;
}

function selectedPriceReference(providerSources: Record<string, unknown>[], usage: ProjectedUsage, range: AiTimeRange, window: AnalysisTimeWindow): RangeReference | undefined {
  const since = localDayEpoch(window.sinceEpoch);
  const until = localDayEpoch(window.untilEpoch);
  let costUsd = 0;
  let pricedTokens = 0;
  let unpricedTokens = 0;
  const labels = new Set<string>();
  const codexRates = new Map<string, SampleRate>();

  for (const source of providerSources) {
    const sourceId = String(source.source_id ?? "");
    const pricing = asRecord(source.pricing);
    for (const row of asArray(pricing.daily)) {
      const epoch = referenceEpoch(row.date);
      if (epoch === undefined || epoch < since || epoch > until) continue;
      const reference = asRecord(row.reference);
      const kind = String(reference.kind ?? "");
      if (sourceId === "codex" && kind === "sampled-standard-api-projection") {
        for (const model of asArray(reference.models)) {
          const id = canonicalModelId(model.id);
          if (!id) continue;
          const rate = codexRates.get(id) ?? { costUsd: 0, pricedTokens: 0 };
          rate.costUsd += number(model.cost_usd);
          rate.pricedTokens += number(model.priced_tokens);
          codexRates.set(id, rate);
        }
        continue;
      }
      const tokens = number(reference.priced_tokens);
      if (!kind || tokens <= 0) continue;
      costUsd += number(reference.cost_usd);
      pricedTokens += tokens;
      unpricedTokens += number(reference.unpriced_tokens);
      labels.add(String(source.label ?? sourceId));
    }
  }

  if (codexRates.size) {
    let codexPriced = 0;
    let codexCost = 0;
    let codexUnpriced = 0;
    for (const [id, tokens] of selectedCodexModels(usage, providerSources, range)) {
      const sample = codexRates.get(id);
      if (!sample || sample.pricedTokens <= 0) {
        codexUnpriced += tokens;
        continue;
      }
      codexPriced += tokens;
      codexCost += tokens * sample.costUsd / sample.pricedTokens;
    }
    if (codexPriced > 0) {
      costUsd += codexCost;
      pricedTokens += codexPriced;
      unpricedTokens += codexUnpriced;
      labels.add("Codex");
    }
  }
  return pricedTokens > 0 ? { costUsd, pricedTokens, unpricedTokens, sources: [...labels] } : undefined;
}

function referencePriceTag(providerSources: Record<string, unknown>[], usage: ProjectedUsage, range: AiTimeRange, window: AnalysisTimeWindow): string {
  const estimate = selectedPriceReference(providerSources, usage, range, window);
  if (!estimate) return "";
  const sources = estimate.sources.join(" / ");
  const title = tr(
    `Local price reference for ${sources}. OpenCode uses provider-reported stored cost; Antigravity uses explicit text-model mappings; Codex projects sampled model prices over matching local model totals. Infer Runtime is excluded. Not an invoice.`,
    `${sources} 的本地价格参考：OpenCode 使用已记录的供应商成本；Antigravity 使用明确的文本模型映射；Codex 将抽样模型价格投影到同名模型的本地总量。暂不包含 Infer Runtime；不是账单。`,
  );
  return `<span class="ai-reference-price" title="${escapeHtml(title)}">${tr("API reference", "API 参考")} ≈ US$${estimate.costUsd.toFixed(2)}</span>`;
}

function renderOverview(usage: ProjectedUsage, providerSources: Record<string, unknown>[], range: AiTimeRange, window: AnalysisTimeWindow, visual: AiHistoryVisual): string {
  const sources = usage.sourceTotals;
  const selectedTotal = [...sources.values()].reduce((sum, value) => sum + value, 0);
  const priceTag = referencePriceTag(providerSources, usage, range, window);
  return `<section class="ai-view-panel"><div class="ai-view-heading"><div><p>${tr("Selected range total", "所选时段总量")}</p><strong>${formatTokens(selectedTotal)}</strong></div><aside><span>${rangeLabel(range)} · ${sources.size} ${tr("Agents", "个 Agent")}</span>${priceTag}</aside></div>${horizontalBars(tr("Usage by Agent", "按 Agent 的用量"), rangeLabel(range), sources, SOURCE_COLORS)}${range === "today" ? rateTrend(usage.intervals, "source", window) : dailyHistory(usage.intervals, "source", range, window, visual, usage.undatedTotal)}</section>`;
}

function renderModels(usage: ProjectedUsage, providerSources: Record<string, unknown>[], range: AiTimeRange, window: AnalysisTimeWindow, visual: AiHistoryVisual): string {
  const models = usage.modelTotals;
  const selectedTotal = [...models.values()].reduce((sum, value) => sum + value, 0);
  const priceTag = referencePriceTag(providerSources, usage, range, window);
  return `<section class="ai-view-panel"><div class="ai-view-heading"><div><p>${tr("Model total in range", "所选时段模型量")}</p><strong>${formatTokens(selectedTotal)}</strong></div><aside><span>${rangeLabel(range)}</span>${priceTag}</aside></div>${horizontalBars(tr("Model composition", "模型构成"), rangeLabel(range), models, MODEL_COLORS, modelLabel)}${range === "today" ? rateTrend(usage.intervals, "model", window) : dailyHistory(usage.intervals, "model", range, window, visual, usage.undatedTotal)}</section>`;
}

function providerDetails(source: Record<string, unknown>, providerPanels: ReadonlyMap<string, boolean>): string {
  const sourceId = String(source.source_id ?? "");
  const open = providerPanels.get(sourceId) !== false;
  const today = windowOf(source, "today");
  const cumulative = windowOf(source, "cumulative");
  const groups = asArray(source.details);
  const started = shortStartedAt(today.started_at);
  const todaySource = [started ? tr(`since ${started}`, `${started} 起`) : "", localized(today.detail)].filter(Boolean).join(" · ");
  return `<details class="ai-provider-panel" data-ai-provider-id="${escapeHtml(sourceId)}"${open ? " open" : ""}><summary><span><strong>${escapeHtml(source.label ?? source.source_id)}</strong><small>${escapeHtml(String(source.collection_method ?? ""))}</small></span><span><small>${tr("Today", "今日")}</small><strong>${today.available ? formatTokens(today.tokens) : "—"}</strong></span><span><small>${tr("Cumulative", "累计")}</small><strong>${cumulative.available ? formatTokens(cumulative.tokens) : "—"}</strong></span><em>${escapeHtml(todaySource)}</em></summary><div class="ai-provider-body">${groups.map((group) => `<section><div class="detail-panel__heading"><h3>${escapeHtml(localized(group.title))}</h3><span>${escapeHtml(localized(group.badge))}</span></div><dl>${asArray(group.metrics).map((metric) => `<div><dt>${escapeHtml(localized(metric.label))}<small>${escapeHtml(localized(metric.detail))}</small></dt><dd>${formatMetric(metric.value, metric.unit)}</dd></div>`).join("")}</dl>${group.note ? `<p>${escapeHtml(localized(group.note))}</p>` : ""}</section>`).join("")}</div></details>`;
}

function renderActivity(providerSources: Record<string, unknown>[], providerPanels: ReadonlyMap<string, boolean>): string {
  return `<section class="ai-view-panel"><div class="ai-view-heading"><div><p>${tr("Provider snapshots", "来源快照")}</p><strong>${providerSources.length}</strong></div><span>${tr("Current diagnostics · not additive", "当前诊断 · 不可与 Token 总量相加")}</span></div><div class="ai-provider-list">${providerSources.map((source) => providerDetails(source, providerPanels)).join("") || `<div class="chart-empty">${tr("No AI usage provider available.", "暂无可用的 AI 用量来源。")}</div>`}</div></section>`;
}

function analysisBody(snapshot: AiAnalysisSnapshot, providerSources: Record<string, unknown>[]): string {
  if (snapshot.mode === "activity") return renderActivity(providerSources, snapshot.providerPanels);
  if (snapshot.loading && !snapshot.points.length) return `<article class="detail-panel ai-analysis-state"><span class="pulse"></span><p>${tr("Loading recorded Token usage…", "正在读取已记录的 Token 用量…")}</p></article>`;
  if (snapshot.error) return `<article class="detail-panel ai-analysis-state ai-analysis-state--error"><strong>${tr("Recorded metrics unavailable", "暂时无法读取历史指标")}</strong><p>${escapeHtml(snapshot.error)}</p></article>`;
  const usage = projectUsage(snapshot.points, providerSources, snapshot.range, snapshot.window);
  return snapshot.mode === "overview"
    ? renderOverview(usage, providerSources, snapshot.range, snapshot.window, snapshot.historyVisual)
    : renderModels(usage, providerSources, snapshot.range, snapshot.window, snapshot.historyVisual);
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
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("AI usage", "AI 用量")}</h2></div><div class="ai-resource-meta"><span class="section-heading__meta">${escapeHtml(sourceNames)}</span><button type="button" class="button button--subtle ai-methodology-link" data-ai-methodology>${tr("Methods & estimates", "口径与估算")}</button></div></div>${diagnostics}<section class="network-detail">${renderCurrentSummary(providerSources, sources)}${renderControls(snapshot)}${analysisBody(snapshot, providerSources)}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map(sourceRow).join("")}</ul></article></section></section>`;
}
function renderCurrentSummary(providerSources: Record<string, unknown>[], sources: SourceProjection[]): string {
  const today = providerSources.reduce((sum, source) => sum + number(windowOf(source, "today").tokens), 0);
  const cumulative = providerSources.reduce((sum, source) => sum + number(windowOf(source, "cumulative").tokens), 0);
  const online = sources.filter((source) => source.enabled && source.status === "ok").length;
  return `<section class="ai-usage-summary"><article><p>${tr("Observed today", "今日已观测")}</p><div class="ai-summary-value"><strong>${formatTokens(today)}</strong></div><small>${tr("Available source windows combined", "合并当前可用来源窗口")}</small></article><article><p>${tr("Local history total", "本机历史总量")}</p><strong>${formatTokens(cumulative)}</strong><small>${tr("All readable local records · not billing", "全部可读本地记录 · 非账单")}</small></article><article><p>${tr("Collector coverage", "采集覆盖")}</p><strong>${online} / ${sources.length}</strong><small>${tr("sources online", "个来源在线")}</small></article></section>`;
}
