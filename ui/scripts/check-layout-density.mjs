import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import ts from "typescript";

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const context = vm.createContext({
  window: { localStorage: { getItem: () => "en", setItem: () => {} } },
  navigator: { language: "en-US" }, document: { documentElement: {} },
  Date, Intl, Map, Set, WeakSet, Error, Number, String, Array, Boolean, Object, Promise,
});
const modules = new Map();
function loadModule(name) {
  if (name.endsWith(".css")) return {};
  if (name === "./agent_client") return { requestAgentCommand: () => { throw new Error("Unexpected native command"); } };
  if (modules.has(name)) return modules.get(name);
  const filename = join(uiRoot, "src", `${name.replace(/^\.\//, "")}.ts`);
  const output = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  modules.set(name, module.exports);
  new vm.Script(`(function(require, module, exports) {${output}\n})`, { filename })
    .runInContext(context)(loadModule, module, module.exports);
  modules.set(name, module.exports);
  return module.exports;
}
const { renderAttentionDiagnostics, bindAttentionDiagnostics } = loadModule("./attention_diagnostics");
const { renderDiagnosticsOverview } = loadModule("./diagnostics_view");
const { renderSystemResourcePage } = loadModule("./system_resource_view");
const { setLocale } = loadModule("./i18n");
const diagnostic = (id, level = "warning") => ({
  id, level, subject: "Collector", title: "Collection failed", current: "Unavailable",
  basis: "Three failed attempts", action: "Check the connection",
});
const opening = (item) => renderAttentionDiagnostics([item]).match(/<details\b[^>]*>/)[0];
function toggle(item, open) {
  const listeners = [];
  const element = {
    dataset: { attentionId: `${item.level}:${item.id}` }, open,
    addEventListener: (type, listener) => { assert.equal(type, "toggle"); listeners.push(listener); },
  };
  const root = { querySelectorAll: () => [element] };
  bindAttentionDiagnostics(root);
  bindAttentionDiagnostics(root);
  assert.equal(listeners.length, 1, "refresh binding must be idempotent");
  listeners[0]();
}

for (const locale of ["en", "zh"]) {
  setLocale(locale);
  for (const level of ["info", "degraded", "warning", "critical"]) {
    const item = diagnostic(`${locale}-${level}`, level);
    assert.equal(/ open>/.test(opening(item)), level === "critical", "critical evidence starts visible");
    toggle(item, level !== "critical");
    assert.equal(/ open>/.test(opening(item)), level !== "critical", "user choice survives a fresh render");
  }
  const escalating = diagnostic(`${locale}-escalation`);
  toggle(escalating, false);
  assert(/ open>/.test(opening({ ...escalating, level: "critical" })), "escalation must reveal critical evidence");
  const unsafe = { ...diagnostic(`unsafe-${locale}`), subject: '<img src=x onerror="bad()">',
    title: "<script>bad()</script>", current: "<input>", basis: "<iframe>", action: "<svg>" };
  const html = renderAttentionDiagnostics([unsafe]);
  for (const tag of ["img", "script", "input", "iframe", "svg"]) assert(!html.includes(`<${tag}`));
  assert(html.includes("&lt;script&gt;"), "compact headers still escape source text");
  assert.equal(renderAttentionDiagnostics([]), "");
  const unknown = renderDiagnosticsOverview(undefined, true);
  assert(unknown.includes(locale === "zh" ? "暂不可用" : "Unavailable"));
  assert(!unknown.includes("0 unresolved") && !unknown.includes("0 项待确认"), "unknown must not appear healthy");
  const active = renderDiagnosticsOverview({ available: true, active_count: 6, active: [], freshness: "current" }, true);
  assert(active.includes("data-diagnostics-open"));
  assert(active.includes(locale === "zh" ? "6 项待确认异常" : "6 unresolved"), "history count stays distinct from confirmed alerts");
  for (const level of ["warning", "critical"]) {
    const html = renderSystemResourcePage({ status: level }, {
      platform: "macos", status: level, capabilities: [], reasons: [`memory_pressure_${level}`],
      memory: { pressure: level }, disk: { total_bytes: 100, free_bytes: 50 }, thermal: { state: "nominal" },
    }, { range: "1h", loading: false, data: { gaugePoints: [], counterPoints: [], bucketSeconds: 900 } });
    const disclosure = html.match(/<details\b[^>]*data-attention-id="[^\"]*system:memory_pressure[^\"]*"[^>]*>/)?.[0];
    assert(disclosure, "native system conditions share the refresh-safe disclosure");
    assert.equal(/ open>/.test(disclosure), level === "critical");
    assert(html.includes(locale === "zh" ? "操作系统" : "operating system"), "native trigger evidence remains available");
  }
}
console.log("Layout behavior: bilingual disclosure defaults, refresh retention, severity escalation, safe content, idempotent binding, and distinct diagnostic counts passed.");
