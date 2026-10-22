import type { DiagnosticCategory, DiagnosticEvent, DiagnosticsProjection } from "./bridge";
import { requestAgentCommand } from "./agent_client";

export type DiagnosticsTimeRange = "24h" | "7d" | "recorded";
export type DiagnosticsCategoryFilter = DiagnosticCategory | "all";

export interface DiagnosticsAnalysisSnapshot {
  range: DiagnosticsTimeRange;
  category: DiagnosticsCategoryFilter;
  data?: DiagnosticsProjection;
  ready: boolean;
  loading: boolean;
  stale: boolean;
  error: boolean;
  expandedEventIds: ReadonlySet<string>;
  currentPanelOpen: boolean;
}

type QueryPayload = { since_epoch?: number; until_epoch: number; category?: DiagnosticCategory; limit: number };
type DiagnosticQuery = (payload: QueryPayload) => Promise<DiagnosticsProjection>;
type CacheEntry = { fetchedAt: number; revision: string; data: DiagnosticsProjection };
type Attempt = { at: number; revision: string };

async function queryDiagnostics(payload: QueryPayload): Promise<DiagnosticsProjection> {
  const result = await requestAgentCommand("diagnostics.query", payload);
  if (result.status !== "ok") throw new Error("Diagnostic history query failed");
  const data = result.payload;
  if (!data || data.schema !== "20261022.1" || typeof data.available !== "boolean"
    || !Array.isArray(data.events) || !Array.isArray(data.active)) {
    throw new Error("Diagnostic history is unavailable");
  }
  return data as unknown as DiagnosticsProjection;
}

function revisionOf(projection?: DiagnosticsProjection): string {
  return [projection?.schema, projection?.available, projection?.events?.[0]?.id,
    projection?.active_count, projection?.freshness, projection?.history_started_at].join(":");
}

function queryPayload(range: DiagnosticsTimeRange, category: DiagnosticsCategoryFilter, now: number): QueryPayload {
  return {
    ...(range === "recorded" ? {} : { since_epoch: now - (range === "24h" ? 86_400 : 7 * 86_400) }),
    until_epoch: now,
    ...(category === "all" ? {} : { category }),
    limit: 200,
  };
}

function filterStaticEvents(events: DiagnosticEvent[], payload: QueryPayload): DiagnosticEvent[] {
  return events.filter((event) => {
    const observedAt = Date.parse(event.observed_at) / 1_000;
    return (!payload.category || event.category === payload.category)
      && Number.isFinite(observedAt) && observedAt <= payload.until_epoch
      && (payload.since_epoch === undefined || observedAt >= payload.since_epoch);
  }).slice(0, payload.limit);
}

/** Owns history query identity, bounded refresh, and diagnostic disclosure state. */
export class DiagnosticsAnalysisController {
  private range: DiagnosticsTimeRange = "7d";
  private category: DiagnosticsCategoryFilter = "all";
  private generation = 0;
  private revision = "";
  private readonly cache = new Map<string, CacheEntry>();
  private readonly loading = new Set<string>();
  private readonly attempts = new Map<string, Attempt>();
  private readonly errors = new Set<string>();
  private readonly expandedEventIds = new Set<string>();
  private currentPanelOpen = false;

  constructor(private readonly query: DiagnosticQuery = queryDiagnostics) {}

  selectRange(range: DiagnosticsTimeRange): void {
    if (range === this.range) return;
    this.range = range;
    this.generation += 1;
  }

  selectCategory(category: DiagnosticsCategoryFilter): void {
    if (category === this.category) return;
    this.category = category;
    this.generation += 1;
  }

  setEventOpen(id: string, open: boolean): void {
    if (open) this.expandedEventIds.add(id);
    else this.expandedEventIds.delete(id);
  }

  setCurrentPanelOpen(open: boolean): void {
    this.currentPanelOpen = open;
  }

  invalidate(): void {
    const cached = this.cache.get(this.key());
    if (cached) this.cache.set(this.key(), { ...cached, fetchedAt: 0 });
    this.attempts.delete(this.key());
    this.errors.delete(this.key());
  }

  snapshot(): DiagnosticsAnalysisSnapshot {
    const key = this.key();
    const entry = this.cache.get(key);
    return {
      range: this.range,
      category: this.category,
      data: entry?.data,
      ready: Boolean(entry),
      loading: this.loading.has(key),
      stale: Boolean(entry && entry.revision !== this.revision),
      error: this.errors.has(key),
      expandedEventIds: this.expandedEventIds,
      currentPanelOpen: this.currentPanelOpen,
    };
  }

  async hydrate(projection: DiagnosticsProjection | undefined, onChange: () => void): Promise<void> {
    const key = this.key();
    const range = this.range;
    const category = this.category;
    const generation = this.generation;
    const revision = revisionOf(projection);
    this.revision = revision;
    const cached = this.cache.get(key);
    const attempt = this.attempts.get(key);
    const now = Date.now();
    if (this.loading.has(key) || (cached?.revision === revision && now - cached.fetchedAt < 30_000)
      || (this.errors.has(key) && attempt?.revision === revision && now - attempt.at < 15_000)) return;
    this.loading.add(key);
    this.attempts.set(key, { at: now, revision });
    this.errors.delete(key);
    onChange();
    try {
      let data: DiagnosticsProjection;
      if (window.__INFRA_SENTINEL_STATIC_DEMO_LOCALE) {
        if (!projection) throw new Error("No static diagnostic history");
        const observedEpoch = Date.parse(projection.observed_at ?? "") / 1_000;
        const payload = queryPayload(range, category, Number.isFinite(observedEpoch) ? observedEpoch : now / 1_000);
        const active = projection.active.filter((item) => !payload.category || item.category === payload.category);
        data = {
          ...projection,
          active,
          active_count: active.length,
          events: filterStaticEvents(projection.events, payload),
          coverage: projection.coverage.filter((item) => !payload.category || item.category === payload.category),
        };
      } else {
        data = await this.query(queryPayload(range, category, now / 1_000));
      }
      if (generation !== this.generation || key !== this.key() || revision !== this.revision) return;
      this.cache.set(key, { fetchedAt: Date.now(), revision, data });
    } catch {
      if (generation === this.generation && key === this.key() && revision === this.revision) this.errors.add(key);
    } finally {
      this.loading.delete(key);
      if (key === this.key()) onChange();
    }
  }

  private key(): string {
    return `${this.range}:${this.category}`;
  }
}
