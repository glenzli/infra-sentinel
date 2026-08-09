import { AnalysisTimeRange, AnalysisTimeWindow, localDayEpoch, nextLocalDayEpoch } from "./analysis_time";
import { asArray, asRecord, number } from "./format";

export type UsageInterval = {
  epoch: number;
  source: string;
  total: number;
  models: Map<string, number>;
};

export type ProjectedUsage = {
  intervals: UsageInterval[];
  sourceTotals: Map<string, number>;
  modelTotals: Map<string, number>;
  datedTotal: number;
  undatedTotal: number;
};

type MutableInterval = UsageInterval & { rawModels: Map<string, number> };
type TodayTarget = { tokens: number; startedEpoch?: number; models: Map<string, number> };

export function canonicalModelId(value: unknown): string {
  const model = String(value ?? "").trim().toLowerCase();
  return model.startsWith("openai/") ? model.slice("openai/".length) : model;
}

function rawIntervals(points: Record<string, unknown>[]): UsageInterval[] {
  const intervals = new Map<string, MutableInterval>();
  for (const point of points) {
    const epoch = number(point.observed_epoch);
    const source = String(point.source_id || "unknown");
    if (!epoch) continue;
    const key = `${epoch}:${source}`;
    const interval = intervals.get(key) ?? { epoch, source, total: 0, models: new Map(), rawModels: new Map() };
    const model = canonicalModelId(asRecord(point.dimensions).model);
    if (model) interval.rawModels.set(model, (interval.rawModels.get(model) ?? 0) + number(point.value));
    else interval.total += number(point.value);
    intervals.set(key, interval);
  }
  return [...intervals.values()].map((interval) => {
    const rawTotal = [...interval.rawModels.values()].reduce((sum, value) => sum + value, 0);
    const authoritativeTotal = interval.total || rawTotal;
    const scale = rawTotal > authoritativeTotal && rawTotal > 0 ? authoritativeTotal / rawTotal : 1;
    const models = new Map([...interval.rawModels].map(([model, value]) => [model, Math.floor(value * scale)]));
    const attributed = [...models.values()].reduce((sum, value) => sum + value, 0);
    const residual = Math.max(0, authoritativeTotal - attributed);
    if (residual) models.set("__unattributed__", residual);
    return { epoch: interval.epoch, source: interval.source, total: authoritativeTotal, models };
  }).sort((left, right) => left.epoch - right.epoch || left.source.localeCompare(right.source));
}

function todayTargets(providerSources: Record<string, unknown>[]): Map<string, TodayTarget> {
  const targets = new Map<string, TodayTarget>();
  for (const source of providerSources) {
    const sourceId = String(source.source_id ?? "");
    const today = asRecord(asRecord(source.usage).today);
    if (!sourceId || today.available === false || typeof today.tokens !== "number") continue;
    const started = new Date(String(today.started_at ?? ""));
    const models = new Map<string, number>();
    for (const model of asArray(source.models)) {
      const id = canonicalModelId(model.id);
      const window = asRecord(model.today);
      if (id && window.available !== false && typeof window.tokens === "number") models.set(id, number(window.tokens));
    }
    targets.set(sourceId, {
      tokens: number(today.tokens),
      ...(Number.isNaN(started.getTime()) ? {} : { startedEpoch: started.getTime() / 1_000 }),
      models,
    });
  }
  return targets;
}

function dailyHistory(
  providerSources: Record<string, unknown>[],
  window: AnalysisTimeWindow,
): { intervals: UsageInterval[]; availableSources: Set<string> } {
  const intervals: UsageInterval[] = [];
  const availableSources = new Set<string>();
  for (const source of providerSources) {
    const sourceId = String(source.source_id ?? "");
    const history = asRecord(source.history);
    if (!sourceId || history.daily_available !== true) continue;
    availableSources.add(sourceId);
    for (const row of asArray(history.daily)) {
      const day = String(row.date ?? "");
      const parsed = new Date(`${day}T00:00:00`);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || Number.isNaN(parsed.getTime())) continue;
      const epoch = parsed.getTime() / 1_000;
      if (epoch < localDayEpoch(window.sinceEpoch) || epoch > localDayEpoch(window.untilEpoch)) continue;
      const total = number(row.tokens);
      const models = new Map<string, number>();
      for (const model of asArray(row.models)) {
        const id = canonicalModelId(model.id);
        if (id) models.set(id, (models.get(id) ?? 0) + number(model.tokens));
      }
      const attributed = [...models.values()].reduce((sum, value) => sum + value, 0);
      if (attributed < total) models.set("__unattributed__", total - attributed);
      intervals.push({ epoch, source: sourceId, total, models });
    }
  }
  return { intervals, availableSources };
}

function allocateModels(
  models: Map<string, number>,
  intervalTotal: number,
  remainingModels: Map<string, number> | undefined,
): Map<string, number> {
  if (!remainingModels?.size) return new Map(models);
  const allocated = new Map<string, number>();
  let attributed = 0;
  for (const [model, value] of models) {
    if (model === "__unattributed__") continue;
    const accepted = Math.min(value, remainingModels.get(model) ?? 0);
    if (!accepted) continue;
    allocated.set(model, accepted);
    remainingModels.set(model, Math.max(0, (remainingModels.get(model) ?? 0) - accepted));
    attributed += accepted;
  }
  if (attributed < intervalTotal) allocated.set("__unattributed__", intervalTotal - attributed);
  return allocated;
}

function addResidual(
  intervals: UsageInterval[],
  target: TodayTarget,
  acceptedTotal: number,
  remainingModels: Map<string, number> | undefined,
  fallbackEpoch: number,
  source: string,
): void {
  let residual = Math.max(0, target.tokens - acceptedTotal);
  if (!residual) return;
  const row = intervals[intervals.length - 1] ?? { epoch: fallbackEpoch, source, total: 0, models: new Map<string, number>() };
  if (!intervals.length) intervals.push(row);
  row.total += residual;
  if (remainingModels?.size) {
    for (const [model, value] of remainingModels) {
      const assigned = Math.min(residual, value);
      if (!assigned) continue;
      row.models.set(model, (row.models.get(model) ?? 0) + assigned);
      residual -= assigned;
      if (!residual) break;
    }
  }
  if (residual) row.models.set("__unattributed__", (row.models.get("__unattributed__") ?? 0) + residual);
}

/**
 * Reconcile the current local day to provider-declared windows.
 *
 * Interval counters are retained for historical shape, but a replayed full-day
 * baseline is never allowed to push the selected day beyond the source's
 * authoritative snapshot. This keeps current summaries and analysis additive
 * without rewriting the evidence store.
 */
function resolvedIntervals(
  points: Record<string, unknown>[],
  providerSources: Record<string, unknown>[],
  window: AnalysisTimeWindow,
): UsageInterval[] {
  let intervals = rawIntervals(points);
  if (window.bucketSeconds === 86_400) {
    const history = dailyHistory(providerSources, window);
    intervals = intervals.filter((interval) => !history.availableSources.has(interval.source));
    intervals.push(...history.intervals);
  }
  const targets = todayTargets(providerSources);
  const todayStart = localDayEpoch(window.untilEpoch);
  const past = intervals.filter((interval) => interval.epoch < todayStart);
  const todayBySource = new Map<string, UsageInterval[]>();
  for (const interval of intervals.filter((item) => item.epoch >= todayStart)) {
    const rows = todayBySource.get(interval.source) ?? [];
    rows.push(interval);
    todayBySource.set(interval.source, rows);
  }
  for (const [source, target] of targets) {
    const candidates = (todayBySource.get(source) ?? [])
      .filter((interval) => window.bucketSeconds === 86_400 || !target.startedEpoch || interval.epoch >= target.startedEpoch)
      .sort((left, right) => left.epoch - right.epoch);
    const accepted: UsageInterval[] = [];
    const remainingModels = target.models.size ? new Map(target.models) : undefined;
    let acceptedTotal = 0;
    for (const interval of candidates) {
      const remaining = Math.max(0, target.tokens - acceptedTotal);
      // Full-day counters replayed after a restart are larger than the amount
      // still missing from this provider window. Drop the whole replay instead
      // of truncating it into a fake interval spike.
      if (!interval.total || interval.total > remaining) continue;
      accepted.push({
        ...interval,
        models: allocateModels(interval.models, interval.total, remainingModels),
      });
      acceptedTotal += interval.total;
    }
    addResidual(accepted, target, acceptedTotal, remainingModels, window.untilEpoch, source);
    past.push(...accepted);
    todayBySource.delete(source);
  }
  for (const rows of todayBySource.values()) past.push(...rows);
  return past.sort((left, right) => left.epoch - right.epoch || left.source.localeCompare(right.source));
}

function totalsForProviderWindow(
  providerSources: Record<string, unknown>[],
  intervals: UsageInterval[],
  windowName: "today" | "cumulative",
): { sources: Map<string, number>; models: Map<string, number> } {
  const sources = new Map<string, number>();
  const models = new Map<string, number>();
  const fallbackSources = aggregateBySource(intervals);
  const fallbackModels = new Map<string, Map<string, number>>();
  for (const interval of intervals) {
    const target = fallbackModels.get(interval.source) ?? new Map<string, number>();
    for (const [model, value] of interval.models) target.set(model, (target.get(model) ?? 0) + value);
    fallbackModels.set(interval.source, target);
  }
  const normalizedSources = new Set<string>();
  for (const source of providerSources) {
    const sourceId = String(source.source_id ?? "");
    const selected = asRecord(asRecord(source.usage)[windowName]);
    if (!sourceId || selected.available === false || typeof selected.tokens !== "number") continue;
    normalizedSources.add(sourceId);
    const total = number(selected.tokens);
    sources.set(sourceId, total);
    const sourceModels = new Map<string, number>();
    for (const model of asArray(source.models)) {
      const id = canonicalModelId(model.id);
      const modelWindow = asRecord(model[windowName]);
      if (id && modelWindow.available !== false && typeof modelWindow.tokens === "number") {
        sourceModels.set(id, (sourceModels.get(id) ?? 0) + number(modelWindow.tokens));
      }
    }
    const rawModelTotal = [...sourceModels.values()].reduce((sum, value) => sum + value, 0);
    const scale = rawModelTotal > total && rawModelTotal > 0 ? total / rawModelTotal : 1;
    let attributed = 0;
    for (const [model, value] of sourceModels) {
      const accepted = Math.floor(value * scale);
      models.set(model, (models.get(model) ?? 0) + accepted);
      attributed += accepted;
    }
    if (attributed < total) models.set("__unattributed__", (models.get("__unattributed__") ?? 0) + total - attributed);
  }
  for (const [source, value] of fallbackSources) {
    if (normalizedSources.has(source)) continue;
    sources.set(source, value);
    for (const [model, tokens] of fallbackModels.get(source) ?? []) {
      models.set(model, (models.get(model) ?? 0) + tokens);
    }
  }
  return { sources, models };
}

/**
 * Normalize source-specific observability into one UI contract.
 *
 * A provider may expose exact calendar days, only Sentinel-observed deltas, or
 * an undated cumulative total.  The rendering layer receives the same totals,
 * dated intervals, and undated remainder in every case.
 */
export function projectUsage(
  points: Record<string, unknown>[],
  providerSources: Record<string, unknown>[],
  range: AnalysisTimeRange,
  window: AnalysisTimeWindow,
): ProjectedUsage {
  const intervals = resolvedIntervals(points, providerSources, window);
  const datedSources = aggregateBySource(intervals);
  const datedTotal = [...datedSources.values()].reduce((sum, value) => sum + value, 0);
  const normalized = range === "today"
    ? totalsForProviderWindow(providerSources, intervals, "today")
    : range === "recorded"
      ? totalsForProviderWindow(providerSources, intervals, "cumulative")
      : { sources: datedSources, models: aggregateByModel(intervals) };
  const selectedTotal = [...normalized.sources.values()].reduce((sum, value) => sum + value, 0);
  return {
    intervals,
    sourceTotals: normalized.sources,
    modelTotals: normalized.models,
    datedTotal,
    undatedTotal: Math.max(0, selectedTotal - datedTotal),
  };
}

export function aggregateBySource(intervals: UsageInterval[]): Map<string, number> {
  const totals = new Map<string, number>();
  for (const interval of intervals) totals.set(interval.source, (totals.get(interval.source) ?? 0) + interval.total);
  return totals;
}

export function aggregateByModel(intervals: UsageInterval[]): Map<string, number> {
  const totals = new Map<string, number>();
  for (const interval of intervals) {
    for (const [model, value] of interval.models) totals.set(model, (totals.get(model) ?? 0) + value);
  }
  return totals;
}

export function completeDailyBuckets(
  values: Map<number, Map<string, number>>,
  range: AnalysisTimeRange,
  window: AnalysisTimeWindow,
): Array<[number, Map<string, number>]> {
  const normalized = new Map<number, Map<string, number>>();
  for (const [epoch, rows] of values) {
    const day = localDayEpoch(epoch);
    const target = normalized.get(day) ?? new Map<string, number>();
    for (const [id, value] of rows) target.set(id, (target.get(id) ?? 0) + value);
    normalized.set(day, target);
  }
  const end = localDayEpoch(window.untilEpoch);
  let start = localDayEpoch(window.sinceEpoch);
  if (range === "recorded") {
    const first = Math.min(...normalized.keys(), end);
    const latestThirty = new Date(end * 1_000);
    latestThirty.setDate(latestThirty.getDate() - 29);
    start = Math.max(first, localDayEpoch(latestThirty.getTime() / 1_000));
  }
  const buckets: Array<[number, Map<string, number>]> = [];
  for (let epoch = start; epoch <= end; epoch = nextLocalDayEpoch(epoch)) {
    buckets.push([epoch, normalized.get(epoch) ?? new Map()]);
  }
  return buckets;
}

export function completeRateEpochs(window: AnalysisTimeWindow): number[] {
  const bucket = window.bucketSeconds;
  const start = Math.floor(window.sinceEpoch / bucket) * bucket;
  const end = Math.floor(window.untilEpoch / bucket) * bucket;
  const epochs: number[] = [];
  for (let epoch = start; epoch <= end && epochs.length < 1_000; epoch += bucket) epochs.push(epoch);
  return epochs;
}
