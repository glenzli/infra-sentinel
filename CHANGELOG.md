# Changelog

## Unreleased

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
