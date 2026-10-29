# Python Agent Skeleton

`infra_sentinel` is the portable, local Agent. It samples resource adapters,
persists bounded metrics, derives a privacy-safe Projection, and serves a
small command protocol to the desktop shell.

`app/agent.py` is the composition root. It is the place that selects and wires
collectors, storage, schedulers, facility monitoring, and Projection
publication. It may coordinate the parts below, but source-specific parsing
belongs in `resources/` and SQL mechanics belong in `metrics/`.

## Ownership map

| Path | Stable responsibility |
| --- | --- |
| `app/` | Configuration, Agent lifecycle, local command protocol, and Projection stream/publication |
| `core/` | Collector contracts and registration, canonical resource model, status stabilization, sample timing, and Projection construction |
| `metrics/` | SQLite storage, pipeline checkpoints/compaction, aggregations, and bounded historical queries |
| `resources/ai/` | Read-only local AI usage adapters and normalized AI-usage contract |
| `resources/facilities/` | Infra Discovery candidate parsing, native local transports, and bounded facility protocol projections |
| `resources/network/` | Local proxy traffic, VPS/Xray reads, billing policy, attribution, and logical-versus-billed traffic semantics |
| `resources/system/` | Capability-driven CPU, memory, disk, and process-IO observation; `backends/` selects one OS-specific implementation |
| `resources/upstream/` | Read-only official upstream-status observation |
| `platform/` | Narrow process-level host primitives: the single-Agent lock, non-destructive parent liveness probes, and console-free child-process options |
| `cli/` | Report and snapshot entry points over the package's typed APIs |

Within the AI boundary, `resources/ai/codex_sampling.py` owns privacy-bounded
Codex rollout token parsing, lineage-aware inherited-prefix suppression,
scoped cross-file deduplication, durable request-increment ledger updates, and
read-only reconstruction. The ledger retains daily totals, current-day hour
buckets, and optional reported timing for each model's latest completed main-chat turn.
Old checkpoints backfill timing in bounded, resumable metadata-only scans without
replaying accounting offsets, aggregates or deduplication keys.
The AI performance owner in `resources/ai/performance.py` validates optional scalar
timing, selects the latest usable sample per exact model, and labels turn averages,
request averages, and client stream rates. Provider
adapters own timestamp and usage correlation; missing timing stays absent.
Timing is diagnostic metadata and never enters token accounting; turn markers
are irreversible hashes. Other AI adapters expose exact event hours when their source has
timestamps and sampled deltas with explicit estimated provenance otherwise.
`resources/ai/codex_telemetry/` owns optional authenticated loopback OTLP/JSON
log admission, volatile request correlation and aggregate-only performance
checkpoints. `protocol.py` drops all non-allowlisted data before shared state;
`receiver.py` bounds transport resources; `state.py` owns pairing and missing-data
policy. Explicit opt-in/configuration lives in `cli/codex_telemetry.py` and its
setup use case, never in a collector. The Agent wires its lifecycle and the
Codex collector consumes only safe diagnostic groups, independently of usage.
`resources/ai/codex.py` consumes that ledger as its
only Codex usage fact source. `cli/codex_usage_audit.py` only renders an
aggregate audit; it does not own parsing or persist user rollout data.
The sampling owner also provides an independent replay of checkpoint-bounded
file prefixes for validation while live logs keep growing; captured counters
and dedup keys are never reused as reference totals.
`app/codex_migration.py` coordinates the one-time ledger rebuild, recoverable
backup, and scoped metric-store replacement without moving those policies into
the Collector or SQLite owner.

`core/diagnostics.py` normalizes already-collected Projection facts into bounded
conditions and explicit observation coverage. `metrics/diagnostics.py` owns the
durable diagnostic journal, atomic occurrence/change/recovery transitions,
retention, and bounded read-only queries. The sampling loop is its only writer;
the desktop and `cli/diagnostics.py` consume the same query contract. Missing or
disabled observation never confirms recovery, and reading a snapshot never
refreshes its observation time.

`resources/ai/dsh.py` owns read-only DeepSeek Harness v4 session usage,
stream/final replacement, retry boundaries, inherited-prefix exclusion, and
event-time history. Its checkpoint retains only hashed request markers.

`resources/ai/pricing_catalog.py` owns the exact-model, effective-dated
api-price contract, the bundled offline fallback, validated Release downloads,
and the last-known-good local cache. Provider collectors may query this owner;
they must not fetch pricing or implement their own update cadence.
`resources/ai/antigravity_pricing.py` owns bounded suffix classification and
dated exact-versus-base price references; it preserves observed model IDs and
exposes access-marker semantics, rollout suspicion and estimate provenance as
optional model metadata. Trial/free labels are not evidence of zero charges.

## State and privacy boundaries

Within facilities, `discovery.py` owns declaration validation and native binding
selection inputs; `protocols.py` owns provider messages, framing, limits and
privacy normalization. `windows_pipe.py` owns bounded native pipe connection and
I/O cancellation lifetimes. `windows_security.py` owns Windows file admission
and connected-server SID checks. Windows transports never enter the metric or UI layers.

Within the system boundary, `resources/system/smartctl.py` owns optional
discovery of an already installed smartctl, bounded read-only NVMe SMART
queries, scalar normalization and the ten-minute cache. The macOS backend
uses it without installing software or elevating privileges. Missing tools or
unreadable devices leave the native host probes intact; raw device identities
and command output never enter Projection or metrics.

- `metrics/` is the durable metric boundary. A resource adapter returns points
  and a bounded current snapshot; it must not open or mutate the metric store.
- `core/projection.py` is the translation boundary from stored/current Agent
  semantics to desktop-visible Projection data. Keep raw transport details
  behind the resource adapter.
- `app/protocol.py` is the allowlisted local command boundary. A command may
  request an Agent-owned action; it is not a generic RPC or system-execution
  channel.
- Resource adapters are read-only observers. Do not capture prompts, model
  responses, credentials, arbitrary project contents, raw external payloads,
  or full connection/packet data.

## Adding or changing a source

1. Pick the resource family by the source's persistent semantics, not its
   transport. A provider-status feed belongs in `upstream/`; an Infra Protocol
   offer belongs in `facilities/`; a host capability belongs in `system/`.
2. Normalize source output to the collector/core contract. Preserve explicit
   absence and error state rather than inventing zero or healthy values.
3. Register it in the Agent composition root only after the adapter has a
   focused test.
4. Change Projection only when the value is stable enough for UI consumption;
   pair it with a projection test.
5. For host-specific code, add a backend under `resources/system/backends/`
   and validate its lifecycle on that OS. Do not scatter platform checks
   through `core/`, `metrics/`, or resource policies.

## Test topology

The `tests/` modules mirror these durable boundaries: adapter modules validate
their protocol/error normalization, metric modules validate persistence/query
rules, and `test_infra_projection.py` verifies desktop-visible semantics. Use
the narrowest matching test first; only run a package-wide suite when a shared
contract or composition root changes.
