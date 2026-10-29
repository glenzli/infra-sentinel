# Isolated CLI token accounting

Normal Codex CLI sessions under the configured Codex home and AGY sessions in
the default state directories are discovered automatically. A run using
`codex exec --ephemeral --ignore-user-config`, or AGY with a private temporary
state directory, may leave no discoverable history. Its outer caller can submit
a usage receipt **after** the isolated child exits. No exporter, hooks inside
the child, localhost access, or weakening of its isolation is required.

This interface records counts. It never infers token speed, first-token delay,
or request duration. It does not discover every isolated CLI invocation.

## Receipt contract

Submit only one final provider-reported usage total per billable attempt. Use a
stable opaque `event_id` across delivery retries; use a new ID for a new billed
attempt, including failed or canary attempts when usage is actually reported.
Do not submit intermediate cumulative snapshots as independent attempts. Do
not fabricate zero usage when a run reports none.

```json
{
  "schema": "infra-sentinel.cli-usage.v1",
  "event_id": "bcdf0495-31bc-41fb-8cb5-1dff6b02ff52",
  "client": "codex",
  "model": "gpt-6.1-terra",
  "model_basis": "requested",
  "coverage": "isolated",
  "completed_at": "2026-10-29T10:57:36.903361+08:00",
  "usage": {
    "input_tokens": 9629,
    "output_tokens": 24415,
    "total_tokens": 34044,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "reasoning_output_tokens": 21606
  }
}
```

`client` is `codex` or `agy`. `model_basis` is `reported` only when the source
reports the actual model identity; otherwise use `requested`. A requested
model name is not independent verification of the backend model.

API reference estimates use the shared price catalog at the receipt's completion
date. AGY names such as `gemini-3.8-flash-high` retain their original identity
but use the dated `gemini-3.8-flash` reference when no exact variant entry exists;
`high` is a recognized effort marker. Known `low`, `medium`, `high`, and
`thinking` markers use the same bounded resolver as ordinary AGY history.
Unknown suffixes and pricing-relevant variants (for example image, TTS, preview,
or a different base model) are not guessed. Exact variant catalog entries win.
These are text API reference values, not the CLI provider's actual charges.
Missing cached-input counts remain unpriced rather than being assumed zero;
Codex also requires cache-write counts when the catalog prices that leg.

The three required counts are nonnegative integers. **Input includes cached
input; output includes reasoning; total equals input + output.** For a source
that reports reasoning separately, the caller must first normalize it into
output. Optional cache/reasoning fields are subsets and must be omitted when
unknown. A source that reports only a total cannot yet use this version.
Unknown fields are rejected; never pass raw CLI output, a prompt, response,
command line, environment, credentials, account or conversation metadata.
Receipts are limited to 4 KiB. Event IDs are hashed before the command is saved.

## Submission and acknowledgement

The Agent must be running for immediate acceptance. From a source install:

```sh
infra-sentinel-cli-usage --receipt /private/tmp/usage-receipt.json
# Or: python3 -m infra_sentinel.cli.usage_receipts < /private/tmp/usage-receipt.json
```

The packaged sidecar exposes the same use case:

```sh
infra-agent --record-cli-usage /private/tmp/usage-receipt.json
# Or: infra-agent --record-cli-usage - < /private/tmp/usage-receipt.json
```

The source CLI accepts `--config` or `--state-dir`, and `--wait-seconds 0..30`.
The packaged entry accepts the Agent's existing `--config`; otherwise both
resolve the normal application state directory. A `queued` response means the
private command has been submitted but is **not yet counted**. It survives an
Agent restart. `ok` with `recorded: true` confirms the transaction; `duplicate:
true` confirms the same receipt was already accepted. Retrying the same event
ID with different counts or metadata is rejected without changing history.

The outer runner should save its stable attempt ID, construct the allowlisted
receipt from the final numeric usage, and submit it outside the child sandbox.
This is an explicit post-run integration; installing Sentinel does not patch
third-party runners or shell commands. Do not resubmit regular sessions already
counted by Codex, AGY, or Infer Runtime. There is no reliable identity join
between an isolated receipt and those independent histories, so their overlap
cannot be detected automatically. The required `coverage: isolated` field is
the caller's assertion, not a filesystem audit.

## Storage, privacy and cost

Only the Agent writes `cli-usage.sqlite3`. It retains a hashed attempt identity,
client/model provenance, completion time, and numeric counters for deduplication
and historical totals. The desktop sees grouped day/model/hour counts under
**CLI receipts**, with no attempt identities or raw artifacts. Past usage is
assigned to its completion day/hour in the host timezone at import.

One small local command and one SQLite transaction are written per new receipt;
duplicate retries do not add usage rows. Unchanged sampling polls only check
the ledger's file metadata and reuse grouped counts. Receipt count, rather than
stream chunk rate or response size, determines storage growth. The ledger keeps
hashed identities for durable deduplication and therefore grows with accepted
attempts. Delete the caller's temporary receipt after acknowledgement if it is
not otherwise needed. No model request or telemetry setting is involved.
