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
  return `<section class="attention-diagnostics attention-diagnostics--${highest}">
    <header><span>${escapeHtml(heading)}</span><em>${diagnostics.length}</em></header>
    <div class="attention-diagnostics__list">${diagnostics.map((item) => `<article class="attention-diagnostic attention-diagnostic--${item.level}">
      <i aria-hidden="true"></i><div class="attention-diagnostic__body"><p><small>${escapeHtml(item.subject)}</small><strong>${escapeHtml(item.title)}</strong></p>
      <dl><div><dt>${tr("Current evidence", "当前证据")}</dt><dd>${escapeHtml(item.current)}</dd></div>${item.basis ? `<div><dt>${tr("Trigger / basis", "触发依据")}</dt><dd>${escapeHtml(item.basis)}</dd></div>` : ""}${item.action ? `<div><dt>${tr("Suggested action", "建议动作")}</dt><dd>${escapeHtml(item.action)}</dd></div>` : ""}</dl></div>
    </article>`).join("")}</div>
  </section>`;
}
