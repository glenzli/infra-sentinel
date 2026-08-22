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
      const cost = number(reference.cost_usd);
      costUsd += cost;
      pricedTokens += tokens;
      unpricedTokens += number(reference.unpriced_tokens);
      labels.add(String(source.label ?? sourceId));
      addValue(byDay, epoch, cost);
      for (const model of asArray(reference.models)) {
        const id = canonicalModelId(model.id);
        if (id) addValue(byModel, id, number(model.cost_usd));
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
      for (const [rawId, tokens] of interval.models) {
        const sample = codexRates.get(canonicalModelId(rawId));
        if (sample?.pricedTokens) intervalCost += tokens * sample.costUsd / sample.pricedTokens;
      }
      addValue(byDay, localDayEpoch(interval.epoch), intervalCost);
    }
    if (codexPriced > 0) {
      costUsd += codexCost;
      pricedTokens += codexPriced;
      unpricedTokens += codexUnpriced;
      labels.add("Codex");
    }
  }
  return pricedTokens > 0 ? { costUsd, pricedTokens, unpricedTokens, sources: [...labels], byModel, byDay } : undefined;
}
