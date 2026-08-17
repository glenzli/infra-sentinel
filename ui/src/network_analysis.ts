import { requestAgentCommand } from "./agent_client";
import { AnalysisTimeRange, analysisTimeWindow } from "./analysis_time";

export type NetworkViewMode = "billing" | "attribution" | "efficiency";
export type NetworkTimeRange = AnalysisTimeRange;

export type NetworkAnalysisData = {
  servicePoints: Record<string, unknown>[];
  localPoints: Record<string, unknown>[];
  routePoints: Record<string, unknown>[];
  vpsPoints: Record<string, unknown>[];
  xrayPoints: Record<string, unknown>[];
};

export type NetworkAnalysisSnapshot = {
  mode: NetworkViewMode;
  range: NetworkTimeRange;
  data: NetworkAnalysisData;
  ready: boolean;
  loading: boolean;
  error?: string;
};

export type NetworkPathTotals = {
  local: number;
  proxy: number;
  xray: number;
  billed: number;
  attributed: number;
  attributionCoverage: number;
};

type CacheEntry = { fetchedAt: number; data: NetworkAnalysisData };

const emptyData = (): NetworkAnalysisData => ({ servicePoints: [], localPoints: [], routePoints: [], vpsPoints: [], xrayPoints: [] });

function pointTotal(points: Record<string, unknown>[]): number {
  return points.reduce((sum, point) => sum + Math.max(0, Number(point.value) || 0), 0);
}

export function networkPathTotals(data: NetworkAnalysisData): NetworkPathTotals {
  const local = pointTotal(data.localPoints);
  const proxy = pointTotal(data.routePoints.filter((point) => {
    const dimensions = point.dimensions;
    return Boolean(dimensions) && typeof dimensions === "object" && String((dimensions as Record<string, unknown>).route ?? "") === "proxy";
  }));
  const attributed = pointTotal(data.servicePoints.filter((point) => {
    const dimensions = point.dimensions;
    return Boolean(dimensions) && typeof dimensions === "object" && String((dimensions as Record<string, unknown>).service ?? "") !== "unattributed";
  }));
  return {
    local,
    proxy,
    xray: pointTotal(data.xrayPoints),
    billed: pointTotal(data.vpsPoints),
    attributed,
    attributionCoverage: local > 0 ? Math.min(1, attributed / local) : 0,
  };
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
  private mode: NetworkViewMode;
  private range: NetworkTimeRange;
  private generation = 0;
  private readonly cache = new Map<string, CacheEntry>();
  private readonly loading = new Set<string>();
  private readonly errors = new Map<string, string>();

  constructor(mode: NetworkViewMode = "billing", range: NetworkTimeRange = "today") {
    this.mode = mode;
    this.range = range;
  }

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
      ready: this.cache.has(key),
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
    const window = analysisTimeWindow(range, mode === "attribution" ? 900 : 86_400);
    const query = (metric: string, sourceId?: string) => requestAgentCommand("metrics.query", {
      since_epoch: window.sinceEpoch,
      until_epoch: window.untilEpoch,
      resource_id: "network",
      ...(sourceId ? { source_id: sourceId } : {}),
      metric,
      bucket_seconds: window.bucketSeconds,
      bucket_offset_seconds: window.bucketOffsetSeconds,
    });

    // The summary path and the selected detail mode must share one immutable
    // time window. Fetch the four path stages for every mode; a mode may add
    // its own detail query, but it never substitutes session counters.
    const [local, routes, vps, xray, services] = await Promise.all([
      query("network.bytes", "local-mihomo"),
      query("network.route_bytes", "local-mihomo"),
      query("network.billable_bytes"),
      query("network.logical_bytes"),
      mode === "attribution" ? query("network.service_bytes", "local-mihomo") : Promise.resolve(undefined),
    ]);
    return {
      servicePoints: services ? pointsFrom(services) : [],
      localPoints: pointsFrom(local),
      routePoints: pointsFrom(routes),
      vpsPoints: pointsFrom(vps),
      xrayPoints: pointsFrom(xray),
    };
  }
}
