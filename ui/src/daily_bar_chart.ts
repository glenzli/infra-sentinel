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

export type DailyBarOverlay = {
  label: string;
  color: string;
  values: Map<number, number>;
  formatValue: (value: number) => string;
};

export type DailyBarChartOptions = {
  title: string;
  detail: string;
  ariaLabel: string;
  footnote: string;
  formatValue: (value: number) => string;
  mode?: "grouped" | "stacked";
  overlay?: DailyBarOverlay;
};

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function dayLabel(epoch: number): string {
  const date = new Date(epoch * 1_000);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function tooltip(bucket: DailyBarBucket, series: DailyBarSeries[], formatValue: (value: number) => string, overlay?: DailyBarOverlay): { label: string; markup: string } {
  const visible = series.filter((item) => (bucket.values.get(item.id) ?? 0) > 0);
  const total = visible.reduce((sum, item) => sum + (bucket.values.get(item.id) ?? 0), 0);
  const heading = `${dayLabel(bucket.epoch)} · ${tr("shown total", "图示合计")} ${formatValue(total)}`;
  const rows = visible.map((item) => `${item.label} ${formatValue(bucket.values.get(item.id) ?? 0)}`);
  const overlayValue = overlay ? overlay.values.get(bucket.epoch) ?? 0 : 0;
  const overlayRow = overlayValue > 0
    ? `<li class="daily-bar-tooltip__overlay"><span>${escapeHtml(overlay?.label ?? "")}</span><b>${escapeHtml(overlay?.formatValue(overlayValue) ?? "")}</b></li>`
    : "";
  return {
    label: [heading, ...rows, overlayValue > 0 ? `${overlay?.label} ${overlay?.formatValue(overlayValue)}` : ""].filter(Boolean).join(" · "),
    markup: `<strong>${escapeHtml(heading)}</strong><ul>${visible.map((item) => `<li><span>${escapeHtml(item.label)}</span><b>${escapeHtml(formatValue(bucket.values.get(item.id) ?? 0))}</b></li>`).join("")}${overlayRow}</ul>`,
  };
}

function niceMaximum(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  return ([1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((step) => normalized <= step) ?? 10) * magnitude;
}

function showDayLabel(index: number, count: number): boolean {
  if (count <= 10) return true;
  if (index === 0 || index === count - 1) return true;
  return count <= 31 ? index % 5 === 0 : index % Math.ceil(count / 7) === 0;
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
  const maximum = niceMaximum(Math.max(...buckets.map((bucket) => stacked
    ? usable.reduce((sum, item) => sum + (bucket.values.get(item.id) ?? 0), 0)
    : Math.max(...usable.map((item) => bucket.values.get(item.id) ?? 0), 0)), 1));
  const barLegend = usable.length > 1
    ? usable.map((item) => `<span><i class="chart-dot" style="background:${item.color}"></i>${escapeHtml(item.label)}</span>`).join("")
    : "";
  const overlay = options.overlay && buckets.some((bucket) => (options.overlay?.values.get(bucket.epoch) ?? 0) > 0) ? options.overlay : undefined;
  const overlayMaximum = overlay ? niceMaximum(Math.max(...buckets.map((bucket) => overlay.values.get(bucket.epoch) ?? 0), 1)) : 0;
  const overlayPoints = overlay ? buckets.map((bucket, index) => {
    const x = (index + .5) / Math.max(1, buckets.length) * 100;
    const y = 100 - ((overlay.values.get(bucket.epoch) ?? 0) / overlayMaximum) * 100;
    return `${x},${Math.max(0, Math.min(100, y))}`;
  }).join(" ") : "";
  const overlayLegend = overlay ? `<span class="daily-bars__overlay-legend"><i style="background:${overlay.color}"></i>${escapeHtml(overlay.label)}</span>` : "";
  const bucketCount = Math.max(1, buckets.length);
  return `<article class="detail-panel daily-history-chart"><div class="detail-panel__heading"><h3>${escapeHtml(options.title)}</h3><span>${escapeHtml(options.detail)}</span></div>${barLegend || overlayLegend ? `<div class="daily-bars__legend">${barLegend}${overlayLegend}</div>` : ""}<div class="daily-bars-frame" style="--daily-bucket-count:${bucketCount}"><span class="daily-bars__axis">${escapeHtml(options.formatValue(maximum))}</span>${overlay ? `<span class="daily-bars__overlay-axis">${escapeHtml(overlay.formatValue(overlayMaximum))}</span>` : ""}<div class="daily-bars__plot"><div class="daily-bars" role="img" aria-label="${escapeHtml(options.ariaLabel)}">${buckets.map((bucket) => {
    const hint = tooltip(bucket, usable, options.formatValue, overlay);
    const empty = usable.every((item) => !(bucket.values.get(item.id) ?? 0));
    return `<div class="daily-bar-day${empty ? " daily-bar-day--empty" : ""}"><button type="button" class="daily-bar-day__trigger" aria-label="${escapeHtml(hint.label)}"><span class="daily-bar-day__bars${stacked ? " daily-bar-day__bars--stacked" : ""}">${usable.map((item) => {
      const value = bucket.values.get(item.id) ?? 0;
      const height = value > 0 ? Math.max(0.2, (value / maximum) * 100) : 0;
      return `<i style="--daily-bar-color:${item.color};height:${height}%"></i>`;
    }).join("")}</span></button><div class="daily-bar-tooltip" aria-hidden="true">${hint.markup}</div></div>`;
  }).join("")}</div>${overlay ? `<svg class="daily-bars__overlay" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><polyline points="${overlayPoints}" style="stroke:${overlay.color}"/></svg>` : ""}</div><div class="daily-bars__labels" aria-hidden="true">${buckets.map((bucket, index) => `<span title="${escapeHtml(dayLabel(bucket.epoch))}">${showDayLabel(index, buckets.length) ? escapeHtml(dayLabel(bucket.epoch)) : ""}</span>`).join("")}</div></div><p class="panel-footnote">${escapeHtml(options.footnote)}</p></article>`;
}
