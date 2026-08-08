import { AgentProjection, ResourceProjection, SourceProjection } from "./bridge";
import { asArray, asRecord, formatTokens, number } from "./format";
import { tr } from "./i18n";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function tokenCard(label: string, value: unknown, detail: string): string {
  return `<article class="network-card network-card--blue"><p>${escapeHtml(label)}</p><strong>${formatTokens(value)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

export function renderAiUsageResourcePage(projection: AgentProjection, resource: ResourceProjection, sources: SourceProjection[]): string {
  const aiUsage = asRecord(projection.infra.ai_usage);
  const openCode = asRecord(aiUsage.opencode);
  const tokens = asRecord(openCode.tokens);
  const models = asArray(openCode.models);
  const outputLabel = Boolean(tokens.output_includes_reasoning)
    ? tr("Output + reasoning", "输出 + 推理")
    : tr("Output", "输出");
  const source = String(openCode.label || "OpenCode");
  const observed = String(openCode.observed_at || "");
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("RESOURCE DETAIL", "资源详情")}</p><h2>${tr("AI usage", "AI 用量")}</h2></div><span class="section-heading__meta">${escapeHtml(source)} · ${tr("today", "今日")}</span></div><section class="network-detail"><div class="network-card-grid ai-token-card-grid">${tokenCard(tr("Reported tokens", "已记录 Token"), tokens.total, tr("OpenCode session statistics", "OpenCode 会话统计"))}${tokenCard(tr("Input", "输入"), tokens.input, tr("provider-reported", "供应商返回"))}${tokenCard(outputLabel, tokens.output, Boolean(tokens.output_includes_reasoning) ? tr("reasoning included", "含推理 Token") : tr("provider-reported", "供应商返回"))}${tokenCard(tr("Reasoning", "推理"), tokens.reasoning, tr("provider-reported", "供应商返回"))}${tokenCard(tr("Cache", "缓存"), number(tokens.cache_read) + number(tokens.cache_write), `${tr("Read", "读取")} ${formatTokens(tokens.cache_read)} · ${tr("Write", "写入")} ${formatTokens(tokens.cache_write)}`)}</div><p class="network-explanation">${tr("OpenCode Desktop is queried read-only for assistant-message token metadata only. Prompts, responses, project paths, account rows, and credentials are never selected or stored.", "只读查询 OpenCode Desktop 的 assistant 消息 Token 元数据；不会选择或存储提示词、响应、项目路径、账户行或认证凭据。")}</p><article class="detail-panel ai-model-panel"><div class="detail-panel__heading"><h3>${tr("Model usage", "模型用量")}</h3><span>${models.length} ${tr("models", "个模型")}</span></div><ul class="traffic-list">${models.map((model) => `<li><span class="ai-model-name">${escapeHtml(model.id)}</span><span class="traffic-list__values"><small>${tr("Input", "输入")} ${formatTokens(model.input_tokens)} · ${outputLabel} ${formatTokens(model.output_tokens)} · ${tr("Reasoning", "推理")} ${formatTokens(model.reasoning_tokens)} · ${tr("Cache", "缓存")} ${formatTokens(number(model.cache_read_tokens) + number(model.cache_write_tokens))}</small><strong>${formatTokens(model.total_tokens)} ${tr("tokens", "Token")}</strong></span></li>`).join("") || `<li class="empty">${tr("OpenCode has not recorded model usage for today.", "OpenCode 今日尚未记录模型用量。")}</li>`}</ul><p class="panel-footnote">${tr("Snapshot", "最近采样")} · ${escapeHtml(observed || tr("waiting", "等待中"))} · ${tr("Exact local session metadata", "精确的本地会话元数据")}</p></article><article class="sources-card sources-card--footer"><div class="sources-card__heading"><h3>${tr("Collector sources", "采集数据源")}</h3><span>${sources.length}</span></div><ul>${sources.map((sourceItem) => `<li class="source-row"><span class="source-state source-state--${escapeHtml(sourceItem.status)}" aria-hidden="true"></span><span class="source-main"><strong>${escapeHtml(sourceItem.label)}</strong><small>${escapeHtml(sourceItem.kind)}</small></span><span class="source-status">${escapeHtml(sourceItem.status)}</span></li>`).join("")}</ul></article></section></section>`;
}
