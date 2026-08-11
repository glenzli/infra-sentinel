import { requestAgentCommand } from "./agent_client";
import { bindLanguagePicker, languagePicker, localizeInlinePairs, tr } from "./i18n";
import { icon } from "./icons";

type JsonObject = Record<string, unknown>;

export interface SettingsPayload {
  schema: string;
  app: JsonObject;
  integrations: JsonObject;
  policies: JsonObject[];
  sources: JsonObject[];
}

export interface SettingsActions {
  cancel(): void;
  saved(settings: SettingsPayload): void;
  languageChanged(): void;
}

type SettingsSection = "general" | "integrations" | "network";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;",
  })[char] ?? char);
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function bool(value: unknown): boolean {
  return value === true;
}

function trafficPolicy(settings: SettingsPayload): JsonObject {
  const policy = settings.policies.find((item) => item.id === "network-traffic-alerts");
  if (!policy) throw new Error("Missing network traffic alert policy.");
  return policy;
}

function remoteSources(settings: SettingsPayload): JsonObject[] {
  return settings.sources.filter((source) => source.kind === "network.linux-xray");
}

function budgetPolicy(settings: SettingsPayload, sourceId: string): JsonObject | undefined {
  return settings.policies.find((item) => item.kind === "network.daily.usage" && item.source_id === sourceId);
}

function nextSourceId(settings: SettingsPayload): string {
  const existing = new Set(settings.sources.map((source) => String(source.id)));
  for (let index = 1; ; index += 1) {
    const candidate = `vps-${index}`;
    if (!existing.has(candidate)) return candidate;
  }
}

function cloneSettings(settings: SettingsPayload): SettingsPayload {
  return JSON.parse(JSON.stringify(settings)) as SettingsPayload;
}

function sourceRow(source: JsonObject, budget: JsonObject | undefined): string {
  const id = String(source.id);
  const enabled = bool(source.enabled);
  const budgetEnabled = Boolean(budget);
  const warning = number(budget?.warning_gib, 1);
  const critical = number(budget?.critical_gib, 2);
  return `
    <article class="host-card" data-source-id="${escapeHtml(id)}">
      <div class="host-card__top"><div><strong>${escapeHtml(source.label)}</strong><small>ID: ${escapeHtml(id)}</small></div><button class="icon-button" type="button" data-remove-source="${escapeHtml(id)}" aria-label="Remove VPS">−</button></div>
      <div class="host-form-grid">
        <label>Display name / 显示名称<input name="label:${escapeHtml(id)}" value="${escapeHtml(source.label)}" required /></label>
        <label>SSH Host / SSH 别名<input name="ssh:${escapeHtml(id)}" value="${escapeHtml(source.ssh_host)}" placeholder="my-vps" /></label>
        <label>Billing comparison / 账单对比方式<select name="billing:${escapeHtml(id)}"><option value="both" ${source.billing_mode === "both" ? "selected" : ""}>Both directions / 双向</option><option value="outbound" ${source.billing_mode === "outbound" ? "selected" : ""}>Outbound / 仅出站</option></select></label>
      </div>
      <div class="host-toggles">
        <label><input name="enabled:${escapeHtml(id)}" type="checkbox" ${enabled ? "checked" : ""} /> Enable host / 启用主机</label>
        <label><input name="xray:${escapeHtml(id)}" type="checkbox" ${bool(source.xray_stats_enabled) ? "checked" : ""} ${enabled ? "" : "disabled"} /> Xray user stats / Xray 用户统计</label>
        <label><input name="budget:${escapeHtml(id)}" type="checkbox" ${budgetEnabled ? "checked" : ""} ${enabled ? "" : "disabled"} /> Daily billing check / 每日账单检测</label>
      </div>
      <div class="host-budget ${budgetEnabled && enabled ? "" : "host-budget--disabled"}">
        <label>Warning threshold / 警告阈值<input name="budget-warning:${escapeHtml(id)}" type="number" min="1" value="${warning}" ${budgetEnabled && enabled ? "" : "disabled"} /><span>GiB/day</span></label>
        <label>Critical threshold / 严重阈值<input name="budget-critical:${escapeHtml(id)}" type="number" min="1" value="${critical}" ${budgetEnabled && enabled ? "" : "disabled"} /><span>GiB/day</span></label>
      </div>
      <p class="host-budget-note">Daily check: resets at 00:00 in this Mac's timezone / 每日检测：按本机时区每日 00:00 重置</p>
    </article>`;
}

function refreshHostControlState(form: HTMLFormElement): void {
  form.querySelectorAll<HTMLElement>(".host-card").forEach((card) => {
    const id = card.dataset.sourceId;
    if (!id) return;
    const enabled = form.elements.namedItem(`enabled:${id}`) as HTMLInputElement | null;
    const xray = form.elements.namedItem(`xray:${id}`) as HTMLInputElement | null;
    const budget = form.elements.namedItem(`budget:${id}`) as HTMLInputElement | null;
    const budgetBox = card.querySelector<HTMLElement>(".host-budget");
    if (!enabled || !xray || !budget || !budgetBox) return;
    xray.disabled = !enabled.checked;
    budget.disabled = !enabled.checked;
    const budgetActive = enabled.checked && budget.checked;
    budgetBox.classList.toggle("host-budget--disabled", !budgetActive);
    budgetBox.querySelectorAll<HTMLInputElement>("input").forEach((input) => { input.disabled = !budgetActive; });
  });
}

function readForm(form: HTMLFormElement, existing: SettingsPayload): SettingsPayload {
  const draft = cloneSettings(existing);
  const alert = trafficPolicy(draft);
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;
  alert.warning_window_minutes = Number(field("warning-window").value);
  alert.warning_mib = Number(field("warning-mib").value);
  alert.critical_window_minutes = Number(field("critical-window").value);
  alert.critical_mib = Number(field("critical-mib").value);
  draft.integrations = {
    ssh_executable: field("integration-ssh-executable").value.trim(),
    opencode_executable: field("integration-opencode-executable").value.trim(),
    opencode_database: field("integration-opencode-database").value.trim(),
    codex_database: field("integration-codex-database").value.trim(),
  };
  const remotes = remoteSources(draft);
  draft.policies = draft.policies.filter((policy) => policy.kind !== "network.daily.usage");
  for (const source of remotes) {
    const id = String(source.id);
    const enabled = field(`enabled:${id}`).checked;
    source.label = field(`label:${id}`).value.trim();
    source.ssh_host = field(`ssh:${id}`).value.trim();
    source.enabled = enabled;
    source.xray_stats_enabled = enabled && field(`xray:${id}`).checked;
    source.billing_mode = (form.elements.namedItem(`billing:${id}`) as HTMLSelectElement).value;
    if (enabled && field(`budget:${id}`).checked) {
      draft.policies.push({
        id: `${id}-daily-usage`, kind: "network.daily.usage", source_id: id,
        warning_gib: Number(field(`budget-warning:${id}`).value),
        critical_gib: Number(field(`budget-critical:${id}`).value),
      });
    }
  }
  return draft;
}

export async function loadSettings(): Promise<SettingsPayload> {
  const result = await requestAgentCommand("configuration.get", {});
  if (result.status !== "ok" || !result.payload || !result.payload.settings) {
    throw new Error(result.message ?? "The Infra Agent rejected the configuration request.");
  }
  return result.payload.settings as SettingsPayload;
}

export function renderSettings(root: HTMLDivElement, initial: SettingsPayload, actions: SettingsActions): void {
  let settings = cloneSettings(initial);
  settings.integrations ??= {};
  let activeSection: SettingsSection = "general";
  const render = (notice = "") => {
    const alert = trafficPolicy(settings);
    const hosts = remoteSources(settings);
    root.innerHTML = `
      <main class="shell">
        <header class="topbar"><button class="brand" id="back" type="button"><span class="brand-mark" aria-hidden="true"><i></i></span><span>Infra Sentinel</span></button><div class="topbar-actions"><button class="button button--subtle" id="back-overview">${icon("arrow-left")}<span>Back to overview / 返回概览</span></button></div></header>
        <section class="settings-header"><p class="eyebrow">CONFIGURATION</p><h1>Settings / 设置</h1><p>Local Mihomo is discovered automatically. Choose how Infra Sentinel appears, which hosts it observes, and when it should notify you.</p></section>
        <div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections"><button type="button" data-settings-section="general" class="${activeSection === "general" ? "is-active" : ""}">General / 通用</button><button type="button" data-settings-section="integrations" class="${activeSection === "integrations" ? "is-active" : ""}">Local integrations / 本地集成</button><button type="button" data-settings-section="network" class="${activeSection === "network" ? "is-active" : ""}">Network configuration / 网络配置</button></nav>
        <form id="settings-form" class="settings-form">
          <section class="settings-section settings-panel ${activeSection === "general" ? "" : "is-hidden"}"><div class="section-heading"><div><p class="eyebrow">GENERAL</p><h2>Appearance / 外观</h2></div></div>
            <div class="general-grid"><label class="setting-choice"><span>Language / 语言</span>${languagePicker()}</label></div>
          </section>
          <section class="settings-section settings-panel ${activeSection === "integrations" ? "" : "is-hidden"}"><div class="section-heading"><div><p class="eyebrow">LOCAL INTEGRATIONS</p><h2>Application paths / 应用路径</h2></div></div>
            <p class="settings-note">Leave a field empty to use platform discovery. Set an absolute path for portable or non-standard installations, including Windows installations outside their usual folders.</p>
            <div class="integration-grid">
              <label><span>SSH executable / SSH 程序</span><input name="integration-ssh-executable" value="${escapeHtml(settings.integrations.ssh_executable)}" placeholder="Auto discover / 自动发现" /></label>
              <label><span>OpenCode executable / OpenCode 程序</span><input name="integration-opencode-executable" value="${escapeHtml(settings.integrations.opencode_executable)}" placeholder="Auto discover / 自动发现" /></label>
              <label><span>OpenCode database / OpenCode 数据库</span><input name="integration-opencode-database" value="${escapeHtml(settings.integrations.opencode_database)}" placeholder="Auto discover / 自动发现" /></label>
              <label><span>Codex database / Codex 数据库</span><input name="integration-codex-database" value="${escapeHtml(settings.integrations.codex_database)}" placeholder="Auto discover / 自动发现" /></label>
            </div>
          </section>
          <section class="settings-section settings-panel ${activeSection === "network" ? "" : "is-hidden"}"><div class="section-heading"><div><p class="eyebrow">NETWORK SOURCES</p><h2>Remote host configuration / 远端主机配置</h2></div><button class="button button--subtle" type="button" id="add-host">${icon("plus")}<span>Add VPS / 添加 VPS</span></button></div>
            <p class="settings-note">Use a Host alias from <code>~/.ssh/config</code>. Xray StatsService remains limited to remote <code>127.0.0.1:10085</code>.</p>
            <div class="host-list">${hosts.map((source) => sourceRow(source, budgetPolicy(settings, String(source.id)))).join("") || "<p class=\"empty\">No remote host configured / 尚未配置远端主机</p>"}</div>
          </section>
          <section class="settings-section settings-panel ${activeSection === "network" ? "" : "is-hidden"}"><div class="section-heading"><div><p class="eyebrow">NETWORK ALERTS</p><h2>Traffic alerts / 流量告警</h2></div></div>
            <div class="alert-grid">
              <label>Warning window / 警告窗口<input name="warning-window" type="number" min="1" max="120" value="${number(alert.warning_window_minutes, 5)}" required /><span>minutes / 分钟</span></label>
              <label>Warning threshold / 警告阈值<input name="warning-mib" type="number" min="1" value="${number(alert.warning_mib, 250)}" required /><span>MiB</span></label>
              <label>Critical window / 严重窗口<input name="critical-window" type="number" min="1" max="240" value="${number(alert.critical_window_minutes, 10)}" required /><span>minutes / 分钟</span></label>
              <label>Critical threshold / 严重阈值<input name="critical-mib" type="number" min="1" value="${number(alert.critical_mib, 1024)}" required /><span>MiB</span></label>
            </div>
          </section>
          <p class="form-notice">${escapeHtml(notice)}</p>
          <div class="form-actions"><button class="button button--subtle" type="button" id="cancel">Cancel / 取消</button><button class="button button--primary" type="submit">Save and apply / 保存并应用</button></div>
        </form></div>
      </main>`;
    const form = root.querySelector<HTMLFormElement>("#settings-form");
    if (!form) return;
    localizeInlinePairs(root);
    refreshHostControlState(form);
    bindLanguagePicker(root, () => render());
    form.addEventListener("change", () => refreshHostControlState(form));
    root.querySelector<HTMLButtonElement>("#back")?.addEventListener("click", actions.cancel);
    root.querySelector<HTMLButtonElement>("#back-overview")?.addEventListener("click", actions.cancel);
    root.querySelector<HTMLButtonElement>("#cancel")?.addEventListener("click", actions.cancel);
    root.querySelectorAll<HTMLButtonElement>("[data-settings-section]").forEach((button) => button.addEventListener("click", () => {
      settings = readForm(form, settings);
      activeSection = button.dataset.settingsSection as SettingsSection;
      render(notice);
    }));
    root.querySelector<HTMLButtonElement>("#add-host")?.addEventListener("click", () => {
      settings.sources.push({ id: nextSourceId(settings), kind: "network.linux-xray", label: "New VPS", enabled: true, ssh_host: "", xray_stats_enabled: false, billing_mode: "both" });
      render();
    });
    root.querySelectorAll<HTMLButtonElement>("[data-remove-source]").forEach((button) => button.addEventListener("click", () => {
      const id = button.dataset.removeSource;
      settings.sources = settings.sources.filter((source) => source.id !== id);
      settings.policies = settings.policies.filter((policy) => policy.source_id !== id);
      render();
    }));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const save = form.querySelector<HTMLButtonElement>("button[type=submit]");
      if (save) { save.disabled = true; save.textContent = tr("Saving…", "正在保存…"); }
      try {
        const candidate = readForm(form, settings);
        const result = await requestAgentCommand("configuration.update", candidate as unknown as Record<string, unknown>);
        if (result.status !== "ok" || !result.payload?.settings) throw new Error(result.message ?? "Settings were rejected.");
        actions.saved(result.payload.settings as SettingsPayload);
      } catch (error) {
        render(`Could not save settings / 无法保存设置：${String(error)}`);
      }
    });
  };
  render();
}
