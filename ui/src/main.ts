import "./styles.css";
import { AgentProjection, OverallStatus, readProjection, resetSession } from "./bridge";
import { formatDuration } from "./format";
import { tr } from "./i18n";
import { renderNetworkResourcePage } from "./network_view";
import { NetworkAnalysisController, NetworkTimeRange, NetworkViewMode } from "./network_analysis";
import { renderAiUsageResourcePage } from "./ai_usage_view";
import { requestAgentCommand } from "./agent_client";
import { renderOverview } from "./overview_view";
import { loadSettings, renderSettings } from "./settings_view";
import { AnalysisScope } from "./analysis_scope";

type AppView = "overview" | "network" | "ai_usage" | "settings";

function appRoot(): HTMLDivElement {
  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) throw new Error("missing app root");
  return root;
}

const root = appRoot();
let activeView: AppView = "overview";
let latestProjection: AgentProjection | undefined;
const networkAnalysis = new NetworkAnalysisController();
let aiAnalysisScope: AnalysisScope = "today";
let aiAnalysisLoading = false;
let aiAnalysisRequestScope: AnalysisScope | undefined;
const aiAnalysisPoints = new Map<AnalysisScope, Record<string, unknown>[]>();
const aiAnalysisFetchedAt = new Map<AnalysisScope, number>();

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function statusLabel(status: OverallStatus): string {
  const labels: Record<string, string> = { healthy: tr("Healthy", "正常"), warning: tr("Attention", "需关注"), critical: tr("Critical", "严重"), degraded: tr("Data source issue", "数据源异常") };
  return labels[status] ?? status;
}

function timestamp(value?: string): string {
  if (!value) return tr("Waiting for first sample", "等待首次采样");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function topbar(status?: OverallStatus): string {
  return `<header class="topbar"><button class="brand" id="home" type="button" aria-label="Infra Sentinel"><span class="brand-mark" aria-hidden="true"><i></i></span><span>Infra Sentinel</span></button><div class="topbar-actions">${status ? `<div class="agent-state"><span class="source-state source-state--${escapeHtml(status)}"></span>${escapeHtml(statusLabel(status))}</div>` : ""}</div></header>`;
}

function bindChrome(): void {
  root.querySelector<HTMLButtonElement>("#home")?.addEventListener("click", () => {
    activeView = "overview";
    if (latestProjection) renderProjection(latestProjection);
    else void refresh();
  });
}

function footer(projection: AgentProjection): string {
  return `<footer>${tr("Updated", "最近采样")} · ${escapeHtml(timestamp(projection.updated_at))} · ${tr("Agent Projection", "Agent Projection")} ${escapeHtml(projection.schema)}</footer>`;
}

function renderProjection(projection: AgentProjection): void {
  latestProjection = projection;
  const network = projection.infra.resources.find((resource) => resource.enabled && resource.id === "network");
  const aiUsage = projection.infra.resources.find((resource) => resource.enabled && resource.id === "ai_usage");
  const sources = network ? projection.infra.sources.filter((source) => source.resource_id === network.id) : [];
  const aiSources = aiUsage ? projection.infra.sources.filter((source) => source.resource_id === aiUsage.id) : [];
  const controls = `<section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(projection.session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="back"><span>← ${tr("Overview", "概览")}</span></button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></section>`;
  const content = activeView === "network" && network
    ? `<section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(projection.session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="back"><span>← ${tr("Overview", "概览")}</span></button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button><button class="button button--danger" id="reset"><span>${tr("Reset totals", "重置统计")}</span></button></div></section>${renderNetworkResourcePage(projection, network, sources, networkAnalysis.snapshot())}`
    : activeView === "ai_usage" && aiUsage
      ? `${controls}${renderAiUsageResourcePage(projection, aiUsage, aiSources, aiAnalysisScope, aiAnalysisPoints.get(aiAnalysisScope) ?? [], aiAnalysisLoading)}`
    : renderOverview(projection);
  root.innerHTML = `<main class="shell">${topbar(projection.infra.overall.status)}${content}${footer(projection)}</main>`;
  root.querySelector<HTMLButtonElement>("#settings")?.addEventListener("click", () => void openSettings());
  root.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  root.querySelector<HTMLButtonElement>("#reset")?.addEventListener("click", () => void requestReset());
  root.querySelector<HTMLButtonElement>("#back")?.addEventListener("click", () => { activeView = "overview"; renderProjection(projection); });
  root.querySelectorAll<HTMLButtonElement>("[data-resource-id]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.resourceId === "network" || button.dataset.resourceId === "ai_usage") { activeView = button.dataset.resourceId; renderProjection(projection); }
  }));
  root.querySelectorAll<HTMLButtonElement>("[data-analysis-resource='ai_usage']").forEach((button) => button.addEventListener("click", () => {
    const scope = button.dataset.analysisScope;
    if (scope === "today" || scope === "cumulative" || scope === "daily") {
      aiAnalysisScope = scope;
      if (latestProjection) renderProjection(latestProjection);
    }
  }));
  root.querySelectorAll<HTMLButtonElement>("[data-network-mode]").forEach((button) => button.addEventListener("click", () => {
    const mode = button.dataset.networkMode as NetworkViewMode;
    if (mode === "billing" || mode === "attribution" || mode === "efficiency") {
      networkAnalysis.selectMode(mode);
      if (latestProjection) renderProjection(latestProjection);
    }
  }));
  root.querySelectorAll<HTMLButtonElement>("[data-network-range]").forEach((button) => button.addEventListener("click", () => {
    const range = button.dataset.networkRange as NetworkTimeRange;
    if (range === "today" || range === "7d" || range === "30d" || range === "recorded") {
      networkAnalysis.selectRange(range);
      if (latestProjection) renderProjection(latestProjection);
    }
  }));
  bindChrome();
  if (activeView === "network") void networkAnalysis.hydrate(() => {
    if (activeView === "network" && latestProjection) renderProjection(latestProjection);
  });
  if (activeView === "ai_usage") void hydrateAiAnalysis();
}

function localDayStartEpoch(): number {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1_000;
}

async function hydrateAiAnalysis(): Promise<void> {
  const maximumAge = aiAnalysisScope === "today" ? 60_000 : 5 * 60_000;
  if (aiAnalysisScope === "cumulative" || aiAnalysisLoading || (aiAnalysisPoints.has(aiAnalysisScope) && Date.now() - (aiAnalysisFetchedAt.get(aiAnalysisScope) ?? 0) < maximumAge)) return;
  const scope = aiAnalysisScope;
  aiAnalysisLoading = true;
  aiAnalysisRequestScope = scope;
  try {
    const daily = scope === "daily";
    const now = Date.now() / 1_000;
    const result = await requestAgentCommand("metrics.query", {
      since_epoch: daily ? now - 30 * 86_400 : localDayStartEpoch(),
      until_epoch: now,
      resource_id: "ai_usage",
      metric: "ai.tokens.total",
      bucket_seconds: daily ? 86_400 : 300,
    });
    if (result.status !== "ok") throw new Error(result.message ?? "Metrics query failed");
    const points = Array.isArray(result.payload?.points)
      ? result.payload.points.filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
      : [];
    if (aiAnalysisRequestScope === scope) { aiAnalysisPoints.set(scope, points); aiAnalysisFetchedAt.set(scope, Date.now()); }
  } catch {
    if (aiAnalysisRequestScope === scope) { aiAnalysisPoints.set(scope, []); aiAnalysisFetchedAt.set(scope, Date.now()); }
  } finally {
    if (aiAnalysisRequestScope === scope) {
      aiAnalysisLoading = false;
      aiAnalysisRequestScope = undefined;
      if (activeView === "ai_usage" && latestProjection) renderProjection(latestProjection);
    }
  }
}

function renderWaiting(message: string, detail?: string): void {
  root.innerHTML = `<main class="shell shell--waiting">${topbar()}<section class="empty-state"><span class="pulse" aria-hidden="true"></span><h1>${escapeHtml(message)}</h1><p>${escapeHtml(detail ?? tr("The desktop shell will show data when the local Infra Agent publishes its first Projection.", "本地 Infra Agent 发布首个 Projection 后，桌面壳会自动显示数据。"))}</p><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></section></main>`;
  root.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  bindChrome();
}

async function refresh(): Promise<void> {
  if (activeView === "settings") return;
  try {
    const projection = await readProjection();
    if (!projection) return renderWaiting(tr("Waiting for Infra Agent", "等待 Infra Agent"));
    renderProjection(projection);
  } catch (error) {
    renderWaiting(tr("Cannot read local state", "无法读取本地状态"), String(error));
  }
}

async function openSettings(): Promise<void> {
  activeView = "settings";
  root.innerHTML = `<main class="shell shell--waiting">${topbar()}<section class="empty-state"><span class="pulse" aria-hidden="true"></span><h1>${tr("Loading settings", "正在读取设置")}</h1></section></main>`;
  bindChrome();
  try {
    const settings = await loadSettings();
    if (activeView !== "settings") return;
    renderSettings(root, settings, {
      cancel: () => { activeView = "overview"; void refresh(); },
      saved: () => { activeView = "overview"; renderWaiting(tr("Settings applied", "设置已应用"), tr("Waiting for the Agent supervisor to restart with the new configuration.", "等待 Agent supervisor 使用新配置重启。")); window.setTimeout(() => void refresh(), 1_000); },
      languageChanged: () => void openSettings(),
    });
  } catch (error) {
    renderWaiting(tr("Unable to load settings", "无法读取设置"), String(error));
  }
}

async function requestReset(): Promise<void> {
  if (!window.confirm(tr("Reset current totals? New local baselines begin after the next Agent sample.", "重置当前统计？下一次 Agent 采样后会重新建立本地基线。"))) return;
  try { await resetSession(); await refresh(); } catch (error) { window.alert(`${tr("Reset request could not be sent", "无法发送重置请求")}：${String(error)}`); }
}

renderWaiting(tr("Connecting to Infra Agent", "正在连接 Infra Agent"));
void refresh();
window.setInterval(() => void refresh(), 2_000);
