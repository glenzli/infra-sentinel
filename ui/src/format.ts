import { tr } from "./i18n";

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function asArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

export function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
}

export function formatBytes(value: unknown): string {
  const amount = number(value);
  if (amount <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const exponent = Math.min(Math.floor(Math.log(amount) / Math.log(1024)), units.length - 1);
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(amount / 1024 ** exponent)} ${units[exponent]}`;
}

export function formatDuration(value: unknown): string {
  const seconds = Math.floor(number(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return tr(`${hours} h ${minutes} min`, `${hours} 小时 ${minutes} 分`);
  return tr(`${minutes} min`, `${minutes} 分钟`);
}
