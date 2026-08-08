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
  status: OverallStatus;
  enabled: boolean;
  primary_metric: string;
  primary_value: number;
  primary_unit: string;
  primary_source_id: string;
  source_count: number;
  online_source_count: number;
}

export interface InfraProjection {
  overall: { status: OverallStatus; active_alerts: number };
  resources: ResourceProjection[];
  sources: SourceProjection[];
}

export interface AgentProjection {
  schema: string;
  updated_at: string;
  protocol: { schema: string; transport: string };
  infra: InfraProjection;
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
