import "./styles.css";
import { AgentProjection, OverallStatus, readProjection, resetSession } from "./bridge";
import { formatDuration } from "./format";
import { tr } from "./i18n";
import { renderNetworkResourcePage } from "./network_view";
import { renderOverview } from "./overview_view";
import { loadSettings, renderSettings } from "./settings_view";

type AppView = "overview" | "network" | "settings";

function appRoot(): HTMLDivElement {
  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) throw new Error("missing app root");
  return root;
}

const root = appRoot();
let activeView: AppView = "overview";
let latestProjection: AgentProjection | undefined;

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
  const sources = network ? projection.infra.sources.filter((source) => source.resource_id === network.id) : [];
  const content = activeView === "network" && network
    ? `<section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(projection.session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="back"><span>← ${tr("Overview", "概览")}</span></button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button><button class="button button--danger" id="reset"><span>${tr("Reset totals", "重置统计")}</span></button></div></section>${renderNetworkResourcePage(projection, network, sources)}`
    : renderOverview(projection);
  root.innerHTML = `<main class="shell">${topbar(projection.infra.overall.status)}${content}${footer(projection)}</main>`;
  root.querySelector<HTMLButtonElement>("#settings")?.addEventListener("click", () => void openSettings());
  root.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  root.querySelector<HTMLButtonElement>("#reset")?.addEventListener("click", () => void requestReset());
  root.querySelector<HTMLButtonElement>("#back")?.addEventListener("click", () => { activeView = "overview"; renderProjection(projection); });
  root.querySelectorAll<HTMLButtonElement>("[data-resource-id]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.resourceId === "network") { activeView = "network"; renderProjection(projection); }
  }));
  bindChrome();
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
