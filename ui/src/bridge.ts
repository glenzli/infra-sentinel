import { invoke } from "@tauri-apps/api/core";

export type OverallStatus = "healthy" | "warning" | "critical" | "degraded" | string;

export interface SourceProjection {
  id: string;
  kind: string;
  resource_id: string;
  label: string;
  enabled: boolean;
  status: string;
  updated_at?: string;
}

export interface ResourceProjection {
  id: string;
  category: string;
  status: OverallStatus;
  enabled: boolean;
  primary_metric: string;
  primary_value: number;
  primary_unit: string;
  primary_source_id: string;
  source_count: number;
  online_source_count: number;
}

export interface FacilityMetric {
  id: string;
  kind: "gauge" | "counter" | "state" | string;
  value: number | string | boolean;
  unit?: string;
  window_seconds?: number;
  dimensions?: Record<string, string>;
}

export interface FacilityObservationSnapshot {
  schema: string;
  schema_version: string;
  captured_at: string;
  sequence: number;
  status: { state: string; reason_codes: string[] };
  headline_metrics: string[];
  metrics: FacilityMetric[];
  issues: Array<{ code: string; severity: string; subject_id?: string; observed_at: string }>;
  extensions?: Record<string, unknown>;
}

export interface FacilityProjection {
  id: string;
  kind: string;
  instance_id: string;
  generation: string;
  label: string;
  status: string;
  observed_at?: string;
  console_url?: string;
  protocol: string;
  protocol_version: string;
  binding: string;
  snapshot?: FacilityObservationSnapshot;
  error_kind?: string;
}

export interface FacilitiesProjection {
  schema: string;
  status: string;
  total: number;
  healthy: number;
  attention: number;
  items: FacilityProjection[];
  error_kind?: string;
}

export interface UpstreamComponentProjection {
  id: string;
  name: string;
  status: string;
  level: string;
  group?: string;
}

export interface UpstreamIncidentProjection {
  id: string;
  name: string;
  status: string;
  impact: string;
  level: string;
  updated_at?: string;
  url?: string;
}

export interface UpstreamProviderProjection {
  id: string;
  label: string;
  status: string;
  available: boolean;
  description: string;
  observed_at: string;
  official_updated_at?: string;
  status_url: string;
  components: UpstreamComponentProjection[];
  incidents: UpstreamIncidentProjection[];
  error_kind?: string;
}

export interface UpstreamStatusProjection {
  schema: string;
  status: string;
  total: number;
  healthy: number;
  attention: number;
  unknown: number;
  updated_at: string;
  items: UpstreamProviderProjection[];
}

export interface InfraProjection {
  overall: { status: OverallStatus; active_alerts: number };
  resources: ResourceProjection[];
  sources: SourceProjection[];
  ai_usage?: Record<string, unknown>;
  facilities?: FacilitiesProjection;
  upstream_status?: UpstreamStatusProjection;
  system?: SystemResourceProjection;
}

export interface SystemResourceProjection {
  schema: string;
  available: boolean;
  platform: string;
  capabilities: string[];
  status: OverallStatus;
  quality: string;
  reasons: string[];
  observed_at: string;
  cpu: { percent: number };
  memory: {
    pressure: string;
    pressure_exact: boolean;
    total_bytes: number;
    available_bytes: number;
    compressed_bytes: number;
    swap_used_bytes: number;
    swapin_bytes_per_second: number;
    swapout_bytes_per_second: number;
  };
  disk: {
    total_bytes: number;
    free_bytes: number;
    used_percent: number;
    read_bytes_per_second: number;
    write_bytes_per_second: number;
    read_iops: number;
    write_iops: number;
    physical_io_available: boolean;
    health?: {
      state: string;
      observed_at: string;
      reason_codes: string[];
      read_errors: number | null;
      write_errors: number | null;
      read_retries: number | null;
      write_retries: number | null;
      interval_seconds: number;
    };
  };
  thermal: { state: string };
  persistence: { interval_seconds: number };
  privacy: string;
}

export interface AgentProjection {
  schema: string;
  updated_at: string;
  protocol: { schema: string; transport: string };
  infra: InfraProjection;
  session: Record<string, unknown>;
  vps: Record<string, unknown>;
  xray_stats: Record<string, unknown>;
}

export interface CommandReceipt {
  id: string;
}

export interface AgentCommandResult {
  schema: string;
  id: string;
  type: string;
  status: "ok" | "rejected" | "error";
  message?: string;
  payload?: Record<string, unknown>;
}

export function readProjection(): Promise<AgentProjection | null> {
  return invoke<AgentProjection | null>("read_projection");
}

export function resetSession(): Promise<CommandReceipt> {
  return submitAgentCommand("session.reset", {});
}

export function submitAgentCommand(commandType: string, payload: Record<string, unknown>): Promise<CommandReceipt> {
  return invoke<CommandReceipt>("submit_agent_command", { commandType, payload });
}

export function readAgentCommandResult(commandId: string): Promise<AgentCommandResult | null> {
  return invoke<AgentCommandResult | null>("read_agent_command_result", { commandId });
}

export function openConsole(url: string): Promise<void> {
  return invoke<void>("open_console", { url });
}

export function openExternalStatus(url: string): Promise<void> {
  return invoke<void>("open_external_status", { url });
}
