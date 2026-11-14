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
const { canonicalModelId, projectUsage } = loadModule("./ai_usage_series");
const { projectPriceReference } = loadModule("./ai_price_reference");
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

const baseModel = "gemini-4-argon";
const effortModels = [baseModel, `${baseModel}-high`, `${baseModel}-xhigh`, `${baseModel}-medium`];
for (const base of [baseModel, "gemini-4-carbon", "gemini-3.8-flash", "gemini-3.1-pro", "gemini-3.1-flash-lite", "claude-fable-5-2"]) {
  for (const effort of ["low", "medium", "high", "xhigh", "thinking"]) {
    assert.equal(canonicalModelId(`${base}-${effort}`), base, "known effort is a model display grouping");
  }
}
for (const model of ["gemini-4-argon-trial", "gemini-4-argon-free", "gemini-4-argon-n", "gemini-3.7-flash-exp-b",
  "gemini-3-flash-preview", "gemini-3-flash-preview-high", "gemini-4-carbon-image", "gemini-4-argon-turbo",
  "gemini-4-argon-xhigh-turbo", "other-high"]) {
  assert.equal(canonicalModelId(model), model, "access, rollout and unsupported variants keep distinct IDs");
}
assert.equal(canonicalModelId(" OpenAI/GPT-6.1-SOL "), "gpt-6.1-sol", "existing OpenAI normalization is preserved");

const modelRows = effortModels.map((id, index) => ({ id, tokens: (index + 1) * 100 }));
const effortSource = {
  source_id: "cli-receipts", label: "CLI receipts",
  usage: { today: usageWindow(1000), cumulative: usageWindow(2000) },
  models: modelRows.map(({ id, tokens }) => ({ id, today: usageWindow(tokens), cumulative: usageWindow(tokens * 2),
    price_reference: { model: baseModel, available: true, match: id === baseModel ? "exact" : "base-reference",
      ...(id === baseModel ? {} : { marker_kind: "effort", marker: id.slice(baseModel.length) }) },
  })),
  history: {
    daily_available: true,
    daily: [0, 6].map((age) => ({ date: dated(age), tokens: 1000, models: modelRows })),
    hourly_available: true,
    hourly: [{ epoch: now.getTime() / 1000 - 3600, tokens: 1000, models: modelRows }],
  },
  pricing: { daily: [0, 6].map((age) => ({ date: dated(age), reference: {
    kind: "catalog-text-api-reference", cost_usd: 14, priced_tokens: 1000, unpriced_tokens: 0,
    models: modelRows.map(({ id, tokens }, index) => ({ id, cost_usd: [1, 2, 3, 8][index], priced_tokens: tokens })),
  } })) },
};
// A second provider using the base name must join the same model row without
// affecting per-source amounts or multiplying the source-level price total.
const baseSource = {
  ...effortSource, source_id: "antigravity", label: "Antigravity",
  usage: { today: usageWindow(50), cumulative: usageWindow(100) },
  models: [{ id: baseModel, today: usageWindow(50), cumulative: usageWindow(100) }],
  history: {
    daily_available: true,
    daily: [0, 6].map((age) => ({ date: dated(age), tokens: 50, models: [{ id: baseModel, tokens: 50 }] })),
    hourly_available: true,
    hourly: [{ epoch: now.getTime() / 1000 - 3600, tokens: 50, models: [{ id: baseModel, tokens: 50 }] }],
  },
  pricing: { daily: [0, 6].map((age) => ({ date: dated(age), reference: {
    kind: "catalog-text-api-reference", cost_usd: .5, priced_tokens: 50,
    models: [{ id: baseModel, cost_usd: .5, priced_tokens: 50 }],
  } })) },
};
const sources = [effortSource, baseSource];
const original = JSON.stringify(sources);
for (const range of ["today", "7d", "30d", "recorded"]) {
  const window = analysisTimeWindow(range, 3600, now);
  const usage = projectUsage([], sources, range, window);
  const total = range === "today" ? 1050 : 2100;
  assert.deepEqual([...usage.modelTotals], [[baseModel, total]], `${range}: base and effort counters must be added`);
  assert.equal(usage.sourceTotals.get("cli-receipts"), range === "today" ? 1000 : 2000);
  assert(usage.intervals.every((row) => [...row.models.keys()].every((id) => id === baseModel)), "every time bucket is grouped");
  assert.equal(usage.intervals.reduce((sum, row) => sum + row.total, 0), total);
  const price = projectPriceReference(sources, usage, range, window);
  const cost = range === "today" ? 14.5 : 29;
  assert.deepEqual([...price.byModel], [[baseModel, cost]], "equivalent value joins model names after pricing, even at different variant rates");
  assert.equal(price.costUsd, cost, "source-level prices are counted once");
  assert.equal(price.pricedTokens, total);
  assert([...price.byBucketModel.values()].every((row) => [...row.keys()].every((id) => id === baseModel)));
  for (const locale of ["en", "zh"]) {
    setLocale(locale);
    for (const measure of ["tokens", "value"]) {
      const html = render(sources, range, "models", measure);
      assert(html.includes(baseModel));
      for (const id of effortModels.slice(1)) assert(!html.includes(id), "effort names stay out of labels and hints");
      assert.equal(annotations(html), "", "effort-only grouping needs no variant warning badge");
    }
  }
}
// Without dated history, preserve the existing distinction between a known
// model total and an unlocated hourly tail. Daily snapshots can use model totals.
const noHistory = { ...effortSource, history: {} };
const fallbackUsage = projectUsage([], [noHistory], "today", analysisTimeWindow("today", 3600, now));
assert.deepEqual([...fallbackUsage.modelTotals], [[baseModel, 1000]]);
assert.deepEqual([...fallbackUsage.intervals[0].models], [["__unattributed__", 1000]]);
const dailyFallback = projectUsage([], [noHistory], "7d", analysisTimeWindow("7d", 3600, now));
assert.deepEqual([...dailyFallback.intervals[0].models], [[baseModel, 1000]], "daily model targets accumulate every effort variant");
const points = modelRows.map(({ id, tokens }) => ({ source_id: "cli-receipts", observed_epoch: now.getTime() / 1000 - 3600,
  dimensions: { model: id }, value: tokens }));
const sampled = projectUsage(points, [noHistory], "today", analysisTimeWindow("today", 3600, now));
assert.deepEqual([...sampled.intervals[0].models], [[baseModel, 1000]], "historical raw metric dimensions are grouped on read");
assert.equal(JSON.stringify(sources), original, "raw provider identities and counters remain unchanged");
console.log("AI model references: four ranges, bilingual model/value views, effort grouping, unchanged totals/prices, fallback and distinct non-effort variants passed.");
