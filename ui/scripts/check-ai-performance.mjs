import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import ts from "typescript";

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const now = new Date(2026, 9, 29, 12);
class Clock extends Date {
  constructor(...args) { super(...(args.length ? args : [now.getTime()])); }
  static now() { return now.getTime(); }
}
const context = vm.createContext({
  window: { localStorage: { getItem: () => "en", setItem: () => {} } },
  navigator: { language: "en-US" }, document: { documentElement: {} },
  Date: Clock, Intl, Map, Set, Error, Number, String, Array, Boolean, Object, Promise,
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
  return module.exports;
}
const { renderAiUsageResourcePage } = loadModule("./ai_usage_view");
const { analysisTimeWindow } = loadModule("./analysis_time");
const { setLocale } = loadModule("./i18n");
const text = (value) => ({ en: value, zh: value });
const metric = (id, value, unit) => ({ id, value, unit, label: text(id) });
const stamp = (age = 0, minute = 30) => new Date(2026, 9, 29 - age, 11, minute).toISOString();
const day = (age) => stamp(age).slice(0, 10);
const group = (id, model, metrics, age = 0, minute = 30) => ({
  id, title: text(id === "turn-performance" ? "Turn average" : id === "request-performance" ? "Request average" : "Stream"),
  sample: { model, recorded_at: stamp(age, minute) }, metrics, badge: text(`${model} ${stamp(age, minute)}`),
});
const latency = (value) => metric("first-token-latency", value, "seconds");
const sol = "gpt-6.1-sol", astra = "gpt-6-astra", terra = "gpt-6.1-terra";
const usage = (tokens) => ({ available: true, tokens });
const source = {
  source_id: "codex", label: "Codex", usage: { today: usage(125), cumulative: usage(200) },
  models: [sol, astra, terra].map((id, index) => ({ id, today: usage(index === 2 ? 0 : 100 - index * 75), cumulative: usage([100, 25, 75][index]) })),
  history: { daily_available: true, daily: [
    { date: day(0), tokens: 125, models: [{ id: sol, tokens: 100 }, { id: astra, tokens: 25 }] },
    { date: day(8), tokens: 75, models: [{ id: terra, tokens: 75 }] },
  ] },
  pricing: { daily: [0, 8].map((age) => ({ date: day(age), reference: {
    kind: "catalog-text-api-reference", cost_usd: 1, priced_tokens: age ? 75 : 125,
    models: (age ? [terra] : [sol, astra]).map((id) => ({ id, cost_usd: .5, tokens: 50 })),
  } })) },
  details: [
    group("turn-performance", sol, [metric("turn-output-rate", 777, "tokens/s"), latency(.11)], 0, 50),
    group("request-performance", sol, [metric("request-output-rate", 8.1, "tokens/s"), latency(8.94)]),
    group("estimated-stream-performance", sol, [metric("estimated-stream-output-rate", 61.6, "tokens/s"), latency(8.94)]),
    group("turn-performance", astra, [metric("turn-output-rate", 888, "tokens/s"), latency(3.45)], 0, 45),
    group("request-performance", terra, [metric("request-output-rate", 999, "tokens/s"), latency(5.55)], 8),
    group("estimated-stream-performance", terra, [metric("estimated-stream-output-rate", 88.8, "tokens/s"), latency(5.55)], 8),
  ],
};
function render(sources, range = "today", mode = "overview", modelMeasure = "tokens") {
  return renderAiUsageResourcePage({ infra: { ai_usage: { sources }, collectors: [] } }, { status: "healthy" }, [], {
    mode, range, modelMeasure, historyVisual: "bars", providerPanels: new Map([["codex", true]]),
    points: [], window: analysisTimeWindow(range, 3600, now), loading: false,
  });
}
function tags(html) {
  return [...html.matchAll(/<button[^>]*class="ai-performance-tag"[^>]*>[\s\S]*?<\/button>/g)].map(([button]) => button).join("\n");
}
for (const locale of ["en", "zh"]) {
  setLocale(locale);
  const delayLabel = locale === "zh" ? "首 Token 延迟" : "First token delay";
  for (const range of ["today", "7d", "30d", "recorded"]) {
    for (const mode of ["overview", "models", "activity"]) {
      const html = render([source], range, mode);
      for (const obsolete of ["Turn average", "Request average", "轮次平均", "请求平均", "777 tokens/s", "888 tokens/s", "999 tokens/s"])
        assert(!html.includes(obsolete), `${locale}/${range}/${mode}: obsolete ${obsolete}`);
      assert(html.includes("61.6 tokens/s") && html.includes("8.94 s"), "rate and delay from the same request");
      assert(html.includes("3.45 s"), "latency-only model stays visible");
      assert(!html.includes("0.11 s"), "newer turn latency must not replace the stream sample's request latency");
      if (mode !== "activity") {
        assert.equal(tags(html).includes("88.8 tokens/s"), ["30d", "recorded"].includes(range), "live time-range filtering");
        const count = [...tags(html).matchAll(new RegExp(`<span>${delayLabel}</span><b>8.94 s`, "g"))].length;
        assert.equal(count, 1, "duplicate latency fields produce one label");
      }
    }
  }
  assert(tags(render([source], "today", "models", "value")).includes("61.6 tokens/s"), "equivalent-value view keeps timing labels");

  const missingPair = structuredClone(source);
  missingPair.details = [
    group("turn-performance", sol, [latency(.11)]), // Same finish time, different timing scope.
    group("request-performance", sol, [latency(.22)], 0, 50),
    group("estimated-stream-performance", sol, [metric("estimated-stream-output-rate", 61.6, "tokens/s")]),
  ];
  const unpaired = tags(render([missingPair]));
  assert(unpaired.includes("61.6 tokens/s") && !unpaired.includes(delayLabel), "no latency borrowed from a different request or a turn");

  const observed = structuredClone(missingPair);
  observed.details = [group("request-performance", sol, [latency(.44)]),
    group("client-stream-performance", sol, [metric("client-stream-output-rate", 22.2, "tokens/s")])];
  const paired = tags(render([source, { ...observed, source_id: "other", label: "Other" }]));
  for (const value of ["61.6 tokens/s", "8.94 s", "22.2 tokens/s", "0.44 s"])
    assert(paired.includes(value), "providers using the same model keep independent sample pairs");

  const absent = structuredClone(missingPair);
  absent.details = [group("turn-performance", sol, [metric("turn-output-rate", 777, "tokens/s")])];
  assert.equal(tags(render([absent])), "", "averages never fill missing stream timing or latency");
  absent.details = [group("request-performance", sol, [latency(0)])];
  assert(tags(render([absent])).includes("0 s"), "reported zero latency is distinct from missing latency");
  absent.details = [group("estimated-stream-performance", sol, [metric("estimated-stream-output-rate", NaN, "tokens/s"), latency(NaN)])];
  assert.equal(tags(render([absent])), "", "invalid numbers stay absent");
}
console.log("AI performance: bilingual overview/model/value/activity views, four ranges, same-request/provider pairing, latency-only and missing-data checks passed.");
