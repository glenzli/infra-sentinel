# Infra Sentinel Source Skeleton

Infra Sentinel is a local-first desktop observer for personal AI
infrastructure. The source tree keeps measurement, durable local state, native
integration, and presentation separate so a new resource does not turn the
desktop UI into a collector or make accounting platform-specific.

For platform support, privacy boundaries, and the resource-provider onboarding
sequence, read [docs/architecture.md](docs/architecture.md). This file answers
the shorter question: **where should a change live?**

## Composition boundaries

| Entry point | Owns | Does not own |
| --- | --- | --- |
| `bin/` | Small executable/release wrappers | Product logic |
| `src/infra_sentinel/app/agent.py` | Agent composition, sampling lifecycle, command loop, and Projection publication | Provider-specific parsing or UI rendering |
| `src/infra_sentinel/` | Portable Agent semantics, resource adapters, metrics, and configuration | Native WebView or tray implementation |
| `ui/src-tauri/src/main.rs` | Tauri desktop composition and native lifecycle | Sampling, accounting, or arbitrary local access |
| `ui/src/` | Projection-driven WebView views and interaction state | Direct filesystem, database, shell, SSH, or socket access |
| `tests/` | Contract, adapter, and projection-level behavior tests | Test-only product ownership |

The Python Agent is the sole writer of sampled state and the sole producer of
the versioned Projection. The desktop shell may submit only allowlisted Agent
commands and consume that Projection. Keep that one-way data boundary intact.

## Python Agent

Read [src/infra_sentinel/SKELETON.md](src/infra_sentinel/SKELETON.md) before
adding Python behavior.

At a glance:

- `app/` assembles lifecycle, configuration, local commands, and Projection
  publication.
- `core/` defines collector, registry, model, stability, timing, and
  Projection semantics shared by resource families.
- `metrics/` owns SQLite persistence, bounded retention/aggregation, and
  historical queries.
- `resources/` owns source-specific protocols and their privacy-bounded
  adapters: AI usage, facilities, network accounting, system resources, and
  provider status.
- `platform/` contains narrow host-process integration that is not a resource
  measurement backend.
- `cli/` contains typed report/snapshot entry points over existing Agent
  semantics.

## Desktop application

Read [ui/SKELETON.md](ui/SKELETON.md) before changing the desktop app.

- `ui/src/` is platform-neutral TypeScript: bridge types, view routing,
  analysis state, formatting/localization, and resource-specific views.
- `ui/src-tauri/src/` is the native boundary: sidecar supervision, Projection
  cache, menu bar, notifications, URL opening, and allowlisted IPC commands.
- The renderer must request new native behavior through the Tauri bridge; it
  must never reach into the Agent state directory or an external provider
  directly.

## Change routing

| If the change is about... | Start here | Pair it with... |
| --- | --- | --- |
| A new data source or provider protocol | `resources/<family>/` | A collector registration in `app/agent.py`, focused adapter tests, and a projection test when the UI-visible contract changes |
| Metric point shape, retention, rollup, or historical range query | `core/` and `metrics/` | Store/query/pipeline tests; do not encode storage rules in a resource adapter |
| Resource status, alerts, or cross-resource presentation fields | `core/` | Projection and status-stability tests |
| A host capability or process/IO backend | `resources/system/backends/` or `platform/` | Declared-capability behavior and target-platform validation; missing data must remain missing |
| Infra Protocol facility discovery or a facility snapshot | `resources/facilities/` | Protocol and observer tests; Discovery advertises candidates, while a real connection establishes liveness |
| Network billing, Mihomo/Xray attribution, or remote VPS observation | `resources/network/` | Policy/remote/Xray tests; keep provider billing and local logical traffic distinct |
| An AI usage adapter | `resources/ai/` | Its contract/collector tests; local estimates and provider-authoritative values must remain labeled as such |
| A new WebView analysis or resource page | `ui/src/*_analysis.ts`, `ui/src/*_view.ts` | Projection types in `bridge.ts` and deterministic rendering behavior |
| A native desktop capability | `ui/src-tauri/src/` | A narrow IPC operation and target build/run verification |

## Guardrails

- Do not make resource adapters write directly to SQLite, render HTML, or
  branch on an unrelated platform.
- Do not let UI code infer missing metrics as zero or bypass the Projection
  schema to inspect local state.
- Do not add OS conditionals to accounting or policy code. Select one
  validated system backend at the platform boundary instead.
- Do not broaden IPC into arbitrary filesystem, shell, SQL, SSH, or URL
  execution. Keep commands explicit and allowlisted.
- Keep prompts, responses, credentials, raw payloads, and sensitive local
  paths out of persistent metrics and Projection data.

## Verification route

Start with the focused test module that owns the changed contract, then add a
projection-level test if data crosses into the desktop boundary. Run the
relevant Python, TypeScript, Rust, and target-platform checks in proportion to
the boundary changed; the detailed commands and supported-platform rule live
in [docs/architecture.md](docs/architecture.md).
