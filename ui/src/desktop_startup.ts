import { getDesktopStartup, setDesktopStartup } from "./bridge";
import { tr } from "./i18n";

/** Desktop-only preference. It never enters the Agent configuration or restarts it. */
export async function bindDesktopStartup(root: HTMLElement): Promise<void> {
  const host = root.querySelector<HTMLElement>("#desktop-startup");
  if (!host) return;
  try {
    const status = await getDesktopStartup();
    if (!host.isConnected || !status.supported) return;
    host.innerHTML = `<label class="setting-choice"><span>${tr("Start when I sign in to Windows", "登录 Windows 时自动启动")}</span><input type="checkbox" id="desktop-startup-enabled" ${status.enabled ? "checked" : ""} /></label><p class="settings-note">${tr("Starts quietly in the system tray. Changes apply immediately. Windows startup settings can also disable this app.", "启动后静默驻留托盘，修改立即生效。也可在 Windows 启动应用设置中禁用。")}</p><p class="settings-note" role="status"></p>`;
    const checkbox = host.querySelector<HTMLInputElement>("input");
    const notice = host.querySelector<HTMLElement>("[role=status]");
    if (!checkbox || !notice) return;
    let enabled = status.enabled;
    checkbox.addEventListener("change", async () => {
      checkbox.disabled = true;
      notice.textContent = tr("Applying…", "正在应用…");
      try {
        const updated = await setDesktopStartup(checkbox.checked);
        enabled = updated.enabled;
        notice.textContent = enabled ? tr("Automatic startup enabled.", "已启用自动启动。") : tr("Automatic startup disabled.", "已关闭自动启动。");
      } catch {
        notice.textContent = tr("Could not change automatic startup. Please try again.", "无法更改自动启动，请重试。");
      } finally {
        checkbox.checked = enabled;
        checkbox.disabled = false;
      }
    });
  } catch {
    if (host.isConnected) {
      host.innerHTML = `<p class="settings-note">${tr("Could not read the startup preference.", "无法读取自动启动设置。")}</p><button type="button" class="button button--subtle">${tr("Retry", "重试")}</button>`;
      host.querySelector("button")?.addEventListener("click", () => void bindDesktopStartup(root));
    }
  }
}
