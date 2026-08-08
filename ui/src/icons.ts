type IconName = "settings" | "refresh" | "reset" | "arrow-left" | "plus";

const paths: Record<IconName, string> = {
  settings: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm0-12.5v2m0 14v2m9-9h-2M5 12H3m15.36-6.36-1.42 1.42M6.06 17.94l-1.42 1.42m0-13.72 1.42 1.42m11.3 10.88 1.42 1.42",
  refresh: "M20 11a8 8 0 0 0-14.7-4.3L3 9m0-5v5h5m-4 4a8 8 0 0 0 14.7 4.3L21 15m0 5v-5h-5",
  reset: "M4 4v6h6M20 20v-6h-6M5.2 15A7 7 0 0 0 18.5 17M18.8 9A7 7 0 0 0 5.5 7",
  "arrow-left": "M19 12H5m6-6-6 6 6 6",
  plus: "M12 5v14M5 12h14",
};

export function icon(name: IconName, label = ""): string {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="${label ? "false" : "true"}"><path d="${paths[name]}"/></svg>`;
}
