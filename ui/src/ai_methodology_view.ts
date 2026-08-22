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
  return `<section class="methodology-card methodology-card--coverage"><div class="methodology-card__heading"><div><p>${tr("Local collection", "本地采集")}</p><h3>${tr("Where these numbers come from", "各来源的采集口径")}</h3></div><span>${sources.length} ${tr("sources", "个来源")}</span></div><div class="methodology-source-list">${sources.map((source) => {
    const today = sourceWindow(source, "today");
    const cumulative = sourceWindow(source, "cumulative");
    const label = String(source.label ?? source.source_id ?? "");
    const todayDetail = localized(today.detail) || String(today.method ?? "");
    const historyDetail = localized(cumulative.detail) || String(cumulative.method ?? "");
    return `<article><div><strong>${escapeHtml(label)}</strong><p>${escapeHtml(todayDetail)}</p></div><div class="methodology-source-window"><span><small>${tr("Today", "今日")}</small><b>${formatTokens(today.tokens)}</b></span><span><small>${tr("History", "历史")}</small><b>${formatTokens(cumulative.tokens)}</b></span></div><small class="methodology-source-history">${escapeHtml(historyDetail)}</small></article>`;
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
  const title = isAntigravity ? tr("Antigravity model mapping", "Antigravity 模型映射") : tr("Codex sampled mapping", "Codex 抽样映射");
  const mode = isAntigravity ? tr("Explicit local model-price mapping", "明确的本地模型价格映射") : tr("JSONL sample projected to matching local models", "JSONL 抽样投影至匹配的本地模型");
  const totalLabel = isAntigravity ? tr("Local history", "本地历史") : tr("Observed sample", "观察样本");
  const todayValue = isAntigravity ? `<div><span>${tr("Today", "今日")}</span><strong>${formatUsd(today?.value)}</strong></div>` : "";
  const unpricedNote = unpriced && number(unpriced.value) > 0
    ? `${tr("Unpriced because the local model has no explicit official price mapping: ", "未计价：本地模型没有明确的官方价格映射 · ")}${formatTokens(unpriced.value)}`
    : "";
  return `<section class="methodology-card methodology-card--pricing methodology-card--${sourceId}"><div class="methodology-card__heading"><div><p>${mode}</p><h3>${title}</h3></div><span>${escapeHtml(localized(pricingGroup.badge))}</span></div><div class="methodology-price-values">${todayValue}<div><span>${totalLabel}</span><strong>${formatUsd(total?.value)}</strong></div><div><span>${tr("Price-matched Tokens", "可计价 Token")}</span><strong>${formatTokens(priced?.value)}</strong></div></div>${unpricedNote ? `<p class="methodology-note methodology-note--warning">${escapeHtml(unpricedNote)}</p>` : ""}<p class="methodology-note">${escapeHtml(localized(pricingGroup.note))}</p></section>`;
}

export function renderAiUsageMethodologyPage(projection: AgentProjection, _resource: ResourceProjection): string {
  const sources = asArray(asRecord(projection.infra.ai_usage).sources).map(asRecord);
  const antigravity = sources.find((source) => source.source_id === "antigravity");
  const codex = sources.find((source) => source.source_id === "codex");
  return `<section class="resource-section resource-section--detail"><div class="section-heading methodology-page-heading"><div><p class="eyebrow">${tr("AI USAGE", "AI 用量")}</p><h2>${tr("Methods & estimates", "口径与估算")}</h2></div><div class="hero-actions"><button class="button button--subtle" type="button" data-ai-methodology-back>← ${tr("AI usage", "AI 用量")}</button><button class="button button--subtle" id="settings"><span>${tr("Settings", "设置")}</span></button><button class="button button--subtle" id="refresh"><span>${tr("Refresh", "刷新")}</span></button></div></div><section class="ai-methodology"><header class="methodology-hero"><div><p>${tr("LOCAL OBSERVATION", "本地观测")}</p><h3>${tr("A useful reading, not a bill", "可供观察，不等同账单")}</h3><span>${tr("Token volume is one resource signal. It does not measure output, value, or remaining subscription quota.", "Token 量只是资源观察维度之一，不代表产出、价值或订阅剩余额度。")}</span></div><b>${tr("Estimate", "估算")}</b></header><section class="methodology-guidance"><i>≈</i><div><strong>${tr("Only explicit price mappings are shown.", "只展示具有明确价格映射的估算。")}</strong><span>${tr("Unknown models stay unpriced; local records never become a provider invoice.", "未匹配模型保持未计价；本地记录不会变成供应商账单。")}</span></div></section><div class="methodology-grid">${sourceCoverage(sources)}<div class="methodology-pricing-stack">${priceReference(codex, "codex")}${priceReference(antigravity, "antigravity")}</div></div><footer class="methodology-boundary"><span>${tr("Privacy boundary", "隐私边界")}</span><p>${tr("Only bounded local metadata and provider-returned counters are read. Prompts, responses, and project content are not retained.", "仅读取受限的本地元数据与供应商返回计数；不保留提示词、响应或项目内容。")}</p></footer></section></section>`;
}
