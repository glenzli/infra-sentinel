# Changelog

## Unreleased

Infra Sentinel now presents Network and AI usage as peer resource modules.

### Added

- Tauri desktop shell with a health-only menu bar, native notifications, settings, and runtime language switching.
- Canonical SQLite metrics, bounded local queries, and independently failing collectors.
- Multiple VPS sources with per-host billing direction, Xray statistics, and daily usage thresholds.
- OpenCode and Codex local Token observation with provider/model composition, rate trends, and daily history.

### Changed

- Renamed the product from Traffic Sentinel to Infra Sentinel.
- Replaced traffic-heavy menu-bar text with quiet health states and a dense resource dashboard.
- Clarified observed-today, local-history, logical-traffic, and VPS-billing scopes throughout the UI.

### Privacy

- AI collectors read aggregate local metadata only; prompts, responses, task titles, project paths, and credentials are excluded.
- Documentation screenshots use generated anonymous data.

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
