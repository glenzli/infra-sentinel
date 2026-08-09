import "./daily_bar_chart.css";
import { tr } from "./i18n";

export type DailyBarSeries = {
  id: string;
  label: string;
  color: string;
};

export type DailyBarBucket = {
  epoch: number;
  values: Map<string, number>;
};

export type DailyBarChartOptions = {
  title: string;
  detail: string;
  ariaLabel: string;
  footnote: string;
  formatValue: (value: number) => string;
  mode?: "grouped" | "stacked";
};

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function dayLabel(epoch: number): string {
  const date = new Date(epoch * 1_000);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function tooltip(bucket: DailyBarBucket, series: DailyBarSeries[], formatValue: (value: number) => string): string {
  const visible = series.filter((item) => (bucket.values.get(item.id) ?? 0) > 0);
  const total = visible.reduce((sum, item) => sum + (bucket.values.get(item.id) ?? 0), 0);
  const rows = visible.map((item) => `${item.label} ${formatValue(bucket.values.get(item.id) ?? 0)}`);
  return [dayLabel(bucket.epoch), `${tr("shown total", "图示合计")} ${formatValue(total)}`, ...rows].join(" · ");
}

/**
 * Render a calendar-aligned daily chart. Grouped series preserve independent
 * boundaries; stacked series are reserved for comparable pieces of one total.
 */
export function renderDailyBarChart(
  series: DailyBarSeries[],
  buckets: DailyBarBucket[],
  options: DailyBarChartOptions,
): string {
  const usable = series.filter((item) => buckets.some((bucket) => (bucket.values.get(item.id) ?? 0) > 0));
  const stacked = options.mode === "stacked";
  const maximum = Math.max(...buckets.map((bucket) => stacked
    ? usable.reduce((sum, item) => sum + (bucket.values.get(item.id) ?? 0), 0)
    : Math.max(...usable.map((item) => bucket.values.get(item.id) ?? 0), 0)), 1);
  const legend = usable.length > 1
    ? `<div class="daily-bars__legend">${usable.map((item) => `<span><i class="chart-dot" style="background:${item.color}"></i>${escapeHtml(item.label)}</span>`).join("")}</div>`
    : "";
  return `<article class="detail-panel daily-history-chart"><div class="detail-panel__heading"><h3>${escapeHtml(options.title)}</h3><span>${escapeHtml(options.detail)}</span></div>${legend}<div class="daily-bars" role="img" aria-label="${escapeHtml(options.ariaLabel)}">${buckets.map((bucket) => {
    const title = tooltip(bucket, usable, options.formatValue);
    return `<div class="daily-bar-day"><div class="daily-bar-day__bars${stacked ? " daily-bar-day__bars--stacked" : ""}" title="${escapeHtml(title)}">${usable.map((item) => {
      const value = bucket.values.get(item.id) ?? 0;
      const height = value > 0 ? Math.max(4, (value / maximum) * 100) : 0;
      return `<i style="--daily-bar-color:${item.color};height:${height}%"></i>`;
    }).join("")}</div><span>${escapeHtml(dayLabel(bucket.epoch))}</span></div>`;
  }).join("")}</div><p class="panel-footnote">${escapeHtml(options.footnote)}</p></article>`;
}
