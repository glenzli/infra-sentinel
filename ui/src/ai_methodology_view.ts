import "./ai_methodology_view.css";
import { AgentProjection, ResourceProjection } from "./bridge";
import { asArray, asRecord, number } from "./format";
import { currentLocale, tr } from "./i18n";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function localized(value: unknown): string {
  const text = asRecord(value);
  return String(text[currentLocale() === "zh" ? "zh" : "en"] ?? text.en ?? text.zh ?? "");
}

function formatUsd(value: unknown): string {
  const amount = number(value);
  const digits = amount > 0 && amount < 0.01 ? 4 : 2;
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(amount);
}

function formatTokens(value: unknown): string {
  const amount = number(value);
  if (currentLocale() === "zh") {
    if (amount >= 100_000_000) return `${(amount / 100_000_000).toFixed(amount >= 1_000_000_000 ? 1 : 2)}亿`;
    if (amount >= 10_000) return `${(amount / 10_000).toFixed(amount >= 1_000_000 ? 1 : 2)}万`;
  }
  return new Intl.NumberFormat().format(Math.round(amount));
}

function metric(group: Record<string, unknown>, id: string): Record<string, unknown> | undefined {
  return asArray(group.metrics).map(asRecord).find((item) => item.id === id);
}

function group(source: Record<string, unknown>, id: string): Record<string, unknown> | undefined {
  return asArray(source.details).map(asRecord).find((item) => item.id === id);
}

function sourceWindow(source: Record<string, unknown>, name: "today" | "cumulative"): Record<string, unknown> {
  return asRecord(asRecord(source.usage)[name]);
}

function sourceCoverage(sources: Record<string, unknown>[]): string {
  return `<section class="methodology-card methodology-card--coverage"><div class="methodology-card__heading"><h3>${tr("Collection windows", "采样窗口")}</h3><span>${sources.length} ${tr("sources", "个来源")}</span></div><div class="methodology-source-list">${sources.map((source) => {
    const today = sourceWindow(source, "today");
    const cumulative = sourceWindow(source, "cumulative");
    const label = String(source.label ?? source.source_id ?? "");
    return `<article><strong>${escapeHtml(label)}</strong><span>${escapeHtml(localized(today.detail) || String(today.method ?? ""))}</span><small>${tr("History", "历史")} · ${escapeHtml(localized(cumulative.detail) || String(cumulative.method ?? ""))}</small></article>`;
  }).join("")}</div></section>`;
}

function priceReference(source: Record<string, unknown> | undefined, sourceId: string): string {
  if (!source) return "";
  const isAntigravity = sourceId === "antigravity";
  const pricingGroup = isAntigravity ? group(source, "antigravity-api-reference") : group(source, "standard-api-estimate");
  if (!pricingGroup) return "";
  const total = metric(pricingGroup, isAntigravity ? "antigravity-api-cumulative" : "standard-api-total");
  const today = metric(pricingGroup, isAntigravity ? "antigravity-api-today" : "standard-api-total");
  const priced = metric(pricingGroup, isAntigravity ? "antigravity-api-priced-tokens" : "standard-api-priced-sample-tokens");
  const unpriced = metric(pricingGroup, isAntigravity ? "antigravity-api-unpriced-tokens" : "");
  const title = isAntigravity ? tr("Antigravity API references", "Antigravity API 参考") : tr("Codex API sample reference", "Codex API 抽样参考");
  const totalLabel = isAntigravity ? tr("Local history", "本地历史") : tr("Observed sample", "观察样本");
  const todayValue = isAntigravity ? `<div><span>${tr("Today", "今日")}</span><strong>${formatUsd(today?.value)}</strong></div>` : "";
  return `<section class="methodology-card methodology-card--pricing"><div class="methodology-card__heading"><h3>${title}</h3><span>${escapeHtml(localized(pricingGroup.badge))}</span></div><div class="methodology-price-values">${todayValue}<div><span>${totalLabel}</span><strong>${formatUsd(total?.value)}</strong></div><div><span>${tr("Price-matched Tokens", "可计价 Token")}</span><strong>${formatTokens(priced?.value)}</strong></div></div>${unpriced && number(unpriced.value) > 0 ? `<p class="methodology-note">${tr("Not priced because no explicit official model-price mapping: ", "未计价：没有明确的官方模型价格映射 · ")}${formatTokens(unpriced.value)}</p>` : ""}<p class="methodology-note">${escapeHtml(localized(pricingGroup.note))}</p></section>`;
}

export function renderAiUsageMethodologyPage(projection: AgentProjection, resource: ResourceProjection): string {
  const sources = asArray(asRecord(projection.infra.ai_usage).sources).map(asRecord);
  const antigravity = sources.find((source) => source.source_id === "antigravity");
  const codex = sources.find((source) => source.source_id === "codex");
  return `<section class="resource-section resource-section--detail"><div class="section-heading methodology-page-heading"><div><p class="eyebrow">${tr("AI USAGE", "AI 用量")}</p><h2>${tr("Methods & estimates", "口径与估算")}</h2></div><div class="hero-actions"><button class="button button--subtle" type="button" data-ai-methodology-back>← ${tr("AI usage", "AI 用量")}</button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></div><section class="ai-methodology"><article class="methodology-intro"><strong>${tr("Local observation, not a provider invoice", "本地观测，不等同供应商账单")}</strong><p>${tr("Usage comes from bounded local metadata and provider-returned counters. API values below are price references for explicit local model mappings only.", "用量仅来自受限的本地元数据和供应商返回计数；下方 API 金额只对应明确本地模型映射的参考价。")}</p></article><div class="methodology-grid">${sourceCoverage(sources)}${priceReference(codex, "codex")}${priceReference(antigravity, "antigravity")}</div></section></section>`;
}
