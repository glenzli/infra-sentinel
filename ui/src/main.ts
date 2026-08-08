import "./styles.css";
import { AgentProjection, OverallStatus, ResourceProjection, SourceProjection, readProjection, resetSession } from "./bridge";
import { formatBytes, formatDuration } from "./format";
import { bindLanguagePicker, languagePicker, tr } from "./i18n";
import { icon } from "./icons";
import { renderNetworkDetail } from "./network_view";
import { loadSettings, renderSettings } from "./settings_view";

function appRoot(): HTMLDivElement {
  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) throw new Error("missing app root");
  return root;
}

const root = appRoot();
let activeView: "overview" | "settings" = "overview";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function resourceLabel(resource: ResourceProjection): string {
  return resource.id === "network" ? tr("Network", "网络") : resource.id;
}

function statusLabel(status: OverallStatus): string {
  const labels: Record<string, string> = {
    healthy: tr("Healthy", "正常"),
    warning: tr("Attention", "需关注"),
    critical: tr("Critical", "严重"),
    degraded: tr("Data source issue", "数据源异常"),
  };
  return labels[status] ?? status;
}

function timestamp(value?: string): string {
  if (!value) return tr("Waiting for first sample", "等待首次采样");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function sourceLabel(source: SourceProjection): string {
  const status = source.enabled ? source.status : "disabled";
  const statusText = status === "ok" ? tr("online", "在线") : status === "disabled" ? tr("disabled", "已停用") : statusLabel(status);
  return `<li class="source-row"><span class="source-state source-state--${escapeHtml(status)}" aria-hidden="true"></span><span class="source-main"><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.kind)}</small></span><span class="source-status">${escapeHtml(statusText)}</span></li>`;
}

function topbar(status?: OverallStatus): string {
  return `<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i></span><span>Infra Sentinel</span></div><div class="topbar-actions">${status ? `<div class="agent-state"><span class="source-state source-state--${escapeHtml(status)}"></span>${escapeHtml(statusLabel(status))}</div>` : ""}${languagePicker()}</div></header>`;
}

function bindChrome(): void {
  bindLanguagePicker(root, () => {
    if (activeView === "settings") void openSettings();
    else void refresh();
  });
}

function renderProjection(projection: AgentProjection): void {
  const infra = projection.infra;
  const resources = infra.resources.filter((resource) => resource.enabled);
  const network = resources.find((resource) => resource.id === "network") ?? resources[0];
  const sources = network ? infra.sources.filter((source) => source.resource_id === network.id) : infra.sources;
  const activeAlerts = infra.overall.active_alerts;
  const session = projection.session;

  root.innerHTML = `<main class="shell">
    ${topbar(infra.overall.status)}
    <section class="dashboard-actions"><div><p class="eyebrow">${tr("CURRENT SESSION", "当前统计周期")}</p><strong>${formatDuration(session.duration_seconds)}</strong></div><div class="hero-actions"><button class="button button--subtle" id="settings">${icon("settings")}<span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh">${icon("refresh")}<span>${tr("Refresh", "刷新")}</span></button><button class="button button--danger" id="reset">${icon("reset")}<span>${tr("Reset totals", "重置统计")}</span></button></div></section>
    <section class="summary-grid" aria-label="${tr("Overview", "概览")}">
      <article class="summary-card summary-card--status"><p>${tr("Overall health", "整体健康")}</p><strong class="status-value status-value--${escapeHtml(infra.overall.status)}">${escapeHtml(statusLabel(infra.overall.status))}</strong><small>${activeAlerts ? tr(`${activeAlerts} active alert${activeAlerts === 1 ? "" : "s"}`, `当前 ${activeAlerts} 个活动告警`) : tr("No active alerts", "暂无活动告警")}</small></article>
      <article class="summary-card"><p>${tr("Active modules", "已启用模块")}</p><strong>${resources.length}</strong><small>${resources.map(resourceLabel).join(" · ") || tr("None", "无")}</small></article>
      <article class="summary-card"><p>${tr("Data sources", "数据源")}</p><strong>${network?.online_source_count ?? 0}<em> / ${network?.source_count ?? 0}</em></strong><small>${tr("online", "在线")}</small></article>
    </section>
    ${network ? `<section class="resource-section"><div class="section-heading"><div><p class="eyebrow">${tr("ACTIVE RESOURCE", "活动资源")}</p><h2>${resourceLabel(network)}</h2></div><span class="pill pill--${escapeHtml(network.status)}">${escapeHtml(statusLabel(network.status))}</span></div>${renderNetworkDetail(projection)}<article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map(sourceLabel).join("") || `<li class="empty">${tr("No configured sources", "尚未配置数据源")}</li>`}</ul></article></section>` : ""}
    <footer>${tr("Updated", "最近采样")} · ${escapeHtml(timestamp(projection.updated_at))} · ${tr("Agent Projection", "Agent Projection")} ${escapeHtml(projection.schema)}</footer>
  </main>`;
  root.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  root.querySelector<HTMLButtonElement>("#reset")?.addEventListener("click", () => void requestReset());
  root.querySelector<HTMLButtonElement>("#settings")?.addEventListener("click", () => void openSettings());
  bindChrome();
}

function renderWaiting(message: string, detail?: string): void {
  root.innerHTML = `<main class="shell shell--waiting">${topbar()}<section class="empty-state"><span class="pulse" aria-hidden="true"></span><h1>${escapeHtml(message)}</h1><p>${escapeHtml(detail ?? tr("The desktop shell will show data when the local Infra Agent publishes its first Projection.", "本地 Infra Agent 发布首个 Projection 后，桌面壳会自动显示数据。"))}</p><button class="button button--subtle" id="refresh">${icon("refresh")}<span>${tr("Refresh", "刷新")}</span></button></section></main>`;
  root.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  bindChrome();
}

async function refresh(): Promise<void> {
  if (activeView !== "overview") return;
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
