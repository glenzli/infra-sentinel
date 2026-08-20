import "./daily_activity_calendar.css";
import { localDayEpoch, nextLocalDayEpoch } from "./analysis_time";
import { tr } from "./i18n";

export type DailyActivitySeries = {
  id: string;
  label: string;
  color: string;
};

export type DailyActivityBucket = {
  epoch: number;
  values: Map<string, number>;
};

export type DailyActivityCalendarOptions = {
  title: string;
  detail: string;
  ariaLabel: string;
  footnote: string;
  formatValue: (value: number) => string;
  endEpoch: number;
};

const WEEKS = 53;
const DAYS_PER_WEEK = 7;

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function dayLabel(epoch: number): string {
  const date = new Date(epoch * 1_000);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function mondayEpoch(epoch: number): number {
  const date = new Date(localDayEpoch(epoch) * 1_000);
  const shift = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - shift);
  return localDayEpoch(date.getTime() / 1_000);
}

function shiftDays(epoch: number, days: number): number {
  let shifted = epoch;
  const step = days < 0 ? -1 : 1;
  for (let index = 0; index < Math.abs(days); index += 1) {
    const date = new Date(shifted * 1_000);
    date.setDate(date.getDate() + step);
    shifted = localDayEpoch(date.getTime() / 1_000);
  }
  return shifted;
}

function activityLevel(total: number, thresholds: number[]): number {
  if (total <= 0) return 0;
  if (!thresholds.length) return 1;
  return 1 + thresholds.filter((threshold) => total >= threshold).length;
}

function quantileThresholds(totals: number[]): number[] {
  const sorted = totals.filter((value) => value > 0).sort((left, right) => left - right);
  if (!sorted.length) return [];
  return [0.2, 0.4, 0.6, 0.8].map((fraction) => sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))]);
}

function tooltip(
  epoch: number,
  values: Map<string, number>,
  series: DailyActivitySeries[],
  formatValue: (value: number) => string,
): { label: string; markup: string } {
  const visible = series.filter((item) => (values.get(item.id) ?? 0) > 0);
  const total = visible.reduce((sum, item) => sum + (values.get(item.id) ?? 0), 0);
  const heading = `${dayLabel(epoch)} · ${tr("shown total", "图示合计")} ${formatValue(total)}`;
  return {
    label: [heading, ...visible.map((item) => `${item.label} ${formatValue(values.get(item.id) ?? 0)}`)].join(" · "),
    markup: `<strong>${escapeHtml(heading)}</strong><ul>${visible.map((item) => `<li><span><i style="background:${item.color}"></i>${escapeHtml(item.label)}</span><b>${escapeHtml(formatValue(values.get(item.id) ?? 0))}</b></li>`).join("")}</ul>`,
  };
}

/**
 * Renders a bounded, GitHub-style activity calendar for recorded daily totals.
 * Colors express relative activity inside the displayed year; exact totals and
 * source/model composition remain available in the per-day tooltip.
 */
export function renderDailyActivityCalendar(
  series: DailyActivitySeries[],
  buckets: DailyActivityBucket[],
  options: DailyActivityCalendarOptions,
): string {
  const usable = series.filter((item) => buckets.some((bucket) => (bucket.values.get(item.id) ?? 0) > 0));
  const valuesByDay = new Map<number, Map<string, number>>();
  for (const bucket of buckets) valuesByDay.set(localDayEpoch(bucket.epoch), bucket.values);
  const end = localDayEpoch(options.endEpoch);
  const start = shiftDays(mondayEpoch(end), -(WEEKS - 1) * DAYS_PER_WEEK);
  const days: number[] = [];
  let epoch = start;
  for (let index = 0; index < WEEKS * DAYS_PER_WEEK; index += 1) {
    days.push(epoch);
    epoch = nextLocalDayEpoch(epoch);
  }
  const totals = days.map((day) => usable.reduce((sum, item) => sum + (valuesByDay.get(day)?.get(item.id) ?? 0), 0));
  const thresholds = quantileThresholds(totals);
  const monthLabels = Array.from({ length: WEEKS }, (_, week) => {
    const weekStart = days[week * DAYS_PER_WEEK];
    const date = new Date(weekStart * 1_000);
    const previous = week > 0 ? new Date(days[(week - 1) * DAYS_PER_WEEK] * 1_000) : undefined;
    const label = week === 0 || previous?.getMonth() !== date.getMonth()
      ? date.toLocaleDateString(undefined, { month: "short" })
      : "";
    return `<span>${escapeHtml(label)}</span>`;
  }).join("");
  const cells = days.map((day) => {
    const future = day > end;
    const values = valuesByDay.get(day) ?? new Map<string, number>();
    const total = usable.reduce((sum, item) => sum + (values.get(item.id) ?? 0), 0);
    if (future) return `<span class="daily-activity-day daily-activity-day--future" aria-hidden="true"></span>`;
    if (total <= 0) return `<span class="daily-activity-day daily-activity-day--empty" title="${escapeHtml(`${dayLabel(day)} · ${tr("no recorded usage", "无已记录用量")}`)}"></span>`;
    const hint = tooltip(day, values, usable, options.formatValue);
    return `<span class="daily-activity-day daily-activity-day--level-${activityLevel(total, thresholds)}"><button type="button" aria-label="${escapeHtml(hint.label)}"><span class="daily-activity-tooltip" aria-hidden="true">${hint.markup}</span></button></span>`;
  }).join("");
  const weekdayLabels = [tr("Mon", "一"), "", tr("Wed", "三"), "", tr("Fri", "五"), "", ""].map((label) => `<span>${escapeHtml(label)}</span>`).join("");
  return `<article class="detail-panel daily-activity-calendar"><div class="detail-panel__heading"><h3>${escapeHtml(options.title)}</h3><span>${escapeHtml(options.detail)}</span></div><div class="daily-activity-calendar__body"><div class="daily-activity-calendar__weekdays" aria-hidden="true">${weekdayLabels}</div><div class="daily-activity-calendar__scroll"><div class="daily-activity-calendar__months" style="--activity-week-count:${WEEKS}">${monthLabels}</div><div class="daily-activity-calendar__grid" role="img" aria-label="${escapeHtml(options.ariaLabel)}" style="--activity-week-count:${WEEKS}">${cells}</div></div></div><div class="daily-activity-calendar__legend" aria-hidden="true"><span>${tr("Less", "较少")}</span><i data-level="0"></i><i data-level="1"></i><i data-level="2"></i><i data-level="3"></i><i data-level="4"></i><i data-level="5"></i><span>${tr("More", "较多")}</span></div><p class="panel-footnote">${escapeHtml(options.footnote)}</p></article>`;
}
