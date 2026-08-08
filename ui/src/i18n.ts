export type UiLocale = "en" | "zh";

const STORAGE_KEY = "infra-sentinel.ui-locale";
let locale: UiLocale = (() => {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "en" || stored === "zh") return stored;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
})();

export function currentLocale(): UiLocale {
  return locale;
}

export function setLocale(next: UiLocale): void {
  locale = next;
  window.localStorage.setItem(STORAGE_KEY, next);
  document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
}

/** Select exactly one localized string; never render bilingual UI copy. */
export function tr(english: string, chinese: string): string {
  return locale === "zh" ? chinese : english;
}

export function languagePicker(): string {
  return `<label class="language-picker" title="${tr("Language", "语言")}">
    <span aria-hidden="true">◎</span>
    <select id="language" aria-label="${tr("Language", "语言")}">
      <option value="en" ${locale === "en" ? "selected" : ""}>English</option>
      <option value="zh" ${locale === "zh" ? "selected" : ""}>中文</option>
    </select>
  </label>`;
}

export function bindLanguagePicker(root: ParentNode, changed: () => void): void {
  root.querySelector<HTMLSelectElement>("#language")?.addEventListener("change", (event) => {
    setLocale((event.currentTarget as HTMLSelectElement).value === "zh" ? "zh" : "en");
    changed();
  });
}

/** Temporary compatibility for migrated copy: selects one side of `en / 中文`. */
export function localizeInlinePairs(root: ParentNode): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  for (const node of nodes) {
    const parts = node.data.split(" / ");
    if (parts.length === 2) node.data = locale === "zh" ? parts[1] : parts[0];
  }
}
