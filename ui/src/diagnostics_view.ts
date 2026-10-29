import "./diagnostics_view.css";
import type { ActiveDiagnostic, DiagnosticCategory, DiagnosticEvent, DiagnosticsProjection } from "./bridge";
import type { DiagnosticsAnalysisSnapshot, DiagnosticsCategoryFilter, DiagnosticsTimeRange } from "./diagnostics_analysis";
import { currentLocale, tr } from "./i18n";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function categoryLabel(category: DiagnosticCategory | string): string {
  const labels: Record<string, string> = {
    source: tr("Collection", "数据采集"),
    network: tr("Network", "网络"),
    system: tr("System", "本机系统"),
    facility: tr("Facilities", "运行设施"),
    upstream: tr("Upstream services", "上游服务"),
  };
  return labels[category] ?? tr("Other", "其他");
}

function timestamp(value?: string | null, compact = false): string {
  const date = value ? new Date(value) : undefined;
  if (!date || Number.isNaN(date.getTime())) return tr("Time unknown", "时间未知");
  return date.toLocaleString(currentLocale() === "zh" ? "zh-CN" : "en-US", compact
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function diagnosticText(item: ActiveDiagnostic, field: "title" | "detail"): string {
  const translated = currentLocale() === "zh" ? item[field === "title" ? "title_zh" : "detail_zh"] : undefined;
  return translated || item[field] || tr("Observation unavailable", "观测信息不可用");
}

function sourceLabel(item: ActiveDiagnostic): string {
  const generic: Record<string, string> = {
    "Local system": tr("Local system", "本机系统"),
    "Facility discovery": tr("Facility discovery", "设施发现"),
    "Observed source": tr("Observed source", "观测来源"),
    "Remote network source": tr("Remote network source", "远端网络来源"),
    Facility: tr("Facility", "运行设施"),
    "Upstream service": tr("Upstream service", "上游服务"),
  };
  return generic[item.source_label] ?? (item.source_label || categoryLabel(item.category));
}

function observationLabel(status: string | undefined): string {
  const labels: Record<string, string> = {
    observed: tr("Observed", "已观测"),
    unknown: tr("Observation unknown", "观测状态未知"),
    disabled: tr("Source disabled", "数据源已停用"),
    unobserved: tr("No longer observed", "已无法继续观测"),
  };
  return labels[status ?? ""] ?? tr("Observation unknown", "观测状态未知");
}

function freshnessLabel(data?: DiagnosticsProjection): string {
  if (data?.freshness === "current") return tr("Latest observation", "最近观测");
  if (data?.freshness === "stale") return tr("Observation is stale", "观测已过期");
  return tr("Freshness unknown", "时效未知");
}

function activeCount(data?: DiagnosticsProjection): number | undefined {
  return data?.available === true && Number.isInteger(data.active_count) && data.active_count >= 0
    ? data.active_count : undefined;
}

function coverageLabel(data?: DiagnosticsProjection): string {
  if (data?.available !== true || !data.coverage?.length) return tr("Observation coverage unknown", "观测覆盖范围未知");
  const observed = data.coverage.filter((item) => item.observation_status === "observed" && item.freshness === "current").length;
  const uncertain = data.coverage.filter((item) => item.observation_status !== "disabled"
    && (item.observation_status !== "observed" || item.freshness !== "current")).length;
  const disabled = data.coverage.filter((item) => item.observation_status === "disabled").length;
  return [tr(`${observed} checks observed`, `${observed} 项检查有观测`),
    uncertain ? tr(`${uncertain} unknown`, `${uncertain} 项未知`) : "",
    disabled ? tr(`${disabled} disabled`, `${disabled} 项停用`) : ""].filter(Boolean).join(" · ");
}

export function renderDiagnosticsOverview(data?: DiagnosticsProjection, compact = false): string {
  const count = activeCount(data);
  const badge = count === undefined ? tr("Unavailable", "暂不可用")
    : tr(`${count} unresolved`, `${count} 项待确认异常`);
  const active = data?.active?.slice(0, 2) ?? [];
  const detail = count === undefined ? tr("Diagnostic history is not available yet.", "诊断历史暂不可用。")
    : active.length ? active.map((item) => `${sourceLabel(item)} · ${diagnosticText(item, "title")}`).join(" · ")
      : tr("No unresolved diagnostics in the latest recorded observation.", "最近一次已记录观测中没有待确认异常。");
  const countClass = `diagnostics-overview__count${count !== undefined && count > 0 ? " diagnostics-overview__count--attention" : ""}`;
  if (compact) return `<button class="diagnostics-overview-link" type="button" data-diagnostics-open aria-label="${escapeHtml(`${tr("View diagnostic history", "查看诊断历史")} · ${badge} · ${detail} · ${freshnessLabel(data)}`)}"><span>${tr("Attention & recovery", "异常与恢复")}</span><span class="${countClass}">${badge}</span><span aria-hidden="true">→</span></button>`;
  return `<section class="diagnostics-overview" aria-label="${tr("Diagnostic history", "诊断历史")}"><div class="diagnostics-overview__body"><div class="diagnostics-overview__heading"><strong>${tr("Attention & recovery", "异常与恢复")}</strong><span class="${countClass}">${badge}</span></div><p>${escapeHtml(detail)}${count !== undefined ? ` · ${escapeHtml(freshnessLabel(data))}` : ""}</p></div><button class="button button--subtle" type="button" data-diagnostics-open>${tr("View history", "查看历史")} →</button></section>`;
}

function renderCurrentAttention(data: DiagnosticsProjection | undefined, state: DiagnosticsAnalysisSnapshot): string {
  if (data?.available !== true || !data.active?.length) return "";
  const active = data.active.filter((item) => state.category === "all" || item.category === state.category);
  if (!active.length) return "";
  return `<details class="diagnostics-current" data-diagnostics-current${state.currentPanelOpen ? " open" : ""}><summary>${tr("Unresolved diagnostics", "待确认异常")}<small>${tr(`${active.length} listed`, `列出 ${active.length} 项`)}</small></summary>${active.map((item) => `<div class="diagnostics-current__row"><span>${escapeHtml(sourceLabel(item))}</span><strong>${escapeHtml(diagnosticText(item, "title"))}${item.observation_status !== "observed" ? `<small>${escapeHtml(observationLabel(item.observation_status))}</small>` : ""}</strong><time title="${escapeHtml(timestamp(item.observed_at))}">${escapeHtml(timestamp(item.observed_at, true))}</time></div>`).join("")}</details>`;
}

function transitionLabel(transition: string): string {
  const labels: Record<string, string> = {
    raised: tr("Raised", "出现"),
    changed: tr("Changed", "变化"),
    recovered: tr("Recovered", "恢复"),
  };
  return labels[transition] ?? tr("Changed", "变化");
}

function severityLabel(severity: string): string {
  const labels: Record<string, string> = {
    info: tr("Information", "信息"), warning: tr("Attention", "需关注"), critical: tr("Critical", "严重"),
  };
  return labels[severity] ?? tr("Unknown", "未知");
}

function renderEvent(event: DiagnosticEvent, expanded: boolean): string {
  const observed = event.observation_status === "observed";
  const transitionClass = observed ? event.transition : "unobserved";
  const detail = diagnosticText(event, "detail");
  return `<details class="diagnostics-event diagnostics-event--${escapeHtml(event.severity)}" data-diagnostics-event="${escapeHtml(event.id)}"${expanded ? " open" : ""}><summary><time class="diagnostics-event__time" datetime="${escapeHtml(event.observed_at)}" title="${escapeHtml(timestamp(event.observed_at))}">${escapeHtml(timestamp(event.observed_at, true))}</time><span class="diagnostics-transition diagnostics-transition--${escapeHtml(transitionClass)}">${escapeHtml(transitionLabel(event.transition))}</span><span class="diagnostics-event__source">${escapeHtml(sourceLabel(event))}<small>${escapeHtml(categoryLabel(event.category))}</small></span><span class="diagnostics-event__title">${escapeHtml(diagnosticText(event, "title"))}${!observed ? `<small>${escapeHtml(observationLabel(event.observation_status))}</small>` : ""}</span></summary><div class="diagnostics-event__detail"><p>${escapeHtml(detail)}</p><dl><div><dt>${tr("Severity", "级别")}</dt><dd>${escapeHtml(severityLabel(event.severity))}</dd></div><div><dt>${tr("First observed", "首次观测")}</dt><dd>${escapeHtml(timestamp(event.started_at))}</dd></div><div><dt>${tr("Observation", "观测状态")}</dt><dd>${escapeHtml(observationLabel(event.observation_status))}</dd></div><div><dt>${tr("Source identifier", "数据源标识")}</dt><dd><code>${escapeHtml(event.source_id)}</code></dd></div><div><dt>${tr("Diagnostic code", "诊断代码")}</dt><dd><code>${escapeHtml(event.code)}</code></dd></div><div><dt>${tr("Event identifier", "事件标识")}</dt><dd><code>${escapeHtml(event.id)}</code></dd></div></dl></div></details>`;
}

function renderFilters(state: DiagnosticsAnalysisSnapshot): string {
  const ranges: Array<[DiagnosticsTimeRange, string]> = [
    ["24h", tr("Last 24 hours", "最近 24 小时")],
    ["7d", tr("Last 7 days", "最近 7 天")],
    ["recorded", tr("Retained history", "保留的历史")],
  ];
  const categories: DiagnosticsCategoryFilter[] = ["all", "source", "network", "system", "facility", "upstream"];
  const status = state.loading ? tr("Updating history…", "正在更新历史…")
    : state.error ? tr("Refresh unavailable", "暂时无法刷新")
      : state.ready ? tr("Newest first", "按最近时间排列") : tr("Waiting for history", "等待历史数据");
  return `<div class="diagnostics-filters"><label>${tr("Time", "时间")}<select data-diagnostics-range aria-label="${tr("Diagnostic time range", "诊断时间范围")}">${ranges.map(([range, label]) => `<option value="${range}"${range === state.range ? " selected" : ""}>${label}</option>`).join("")}</select></label><label>${tr("Category", "类别")}<select data-diagnostics-category aria-label="${tr("Diagnostic category", "诊断类别")}">${categories.map((category) => `<option value="${category}"${category === state.category ? " selected" : ""}>${category === "all" ? tr("All categories", "全部类别") : categoryLabel(category)}</option>`).join("")}</select></label><p class="diagnostics-filters__status" role="status">${status}</p></div>`;
}

export function renderDiagnosticsPage(current: DiagnosticsProjection | undefined, state: DiagnosticsAnalysisSnapshot): string {
  const data = state.data;
  const latest = data ?? (state.category === "all" ? current : undefined);
  const count = activeCount(latest);
  const events = data?.available === true && Array.isArray(data.events) ? data.events : [];
  const historyStart = latest?.history_started_at ? timestamp(latest.history_started_at) : tr("Not recorded yet", "尚未开始记录");
  const retention = Number.isInteger(latest?.retention_days) && (latest?.retention_days ?? 0) > 0
    ? tr(`${latest!.retention_days} days retained locally`, `本机保留 ${latest!.retention_days} 天`) : tr("Retention unknown", "保留期限未知");
  const notice = state.error ? `<p class="diagnostics-notice diagnostics-notice--error" role="status">${state.ready ? tr("History could not be refreshed. Showing the last loaded results; use Refresh to retry.", "暂时无法刷新历史，当前显示上次读取结果；可点击刷新重试。") : tr("History could not be loaded. Use Refresh to retry.", "暂时无法读取历史，可点击刷新重试。")}</p>`
    : state.stale ? `<p class="diagnostics-notice">${tr("Showing earlier results while the latest changes load.", "正在读取最新变化，当前显示此前的查询结果。")}</p>` : "";
  const freshnessNotice = latest?.freshness !== "current" ? `<p class="diagnostics-notice">${escapeHtml(freshnessLabel(latest))} · ${latest?.observed_at ? tr("Last observation", "上次观测") : tr("Observation time", "观测时间")} ${escapeHtml(timestamp(latest?.observed_at))}</p>` : "";
  const emptyText = state.loading && !state.ready ? tr("Loading diagnostic history…", "正在读取诊断历史…")
    : state.error && !state.ready ? tr("No history results loaded.", "尚未读取历史结果。")
      : !state.ready || data?.available !== true ? tr("Diagnostic history is currently unavailable.", "诊断历史暂不可用。")
        : tr("No recorded changes match these filters.", "当前筛选范围内没有已记录的变化。");
  const resultCount = !state.ready || data?.available !== true ? tr("No history results available", "暂无可用历史结果")
    : data.truncated ? tr(`Showing the newest ${events.length} matching changes (result limit reached).`, `仅显示最近 ${events.length} 条匹配变化（已达显示上限）。`)
      : tr(`${events.length} recorded changes`, `${events.length} 条已记录变化`);
  const coverageLimits = data?.available === true ? [
    data.history_truncated ? tr("Earlier history has been trimmed by retention or row limits.", "较早历史已按保留期限或记录上限裁剪。") : "",
    data.sample_truncated ? tr("The latest sample exceeded the observation limit; coverage is partial.", "本次采样超出观测上限，覆盖范围不完整。") : "",
  ].filter(Boolean).join(" ") : "";
  return `<section class="diagnostics-page"><header class="diagnostics-page__heading"><div><h1>${tr("Diagnostic history", "诊断历史")}</h1><p>${tr("Anomalies, observation gaps and recovery across infrastructure.", "集中查看基础设施的异常、观测中断与恢复。")}</p></div><div class="diagnostics-page__count${count !== undefined && count > 0 ? " diagnostics-page__count--attention" : ""}"><strong>${count === undefined ? "—" : count}</strong><span>${tr("unresolved diagnostics", "项待确认异常")}</span></div></header>${renderCurrentAttention(latest, state)}<section class="diagnostics-history" aria-label="${tr("Recorded changes", "已记录变化")}">${renderFilters(state)}${freshnessNotice}${notice}<div class="diagnostics-table"><div class="diagnostics-table__heading" aria-hidden="true"><span>${tr("Observed", "观测时间")}</span><span>${tr("Change", "变化")}</span><span>${tr("Source", "来源")}</span><span>${tr("Observation", "观测内容")}</span><span></span></div>${events.length ? events.map((event) => renderEvent(event, state.expandedEventIds.has(event.id))).join("") : `<p class="diagnostics-empty">${emptyText}</p>`}</div><div class="diagnostics-history__footer"><span>${escapeHtml(resultCount)}</span>${coverageLimits ? `<span>${escapeHtml(coverageLimits)}</span>` : ""}<span>${escapeHtml(coverageLabel(latest))}</span><span>${escapeHtml(retention)} · ${tr("Recording began", "开始记录")} ${escapeHtml(historyStart)}</span><span>${escapeHtml(freshnessLabel(latest))} · ${escapeHtml(timestamp(latest?.observed_at))}</span></div></section></section>`;
}
