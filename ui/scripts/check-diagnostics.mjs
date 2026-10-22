import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import ts from "typescript";

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
let now = Date.parse("2026-10-22T12:00:00Z");
const storage = new Map();
const window = {
  localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
};
const context = vm.createContext({
  window,
  navigator: { language: "en-US" },
  document: { documentElement: {} },
  Date: class extends Date { static now() { return now; } },
  Intl, Map, Set, Error, Number, String, Array, Boolean, Object, Promise,
});
const modules = new Map();

function loadModule(name) {
  if (name.endsWith(".css")) return {};
  if (name === "./agent_client") return { requestAgentCommand: () => { throw new Error("Unexpected native command"); } };
  if (modules.has(name)) return modules.get(name);
  const filename = join(uiRoot, "src", `${name.replace(/^\.\//, "")}.ts`);
  const source = readFileSync(filename, "utf8");
  const output = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  modules.set(name, module.exports);
  const execute = new vm.Script(`(function(require, module, exports) {${output}\n})`, { filename }).runInContext(context);
  execute(loadModule, module, module.exports);
  modules.set(name, module.exports);
  return module.exports;
}

const { DiagnosticsAnalysisController } = loadModule("./diagnostics_analysis");
const { renderDiagnosticsPage, renderDiagnosticsOverview } = loadModule("./diagnostics_view");
const { setLocale } = loadModule("./i18n");
const iso = (offset = 0) => new Date(now + offset).toISOString();
const event = (id, category = "system", fields = {}) => ({
  id: `event:${id}`, diagnostic_id: "diagnostic:fixture", category,
  source_id: "local-system", source_label: "Local system", code: "system.fixture",
  severity: "warning", observed_at: iso(-60_000), started_at: iso(-3_600_000),
  title: "Memory pressure", title_zh: "内存压力", detail: "Pressure was observed.", detail_zh: "已观测到内存压力。",
  observation_status: "observed", transition: "raised", ...fields,
});
const projection = (events = [event(1)], fields = {}) => ({
  schema: "20261022.1", available: true, generated_at: iso(), observed_at: iso(), history_started_at: iso(-86_400_000),
  retention_days: 30, active_count: 1, active: [event(1)], events, truncated: false, history_truncated: false, sample_truncated: false,
  coverage: [{ id: "diagnostic:fixture", category: "system", source_id: "local-system", source_label: "Local system",
    code: "system.fixture", observation_status: "observed", observed_at: iso(), freshness: "current" }],
  freshness: "current", ...fields,
});
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

let requests = [];
const initial = projection();
const controller = new DiagnosticsAnalysisController(async (payload) => { requests.push(payload); return initial; });
await controller.hydrate(initial, () => {});
await controller.hydrate(initial, () => {});
assert.equal(requests.length, 1, "an unchanged observation must reuse the bounded query");
assert.equal(requests[0].limit, 200);
assert.equal(requests[0].until_epoch - requests[0].since_epoch, 7 * 86_400);
assert.equal(requests[0].category, undefined);
controller.setEventOpen("event:1", true);
assert(controller.snapshot().expandedEventIds.has("event:1"));
await controller.hydrate(projection([event(2)]), () => {});
assert.equal(requests.length, 2, "a new journal event must invalidate cached history");

const pending = [];
const race = new DiagnosticsAnalysisController((payload) => {
  const item = { ...deferred(), payload };
  pending.push(item);
  return item.promise;
});
const oldAll = race.hydrate(initial, () => {});
race.selectCategory("system");
const newerSystem = race.hydrate(initial, () => {});
pending[0].resolve(projection([event(90)]));
await oldAll;
assert.equal(race.snapshot().ready, false, "a superseded filter result must not become visible");
pending[1].resolve(projection([event(91)]));
await newerSystem;
assert.equal(race.snapshot().data.events[0].id, "event:91");
assert.equal(pending[1].payload.category, "system");
race.selectRange("recorded");
const recorded = race.hydrate(initial, () => {});
assert.equal(pending[2].payload.since_epoch, undefined, "retained history must preserve backend retention semantics");
pending[2].resolve(initial);
await recorded;

let shouldFail = false;
let failRequests = 0;
const retry = new DiagnosticsAnalysisController(async () => {
  failRequests += 1;
  if (shouldFail) throw new Error("Unavailable");
  return initial;
});
await retry.hydrate(initial, () => {});
shouldFail = true;
now += 31_000;
await retry.hydrate(initial, () => {});
await retry.hydrate(initial, () => {});
assert.equal(failRequests, 2, "failed refreshes must not loop on every projection update");
assert.equal(retry.snapshot().ready, true, "refresh failures must retain the last loaded rows");
assert.equal(retry.snapshot().error, true);
retry.invalidate();
assert.equal(retry.snapshot().ready, true, "manual retry must keep existing results visible");
shouldFail = false;
await retry.hydrate(initial, () => {});
assert.equal(retry.snapshot().error, false);

window.__INFRA_SENTINEL_STATIC_DEMO_LOCALE = "en";
const staticOnly = new DiagnosticsAnalysisController(async () => { throw new Error("Static fixture sent a native command"); });
staticOnly.selectCategory("system");
staticOnly.selectRange("24h");
const fixture = projection([event(2), event(3, "network"), event(4, "system", { observed_at: iso(-2 * 86_400_000) })], {
  active: [event(2), event(3, "network")], active_count: 2,
});
await staticOnly.hydrate(fixture, () => {});
assert.equal(staticOnly.snapshot().data.events.length, 1, "static demo filters must honor category and time");
assert.equal(staticOnly.snapshot().data.active_count, 1, "static demo category must scope unresolved diagnostics");
delete window.__INFRA_SENTINEL_STATIC_DEMO_LOCALE;

const state = { ...controller.snapshot(), data: projection([
  event(1, "system", { title: '<img src=x onerror="alert(1)">', detail: "<script>no</script>" }),
  event(2, "network", { transition: "recovered", observation_status: "unknown" }),
]), ready: true, loading: false, stale: false, error: false };
let html = renderDiagnosticsPage(undefined, state);
assert(html.includes("&lt;img"));
assert(!html.includes("<img"));
assert(!html.includes("<script>"));
assert(html.includes("diagnostics-transition--unobserved"));
assert(!html.includes("diagnostics-transition--recovered"), "unknown observations must never receive recovery styling");
assert(html.includes("data-diagnostics-event=\"event:1\" open"), "expanded state must survive rendering");
assert(html.includes("data-diagnostics-category"));
html = renderDiagnosticsOverview();
assert(html.includes("Unavailable"));
assert(!html.includes("0 unresolved"), "missing diagnostics must not become a zero count");
html = renderDiagnosticsPage(undefined, { ...state, data: undefined, ready: false });
assert(html.includes("Freshness unknown"));
assert(html.includes("No history results available"));
html = renderDiagnosticsPage(undefined, { ...state, data: projection([], { history_truncated: true }) });
assert(html.includes("0 recorded changes"));
assert(html.includes("Earlier history has been trimmed"));
assert(!html.includes("result limit reached"), "retention loss must not imply more matching query results");
assert(!html.includes("newest 0"));
html = renderDiagnosticsPage(undefined, { ...state, data: projection([event(1)], { truncated: true }) });
assert(html.includes("newest 1 matching changes (result limit reached)"));
assert(!html.includes("Earlier history has been trimmed"), "query limits must not imply durable history loss");
assert(!html.includes("coverage is partial"));
html = renderDiagnosticsPage(undefined, { ...state, data: projection([], { sample_truncated: true }) });
assert(html.includes("latest sample exceeded the observation limit; coverage is partial"));
assert(!html.includes("result limit reached"), "sampling bounds must not imply more matching events");
assert(!html.includes("Earlier history has been trimmed"));
setLocale("zh");
html = renderDiagnosticsPage(undefined, { ...state, data: initial });
assert(html.includes("诊断历史"));
assert(html.includes("内存压力"));
assert(html.includes("本机系统"));
assert(!html.includes("Diagnostic history"));
assert(!html.includes("Memory pressure"));
html = renderDiagnosticsPage(undefined, { ...state, data: projection([event(1)], { truncated: true, history_truncated: true, sample_truncated: true }) });
assert(html.includes("已达显示上限"));
assert(html.includes("较早历史已按保留期限或记录上限裁剪"));
assert(html.includes("本次采样超出观测上限，覆盖范围不完整"));

// Exercise the production DOM update without bootstrapping the desktop bridge.
const mainSource = readFileSync(join(uiRoot, "src/main.ts"), "utf8");
const mainAst = ts.createSourceFile("main.ts", mainSource, ts.ScriptTarget.ES2022, true);
const helperNames = new Set(["replaceAroundRetainedChild", "updateDiagnosticsShell"]);
const domHelpers = mainAst.statements.filter((node) => ts.isFunctionDeclaration(node) && helperNames.has(node.name?.text));
assert.equal(domHelpers.length, helperNames.size);
const helperOutput = ts.transpileModule(domHelpers.map((node) => node.getText(mainAst)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const updateDiagnosticsShell = new vm.Script(`(function() {${helperOutput}\nreturn updateDiagnosticsShell;})()`).runInContext(context);
const protectedSelects = new Set();
class TestElement {
  constructor(className, textContent = "") {
    this.className = className;
    this.parentElement = null;
    this.childNodes = [];
    this.textContent = textContent;
    this.connectedRoot = false;
    this.disconnects = 0;
  }
  get isConnected() { return this.connectedRoot || Boolean(this.parentElement?.isConnected); }
  contains(node) { return this === node || this.childNodes.some((child) => child.contains(node)); }
  querySelector(selector) {
    for (const child of this.childNodes) {
      if (child.className === selector.slice(1)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  removeChild(child) {
    assert.equal(child.parentElement, this);
    for (const select of protectedSelects) {
      if (child.contains(select) && select.isConnected) select.disconnects += 1;
    }
    this.childNodes.splice(this.childNodes.indexOf(child), 1);
    child.parentElement = null;
    return child;
  }
  insertBefore(child, before) {
    if (child.parentElement) child.parentElement.removeChild(child);
    const index = before === null ? this.childNodes.length : this.childNodes.indexOf(before);
    assert(index >= 0);
    this.childNodes.splice(index, 0, child);
    child.parentElement = this;
    return child;
  }
}
const element = (className, ...children) => {
  const node = new TestElement(className);
  for (const child of children) node.insertBefore(child, null);
  return node;
};
const shellFor = (label) => element("shell",
  new TestElement("topbar", label), new TestElement("dashboard-actions", label),
  element("diagnostics-page", new TestElement("diagnostics-page__heading", label),
    element("diagnostics-history",
      element("diagnostics-filters", new TestElement("range"), new TestElement("category"), new TestElement("diagnostics-filters__status", label)),
      new TestElement("diagnostics-table", label), new TestElement("diagnostics-history__footer", label))),
  new TestElement("footer", label));
const existingShell = shellFor("initial");
existingShell.connectedRoot = true;
const originalPage = existingShell.querySelector(".diagnostics-page");
const originalHistory = existingShell.querySelector(".diagnostics-history");
const originalFilters = existingShell.querySelector(".diagnostics-filters");
const originalRange = existingShell.querySelector(".range");
const originalCategory = existingShell.querySelector(".category");
originalRange.value = "recorded";
protectedSelects.add(originalRange);
protectedSelects.add(originalCategory);
for (let cycle = 1; cycle <= 4; cycle += 1) {
  assert.equal(updateDiagnosticsShell(existingShell, shellFor(`refresh ${cycle}`)), true);
  assert.equal(existingShell.querySelector(".range"), originalRange, "sampling and query renders must keep the native range select");
  assert.equal(existingShell.querySelector(".category"), originalCategory, "sampling and query renders must keep the native category select");
  assert.equal(existingShell.querySelector(".diagnostics-page"), originalPage);
  assert.equal(existingShell.querySelector(".diagnostics-history"), originalHistory);
  assert.equal(existingShell.querySelector(".diagnostics-filters"), originalFilters);
  assert.equal(originalRange.isConnected, true);
  assert.equal(originalCategory.isConnected, true);
  assert.equal(originalRange.disconnects, 0, "a native popup must not lose its connected select or an ancestor");
  assert.equal(originalCategory.disconnects, 0);
  assert.equal(originalRange.value, "recorded", "refresh must not overwrite an in-progress selection");
  assert.equal(existingShell.querySelector(".diagnostics-table").textContent, `refresh ${cycle}`, "history must keep refreshing while a select remains focused");
  assert.equal(existingShell.querySelector(".diagnostics-filters__status").textContent, `refresh ${cycle}`);
}
assert.equal(updateDiagnosticsShell(existingShell, element("shell", new TestElement("overview"))), false, "other routes must retain normal full rendering");
assert.equal(originalCategory.isConnected, true, "a route mismatch must leave the current DOM untouched for the caller");

console.log("Diagnostic UI checks passed: bounded queries, cache freshness, filter races, retry retention, static isolation, safe rendering, unknown state, distinct limit flags, localization, and stable native filter DOM across refreshes.");
