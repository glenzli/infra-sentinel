# Desktop UI Skeleton

`ui/` is a cross-platform desktop shell around the local Infra Agent. The
renderer is a Projection consumer; the Tauri side is a narrow native bridge.
Neither layer owns traffic accounting, token aggregation, collector scheduling,
or durable metrics.

## Two layers

| Path | Owns | Must not own |
| --- | --- | --- |
| `src/` | Projection types and IPC client, view routing, resource pages, local analysis state, charts, formatting, localization, and CSS | Direct Agent-state reads, arbitrary local commands, sampling, or persistence |
| `src-tauri/src/` | Sidecar supervision, Projection cache, native menu/notifications, external Console/status links, application paths, an opt-in static documentation-demo gate, and allowlisted Tauri commands | Resource-specific accounting, data normalization, renderer business rules, or a general-purpose test mode |

`src/main.ts` is the renderer composition root: it routes between the overview,
resource pages, the AI methodology page, facility detail, diagnostic history, and settings. Keep page rendering in the
corresponding `*_view.ts` module and stateful time-range/filter behavior in
the matching `*_analysis.ts` module.

## Renderer routing

| Concern | Start here |
| --- | --- |
| Projection/command types or one new allowlisted operation | `src/bridge.ts`, then the paired Rust bridge implementation |
| Shared formatting, localization, visual primitives | `src/format.ts`, `src/i18n.ts`, `src/icons.ts`, or `src/styles.css` |
| Shared attention summaries, keyboard-native disclosure and refresh-safe expansion choices | `src/attention_diagnostics.ts` and its scoped CSS; `main.ts` binds fresh elements |
| Network, AI, or system time-range state | The matching `*_analysis.ts` module |
| Cross-resource diagnostic history, category/time filters, and disclosure state | `src/diagnostics_analysis.ts` for bounded read-only queries and query identity; `src/diagnostics_view.ts` for localized event rows and the overview summary |
| Network, AI, AI methodology, system, facility, or upstream-status presentation | The matching `*_view.ts` and scoped CSS module |
| Reusable time-bucket chart behavior | `src/time_bucket_bar_chart.ts` for hourly/daily quantitative bars and `src/daily_activity_calendar.ts` for recorded-history heatmaps, with their scoped CSS |
| Overview-only composition | `src/overview_view.ts` |
| Native menu, notification, URL, sidecar, cache, or app-path behavior | The matching module under `src-tauri/src/` |

## Native bridge rules

Every renderer-to-native operation has three narrow pieces:

1. A typed renderer call in `src/bridge.ts`.
2. An explicit Tauri command in `src-tauri/src/agent_bridge.rs` or the native
   module that owns the capability.
3. An Agent-side allowlist/protocol change only when the desktop actually
   needs a new Agent operation.

Do not turn that path into arbitrary file access, command execution, database
queries, SSH, or unchecked URL launching. The renderer always consumes the
versioned Projection produced by the Agent; it does not reconstruct resource
state from local files.

Diagnostic history uses the allowlisted `diagnostics.query` Agent command.
The renderer keeps unknown, disabled, or unobserved sources distinct from
recorded recovery, and it does not infer a healthy state from missing history.
Only the explicit static documentation-demo gate filters its supplied
Projection events locally instead of submitting a command.

## Static documentation demos

`../scripts/write_demo_projection.py` creates an anonymous fixture and
`../scripts/capture-demo-screenshots.sh` starts an already-built desktop app
for README captures. `src-tauri/src/agent_supervisor.rs` owns the narrow,
explicit static-Projection gate: when it is enabled, it loads only the given
fixture, skips Agent bootstrap and sidecar launch, and may show the dashboard
for capture.

This is not an IPC feature, persistent preference, fallback data source, or
general development mode. Do not expose it to the renderer, reuse a user's
state directory, or let it contact collectors, local facilities, remote hosts,
or upstream providers.

## Verification route

For renderer changes, run the TypeScript/Vite build. For native bridge or
sidecar lifecycle changes, also run the Rust checks and a target-platform
desktop build. When a UI field changes its meaning, pair the UI change with a
Python Projection test—the UI should adapt to a stable Projection contract,
not encode provider transport quirks.

`scripts/package-agent.mjs` owns native-host PyInstaller packaging and the
Tauri target filename. The shell entry delegates to it; Windows builds use
the same owner through npm without requiring a shell. Keep the Agent's console
subsystem and redirected stdout; native launch flags suppress its console
window without removing the Projection pipe.
