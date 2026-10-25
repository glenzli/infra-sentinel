// Run the production UI aggregation against a packaged-Agent audit capture.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const ui = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const directory = resolve(process.argv[2]);
const read = (name) => JSON.parse(readFileSync(resolve(directory, name), "utf8"));
const audit = read("audit.json"), document = read("projection.json"), queries = read("queries.json");
const modulePath = resolve(directory, "ui-aggregation.mjs");
await build({
  stdin: { contents: 'export { projectUsage, canonicalModelId } from "./src/ai_usage_series.ts";',
    resolveDir: ui, loader: "ts" },
  bundle: true, platform: "node", format: "esm", outfile: modulePath,
});
// The UI formatting dependency initializes its locale on import.
globalThis.window = { __INFRA_SENTINEL_STATIC_DEMO_LOCALE: "en" };
const { projectUsage, canonicalModelId } = await import(pathToFileURL(modulePath));
const source = document.infra.ai_usage.sources.find((value) => value.source_id === "codex");
const windows = [];
for (const [range, response] of Object.entries(queries)) {
  const query = response.query;
  const window = { sinceEpoch: query.since_epoch, untilEpoch: query.until_epoch,
    bucketSeconds: query.bucket_seconds, bucketOffsetSeconds: query.bucket_offset_seconds };
  const usage = projectUsage(response.points, [source], range, window);
  const expectedModels = new Map();
  let expected = 0;
  for (const [day, values] of Object.entries(audit.days)) {
    const epoch = new Date(`${day}T00:00:00`).getTime() / 1000;
    if (range !== "recorded" && (epoch < window.sinceEpoch || epoch > window.untilEpoch)) continue;
    expected += values.total_tokens;
    for (const [model, tokens] of Object.entries(values.models)) {
      const id = canonicalModelId(model);
      expectedModels.set(id, (expectedModels.get(id) ?? 0) + tokens);
    }
  }
  assert.equal(usage.sourceTotals.get("codex") ?? 0, expected, `${range}: UI source total`);
  const nonzero = (entries) => Object.fromEntries([...entries].filter(([, value]) => value !== 0).sort());
  assert.deepEqual(nonzero(usage.modelTotals), nonzero(expectedModels), `${range}: UI model totals`);
  assert.equal(usage.datedTotal + usage.undatedTotal, expected, `${range}: UI dated coverage`);
  windows.push(range);
}
console.log(JSON.stringify({ status: "ok", windows, production_ui_aggregation: true }));
