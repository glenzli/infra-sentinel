import "./styles.css";
import "./facility_view.css";
import "./upstream_status_view.css";
import { AgentProjection, OverallStatus, openConsole, openExternalStatus, readProjection, resetSession } from "./bridge";
import { formatDuration } from "./format";
import { tr } from "./i18n";
import { renderNetworkResourcePage } from "./network_view";
import { NetworkAnalysisController, NetworkTimeRange, NetworkViewMode } from "./network_analysis";
import { renderAiUsageResourcePage } from "./ai_usage_view";
import { AiAnalysisController, AiTimeRange, AiViewMode } from "./ai_analysis";
import { renderOverview } from "./overview_view";
import { loadSettings, renderSettings } from "./settings_view";
import { renderFacilityDetailPage } from "./facility_view";
import { renderUpstreamStatusResourcePage } from "./upstream_status_view";
import { renderSystemResourcePage } from "./system_resource_view";
import { SystemResourceAnalysisController, SystemTimeRange } from "./system_resource_analysis";

type AppView = "overview" | "system" | "network" | "ai_usage" | "upstream_status" | "facility" | "settings";

function appRoot(): HTMLDivElement {
  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) throw new Error("missing app root");
  return root;
}

const root = appRoot();
let activeView: AppView = "overview";
let selectedFacilityId: string | undefined;
let latestProjection: AgentProjection | undefined;
const networkAnalysis = new NetworkAnalysisController();
const networkOverviewAnalysis = new NetworkAnalysisController("attribution", "today");
const aiAnalysis = new AiAnalysisController();
const systemAnalysis = new SystemResourceAnalysisController();

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
    selectedFacilityId = undefined;
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
  const system = projection.infra.resources.find((resource) => resource.enabled && resource.id === "system");
  const aiUsage = projection.infra.resources.find((resource) => resource.enabled && resource.id === "ai_usage");
  const upstreamStatus = projection.infra.resources.find((resource) => resource.enabled && resource.id === "upstream_status");
  const sources = network ? projection.infra.sources.filter((source) => source.resource_id === network.id) : [];
  const aiSources = aiUsage ? projection.infra.sources.filter((source) => source.resource_id === aiUsage.id) : [];
  const controls = `<section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(projection.session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="back"><span>← ${tr("Overview", "概览")}</span></button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></section>`;
  const facilityControls = `<section class="dashboard-actions"><div><p class="eyebrow">${tr("FACILITY", "运行设施")}</p><strong>${projection.infra.facilities?.total ?? 0} ${tr("discovered", "个已发现")}</strong></div><div class="hero-actions"><button class="button button--subtle" id="back"><span>← ${tr("Overview", "概览")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></section>`;
  const selectedFacility = projection.infra.facilities?.items.find((facility) => facility.id === selectedFacilityId);
  const content = activeView === "network" && network
    ? `<section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(projection.session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="back"><span>← ${tr("Overview", "概览")}</span></button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button><button class="button button--danger" id="reset"><span>${tr("Reset totals", "重置统计")}</span></button></div></section>${renderNetworkResourcePage(projection, network, sources, networkAnalysis.snapshot())}`
    : activeView === "system" && system
      ? `${controls}${renderSystemResourcePage(system, projection.infra.system, systemAnalysis.snapshot())}`
    : activeView === "ai_usage" && aiUsage
      ? `${controls}${renderAiUsageResourcePage(projection, aiUsage, aiSources, aiAnalysis.snapshot())}`
    : activeView === "upstream_status" && upstreamStatus
      ? `${controls}${renderUpstreamStatusResourcePage(upstreamStatus, projection.infra.upstream_status)}`
    : activeView === "facility"
      ? `${facilityControls}${renderFacilityDetailPage(selectedFacility)}`
    : renderOverview(projection, networkOverviewAnalysis.snapshot());
  root.innerHTML = `<main class="shell">${topbar(projection.infra.overall.status)}${content}${footer(projection)}</main>`;
  root.querySelector<HTMLButtonElement>("#settings")?.addEventListener("click", () => void openSettings());
  root.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  root.querySelector<HTMLButtonElement>("#reset")?.addEventListener("click", () => void requestReset());
  root.querySelector<HTMLButtonElement>("#back")?.addEventListener("click", () => { activeView = "overview"; selectedFacilityId = undefined; renderProjection(projection); });
  root.querySelectorAll<HTMLButtonElement>("[data-resource-id]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.resourceId === "system" || button.dataset.resourceId === "network" || button.dataset.resourceId === "ai_usage" || button.dataset.resourceId === "upstream_status") { activeView = button.dataset.resourceId; renderProjection(projection); }
  }));
  root.querySelectorAll<HTMLElement>("[data-facility-id]").forEach((card) => {
    const openDetails = () => {
      const facilityId = card.dataset.facilityId;
      if (!facilityId) return;
      selectedFacilityId = facilityId;
      activeView = "facility";
      renderProjection(projection);
    };
    card.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest("[data-console-url]")) return;
      openDetails();
    });
    card.addEventListener("keydown", (event) => {
      if (event.target instanceof Element && event.target.closest("[data-console-url]")) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openDetails();
    });
  });
  root.querySelectorAll<HTMLButtonElement>("[data-console-url]").forEach((button) => button.addEventListener("click", async () => {
    const url = button.dataset.consoleUrl;
    if (!url) return;
    try { await openConsole(url); } catch (error) { window.alert(`${tr("Cannot open Console", "无法打开 Console")}：${String(error)}`); }
  }));
  root.querySelectorAll<HTMLButtonElement>("[data-status-url]").forEach((button) => button.addEventListener("click", async () => {
    const url = button.dataset.statusUrl;
    if (!url) return;
    try { await openExternalStatus(url); } catch (error) { window.alert(`${tr("Cannot open official status page", "无法打开官方状态页")}：${String(error)}`); }
  }));
  root.querySelectorAll<HTMLButtonElement>("[data-ai-mode]").forEach((button) => button.addEventListener("click", () => {
    const mode = button.dataset.aiMode as AiViewMode;
    if (mode === "overview" || mode === "models" || mode === "activity") {
      aiAnalysis.selectMode(mode);
      if (latestProjection) renderProjection(latestProjection);
    }
  }));
  root.querySelectorAll<HTMLButtonElement>("[data-ai-range]").forEach((button) => button.addEventListener("click", () => {
    const range = button.dataset.aiRange as AiTimeRange;
    if (range === "today" || range === "7d" || range === "30d" || range === "recorded") {
      aiAnalysis.selectRange(range);
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
  root.querySelectorAll<HTMLButtonElement>("[data-system-range]").forEach((button) => button.addEventListener("click", () => {
    const range = button.dataset.systemRange as SystemTimeRange;
    if (range === "1h" || range === "24h" || range === "7d" || range === "30d") {
      systemAnalysis.selectRange(range);
      if (latestProjection) renderProjection(latestProjection);
    }
  }));
  bindChrome();
  if (activeView === "network") void networkAnalysis.hydrate(() => {
    if (activeView === "network" && latestProjection) renderProjection(latestProjection);
  });
  if (activeView === "overview" && network) void networkOverviewAnalysis.hydrate(() => {
    if (activeView === "overview" && latestProjection) renderProjection(latestProjection);
  });
  if (activeView === "ai_usage") void aiAnalysis.hydrate(() => {
    if (activeView === "ai_usage" && latestProjection) renderProjection(latestProjection);
  });
  if (activeView === "system") void systemAnalysis.hydrate(() => {
    if (activeView === "system" && latestProjection) renderProjection(latestProjection);
  });
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
