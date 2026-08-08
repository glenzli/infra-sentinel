import { requestAgentCommand } from "./agent_client";
import { bindLanguagePicker, languagePicker, localizeInlinePairs, tr } from "./i18n";
import { icon } from "./icons";

type JsonObject = Record<string, unknown>;

export interface SettingsPayload {
  schema: string;
  app: JsonObject;
  policies: JsonObject[];
  sources: JsonObject[];
}

export interface SettingsActions {
  cancel(): void;
  saved(settings: SettingsPayload): void;
  languageChanged(): void;
}

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
  return settings.policies.find((item) => item.kind === "network.billing.budget" && item.source_id === sourceId);
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
        <label>Billing / 计费方式<select name="billing:${escapeHtml(id)}"><option value="both" ${source.billing_mode === "both" ? "selected" : ""}>Both directions / 双向</option><option value="outbound" ${source.billing_mode === "outbound" ? "selected" : ""}>Outbound / 仅出站</option></select></label>
        <label>Cycle day / 周期日<input name="cycle:${escapeHtml(id)}" type="number" min="1" max="31" value="${number(source.billing_cycle_start_day, 1)}" required /></label>
      </div>
      <div class="host-toggles">
        <label><input name="enabled:${escapeHtml(id)}" type="checkbox" ${enabled ? "checked" : ""} /> Enable host / 启用主机</label>
        <label><input name="xray:${escapeHtml(id)}" type="checkbox" ${bool(source.xray_stats_enabled) ? "checked" : ""} ${enabled ? "" : "disabled"} /> Xray user stats / Xray 用户统计</label>
        <label><input name="budget:${escapeHtml(id)}" type="checkbox" ${budgetEnabled ? "checked" : ""} ${enabled ? "" : "disabled"} /> Billing budget / 账单预算</label>
      </div>
      <div class="host-budget ${budgetEnabled && enabled ? "" : "host-budget--disabled"}">
        <label>Warning / 警告<input name="budget-warning:${escapeHtml(id)}" type="number" min="1" value="${warning}" ${budgetEnabled && enabled ? "" : "disabled"} /><span>GiB</span></label>
        <label>Critical / 严重<input name="budget-critical:${escapeHtml(id)}" type="number" min="1" value="${critical}" ${budgetEnabled && enabled ? "" : "disabled"} /><span>GiB</span></label>
      </div>
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
  const remotes = remoteSources(draft);
  draft.policies = draft.policies.filter((policy) => policy.kind !== "network.billing.budget");
  for (const source of remotes) {
    const id = String(source.id);
    const enabled = field(`enabled:${id}`).checked;
    source.label = field(`label:${id}`).value.trim();
    source.ssh_host = field(`ssh:${id}`).value.trim();
    source.enabled = enabled;
    source.xray_stats_enabled = enabled && field(`xray:${id}`).checked;
    source.billing_mode = (form.elements.namedItem(`billing:${id}`) as HTMLSelectElement).value;
    source.billing_cycle_start_day = Number(field(`cycle:${id}`).value);
    if (enabled && field(`budget:${id}`).checked) {
      draft.policies.push({
        id: `${id}-billing-budget`, kind: "network.billing.budget", source_id: id,
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
  const render = (notice = "") => {
    const alert = trafficPolicy(settings);
    const hosts = remoteSources(settings);
    root.innerHTML = `
      <main class="shell">
        <header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i></span><span>Infra Sentinel</span></div><div class="topbar-actions">${languagePicker()}<button class="button button--subtle" id="back">${icon("arrow-left")}<span>Back to overview / 返回概览</span></button></div></header>
        <section class="settings-header"><p class="eyebrow">CONFIGURATION</p><h1>Settings / 设置</h1><p>Local Mihomo is discovered automatically. Configure only alert policy and remote hosts.</p></section>
        <form id="settings-form" class="settings-form">
          <section class="settings-section"><div class="section-heading"><div><p class="eyebrow">ALERT POLICY</p><h2>Traffic alerts / 流量告警</h2></div></div>
            <div class="alert-grid">
              <label>Warning window / 警告窗口<input name="warning-window" type="number" min="1" max="120" value="${number(alert.warning_window_minutes, 5)}" required /><span>minutes / 分钟</span></label>
              <label>Warning threshold / 警告阈值<input name="warning-mib" type="number" min="1" value="${number(alert.warning_mib, 250)}" required /><span>MiB</span></label>
              <label>Critical window / 严重窗口<input name="critical-window" type="number" min="1" max="240" value="${number(alert.critical_window_minutes, 10)}" required /><span>minutes / 分钟</span></label>
              <label>Critical threshold / 严重阈值<input name="critical-mib" type="number" min="1" value="${number(alert.critical_mib, 1024)}" required /><span>MiB</span></label>
            </div>
          </section>
          <section class="settings-section"><div class="section-heading"><div><p class="eyebrow">REMOTE HOSTS</p><h2>Host configuration / 主机配置</h2></div><button class="button button--subtle" type="button" id="add-host">${icon("plus")}<span>Add VPS / 添加 VPS</span></button></div>
            <p class="settings-note">Use a Host alias from <code>~/.ssh/config</code>. Xray StatsService remains limited to remote <code>127.0.0.1:10085</code>.</p>
            <div class="host-list">${hosts.map((source) => sourceRow(source, budgetPolicy(settings, String(source.id)))).join("") || "<p class=\"empty\">No remote host configured / 尚未配置远端主机</p>"}</div>
          </section>
          <p class="form-notice">${escapeHtml(notice)}</p>
          <div class="form-actions"><button class="button button--subtle" type="button" id="cancel">Cancel / 取消</button><button class="button button--primary" type="submit">Save and apply / 保存并应用</button></div>
        </form>
      </main>`;
    const form = root.querySelector<HTMLFormElement>("#settings-form");
    if (!form) return;
    localizeInlinePairs(root);
    refreshHostControlState(form);
    bindLanguagePicker(root, actions.languageChanged);
    form.addEventListener("change", () => refreshHostControlState(form));
    root.querySelector<HTMLButtonElement>("#back")?.addEventListener("click", actions.cancel);
    root.querySelector<HTMLButtonElement>("#cancel")?.addEventListener("click", actions.cancel);
    root.querySelector<HTMLButtonElement>("#add-host")?.addEventListener("click", () => {
      settings.sources.push({ id: nextSourceId(settings), kind: "network.linux-xray", label: "New VPS", enabled: true, ssh_host: "", xray_stats_enabled: false, billing_cycle_start_day: 1, billing_mode: "both" });
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
      if (save) { save.disabled = true; save.textContent = "Saving… / 正在保存…"; }
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
