import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatTokens, number } from "./format";
import { currentLocale, tr } from "./i18n";
import { AnalysisScope, renderAnalysisScopes } from "./analysis_scope";
import { DailyBarBucket, DailyBarSeries, renderDailyBarChart } from "./daily_bar_chart";

type ModelBucket = { epoch: number; model: string; value: number };

const MODEL_COLORS = ["#1a73e8", "#a142f4", "#1e8e3e", "#e37400", "#00838f"];

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
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

function tokenCard(label: string, value: unknown, detail: string, unit: unknown = "tokens"): string {
  return `<article class="network-card network-card--blue"><p>${escapeHtml(label)}</p><strong>${formatMetric(value, unit)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

function usageWindow(source: Record<string, unknown>, scope: "today" | "cumulative"): Record<string, unknown> {
  return asRecord(asRecord(source.usage)[scope]);
}

function usageCard(source: Record<string, unknown>, scope: "today" | "cumulative"): string {
  const window = usageWindow(source, scope);
  const available = Boolean(window.available);
  const label = scope === "today" ? tr("Observed today", "今日已观测") : tr("Cumulative", "累计消耗");
  const started = String(window.started_at ?? "");
  const detail = `${localized(window.detail)}${started ? ` · ${started}` : ""}`;
  return available
    ? tokenCard(label, window.tokens, detail)
    : `<article class="network-card network-card--blue"><p>${escapeHtml(label)}</p><strong>—</strong><small>${escapeHtml(detail)}</small></article>`;
}

function canonicalModelId(value: unknown): string {
  const model = String(value ?? "").trim().toLowerCase();
  return model.startsWith("openai/") ? model.slice("openai/".length) : model;
}

function niceTokenAxisMaximum(value: number): number {
  if (value <= 0) return 1_000;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = (value * 1.12) / magnitude;
  const step = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((candidate) => normalized <= candidate) ?? 10;
  return step * magnitude;
}

function modelBuckets(points: Record<string, unknown>[]): ModelBucket[] {
  return points.flatMap((point) => {
    const dimensions = asRecord(point.dimensions);
    const model = canonicalModelId(dimensions.model);
    const epoch = number(point.observed_epoch);
    if (!model || !epoch) return [];
    return [{ epoch, model, value: number(point.value) }];
  });
}

function sourceModelTotals(sources: Record<string, unknown>[], scope: AnalysisScope): Map<string, number> {
  const totals = new Map<string, number>();
  const window = scope === "cumulative" ? "cumulative" : "today";
  for (const source of sources) {
    for (const model of asArray(source.models)) {
      const id = canonicalModelId(model.id);
      const value = number(asRecord(model[window]).tokens);
      if (id) totals.set(id, (totals.get(id) ?? 0) + value);
    }
  }
  return totals;
}

function modelTotalsChart(models: string[], totals: Map<string, number>, scope: AnalysisScope): string {
  const maximum = Math.max(...models.map((model) => totals.get(model) ?? 0), 1);
  const title = scope === "cumulative" ? tr("Recorded model token totals", "模型 Token 记录累计") : tr("Today's model token totals", "当日模型 Token 汇总");
  const total = models.reduce((sum, model) => sum + (totals.get(model) ?? 0), 0);
  let cursor = 0;
  const segments = models.map((model, index) => {
    const next = cursor + ((totals.get(model) ?? 0) / Math.max(total, 1)) * 100;
    const segment = `${MODEL_COLORS[index]} ${cursor}% ${next}%`;
    cursor = next;
    return segment;
  }).join(", ");
  const donut = models.length ? `<aside class="ai-model-share"><i style="background:conic-gradient(${segments})"></i><strong>${formatTokens(total)}</strong><small>${tr("top-model total", "前五模型合计")}</small></aside>` : "";
  return `<article class="detail-panel ai-model-total-chart"><div class="detail-panel__heading"><h3>${title}</h3><span>${tr("available local sources", "已可用本地来源")}</span></div><div class="ai-model-total-chart__body"><div class="ai-model-bars">${models.map((model, index) => {
    const totalForModel = totals.get(model) ?? 0;
    const percentage = Math.max(1, Math.min(100, (totalForModel / maximum) * 100));
    return `<div class="ai-model-bar"><div><span><i class="chart-dot" style="background:${MODEL_COLORS[index]}"></i>${escapeHtml(model)}</span><strong>${formatTokens(totalForModel)}</strong></div><p><i style="background:${MODEL_COLORS[index]};width:${percentage}%"></i></p></div>`;
  }).join("")}</div>${donut}</div><p class="panel-footnote">${tr("Totals combine only unambiguous model identifiers across available local sources.", "仅对可明确对应的模型标识跨可用本地来源合并。")}</p></article>`;
}

export function renderAiModelTrend(points: Record<string, unknown>[], loading = false): string {
  if (loading) return `<section class="trend-panel ai-model-trend"><div class="detail-panel__heading"><h3>${tr("Model token consumption rate", "模型 Token 消耗速率")}</h3><span>${tr("Token / min", "Token / 分钟")}</span></div><div class="chart-empty">${tr("Loading today's model activity…", "正在读取今日模型活动…")}</div></section>`;
  const buckets = modelBuckets(points);
  if (!buckets.length) return `<section class="trend-panel ai-model-trend"><div class="detail-panel__heading"><h3>${tr("Model token consumption rate", "模型 Token 消耗速率")}</h3><span>${tr("Token / min", "Token / 分钟")}</span></div><div class="chart-empty">${tr("Waiting for enough AI usage samples.", "等待足够的 AI 用量采样。")}</div></section>`;
  const totals = new Map<string, number>();
  for (const bucket of buckets) totals.set(bucket.model, (totals.get(bucket.model) ?? 0) + bucket.value);
  const models = [...totals.entries()].sort((left, right) => right[1] - left[1]).slice(0, 5).map(([model]) => model);
  const epochs = [...new Set(buckets.map((bucket) => bucket.epoch))].sort((left, right) => left - right);
  if (epochs.length < 2) return `<section class="trend-panel ai-model-trend"><div class="detail-panel__heading"><h3>${tr("Model token consumption rate", "模型 Token 消耗速率")}</h3><span>${tr("Token / min", "Token / 分钟")}</span></div><div class="chart-empty">${tr("Waiting for the next five-minute model interval.", "等待下一个 5 分钟模型采样区间。")}</div></section>`;
  const values = new Map<string, Map<number, number>>();
  for (const model of models) values.set(model, new Map());
  for (const bucket of buckets) {
    const series = values.get(bucket.model);
    if (series) series.set(bucket.epoch, (series.get(bucket.epoch) ?? 0) + bucket.value / 5);
  }
  const axisMaximum = niceTokenAxisMaximum(Math.max(...models.flatMap((model) => epochs.map((epoch) => values.get(model)?.get(epoch) ?? 0))));
  const pointString = (model: string) => epochs.map((epoch, index) => {
    const value = values.get(model)?.get(epoch) ?? 0;
    const x = epochs.length === 1 ? 50 : (index / (epochs.length - 1)) * 100;
    return `${x},${92 - (value / axisMaximum) * 82}`;
  }).join(" ");
  const legend = models.map((model, index) => `<span><i class="chart-dot" style="background:${MODEL_COLORS[index]}"></i>${escapeHtml(model)}</span>`).join("");
  return `<section class="trend-panel ai-model-trend"><div class="detail-panel__heading"><h3>${tr("Model token consumption rate", "模型 Token 消耗速率")}</h3><span>${tr("Token / min", "Token / 分钟")}</span></div><div class="traffic-chart-frame"><span class="chart-axis-label chart-axis-label--peak">${formatTokens(axisMaximum)}</span><span class="chart-axis-label chart-axis-label--mid">${formatTokens(axisMaximum / 2)}</span><span class="chart-axis-label chart-axis-label--zero">0</span><svg class="traffic-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="${tr("Model token consumption rate", "模型 Token 消耗速率")}"><path class="chart-grid chart-grid--reference" d="M0 10H100" /><path class="chart-grid" d="M0 51H100M0 92H100" />${models.map((model, index) => `<polyline class="chart-line" style="stroke:${MODEL_COLORS[index]}" points="${pointString(model)}" />`).join("")}</svg></div><div class="traffic-chart__timeline"><span>${tr("Today", "今天")}</span><span>${tr("Now", "现在")}</span></div><div class="chart-legend ai-model-trend__legend">${legend}</div><p class="panel-footnote">${tr("Values are five-minute stored increments normalized to Token per minute; they are not lifetime counters.", "数值由 5 分钟区间的已记录增量换算为每分钟 Token，不使用生命周期累计值。")}</p></section>`;
}

function renderDailyModelHistory(points: Record<string, unknown>[], loading: boolean): string {
  if (loading) return `<article class="detail-panel ai-model-total-chart"><div class="detail-panel__heading"><h3>${tr("Daily token history", "每日 Token 历史")}</h3><span>${tr("last 30 days", "近 30 天")}</span></div><div class="chart-empty">${tr("Loading recorded daily totals…", "正在读取已记录的每日总量…")}</div></article>`;
  const buckets = modelBuckets(points);
  if (!buckets.length) return `<article class="detail-panel ai-model-total-chart"><div class="detail-panel__heading"><h3>${tr("Daily token history", "每日 Token 历史")}</h3><span>${tr("last 30 days", "近 30 天")}</span></div><div class="chart-empty">${tr("Daily history begins when Infra Sentinel records model increments.", "每日历史会从 Infra Sentinel 开始记录模型增量后出现。")}</div></article>`;
  const totals = new Map<string, number>();
  for (const bucket of buckets) totals.set(bucket.model, (totals.get(bucket.model) ?? 0) + bucket.value);
  const modelIds = [...totals.entries()].sort((left, right) => right[1] - left[1]).slice(0, 4).map(([model]) => model);
  const hasOther = totals.size > modelIds.length;
  const series: DailyBarSeries[] = modelIds.map((model, index) => ({ id: model, label: model, color: MODEL_COLORS[index] }));
  if (hasOther) series.push({ id: "__other_models__", label: tr("Other models", "其他模型"), color: "#7b8794" });
  const days = new Map<number, Map<string, number>>();
  for (const bucket of buckets) {
    const values = days.get(bucket.epoch) ?? new Map<string, number>();
    const id = modelIds.includes(bucket.model) ? bucket.model : "__other_models__";
    values.set(id, (values.get(id) ?? 0) + bucket.value);
    days.set(bucket.epoch, values);
  }
  const dailyBuckets: DailyBarBucket[] = [...days.entries()].sort(([left], [right]) => left - right).map(([epoch, values]) => ({ epoch, values }));
  return renderDailyBarChart(series, dailyBuckets, {
    title: tr("Daily token history", "每日 Token 历史"), detail: tr("by model · last 30 days", "按模型 · 近 30 天"),
    ariaLabel: tr("Daily token history by model", "按模型的每日 Token 历史"), formatValue: formatTokens,
    footnote: tr("Each day groups the four largest recorded models; all remaining models are shown together.", "每一天按累计最大的四个已记录模型分组，其余模型合并展示。"),
  });
}

function renderAiAnalysis(sources: Record<string, unknown>[], scope: AnalysisScope, points: Record<string, unknown>[], loading: boolean): string {
  const totals = sourceModelTotals(sources, scope);
  const models = [...totals.entries()].sort((left, right) => right[1] - left[1]).slice(0, 5).map(([model]) => model);
  const body = scope === "daily" ? renderDailyModelHistory(points, loading) : `${modelTotalsChart(models, totals, scope)}${scope === "today" ? renderAiModelTrend(points, loading) : ""}`;
  return `<section class="analysis-panel">${renderAnalysisScopes("ai_usage", scope)}${body}</section>`;
}

function sourceDetails(source: Record<string, unknown>): string {
  const title = String(source.label ?? source.source_id ?? "AI");
  const groups = asArray(source.details);
  return `<section class="ai-source-section"><div class="detail-panel__heading"><h3>${escapeHtml(title)} · ${tr("usage", "用量")}</h3><span>${escapeHtml(String(source.collection_method ?? ""))}</span></div><div class="network-card-grid ai-token-card-grid">${usageCard(source, "today")}${usageCard(source, "cumulative")}</div>${groups.map((group) => {
    const metrics = asArray(group.metrics);
    return `<div class="detail-panel__heading"><h3>${escapeHtml(localized(group.title))}</h3><span>${escapeHtml(localized(group.badge))}</span></div><div class="network-card-grid ai-token-card-grid">${metrics.map((metric) => tokenCard(localized(metric.label), metric.value, localized(metric.detail), metric.unit)).join("")}</div>${group.note ? `<p class="network-explanation">${escapeHtml(localized(group.note))}</p>` : ""}`;
  }).join("")}</section>`;
}

export function renderAiUsageResourcePage(projection: AgentProjection, resource: ResourceProjection, sources: SourceProjection[], scope: AnalysisScope, points: Record<string, unknown>[], loading = false): string {
  const aiUsage = asRecord(projection.infra.ai_usage);
  const providerSources = asArray(aiUsage.sources);
  const aggregate = asRecord(aiUsage.aggregate);
  const aggregateToday = asRecord(aggregate.today);
  const aggregateCumulative = asRecord(aggregate.cumulative);
  const sourceNames = providerSources.map((source) => String(source.label ?? source.source_id ?? "")).filter(Boolean).join(" · ");
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("AI usage", "AI 用量")}</h2></div><span class="section-heading__meta">${escapeHtml(sourceNames)}</span></div><section class="network-detail"><section class="ai-source-section"><div class="detail-panel__heading"><h3>${tr("Local usage rollup", "本机用量汇总")}</h3><span>${tr("not billing", "非账单")}</span></div><div class="network-card-grid ai-token-card-grid">${tokenCard(tr("Observed today", "今日已观测"), aggregateToday.tokens, tr("available local sources", "已可用本地来源"))}${tokenCard(tr("Local cumulative", "本地累计记录"), aggregateCumulative.tokens, tr("available history sources", "可用历史来源"))}</div><p class="network-explanation">${tr("Each provider declares whether a window is native or locally observed. The rollup only adds available windows and is useful for comparison, not provider billing.", "每个提供方都会声明时间口径是原生统计还是本机观测；汇总只相加可用窗口，适合比较，不是供应商账单。")}</p></section><div id="ai-analysis">${renderAiAnalysis(providerSources, scope, points, loading)}</div>${providerSources.map(sourceDetails).join("")}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map((sourceItem) => `<li class="source-row"><span class="source-state source-state--${escapeHtml(sourceItem.status)}" aria-hidden="true"></span><span class="source-main"><strong>${escapeHtml(sourceItem.label)}</strong><small>${escapeHtml(sourceItem.kind)}</small></span><span class="source-status">${escapeHtml(sourceItem.status)}</span></li>`).join("")}</ul></article></section></section>`;
}
