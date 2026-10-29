# Changelog

## Unreleased

## 2.3.0 — 2026-10-29

Shared source version with macOS validation. Published platform downloads keep
their own release status; the existing Windows preview remains 2.2.0-beta.1.

### Added

- Explicit, reversible Codex telemetry setup and an authenticated loopback OTLP
  log receiver with bounded memory, concurrency, admission and numeric-only storage.
- Per-model request timing, first-token delay and receiver diagnostics, independent
  of Token accounting. Local Sol and Astra desktop requests have been verified.
- Codex telemetry setup, privacy, performance and macOS proxy troubleshooting notes.
- Counts-only usage receipts for isolated Codex and AGY CLI attempts, with a
  private local command, durable idempotent storage, and daily/model/hour totals.
  The outer runner submits final usage; temporary sessions are not auto-discovered.

### Changed

- Show stream rate and first-token delay from the same source/model/request;
  Codex estimates subtract first-token waiting and omit unreliable timing samples.
- Remove turn/request averages from normal AI views. Keep sample hints to source,
  model and time, with full calculation notes on Methods & estimates.
- Include updated bundled API-price coverage and GPT-6.1 Terra accounting support.
- Apply the shared dated price catalog to CLI receipts, including recognized
  AGY effort suffixes such as `gemini-3.8-flash-high`. Preserve raw model IDs,
  count reasoning once, and leave missing required cache breakdowns unpriced.
- Compact the AI detail header and summary cards, use single-line tabs, and
  place range and model-measure controls together with narrow-window wrapping.

### Setup and platform notes

- Telemetry requires one explicit enable command. Existing third-party exporters
  are preserved; running Codex processes need restarting to load configuration.
- macOS loopback proxy exceptions are documented separately from automatic setup.
- Windows and Linux packaging/runtime acceptance for this update remains separate.

## 2.2.0-beta.1 — 2026-10-25

Windows x64 preview. The macOS stable release remains 2.1.2.

### Added

- Native Windows desktop and tray, single-instance activation, console-free
  Agent supervision, current-user NSIS installation and a bundled WebView2 bootstrapper.
- Native Windows Infra Protocol Named Pipe consumption for PCP, Infer Runtime
  and Dev Mesh Observer, including private Discovery file ACLs, server SID
  admission and bounded, safely cancelled byte-stream I/O.
- Reproducible packaged Windows checks and independent replay of captured Codex
  file prefixes, validating restart accounting and production UI aggregates
  while live rollouts keep growing.

### Fixed

- Probe Windows parent-process liveness without using POSIX signal semantics.
- Honor CODEX_HOME, close SQLite migration backup handles, use UTF-8 for local
  subprocess output, and preserve portable event-time conversions and test fixtures.
- Keep unsupported local Mihomo observation disabled without false failures
  or fabricated zero-value metrics. SSH remote observation remains available.

### Preview limits

- Unsigned Windows x64 installer; Windows notifications and local Mihomo
  Controller support remain pending, along with extended sleep/resume testing.
- Native pipe contract fixtures pass for all three facility protocols;
  real-product Windows interoperability requires providers publishing compatible offers.
- No new macOS binary is included. Linux desktop publication follows Windows.

## 2.1.2 — 2026-10-22

### Added

- Local diagnostic history for observation changes, severity changes, and confirmed
  recoveries, with category/time filters, bounded retention, and restart persistence.
- Read-only desktop and CLI queries over the same diagnostic journal, with source
  freshness and unknown coverage kept distinct from recovery.
- Optional NVMe SSD health, wear, temperature, and lifetime counters from an
  already installed, readable `smartctl`, without installing tools or elevating access.

### Fixed

- Limit suspected-rollout, trial, free-tier, and base-estimate model annotations
  to each provider's usage in the selected range, including model and value views.
  All history retains cumulative model coverage; counts describe models, not requests.
- Preserve each day's model breakdown in activity calendars, including models
  outside the leading models across all recorded history.

## 2.1.1 — 2026-10-18

### Fixed

- Keep each model's latest usable speed independently within Codex, OpenCode,
  DSH, and Antigravity, instead of letting one model replace another's sample.
- Filter inline speed samples by the selected calendar range in usage overview
  and model analysis, keeping older model samples out of the current-day view.
- Recover per-model Codex timing from readable older logs with bounded,
  resumable metadata scans, without replaying Token accounting.

### Changed

- Show the model beside each inline speed sample; hover or focus still reveals
  recording time, latency, duration, and the applicable timing scope.

## 2.1.0 — 2026-10-16

### Added

- Read-only DSH v4 session usage, including Zstd sessions, final-versus-streamed
  usage replacement, retry boundaries, and dated model history.
- Separately labeled turn-average, request-average, and client-stream speeds
  for Codex, OpenCode, DSH, and Antigravity when reliable timing is available.
- Bounded model suffix interpretation with dated base-model reference estimates:
  trial/trail and free labels are separate from suspected rollout markers.

### Changed

- Place speed and model-reference annotations next to model usage.
- Default Agent activity to compact summary rows; expanded diagnostics use
  two-column metric groups and accessible, scrollable explanation hints.
- Bundle api-price catalog 2026.10.16.1 and total Antigravity historical reference
  value using each usage day's effective price. Catalog updates remain independent
  of application releases; reference values are not provider bills.

### Fixed

- Keep facility projections current while Mihomo is unavailable.
- Retain packaged CA roots for pricing downloads and allow normal release-download
  latency without discarding the last valid catalog.

## 2.0.2 — 2026-09-08

### Fixed

- Recover Antigravity generation dates from execution-linked step metadata when
  the generation timestamp is absent, instead of using database modification time.
- Exclude generations without a reliable timestamp from all usage totals and
  report their count separately. Dated records retain their model attribution.
- Build the app bundle directly for ZIP distribution without requiring DMG packaging.

## 2.0.1 — 2026-09-06

### Changed

- Moved exact text-token reference prices into the independently released
  `glenzli/api-price` catalog.
- Added GPT-6 Astra and Gemini 3.8 Flash price periods, including Gemini's
  published January 1, 2027 change.
- Added validated daily, missing-model, and manual catalog checks with an
  offline bundled fallback and no local usage disclosure.

## 2.0.0 — 2026-08-28

Infra Sentinel replaces the original Traffic Sentinel app with a resource-oriented desktop observer and a separately packaged local Agent.

### Highlights

- Tauri desktop shell with a health-only menu bar, native notifications, settings, runtime language switching, and independently failing resource modules.
- Canonical SQLite metrics with bounded retention, hourly current-day buckets, daily historical rollups, and local queries.
- Multiple VPS sources with per-host billing direction, Xray statistics, and daily usage thresholds.
- Local AI usage observation for Codex, OpenCode, Antigravity, and Infer Runtime, with provider/model composition and local API reference values.
- Infra Protocol discovery and observation for compatible local facilities.
- CPU, memory, disk, thermal, and best-effort per-App disk I/O observation on macOS.

### Changed

- Renamed the product from Traffic Sentinel to Infra Sentinel.
- Replaced the Objective-C menu-bar application with a Tauri desktop shell and packaged Python Agent.
- Replaced `threads.tokens_used` accounting with privacy-bounded Codex rollout JSONL request increments, fork-aware replay suppression, and durable deduplication markers.
- Added hourly Token and equivalent-value charts for the current day; older AI history is compacted by calendar day.
- Kept provider-reported cost, local API reference value, logical traffic, and VPS billing as separate accounting scopes.

### Upgrade notes

- The bundle identifier changed from `com.local.traffic-sentinel` to `com.glenzli.infra-sentinel`; macOS treats Infra Sentinel as a separate application.
- On first launch, Infra Sentinel copies the legacy `Traffic Sentinel/config.toml` only when the new configuration does not exist. The legacy directory remains untouched.
- Traffic Sentinel v1 sample history is not imported into the v2 metric store. Codex history is rebuilt only from rollout JSONL still visible on this machine.

### Privacy

- AI collectors read aggregate local metadata only; prompts, responses, task titles, project paths, and credentials are excluded.
- Documentation screenshots use generated anonymous data.

### Distribution

- macOS 13 or newer on Apple Silicon (`arm64`).
- Ad-hoc signed and not Apple-notarized; macOS may require manual first-launch confirmation.

## 1.0.0 — 2026-07-30

Traffic Sentinel's first public release.

### Highlights

- Exact local Mihomo totals with high-frequency, privacy-safe domain attribution.
- Proxy route, direct traffic, unattributed traffic, and recent rate trends.
- Optional Linux VPS billing reconciliation over short-lived read-only SSH.
- Optional per-user Xray StatsService accounting.
- Native bilingual macOS menu bar app, dashboard, settings, and alerts.

### Prebuilt app requirements

- macOS 13 or newer.
- Apple Silicon (`arm64`).
- Python 3.11 or newer.
- Ad-hoc signed and not Apple-notarized; macOS may ask the user to confirm the first launch.

The source tree remains the canonical distribution and can build a native app for the current Mac architecture.
