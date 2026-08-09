import { requestAgentCommand } from "./agent_client";

export type AiViewMode = "overview" | "models" | "activity";
export type AiTimeRange = "today" | "7d" | "30d" | "recorded";

export type AiAnalysisSnapshot = {
  mode: AiViewMode;
  range: AiTimeRange;
  points: Record<string, unknown>[];
  loading: boolean;
  error?: string;
};

type CacheEntry = { fetchedAt: number; points: Record<string, unknown>[] };

function localDayStartEpoch(): number {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1_000;
}

function rangeStart(range: AiTimeRange, now: number): number {
  if (range === "today") return localDayStartEpoch();
  if (range === "7d") return now - 7 * 86_400;
  if (range === "30d") return now - 30 * 86_400;
  return now - 730 * 86_400;
}

/** Owns AI usage selection, bounded history queries, caching, and stale-result rejection. */
export class AiAnalysisController {
  private mode: AiViewMode = "overview";
  private range: AiTimeRange = "today";
  private generation = 0;
  private readonly cache = new Map<AiTimeRange, CacheEntry>();
  private readonly loading = new Set<AiTimeRange>();
  private readonly errors = new Map<AiTimeRange, string>();

  selectMode(mode: AiViewMode): void {
    if (mode === this.mode) return;
    this.mode = mode;
  }

  selectRange(range: AiTimeRange): void {
    if (range === this.range) return;
    this.range = range;
    this.generation += 1;
  }

  snapshot(): AiAnalysisSnapshot {
    return {
      mode: this.mode,
      range: this.range,
      points: this.cache.get(this.range)?.points ?? [],
      loading: this.loading.has(this.range),
      error: this.errors.get(this.range),
    };
  }

  async hydrate(onChange: () => void): Promise<void> {
    const range = this.range;
    const generation = this.generation;
    const cached = this.cache.get(range);
    const maximumAge = range === "today" ? 60_000 : 5 * 60_000;
    if (this.loading.has(range) || (cached && Date.now() - cached.fetchedAt < maximumAge)) return;
    this.loading.add(range);
    this.errors.delete(range);
    onChange();
    try {
      const now = Date.now() / 1_000;
      const result = await requestAgentCommand("metrics.query", {
        since_epoch: rangeStart(range, now),
        until_epoch: now,
        resource_id: "ai_usage",
        metric: "ai.tokens.total",
        bucket_seconds: range === "today" ? 300 : 86_400,
      });
      if (result.status !== "ok") throw new Error(result.message ?? "Metrics query failed");
      const points = Array.isArray(result.payload?.points)
        ? result.payload.points.filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
        : [];
      if (generation === this.generation && range === this.range) {
        this.cache.set(range, { fetchedAt: Date.now(), points });
      }
    } catch (error) {
      if (generation === this.generation && range === this.range) {
        this.errors.set(range, error instanceof Error ? error.message : String(error));
      }
    } finally {
      this.loading.delete(range);
      if (range === this.range) onChange();
    }
  }
}
