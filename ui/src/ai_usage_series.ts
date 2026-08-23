import { AnalysisTimeRange, AnalysisTimeWindow, localDayEpoch, nextLocalDayEpoch } from "./analysis_time";
import { asArray, asRecord, number } from "./format";

export type UsageInterval = {
  epoch: number;
  source: string;
  total: number;
  models: Map<string, number>;
  estimated: boolean;
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
    const interval = intervals.get(key) ?? {
      epoch, source, total: 0, models: new Map(), rawModels: new Map(), estimated: false,
    };
    interval.estimated ||= point.estimated === true;
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
    return { epoch: interval.epoch, source: interval.source, total: authoritativeTotal, models, estimated: interval.estimated };
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
      intervals.push({ epoch, source: sourceId, total, models, estimated: false });
    }
  }
  return { intervals, availableSources };
}

function hourlyHistory(
  providerSources: Record<string, unknown>[],
  window: AnalysisTimeWindow,
): { intervals: UsageInterval[]; availableSources: Set<string> } {
  const intervals: UsageInterval[] = [];
  const availableSources = new Set<string>();
  for (const source of providerSources) {
    const sourceId = String(source.source_id ?? "");
    const history = asRecord(source.history);
    if (!sourceId || history.hourly_available !== true) continue;
    availableSources.add(sourceId);
    for (const row of asArray(history.hourly)) {
      const epoch = number(row.epoch);
      if (!epoch || epoch < window.sinceEpoch || epoch > window.untilEpoch) continue;
      const total = number(row.tokens);
      const models = new Map<string, number>();
      for (const model of asArray(row.models)) {
        const id = canonicalModelId(model.id);
        if (id) models.set(id, (models.get(id) ?? 0) + number(model.tokens));
      }
      const attributed = [...models.values()].reduce((sum, value) => sum + value, 0);
      if (attributed < total) models.set("__unattributed__", total - attributed);
      intervals.push({ epoch, source: sourceId, total, models, estimated: row.estimated === true });
    }
    const unattributed = number(history.hourly_unattributed_tokens);
    if (unattributed > 0) {
      const today = asRecord(asRecord(source.usage).today);
      const started = new Date(String(today.started_at ?? ""));
      const startedEpoch = Number.isNaN(started.getTime()) ? window.sinceEpoch : started.getTime() / 1_000;
      const epoch = Math.floor(startedEpoch / window.bucketSeconds) * window.bucketSeconds;
      const existing = intervals.find((interval) => interval.source === sourceId && interval.epoch === epoch);
      const row = existing ?? { epoch, source: sourceId, total: 0, models: new Map<string, number>(), estimated: true };
      if (!existing) intervals.push(row);
      row.total += unattributed;
      row.estimated = true;
      row.models.set("__unattributed__", (row.models.get("__unattributed__") ?? 0) + unattributed);
    }
  }
  return { intervals, availableSources };
}

function addNormalizedModels(
  destination: Map<string, number>,
  sourceModels: Map<string, number>,
  authoritativeTotal: number,
): void {
  const rawTotal = [...sourceModels.values()].reduce((sum, value) => sum + value, 0);
  const scale = rawTotal > authoritativeTotal && rawTotal > 0 ? authoritativeTotal / rawTotal : 1;
  let attributed = 0;
  for (const [model, value] of sourceModels) {
    const accepted = Math.floor(value * scale);
    if (!accepted) continue;
    destination.set(model, (destination.get(model) ?? 0) + accepted);
    attributed += accepted;
  }
  if (attributed < authoritativeTotal) {
    destination.set("__unattributed__", (destination.get("__unattributed__") ?? 0) + authoritativeTotal - attributed);
  }
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
  const row = intervals.find((interval) => interval.epoch === fallbackEpoch)
    ?? { epoch: fallbackEpoch, source, total: 0, models: new Map<string, number>(), estimated: true };
  if (!intervals.includes(row)) intervals.push(row);
  row.total += residual;
  row.estimated = true;
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
  const nativeHourlySources = new Set<string>();
  if (window.bucketSeconds === 86_400) {
    const history = dailyHistory(providerSources, window);
    intervals = intervals.filter((interval) => !history.availableSources.has(interval.source));
    intervals.push(...history.intervals);
  } else if (window.bucketSeconds === 3_600) {
    const history = hourlyHistory(providerSources, window);
    for (const source of history.availableSources) nativeHourlySources.add(source);
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
    // Daily views show a completed historical day plus the current local day.
    // The latter is still open, so use the provider snapshot directly: it is
    // the only source that can account for threads whose model changed after
    // earlier interval samples were recorded.
    if (window.bucketSeconds === 86_400) {
      past.push({
        epoch: todayStart,
        source,
        total: target.tokens,
        models: new Map(target.models),
        estimated: false,
      });
      todayBySource.delete(source);
      continue;
    }
    const candidates = (todayBySource.get(source) ?? [])
      .filter((interval) => (
        window.bucketSeconds === 86_400
        || nativeHourlySources.has(source)
        || !target.startedEpoch
        || interval.epoch >= target.startedEpoch
      ))
      .sort((left, right) => left.epoch - right.epoch);
    const accepted: UsageInterval[] = [];
    let acceptedTotal = 0;
    for (const interval of candidates) {
      const remaining = Math.max(0, target.tokens - acceptedTotal);
      // Full-day counters replayed after a restart are larger than the amount
      // still missing from this provider window. Drop the whole replay instead
      // of truncating it into a fake interval spike.
      if (!interval.total || interval.total > remaining) continue;
      // Metric points retain the model that was reported at the time of the
      // increment.  Do not re-allocate old intervals using the current
      // snapshot: a thread can be reclassified to a different model later.
      accepted.push(interval);
      acceptedTotal += interval.total;
    }
    // The only un-attributed component is the currently unpersisted tail, not
    // a retrospective reclassification of already observed model increments.
    const anchor = Math.floor((target.startedEpoch ?? window.sinceEpoch) / window.bucketSeconds) * window.bucketSeconds;
    addResidual(accepted, target, acceptedTotal, undefined, anchor, source);
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
    addNormalizedModels(models, sourceModels, total);
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

export function completeIntervalEpochs(window: AnalysisTimeWindow): number[] {
  const bucket = window.bucketSeconds;
  const start = Math.floor(window.sinceEpoch / bucket) * bucket;
  const end = Math.floor(window.untilEpoch / bucket) * bucket;
  const epochs: number[] = [];
  for (let epoch = start; epoch <= end && epochs.length < 1_000; epoch += bucket) epochs.push(epoch);
  return epochs;
}
