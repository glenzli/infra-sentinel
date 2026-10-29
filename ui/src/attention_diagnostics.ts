import "./attention_diagnostics.css";

import { tr } from "./i18n";

export type AttentionDiagnosticLevel = "info" | "degraded" | "warning" | "critical";

export interface AttentionDiagnostic {
  id: string;
  level: AttentionDiagnosticLevel;
  subject: string;
  title: string;
  current: string;
  basis?: string;
  action?: string;
}

const disclosureState = new Map<string, boolean>();
const boundDisclosures = new WeakSet<HTMLDetailsElement>();

/** Keep presentation choices across live refreshes, separately by severity. */
export function bindAttentionDiagnostics(root: ParentNode): void {
  root.querySelectorAll<HTMLDetailsElement>("details[data-attention-id]").forEach((details) => {
    if (boundDisclosures.has(details)) return;
    boundDisclosures.add(details);
    details.addEventListener("toggle", () => {
      const id = details.dataset.attentionId;
      if (!id) return;
      disclosureState.delete(id);
      disclosureState.set(id, details.open);
      if (disclosureState.size > 128) disclosureState.delete(disclosureState.keys().next().value!);
    });
  });
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

export function renderAttentionDiagnostics(
  diagnostics: AttentionDiagnostic[],
  heading = tr("Why this needs attention", "为什么需要关注"),
): string {
  if (!diagnostics.length) return "";
  const highest = diagnostics.some((item) => item.level === "critical")
    ? "critical"
    : diagnostics.some((item) => item.level === "warning")
      ? "warning"
      : diagnostics.some((item) => item.level === "degraded") ? "degraded" : "info";
  return `<section class="attention-diagnostics attention-diagnostics--${highest}" aria-label="${escapeHtml(heading)}">
    ${diagnostics.length > 1 ? `<header><span>${escapeHtml(heading)}</span><em>${diagnostics.length}</em></header>` : ""}
    <div class="attention-diagnostics__list">${diagnostics.map((item) => {
      const id = `${item.level}:${item.id}`;
      const open = disclosureState.get(id) ?? item.level === "critical";
      return `<details class="attention-diagnostic attention-diagnostic--${item.level}" data-attention-id="${escapeHtml(id)}"${open ? " open" : ""}>
        <summary><i aria-hidden="true"></i><small>${escapeHtml(item.subject)}</small><strong>${escapeHtml(item.title)}</strong><span>${tr("Details", "详情")}</span></summary>
        <div class="attention-diagnostic__body"><dl><div><dt>${tr("Current evidence", "当前证据")}</dt><dd>${escapeHtml(item.current)}</dd></div>${item.basis ? `<div><dt>${tr("Trigger / basis", "触发依据")}</dt><dd>${escapeHtml(item.basis)}</dd></div>` : ""}${item.action ? `<div><dt>${tr("Suggested action", "建议动作")}</dt><dd>${escapeHtml(item.action)}</dd></div>` : ""}</dl></div>
      </details>`;
    }).join("")}</div>
  </section>`;
}
