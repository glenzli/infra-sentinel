import { tr } from "./i18n";

/** Shared time scale for resource analysis. The visible copy intentionally
 * says recorded cumulative rather than pretending to know device lifetime. */
export type AnalysisScope = "today" | "cumulative" | "daily";

export function renderAnalysisScopes(resourceId: "network" | "ai_usage", selected: AnalysisScope): string {
  const scopes: Array<[AnalysisScope, string]> = [
    ["today", tr("Today", "今日")],
    ["cumulative", tr("Recorded total", "记录累计")],
    ["daily", tr("By day", "按日")],
  ];
  return `<div class="analysis-scopes" role="group" aria-label="${tr("Analysis time scale", "分析时间范围")}">${scopes.map(([scope, label]) => `<button class="analysis-scope${scope === selected ? " is-active" : ""}" type="button" data-analysis-resource="${resourceId}" data-analysis-scope="${scope}">${label}</button>`).join("")}</div>`;
}
