import "./styles.css";
import { AgentProjection, OverallStatus, ResourceProjection, SourceProjection, readProjection, resetSession } from "./bridge";

function appRoot(): HTMLDivElement {
  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) throw new Error("missing app root");
  return root;
}

const root = appRoot();

const formatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const exponent = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${formatter.format(value / 1024 ** exponent)} ${units[exponent]}`;
}

function resourceLabel(resource: ResourceProjection): string {
  return resource.id === "network" ? "Network / 网络" : resource.id;
}

function statusLabel(status: OverallStatus): string {
  const labels: Record<string, string> = {
    healthy: "Healthy / 正常",
    warning: "Attention / 需关注",
    critical: "Critical / 严重",
    degraded: "Degraded / 数据源异常",
  };
  return labels[status] ?? status;
}

function relativeTimestamp(value?: string): string {
  if (!value) return "Waiting for first sample / 等待首次采样";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function renderSource(source: SourceProjection): string {
  const state = source.enabled ? source.status : "disabled";
  return `
    <li class="source-row">
      <span class="source-state source-state--${state}" aria-hidden="true"></span>
      <span class="source-main"><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.kind)}</small></span>
      <span class="source-status">${escapeHtml(state)}</span>
    </li>`;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function renderProjection(projection: AgentProjection): void {
  const infra = projection.infra;
  const resources = infra.resources.filter((resource) => resource.enabled);
  const network = resources.find((resource) => resource.id === "network") ?? resources[0];
  const sources = network
    ? infra.sources.filter((source) => source.resource_id === network.id)
    : infra.sources;
  const primaryValue = network?.primary_unit === "bytes"
    ? formatBytes(network.primary_value)
    : formatter.format(network?.primary_value ?? 0);
  const activeAlerts = infra.overall.active_alerts;

  root.innerHTML = `
    <main class="shell">
      <header class="topbar">
        <div class="brand"><span class="brand-mark" aria-hidden="true"><i></i></span><span>Infra Sentinel</span></div>
        <div class="agent-state"><span class="source-state source-state--${escapeHtml(infra.overall.status)}"></span>${escapeHtml(statusLabel(infra.overall.status))}</div>
      </header>
      <section class="hero">
        <div>
          <p class="eyebrow">PERSONAL AI INFRA</p>
          <h1>One calm view of what your infrastructure is doing.</h1>
          <p class="lede">一个本地优先的资源监控面板。当前已接入网络模块，其他模块只在真正启用后出现。</p>
        </div>
        <div class="hero-actions">
          <button class="button button--subtle" id="refresh">Refresh / 刷新</button>
          <button class="button button--danger" id="reset">Reset totals / 重置统计</button>
        </div>
      </section>
      <section class="summary-grid" aria-label="Overview">
        <article class="summary-card summary-card--status">
          <p>Overall health / 整体健康</p>
          <strong class="status-value status-value--${escapeHtml(infra.overall.status)}">${escapeHtml(statusLabel(infra.overall.status))}</strong>
          <small>${activeAlerts ? `${activeAlerts} active alert${activeAlerts === 1 ? "" : "s"}` : "No active alerts / 暂无活动告警"}</small>
        </article>
        <article class="summary-card">
          <p>Active modules / 已启用模块</p>
          <strong>${resources.length}</strong>
          <small>${resources.map(resourceLabel).join(" · ") || "None"}</small>
        </article>
        <article class="summary-card">
          <p>Data sources / 数据源</p>
          <strong>${network?.online_source_count ?? 0}<em> / ${network?.source_count ?? 0}</em></strong>
          <small>online / 在线</small>
        </article>
      </section>
      ${network ? `
        <section class="resource-section">
          <div class="section-heading"><div><p class="eyebrow">ACTIVE RESOURCE</p><h2>${resourceLabel(network)}</h2></div><span class="pill pill--${escapeHtml(network.status)}">${escapeHtml(statusLabel(network.status))}</span></div>
          <div class="network-layout">
            <article class="metric-card">
              <p>Primary cumulative metric / 当前累计</p>
              <strong>${primaryValue}</strong>
              <small>${escapeHtml(network.primary_metric)} · ${escapeHtml(network.primary_source_id)}</small>
            </article>
            <article class="sources-card">
              <div class="sources-card__heading"><h3>Sources / 数据源</h3><span>${sources.length}</span></div>
              <ul>${sources.map(renderSource).join("") || "<li class=\"empty\">No configured sources / 尚未配置数据源</li>"}</ul>
            </article>
          </div>
        </section>` : ""}
      <footer>Agent Projection ${escapeHtml(projection.schema)} · ${escapeHtml(projection.protocol.transport)} · ${escapeHtml(relativeTimestamp(projection.updated_at))}</footer>
    </main>`;

  document.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
  document.querySelector<HTMLButtonElement>("#reset")?.addEventListener("click", () => void requestReset());
}

function renderWaiting(message: string): void {
  root.innerHTML = `
    <main class="shell shell--waiting">
      <header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i></span><span>Infra Sentinel</span></div></header>
      <section class="empty-state"><span class="pulse" aria-hidden="true"></span><h1>${escapeHtml(message)}</h1><p>The desktop shell is ready. It will show data when the local Infra Agent publishes its first Projection.<br/>桌面外壳已就绪；本地 Infra Agent 发布首个 Projection 后会自动显示数据。</p><button class="button button--subtle" id="refresh">Refresh / 刷新</button></section>
    </main>`;
  document.querySelector<HTMLButtonElement>("#refresh")?.addEventListener("click", () => void refresh());
}

async function refresh(): Promise<void> {
  try {
    const projection = await readProjection();
    if (!projection) {
      renderWaiting("Waiting for Infra Agent / 等待 Infra Agent");
      return;
    }
    renderProjection(projection);
  } catch (error) {
    renderWaiting(`Cannot read local state / 无法读取本地状态：${String(error)}`);
  }
}

async function requestReset(): Promise<void> {
  if (!window.confirm("Reset the current totals? This establishes new local baselines after the next Agent sample.\n\n重置当前统计？下一次 Agent 采样后会重新建立本地基线。")) return;
  try {
    await resetSession();
    await refresh();
  } catch (error) {
    window.alert(`Reset request could not be sent / 无法发送重置请求：${String(error)}`);
  }
}

renderWaiting("Connecting to Infra Agent / 正在连接 Infra Agent");
void refresh();
window.setInterval(() => void refresh(), 2_000);
