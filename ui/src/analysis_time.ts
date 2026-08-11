export type AnalysisTimeRange = "today" | "7d" | "30d" | "recorded";

export type AnalysisTimeWindow = {
  sinceEpoch: number;
  untilEpoch: number;
  bucketSeconds: 900 | 86_400;
  bucketOffsetSeconds: number;
};

const DAY_SECONDS = 86_400;

function localMidnight(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function shiftLocalDays(date: Date, days: number): Date {
  const shifted = new Date(date);
  shifted.setDate(shifted.getDate() + days);
  return shifted;
}

/** Owns the calendar meaning shared by network and AI analysis queries. */
export function analysisTimeWindow(
  range: AnalysisTimeRange,
  todayBucketSeconds: 900 | 86_400 = 900,
  now = new Date(),
): AnalysisTimeWindow {
  const midnight = localMidnight(now);
  const untilEpoch = now.getTime() / 1_000;
  const dayStartEpoch = midnight.getTime() / 1_000;
  const daysBack = range === "7d" ? 6 : range === "30d" ? 29 : range === "recorded" ? 729 : 0;
  const sinceEpoch = shiftLocalDays(midnight, -daysBack).getTime() / 1_000;
  const bucketSeconds = range === "today" ? todayBucketSeconds : DAY_SECONDS;
  const bucketOffsetSeconds = bucketSeconds === DAY_SECONDS
    ? ((dayStartEpoch % DAY_SECONDS) + DAY_SECONDS) % DAY_SECONDS
    : 0;
  return { sinceEpoch, untilEpoch, bucketSeconds, bucketOffsetSeconds };
}

export function localDayEpoch(epoch: number): number {
  const date = new Date(epoch * 1_000);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime() / 1_000;
}

export function nextLocalDayEpoch(epoch: number): number {
  const date = new Date(epoch * 1_000);
  date.setDate(date.getDate() + 1);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime() / 1_000;
}
