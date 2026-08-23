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
| `resources/facilities/` | Infra Discovery candidate parsing, socket observation, and bounded facility protocol projections |
| `resources/network/` | Local proxy traffic, VPS/Xray reads, billing policy, attribution, and logical-versus-billed traffic semantics |
| `resources/system/` | Capability-driven CPU, memory, disk, and process-IO observation; `backends/` selects one OS-specific implementation |
| `resources/upstream/` | Read-only official upstream-status observation |
| `platform/` | Narrow process-level host primitives such as the single-Agent lock |
| `cli/` | Report and snapshot entry points over the package's typed APIs |

Within the AI boundary, `resources/ai/codex_sampling.py` owns privacy-bounded
Codex rollout token parsing, lineage-aware inherited-prefix suppression,
scoped cross-file deduplication, durable request-increment ledger updates, and
read-only reconstruction. The ledger retains daily totals plus current-day
hour buckets; other AI adapters expose exact event hours when their source has
timestamps and sampled deltas with explicit estimated provenance otherwise.
`resources/ai/codex.py` consumes that ledger as its
only Codex usage fact source. `cli/codex_usage_audit.py` only renders an
aggregate audit; it does not own parsing or persist user rollout data.
`app/codex_migration.py` coordinates the one-time ledger rebuild, recoverable
backup, and scoped metric-store replacement without moving those policies into
the Collector or SQLite owner.

## State and privacy boundaries

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
