import { AnalysisTimeWindow, localDayEpoch } from "./analysis_time";
import { canonicalModelId, ProjectedUsage } from "./ai_usage_series";
import { asArray, asRecord, number } from "./format";

export type PriceReference = {
  costUsd: number;
  pricedTokens: number;
  unpricedTokens: number;
  sources: string[];
  byModel: Map<string, number>;
  byDay: Map<number, number>;
  byDayModel: Map<number, Map<string, number>>;
  byBucket: Map<number, number>;
  byBucketModel: Map<number, Map<string, number>>;
};

type SampleRate = { costUsd: number; pricedTokens: number };

function referenceEpoch(day: unknown): number | undefined {
  const date = String(day ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return undefined;
  const epoch = new Date(`${date}T00:00:00`).getTime() / 1_000;
  return Number.isFinite(epoch) ? epoch : undefined;
}

function selectedCodexModels(
  usage: ProjectedUsage,
  providerSources: Record<string, unknown>[],
  range: "today" | "7d" | "30d" | "recorded",
): Map<string, number> {
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

function addValue<Key>(target: Map<Key, number>, key: Key, value: number): void {
  if (value > 0) target.set(key, (target.get(key) ?? 0) + value);
}

function addDayModelValue(target: Map<number, Map<string, number>>, day: number, model: string, value: number): void {
  if (value <= 0 || !model) return;
  const models = target.get(day) ?? new Map<string, number>();
  addValue(models, model, value);
  target.set(day, models);
}

function addSampleRate(target: Map<string, SampleRate>, key: string, costUsd: number, pricedTokens: number): void {
  if (!key || costUsd <= 0 || pricedTokens <= 0) return;
  const rate = target.get(key) ?? { costUsd: 0, pricedTokens: 0 };
  rate.costUsd += costUsd;
  rate.pricedTokens += pricedTokens;
  target.set(key, rate);
}

/**
 * Project daily source references onto the selected local Token window.
 *
 * Provider-reported values remain direct. Codex has only bounded local JSONL
 * samples, so its blended matching-model rate is projected over its selected
 * local model totals. Nothing here is a provider invoice.
 */
export function projectPriceReference(
  providerSources: Record<string, unknown>[],
  usage: ProjectedUsage,
  range: "today" | "7d" | "30d" | "recorded",
  window: AnalysisTimeWindow,
): PriceReference | undefined {
  const since = localDayEpoch(window.sinceEpoch);
  const until = localDayEpoch(window.untilEpoch);
  let costUsd = 0;
  let pricedTokens = 0;
  let unpricedTokens = 0;
  const labels = new Set<string>();
  const byModel = new Map<string, number>();
  const byDay = new Map<number, number>();
  const byDayModel = new Map<number, Map<string, number>>();
  const codexRates = new Map<string, SampleRate>();
  const sourceRates = new Map<string, SampleRate>();
  const sourceModelRates = new Map<string, Map<string, SampleRate>>();

  for (const source of providerSources) {
    const sourceId = String(source.source_id ?? "");
    const pricing = asRecord(source.pricing);
    for (const row of asArray(pricing.daily)) {
      const epoch = referenceEpoch(row.date);
      if (epoch === undefined || epoch < since || epoch > until) continue;
      const reference = asRecord(row.reference);
      const kind = String(reference.kind ?? "");
      if (sourceId === "codex" && kind === "local-rollout-standard-api-projection") {
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
      const cost = number(reference.cost_usd);
      addSampleRate(sourceRates, sourceId, cost, tokens);
      costUsd += cost;
      pricedTokens += tokens;
      unpricedTokens += number(reference.unpriced_tokens);
      labels.add(String(source.label ?? sourceId));
      addValue(byDay, epoch, cost);
      let attributedCost = 0;
      for (const model of asArray(reference.models)) {
        const id = canonicalModelId(model.id);
        const modelCost = number(model.cost_usd);
        const modelTokens = number(model.priced_tokens);
        if (!id || modelCost <= 0) continue;
        const modelRates = sourceModelRates.get(sourceId) ?? new Map<string, SampleRate>();
        addSampleRate(modelRates, id, modelCost, modelTokens);
        sourceModelRates.set(sourceId, modelRates);
        attributedCost += modelCost;
        addValue(byModel, id, modelCost);
        addDayModelValue(byDayModel, epoch, id, modelCost);
      }
      // Preserve a source-level price whose provider did not attach model rows.
      const unattributed = Math.max(0, cost - attributedCost);
      if (unattributed > 0) {
        addValue(byModel, "__priced_unattributed__", unattributed);
        addDayModelValue(byDayModel, epoch, "__priced_unattributed__", unattributed);
      }
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
      const cost = tokens * sample.costUsd / sample.pricedTokens;
      codexPriced += tokens;
      codexCost += cost;
      addValue(byModel, id, cost);
    }
    for (const interval of usage.intervals) {
      if (interval.source !== "codex") continue;
      let intervalCost = 0;
      const day = localDayEpoch(interval.epoch);
      for (const [rawId, tokens] of interval.models) {
        const id = canonicalModelId(rawId);
        const sample = codexRates.get(id);
        if (!sample?.pricedTokens) continue;
        const modelCost = tokens * sample.costUsd / sample.pricedTokens;
        intervalCost += modelCost;
        addDayModelValue(byDayModel, day, id, modelCost);
      }
      addValue(byDay, day, intervalCost);
    }
    if (codexPriced > 0) {
      costUsd += codexCost;
      pricedTokens += codexPriced;
      unpricedTokens += codexUnpriced;
      labels.add("Codex");
    }
  }
  const byBucket = new Map<number, number>();
  const byBucketModel = new Map<number, Map<string, number>>();
  if (range === "today") {
    for (const interval of usage.intervals) {
      const epoch = Math.floor(interval.epoch / window.bucketSeconds) * window.bucketSeconds;
      const modelRates = interval.source === "codex" ? codexRates : sourceModelRates.get(interval.source);
      const sourceRate = sourceRates.get(interval.source);
      let intervalCost = 0;
      let attributedTokens = 0;
      let unattributedTokens = 0;
      for (const [rawId, tokens] of interval.models) {
        const id = canonicalModelId(rawId);
        if (id === "__unattributed__" || id === "__priced_unattributed__") {
          unattributedTokens += tokens;
          continue;
        }
        attributedTokens += tokens;
        const rate = modelRates?.get(id);
        if (!rate?.pricedTokens) continue;
        const modelCost = tokens * rate.costUsd / rate.pricedTokens;
        intervalCost += modelCost;
        addDayModelValue(byBucketModel, epoch, id, modelCost);
      }
      const fallbackTokens = unattributedTokens + Math.max(0, interval.total - attributedTokens - unattributedTokens);
      if (fallbackTokens > 0 && sourceRate?.pricedTokens) {
        const fallbackCost = fallbackTokens * sourceRate.costUsd / sourceRate.pricedTokens;
        intervalCost += fallbackCost;
        addDayModelValue(byBucketModel, epoch, "__priced_unattributed__", fallbackCost);
      }
      addValue(byBucket, epoch, intervalCost);
    }
  } else {
    for (const [epoch, value] of byDay) byBucket.set(epoch, value);
    for (const [epoch, models] of byDayModel) byBucketModel.set(epoch, new Map(models));
  }
  return pricedTokens > 0 ? {
    costUsd, pricedTokens, unpricedTokens, sources: [...labels],
    byModel, byDay, byDayModel, byBucket, byBucketModel,
  } : undefined;
}
