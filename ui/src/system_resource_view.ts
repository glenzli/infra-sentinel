import "./system_resource_view.css";
import { ResourceProjection, SystemResourceProjection } from "./bridge";
import { formatBytes, number } from "./format";
import { tr } from "./i18n";
import { SystemAnalysisSnapshot, SystemTimeRange } from "./system_resource_analysis";

const COLORS = { cpu: "#3178dc", read: "#2f9461", write: "#9468c9" };

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[char] ?? char);
}

function percent(value: unknown): string {
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(number(value))}%`;
}

function pressure(value: string): string {
  return ({ normal: tr("Normal", "正常"), warning: tr("Elevated", "升高"), critical: tr("Critical", "严重"), unavailable: tr("Unavailable", "不可用") } as Record<string, string>)[value] ?? value;
}

function thermal(value: string): string {
  return ({ nominal: tr("Nominal", "正常"), fair: tr("Fair", "温和"), serious: tr("Serious", "较高"), critical: tr("Critical", "严重"), unavailable: tr("Unavailable", "不可用") } as Record<string, string>)[value] ?? value;
}

function diskHealth(value: string): string {
  return ({ healthy: tr("Healthy", "健康"), warning: tr("Attention", "需关注"), critical: tr("Critical", "严重"), unknown: tr("Unknown", "未知") } as Record<string, string>)[value] ?? value;
}

function overallStatus(value: string): string {
  return ({ healthy: tr("Normal", "正常"), warning: tr("Attention", "需关注"), critical: tr("Critical", "严重"), degraded: tr("Limited", "受限") } as Record<string, string>)[value] ?? value;
}

function count(value: number | null | undefined): string {
  return value == null ? "—" : new Intl.NumberFormat().format(value);
}

function combinedCount(left: number | null | undefined, right: number | null | undefined): string {
  return left == null && right == null ? "—" : count((left ?? 0) + (right ?? 0));
}

function supports(snapshot: SystemResourceProjection | undefined, capability: string): boolean {
  return snapshot?.capabilities?.includes(capability) ?? false;
}

function platformLabel(platform: string | undefined): string {
  if (platform === "macos") return tr("This Mac", "本机 Mac");
  if (platform === "windows") return tr("This Windows PC", "本机 Windows PC");
  if (platform === "linux") return tr("This Linux host", "本机 Linux 主机");
  return tr("This host", "本机系统");
}

export function renderSystemResourceCard(resource: ResourceProjection, snapshot?: SystemResourceProjection): string {
  const cpu = snapshot?.cpu?.percent ?? 0;
  const memory = snapshot?.memory;
  const disk = snapshot?.disk;
  const metrics = [
    supports(snapshot, "cpu.utilization") ? `<span><small>CPU</small><strong>${percent(cpu)}</strong></span>` : "",
    supports(snapshot, "memory.pressure")
      ? `<span><small>${tr("Memory pressure", "内存压力")}</small><strong>${escapeHtml(pressure(memory?.pressure ?? "unavailable"))}</strong></span>`
      : supports(snapshot, "memory.capacity") ? `<span><small>${tr("Memory available", "可用内存")}</small><strong>${formatBytes(memory?.available_bytes)}</strong></span>` : "",
    supports(snapshot, "disk.throughput") ? `<span><small>${tr("Disk read", "磁盘读取")}</small><strong>${formatBytes(disk?.read_bytes_per_second)}/s</strong></span>` : "",
    supports(snapshot, "disk.throughput") ? `<span><small>${tr("Disk write", "磁盘写入")}</small><strong>${formatBytes(disk?.write_bytes_per_second)}/s</strong></span>` : "",
    !supports(snapshot, "disk.throughput") && supports(snapshot, "disk.capacity") ? `<span><small>${tr("Disk available", "磁盘可用")}</small><strong>${formatBytes(disk?.free_bytes)}</strong></span>` : "",
  ].filter(Boolean).join("");
  const footerFacts = [
    supports(snapshot, "thermal.pressure") ? `${tr("Thermal", "温度压力")} ${escapeHtml(thermal(snapshot?.thermal?.state ?? "unavailable"))}` : "",
    supports(snapshot, "disk.health") ? `${tr("Disk", "磁盘")} ${escapeHtml(diskHealth(disk?.health?.state ?? "unknown"))}` : "",
  ].filter(Boolean).join(" · ") || tr("Platform capabilities detected", "已按平台能力检测");
  return `<button class="resource-card resource-card--system resource-card--${escapeHtml(resource.status)}" type="button" data-resource-id="system"><div class="resource-card__heading"><span class="resource-card__identity"><span class="resource-card__state source-state source-state--${escapeHtml(resource.status)}" aria-hidden="true"></span><p>${escapeHtml(platformLabel(snapshot?.platform))}</p></span><span class="pill pill--${escapeHtml(resource.status)}">${escapeHtml(overallStatus(resource.status))}</span></div><div class="system-overview__metrics">${metrics}</div><div class="network-overview__footer system-overview__footer"><span>${footerFacts}</span><span>${tr("Details", "详情")} →</span></div></button>`;
}

function metric(points: Record<string, unknown>[], name: string): Record<string, unknown>[] {
  return points.filter((point) => point.metric === name).sort((left, right) => number(left.observed_epoch) - number(right.observed_epoch));
}

function sum(points: Record<string, unknown>[]): number {
  return points.reduce((total, point) => total + number(point.value), 0);
}

function max(points: Record<string, unknown>[]): number {
  return points.reduce((value, point) => Math.max(value, number(point.value)), 0);
}

function min(points: Record<string, unknown>[]): number {
  return points.reduce((value, point) => Math.min(value, number(point.value)), Number.POSITIVE_INFINITY);
}

type ChartSeries = { label: string; color: string; values: Array<{ epoch: number; value: number }> };

function niceMaximum(value: number): number {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const scaled = value / power;
  const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return nice * power;
}

function niceBinaryMaximum(value: number): number {
  const units = [1, 1024, 1024 ** 2, 1024 ** 3, 1024 ** 4];
  let unitIndex = 0;
  for (let index = 1; index < units.length && value >= units[index]; index += 1) unitIndex = index;
  let rounded = niceMaximum(value / units[unitIndex]);
  if (rounded >= 1024 && unitIndex < units.length - 1) {
    unitIndex += 1;
    rounded = niceMaximum(value / units[unitIndex]);
  }
  return rounded * units[unitIndex];
}

function lineChart(
  series: ChartSeries[],
  sinceEpoch: number,
  untilEpoch: number,
  formatAxis: (value: number) => string,
  maximum: (value: number) => number = niceMaximum,
): string {
  const available = series.filter((item) => item.values.length);
  if (!available.length) return `<div class="system-chart__empty">${tr("History begins after the first five-minute rollup.", "首个 5 分钟汇总完成后开始显示历史。")}</div>`;
  const ceiling = maximum(Math.max(...available.flatMap((item) => item.values.map((point) => point.value)), 1));
  const span = Math.max(1, untilEpoch - sinceEpoch);
  const paths = available.map((item) => {
    const path = item.values.map((point, index) => {
      const x = Math.max(0, Math.min(100, (point.epoch - sinceEpoch) / span * 100));
      const y = 91 - Math.max(0, Math.min(1, point.value / ceiling)) * 82;
      return `${index ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
    }).join(" ");
    return `<path d="${path}" fill="none" stroke="${item.color}" stroke-width="1.7" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>`;
  }).join("");
  return `<div class="system-chart"><div class="system-chart__axis"><span>${escapeHtml(formatAxis(ceiling))}</span><span>${escapeHtml(formatAxis(ceiling / 2))}</span><span>${escapeHtml(formatAxis(0))}</span></div><svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img"><path d="M0 9H100 M0 50H100 M0 91H100" class="system-chart__grid"/>${paths}</svg><div class="system-chart__timeline"><span>${new Date(sinceEpoch * 1_000).toLocaleString()}</span><span>${tr("Now", "现在")}</span></div><div class="system-chart__legend">${available.map((item) => `<span><i style="background:${item.color}"></i>${escapeHtml(item.label)}</span>`).join("")}</div></div>`;
}

function rangeLabel(range: SystemTimeRange): string {
  return ({ "1h": tr("Last hour", "近 1 小时"), "24h": tr("Last 24 hours", "近 24 小时"), "7d": tr("Last 7 days", "近 7 天"), "30d": tr("Last 30 days", "近 30 天") })[range];
}

function controls(snapshot: SystemAnalysisSnapshot): string {
  const ranges: Array<[SystemTimeRange, string]> = [["1h", tr("1 hour", "1 小时")], ["24h", tr("24 hours", "24 小时")], ["7d", tr("7 days", "7 天")], ["30d", tr("30 days", "30 天")]];
  return `<section class="system-analysis-toolbar"><span>${tr("Time range", "时间范围")}</span><div>${ranges.map(([range, label]) => `<button type="button" class="system-range${snapshot.range === range ? " is-active" : ""}" data-system-range="${range}">${label}</button>`).join("")}</div></section>`;
}

export function renderSystemResourcePage(
  resource: ResourceProjection,
  snapshot: SystemResourceProjection | undefined,
  analysis: SystemAnalysisSnapshot,
): string {
  const memory = snapshot?.memory;
  const disk = snapshot?.disk;
  const hasCpu = supports(snapshot, "cpu.utilization");
  const hasMemoryCapacity = supports(snapshot, "memory.capacity");
  const hasMemoryPressure = supports(snapshot, "memory.pressure");
  const hasSwap = supports(snapshot, "memory.swap");
  const hasDiskCapacity = supports(snapshot, "disk.capacity");
  const hasDiskThroughput = supports(snapshot, "disk.throughput");
  const hasDiskHealth = supports(snapshot, "disk.health");
  const hasThermal = supports(snapshot, "thermal.pressure");
  const cpuPoints = metric(analysis.data.gaugePoints, "system.cpu.percent");
  const readPoints = metric(analysis.data.counterPoints, "system.disk.read_bytes");
  const writePoints = metric(analysis.data.counterPoints, "system.disk.write_bytes");
  const rateSeries = (points: Record<string, unknown>[]) => points.map((point) => ({ epoch: number(point.observed_epoch), value: number(point.value) / Math.max(1, analysis.data.bucketSeconds) }));
  const freePoints = metric(analysis.data.gaugePoints, "system.disk.free_bytes");
  const swapPoints = metric(analysis.data.gaugePoints, "system.memory.swap_used_bytes");
  const currentCards = [
    hasCpu ? `<article class="network-card network-card--blue"><p>CPU</p><strong>${percent(snapshot?.cpu?.percent)}</strong><small>${tr("current host utilization", "当前整机使用率")}</small></article>` : "",
    hasMemoryPressure ? `<article class="network-card network-card--purple"><p>${tr("Memory pressure", "内存压力")}</p><strong>${escapeHtml(pressure(memory?.pressure ?? "unavailable"))}</strong><small>${formatBytes(memory?.available_bytes)} ${tr("available", "可用")} · ${formatBytes(memory?.compressed_bytes)} ${tr("compressed", "压缩")}</small></article>`
      : hasMemoryCapacity ? `<article class="network-card network-card--purple"><p>${tr("Memory available", "可用内存")}</p><strong>${formatBytes(memory?.available_bytes)}</strong><small>${formatBytes(memory?.total_bytes)} ${tr("physical memory", "物理内存")}</small></article>` : "",
    hasDiskHealth ? `<article class="network-card network-card--orange"><p>${tr("Disk health", "磁盘健康")}</p><strong>${escapeHtml(diskHealth(disk?.health?.state ?? "unknown"))}</strong><small>${tr("native check every 6 hours", "原生状态每 6 小时读取")}</small></article>`
      : hasDiskCapacity ? `<article class="network-card network-card--orange"><p>${tr("Disk available", "磁盘可用")}</p><strong>${formatBytes(disk?.free_bytes)}</strong><small>${percent(disk?.used_percent)} ${tr("used", "已使用")}</small></article>` : "",
    hasSwap ? `<article class="network-card"><p>Swap</p><strong>${formatBytes(memory?.swap_used_bytes)}</strong><small>↑ ${formatBytes(memory?.swapout_bytes_per_second)}/s · ↓ ${formatBytes(memory?.swapin_bytes_per_second)}/s</small></article>` : "",
  ].filter(Boolean).join("");
  const rangeSummary = [
    hasDiskThroughput ? `<span><small>${tr("Physical reads", "物理读取")}</small><strong>${formatBytes(sum(readPoints))}</strong></span>` : "",
    hasDiskThroughput ? `<span><small>${tr("Physical writes", "物理写入")}</small><strong>${formatBytes(sum(writePoints))}</strong></span>` : "",
    hasCpu ? `<span><small>${tr("Peak CPU", "CPU 峰值")}</small><strong>${percent(max(cpuPoints))}</strong></span>` : "",
    hasDiskCapacity ? `<span><small>${tr("Minimum free disk", "最低磁盘可用")}</small><strong>${formatBytes(Number.isFinite(min(freePoints)) ? min(freePoints) : disk?.free_bytes)}</strong></span>` : "",
    hasSwap ? `<span><small>${tr("Peak swap", "Swap 峰值")}</small><strong>${formatBytes(max(swapPoints) || memory?.swap_used_bytes)}</strong></span>` : "",
  ].filter(Boolean).join("");
  const historyPanels = [
    hasCpu ? `<article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("CPU utilization", "CPU 使用率")}</h3><span>${rangeLabel(analysis.range)}</span></div>${lineChart([{ label: "CPU", color: COLORS.cpu, values: cpuPoints.map((point) => ({ epoch: number(point.observed_epoch), value: number(point.value) })) }], analysis.data.sinceEpoch, analysis.data.untilEpoch, percent)}</article>` : "",
    hasDiskThroughput ? `<article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Physical disk throughput", "物理磁盘吞吐")}</h3><span>${tr("bytes / second", "字节 / 秒")}</span></div>${lineChart([{ label: tr("Read", "读取"), color: COLORS.read, values: rateSeries(readPoints) }, { label: tr("Write", "写入"), color: COLORS.write, values: rateSeries(writePoints) }], analysis.data.sinceEpoch, analysis.data.untilEpoch, (value) => `${formatBytes(value)}/s`, niceBinaryMaximum)}</article>` : "",
  ].filter(Boolean).join("");
  const historyState = analysis.error
    ? `<article class="detail-panel system-state system-state--error">${tr("Historical metrics are temporarily unavailable.", "历史指标暂时不可用。")} ${escapeHtml(analysis.error)}</article>`
    : analysis.loading && !analysis.ready
      ? `<article class="detail-panel system-state"><span class="pulse"></span>${tr("Loading system history", "正在读取系统历史")}</article>`
      : `<section class="system-history-grid">${historyPanels}</section>`;
  const currentIoPanel = hasDiskThroughput ? `<article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("Current I/O", "当前 I/O")}</h3><span>${disk?.physical_io_available ? tr("physical devices", "物理设备") : tr("partial", "不完整")}</span></div><ul class="traffic-list"><li><span>${tr("Read", "读取")}</span><strong>${formatBytes(disk?.read_bytes_per_second)}/s · ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(number(disk?.read_iops))} IOPS</strong></li><li><span>${tr("Write", "写入")}</span><strong>${formatBytes(disk?.write_bytes_per_second)}/s · ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(number(disk?.write_iops))} IOPS</strong></li>${hasDiskHealth ? `<li><span>${tr("Errors / retries since boot", "本次启动以来错误 / 重试")}</span><strong>${combinedCount(disk?.health?.read_errors, disk?.health?.write_errors)} / ${combinedCount(disk?.health?.read_retries, disk?.health?.write_retries)}</strong></li>` : ""}</ul></article>` : "";
  const pressureItems = [
    hasMemoryPressure ? `<li><span>${tr("Memory pressure", "内存压力")}</span><strong>${escapeHtml(pressure(memory?.pressure ?? "unavailable"))}</strong></li>` : "",
    hasThermal ? `<li><span>${tr("Thermal state", "温度压力")}</span><strong>${escapeHtml(thermal(snapshot?.thermal?.state ?? "unavailable"))}</strong></li>` : "",
    hasDiskHealth ? `<li><span>${tr("Disk health", "磁盘健康")}</span><strong>${escapeHtml(diskHealth(disk?.health?.state ?? "unknown"))}</strong></li>` : "",
  ].filter(Boolean).join("");
  const pressurePanel = pressureItems ? `<article class="detail-panel"><div class="detail-panel__heading"><h3>${tr("System pressure", "系统压力")}</h3><span>${escapeHtml(platformLabel(snapshot?.platform))}</span></div><ul class="traffic-list">${pressureItems}</ul></article>` : "";
  const secondary = currentIoPanel || pressurePanel ? `<section class="system-secondary-grid">${currentIoPanel}${pressurePanel}</section>` : "";
  return `<section class="resource-section resource-section--detail"><div class="section-heading"><div><p class="eyebrow">${tr("LOCAL SYSTEM", "本机系统")}</p><h2>${escapeHtml(platformLabel(snapshot?.platform))}</h2></div><span class="pill pill--${escapeHtml(resource.status)}">${escapeHtml(overallStatus(resource.status))}</span></div><div class="system-current-grid">${currentCards}</div>${controls(analysis)}<section class="system-range-summary">${rangeSummary}</section>${historyState}${secondary}<p class="system-privacy">${tr("Host-wide aggregate counters only. No file names, paths, process arguments, window titles, or user content are recorded. History is persisted every five minutes; supported disk health checks run every six hours.", "仅记录整机聚合计数；不记录文件名、路径、进程参数、窗口标题或用户内容。历史每 5 分钟落盘；平台支持时，磁盘健康每 6 小时读取一次。")}</p></section>`;
}
