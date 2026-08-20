import { requestAgentCommand } from "./agent_client";
import { AnalysisTimeRange, AnalysisTimeWindow, analysisTimeWindow } from "./analysis_time";

export type AiViewMode = "overview" | "models" | "activity";
export type AiTimeRange = AnalysisTimeRange;
export type AiHistoryVisual = "bars" | "calendar";

export type AiAnalysisSnapshot = {
  mode: AiViewMode;
  range: AiTimeRange;
  historyVisual: AiHistoryVisual;
  points: Record<string, unknown>[];
  window: AnalysisTimeWindow;
  loading: boolean;
  error?: string;
};

type CacheEntry = { fetchedAt: number; points: Record<string, unknown>[]; window: AnalysisTimeWindow };

/** Owns AI usage selection, bounded history queries, caching, and stale-result rejection. */
export class AiAnalysisController {
  private mode: AiViewMode = "overview";
  private range: AiTimeRange = "today";
  private historyVisual: AiHistoryVisual = "bars";
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
  }

  selectHistoryVisual(visual: AiHistoryVisual): void {
    this.historyVisual = visual;
  }

  snapshot(): AiAnalysisSnapshot {
    return {
      mode: this.mode,
      range: this.range,
      historyVisual: this.historyVisual,
      points: this.cache.get(this.range)?.points ?? [],
      window: this.cache.get(this.range)?.window ?? analysisTimeWindow(this.range),
      loading: this.loading.has(this.range),
      error: this.errors.get(this.range),
    };
  }

  async hydrate(onChange: () => void): Promise<void> {
    const range = this.range;
    const cached = this.cache.get(range);
    const maximumAge = range === "today" ? 60_000 : 5 * 60_000;
    if (this.loading.has(range) || (cached && Date.now() - cached.fetchedAt < maximumAge)) return;
    this.loading.add(range);
    this.errors.delete(range);
    onChange();
    try {
      const window = analysisTimeWindow(range);
      const result = await requestAgentCommand("metrics.query", {
        since_epoch: window.sinceEpoch,
        until_epoch: window.untilEpoch,
        resource_id: "ai_usage",
        metric: "ai.tokens.total",
        bucket_seconds: window.bucketSeconds,
        bucket_offset_seconds: window.bucketOffsetSeconds,
      });
      if (result.status !== "ok") throw new Error(result.message ?? "Metrics query failed");
      const points = Array.isArray(result.payload?.points)
        ? result.payload.points.filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
        : [];
      this.cache.set(range, { fetchedAt: Date.now(), points, window });
    } catch (error) {
      this.errors.set(range, error instanceof Error ? error.message : String(error));
    } finally {
      this.loading.delete(range);
      if (range === this.range) onChange();
    }
  }
}
