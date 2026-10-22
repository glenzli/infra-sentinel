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
  Date, Intl, Map, Set, Error, Number, String, Array, Boolean, Object, Promise,
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
const now = new Date(2026, 9, 22, 12);
const dated = (daysAgo) => {
  const date = new Date(2026, 9, 22 - daysAgo);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
};
const usageWindow = (tokens) => ({ available: true, tokens });
const rows = [
  { id: "current-trial", age: 0, tokens: 100, kind: "trial" },
  { id: "weekly-rollout", age: 6, tokens: 40, kind: "rollout" },
  { id: "monthly-rollout", age: 29, tokens: 30, kind: "rollout" },
  { id: "older-rollout", age: 30, tokens: 20, kind: "rollout" },
  { id: "undated-free", age: null, tokens: 10, kind: "free" },
  { id: "unused-rollout", age: 0, tokens: 0, kind: "rollout" },
];
const antigravity = {
  source_id: "antigravity", label: "Antigravity",
  usage: { today: usageWindow(100), cumulative: usageWindow(200) },
  models: rows.map(({ id, age, tokens, kind }) => ({
    id, today: usageWindow(age === 0 ? tokens : 0), cumulative: usageWindow(tokens),
    price_reference: { model: id, available: true, match: "exact", marker_kind: kind, suspected_rollout: kind === "rollout" },
  })),
  history: { daily_available: true, daily: rows.filter((row) => row.age !== null).map(({ id, age, tokens }) => ({
    date: dated(age), tokens, models: [{ id, tokens }],
  })) },
};
// The same model used by a different provider must not inherit Antigravity's badge.
const other = {
  source_id: "other", label: "Other", usage: { today: usageWindow(50), cumulative: usageWindow(50) },
  models: [{ id: "older-rollout", today: usageWindow(50), cumulative: usageWindow(50) }],
};
function render(sources, range, mode = "overview", modelMeasure = "tokens", points = []) {
  return renderAiUsageResourcePage(
    { infra: { ai_usage: { sources }, collectors: [] } }, { status: "healthy" }, [],
    { mode, range, modelMeasure, historyVisual: "bars", providerPanels: new Map(), points,
      window: analysisTimeWindow(range, 3600, now), loading: false },
  );
}
function annotations(html) {
  return [...html.matchAll(/<button[^>]*class="[^"]*ai-model-reference-tag\b[^>]*>[\s\S]*?<\/button>/g)]
    .map(([button]) => button).join("\n");
}

const expected = {
  today: ["current-trial"],
  "7d": ["current-trial", "weekly-rollout"],
  "30d": ["current-trial", "weekly-rollout", "monthly-rollout"],
  recorded: ["current-trial", "weekly-rollout", "monthly-rollout", "older-rollout", "undated-free"],
};
for (const locale of ["en", "zh"]) {
  setLocale(locale);
  for (const [range, wanted] of Object.entries(expected)) {
    for (const mode of ["overview", "models"]) {
      const result = annotations(render([antigravity, other], range, mode));
      for (const { id } of rows) {
        assert.equal(result.includes(`Antigravity · ${id}`), wanted.includes(id), `${locale}/${range}/${mode}: ${id}`);
      }
      if (mode === "overview") {
        const rolloutCount = wanted.filter((id) => id.endsWith("rollout")).length;
        if (rolloutCount) assert(result.includes(`${locale === "zh" ? "疑似灰度" : "Suspected rollout"} ${rolloutCount}`));
        assert(!result.includes("across local history") && !result.includes("本机历史已记录"));
      }
    }
  }
  const plain = structuredClone(antigravity);
  plain.models[0].price_reference = {};
  assert.equal(annotations(render([plain], "today")), "", "no badge when only ordinary models were used in range");

  const priced = structuredClone(antigravity);
  priced.pricing = { daily: [{ date: dated(0), reference: {
    kind: "catalog-text-api-reference", cost_usd: 1, priced_tokens: 100,
    models: [{ id: "current-trial", cost_usd: 1, priced_tokens: 100 }],
  } }] };
  const valueAnnotations = annotations(render([priced], "today", "models", "value"));
  assert(valueAnnotations.includes("Antigravity · current-trial"), "equivalent-value mode retains an in-range badge");
  assert(!valueAnnotations.includes("rollout"), "equivalent-value mode excludes historical variants");
}

const fallback = structuredClone(antigravity);
fallback.usage = {};
fallback.history = {};
const point = { source_id: "antigravity", observed_epoch: now.getTime() / 1000 - 3600,
  dimensions: { model: "weekly-rollout" }, value: 10 };
const fallbackAnnotations = annotations(render([fallback], "7d", "overview", "tokens", [point]));
assert(fallbackAnnotations.includes("Antigravity · weekly-rollout"), "observed interval fallback retains the used model");
assert(!fallbackAnnotations.includes("Antigravity · older-rollout"), "interval fallback excludes unused historical models");
console.log("AI model references: four ranges, source/model/value views, bilingual labels, empty selection and source attribution passed.");
