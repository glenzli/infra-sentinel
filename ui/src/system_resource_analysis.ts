import { requestAgentCommand } from "./agent_client";

export type SystemTimeRange = "1h" | "24h" | "7d" | "30d";

export type SystemAnalysisData = {
  gaugePoints: Record<string, unknown>[];
  counterPoints: Record<string, unknown>[];
  sinceEpoch: number;
  untilEpoch: number;
  bucketSeconds: number;
};

export type SystemAnalysisSnapshot = {
  range: SystemTimeRange;
  data: SystemAnalysisData;
  ready: boolean;
  loading: boolean;
  error?: string;
};

type CacheEntry = { fetchedAt: number; data: SystemAnalysisData };

function windowFor(range: SystemTimeRange): Omit<SystemAnalysisData, "gaugePoints" | "counterPoints"> & { bucketOffsetSeconds: number } {
  const untilEpoch = Date.now() / 1_000;
  const seconds = range === "1h" ? 3_600 : range === "24h" ? 86_400 : range === "7d" ? 7 * 86_400 : 30 * 86_400;
  const bucketSeconds = range === "1h" || range === "24h" ? 900 : range === "7d" ? 3_600 : 86_400;
  const localMidnight = new Date();
  localMidnight.setHours(0, 0, 0, 0);
  const dayStartEpoch = localMidnight.getTime() / 1_000;
  return {
    sinceEpoch: untilEpoch - seconds,
    untilEpoch,
    bucketSeconds,
    bucketOffsetSeconds: bucketSeconds === 86_400 ? ((dayStartEpoch % 86_400) + 86_400) % 86_400 : 0,
  };
}

function pointsFrom(result: Awaited<ReturnType<typeof requestAgentCommand>>): Record<string, unknown>[] {
  if (result.status !== "ok") throw new Error(result.message ?? "Metrics query failed");
  return Array.isArray(result.payload?.points)
    ? result.payload.points.filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
    : [];
}

/** Owns bounded system history queries and stale-result rejection. */
export class SystemResourceAnalysisController {
  private range: SystemTimeRange = "1h";
  private generation = 0;
  private readonly cache = new Map<SystemTimeRange, CacheEntry>();
  private readonly loading = new Set<SystemTimeRange>();
  private readonly errors = new Map<SystemTimeRange, string>();

  selectRange(range: SystemTimeRange): void {
    if (range === this.range) return;
    this.range = range;
    this.generation += 1;
  }

  snapshot(): SystemAnalysisSnapshot {
    return {
      range: this.range,
      data: this.cache.get(this.range)?.data ?? { gaugePoints: [], counterPoints: [], sinceEpoch: 0, untilEpoch: 0, bucketSeconds: 900 },
      ready: this.cache.has(this.range),
      loading: this.loading.has(this.range),
      error: this.errors.get(this.range),
    };
  }

  async hydrate(onChange: () => void): Promise<void> {
    const range = this.range;
    const generation = this.generation;
    const cached = this.cache.get(range);
    if (this.loading.has(range) || (cached && Date.now() - cached.fetchedAt < 60_000)) return;
    this.loading.add(range);
    this.errors.delete(range);
    onChange();
    try {
      const data = await this.fetch(range);
      if (generation !== this.generation || range !== this.range) return;
      this.cache.set(range, { fetchedAt: Date.now(), data });
    } catch (error) {
      if (generation !== this.generation || range !== this.range) return;
      this.errors.set(range, error instanceof Error ? error.message : String(error));
    } finally {
      this.loading.delete(range);
      if (generation === this.generation && range === this.range) onChange();
    }
  }

  private async fetch(range: SystemTimeRange): Promise<SystemAnalysisData> {
    const window = windowFor(range);
    const query = (instrument: "counter" | "gauge") => requestAgentCommand("metrics.query", {
      since_epoch: window.sinceEpoch,
      until_epoch: window.untilEpoch,
      resource_id: "system",
      instrument,
      bucket_seconds: window.bucketSeconds,
      bucket_offset_seconds: window.bucketOffsetSeconds,
    });
    const [gauges, counters] = await Promise.all([query("gauge"), query("counter")]);
    return {
      gaugePoints: pointsFrom(gauges),
      counterPoints: pointsFrom(counters),
      sinceEpoch: window.sinceEpoch,
      untilEpoch: window.untilEpoch,
      bucketSeconds: window.bucketSeconds,
    };
  }
}
