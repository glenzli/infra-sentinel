# CLI token accounting receipts

Normal Codex CLI sessions under the configured Codex home and AGY sessions in
the default state directories are discovered automatically. A run using
`codex exec --ephemeral --ignore-user-config`, or AGY with a private temporary
state directory, may leave no discoverable history. Its outer caller can submit
a usage receipt **after** the isolated child exits. No exporter, hooks inside
the child, localhost access, or weakening of its isolation is required.
Claude Code sessions not counted by another source can use the same ledger,
with the native result adapter below. This does not install hooks or scan
Claude Code history automatically.

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

`client` is `codex`, `agy` or `claude-code`. `model_basis` is `reported` when
the native result reports the model identity; otherwise use `requested`.
Neither a requested name nor a native CLI label independently verifies a
proxy's backend identity. Claude Code native context selectors such as `[1m]`
are preserved in receipts and omitted from chart names. Its dedicated
`claude-code` catalog scope prices only evidenced, dated model references.
Known input, cache-read and output legs can be estimated independently; cache
writes without a known TTL tariff remain unpriced, with their tokens counted.

API reference estimates use the shared price catalog at the receipt's completion
date. AGY names such as `gemini-3.8-flash-high` retain their original identity
but use the dated `gemini-3.8-flash` reference when no exact variant entry exists;
`high` is a recognized effort marker. Known `none`, `minimal`, `low`, `medium`,
`high`, `xhigh` and `max` levels apply to newly observed model families too;
known-family `thinking` markers use the same resolver as ordinary AGY history.
The desktop groups these known effort variants under the base model in Token,
history, and equivalent-value views, adding their counters and already computed
price references. Raw model names remain in receipts and provider snapshots for
audit and exact dated-price matching; effort does not create a separate display
row or variant warning.
Context selectors such as Claude Code's `[1m]` are also omitted in model and
equivalent-value charts without changing the original tariff or counters.
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
cannot be detected automatically. Codex and AGY require `coverage: isolated`.
Claude Code also accepts `coverage: uncollected` for sessions the caller has
verified are not already counted. These fields are caller assertions, not
filesystem or operating-system isolation audits.

## Claude Code native terminal adapter

An outer runner can pass the final native stream-JSON `type: result` object to:

```python
from infra_sentinel.cli.usage_receipts import submit_claude_code_result

acknowledgements = submit_claude_code_result(
    state_dir, native_terminal,
    completed_at=original_result_seen_at, fresh_session=True,
)
```

The caller must verify the session was freshly created, no other collector
counts it, and the completion timestamp is the original observed result time.
The pure `resources.ai.claude_code_usage.claude_code_receipts` helper returns
counts-only receipts for the same existing source/packaged submission paths.
The raw result stays in the caller's memory and never enters Sentinel's IPC.

The adapter requires complete native `usage` and `modelUsage` counters and
checks their totals agree. Native uncached input, cache reads and cache creation
are disjoint: all three enter receipt input. Native output already includes
thinking; known thinking counters remain an output subset. Each model gets one
receipt, keyed by a hash of the native session and model, so copied archives
and delivery retries do not add consumption. Original native model labels are
retained, including proxy labels; provider claims and `costUSD` are ignored.

Session totals are cumulative. Resumed sessions are rejected by this adapter;
they need a separately evidenced delta before submission. Intermediate
snapshots, incomplete counters and conflicting aggregate/model totals are
rejected. Zero-usage terminals create no rows. A failed terminal with complete,
positive usage still counts, independently of the outer task's success.

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
