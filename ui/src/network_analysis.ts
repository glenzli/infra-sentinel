import { requestAgentCommand } from "./agent_client";

export type NetworkViewMode = "billing" | "attribution" | "efficiency";
export type NetworkTimeRange = "today" | "7d" | "30d" | "recorded";

export type NetworkAnalysisData = {
  servicePoints: Record<string, unknown>[];
  localPoints: Record<string, unknown>[];
  vpsPoints: Record<string, unknown>[];
  xrayPoints: Record<string, unknown>[];
};

export type NetworkAnalysisSnapshot = {
  mode: NetworkViewMode;
  range: NetworkTimeRange;
  data: NetworkAnalysisData;
  loading: boolean;
  error?: string;
};

type CacheEntry = { fetchedAt: number; data: NetworkAnalysisData };

const emptyData = (): NetworkAnalysisData => ({ servicePoints: [], localPoints: [], vpsPoints: [], xrayPoints: [] });

function localDayStartEpoch(): number {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1_000;
}

function rangeStart(range: NetworkTimeRange, now: number): number {
  if (range === "today") return localDayStartEpoch();
  if (range === "7d") return now - 7 * 86_400;
  if (range === "30d") return now - 30 * 86_400;
  return now - 730 * 86_400;
}

function pointsFrom(result: Awaited<ReturnType<typeof requestAgentCommand>>): Record<string, unknown>[] {
  if (result.status !== "ok") throw new Error(result.message ?? "Metrics query failed");
  return Array.isArray(result.payload?.points)
    ? result.payload.points.filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
    : [];
}

/**
 * Owns the network analysis query lifecycle. The view only receives a coherent
 * mode/range snapshot, while request identity, caching, and stale-result
 * rejection stay outside the application shell.
 */
export class NetworkAnalysisController {
  private mode: NetworkViewMode = "billing";
  private range: NetworkTimeRange = "30d";
  private generation = 0;
  private readonly cache = new Map<string, CacheEntry>();
  private readonly loading = new Set<string>();
  private readonly errors = new Map<string, string>();

  selectMode(mode: NetworkViewMode): void {
    if (mode === this.mode) return;
    this.mode = mode;
    this.generation += 1;
  }

  selectRange(range: NetworkTimeRange): void {
    if (range === this.range) return;
    this.range = range;
    this.generation += 1;
  }

  snapshot(): NetworkAnalysisSnapshot {
    const key = this.key();
    return {
      mode: this.mode,
      range: this.range,
      data: this.cache.get(key)?.data ?? emptyData(),
      loading: this.loading.has(key),
      error: this.errors.get(key),
    };
  }

  async hydrate(onChange: () => void): Promise<void> {
    const mode = this.mode;
    const range = this.range;
    const key = this.key(mode, range);
    const generation = this.generation;
    const maximumAge = range === "today" ? 60_000 : 5 * 60_000;
    const cached = this.cache.get(key);
    if (this.loading.has(key) || (cached && Date.now() - cached.fetchedAt < maximumAge)) return;

    this.loading.add(key);
    this.errors.delete(key);
    onChange();
    try {
      const data = await this.fetch(mode, range);
      if (generation !== this.generation || key !== this.key()) return;
      this.cache.set(key, { fetchedAt: Date.now(), data });
    } catch (error) {
      if (generation !== this.generation || key !== this.key()) return;
      this.errors.set(key, error instanceof Error ? error.message : String(error));
    } finally {
      this.loading.delete(key);
      if (generation === this.generation && key === this.key()) onChange();
    }
  }

  private key(mode = this.mode, range = this.range): string {
    return `${mode}:${range}`;
  }

  private async fetch(mode: NetworkViewMode, range: NetworkTimeRange): Promise<NetworkAnalysisData> {
    const now = Date.now() / 1_000;
    const since = rangeStart(range, now);
    const bucketSeconds = mode === "attribution" && range === "today" ? 300 : 86_400;
    const query = (metric: string, sourceId?: string) => requestAgentCommand("metrics.query", {
      since_epoch: since,
      until_epoch: now,
      resource_id: "network",
      ...(sourceId ? { source_id: sourceId } : {}),
      metric,
      bucket_seconds: bucketSeconds,
    });

    if (mode === "attribution") {
      const [services, local] = await Promise.all([
        query("network.service_bytes", "local-mihomo"),
        query("network.bytes", "local-mihomo"),
      ]);
      return { ...emptyData(), servicePoints: pointsFrom(services), localPoints: pointsFrom(local) };
    }
    if (mode === "billing") {
      return { ...emptyData(), vpsPoints: pointsFrom(await query("network.billable_bytes")) };
    }
    const [vps, xray] = await Promise.all([
      query("network.billable_bytes"),
      query("network.logical_bytes"),
    ]);
    return { ...emptyData(), vpsPoints: pointsFrom(vps), xrayPoints: pointsFrom(xray) };
  }
}
