# Infra Sentinel

**English** | [中文](README.zh-CN.md)

Infra Sentinel is a local-first observability dashboard for personal AI infrastructure. It currently covers two metered resource categories and the health of participating local facilities:

- **Network**: local Mihomo traffic, domain and proxy-route attribution, Linux VPS billable traffic, and Xray per-user logical traffic;
- **AI usage**: local Token records from OpenCode and Codex, model composition, consumption rate, and Agent activity;
- **Local facilities**: automatically discovered, protocol-bounded health projections for compatible runtimes and services.

It also discovers participating local infrastructure facilities through the
[Infra Protocol](https://github.com/glenzli/infra-protocol) discovery contract. Each facility appears
as an independent resource card with a bounded, read-only detail view. Facility-specific diagnosis
and operations remain in its native Console, opened in the system browser.

The menu bar icon only communicates overall health. Open the app to inspect resource details, trends, source discrepancies, and alerts.

Infra Sentinel does not capture packets, read prompts or response bodies, inspect URL paths or project files, terminate processes, delete files, disconnect the network, or modify proxy configuration.

![Infra Sentinel network and AI resource modules](assets/overview-en.png)

![Infra Sentinel AI usage](assets/ai-usage-en.png)

> Screenshots use anonymous demo data. They contain no real hostnames, SSH aliases, IP addresses, accounts, or local paths.

## Questions it answers

Infra Sentinel does not force bytes, Tokens, and alerts into a synthetic “score.” Each resource module keeps its native unit and measurement semantics so it can answer questions such as:

- Which resource category consumed the most today?
- Which Agent, model, domain, proxy route, or VPS produced that usage?
- Is the current growth rate abnormal?
- Why do local observations, proxy logical traffic, and VPS billable traffic differ?
- Is a data source unavailable, or is its observation window incomplete?
- Which local infrastructure facilities are healthy or degraded, and where is their native Console?

When data cannot be obtained reliably, the interface reports it as unknown, hides the unavailable module, or marks the source unhealthy. It does not present an inferred value as a bill.

## Network module

Local network accounting uses a Mihomo / Clash Meta-compatible core as its source of truth:

- Automatically discovers an accessible Mihomo Unix socket for the current user;
- Persists one interval every 5 seconds and, by default, reads active connections every 250 ms within that interval;
- Uses adjacent deltas of `uploadTotal + downloadTotal` as the exact local total;
- Attributes traffic by registrable domain and actual `chains`, including proxy, `DIRECT`, rejected, and unknown routes;
- Preserves short-lived connections missed between polling points as unattributed traffic instead of silently dropping them.

These invariants always hold:

```text
Attributed domains + unattributed = exact Mihomo delta
Proxy + DIRECT + rejected + unknown + unattributed = exact Mihomo delta
```

Remote hosts are optional. Every host maintains an independent baseline, billing direction, and daily usage thresholds:

- Accesses a Linux VPS briefly and read-only through a Host alias in `~/.ssh/config`;
- Automatically selects the public interface under `/sys/class/net` and reads RX/TX bytes and packet counts;
- Can optionally read Xray StatsService when it listens only on remote `127.0.0.1:10085`;
- Can aggregate billable traffic across VPS hosts, while ratios remain meaningful only within the same host and coverage scope.

VPS accounting supports either bidirectional `RX + TX` billing or `TX`-only billing. Xray user logical traffic and interface-level billable traffic represent different physical layers and are never added together as one total.

## AI usage module

The AI module reads only aggregate metadata already stored by local clients. Every Provider implements a shared usage contract: today’s usage, readable historical total, model dimensions, and source-specific diagnostics.

### OpenCode

- Preferentially aggregates Token metadata from OpenCode Desktop assistant messages through read-only SQLite access;
- Can expose input, output, reasoning, cache, model, message count, and Provider-reported cost;
- Falls back to a compatible `opencode stats --days 0 --models` only when the Desktop database is unavailable;
- Uses persistent counter checkpoints so restarting the app does not write the same daily increment twice.

### Codex

- Reads main-task Token counters, model metadata, and derived topology from `~/.codex/state_5.sqlite` in read-only mode;
- Measures today’s usage from Sentinel’s first local baseline of the day;
- Aggregates positive deltas from each main task by model, preserving equality between the Codex total and the sum of model totals;
- Shows main tasks, sub-agents, recent activity, and maximum derivation depth without reading task titles or content.

OpenCode calendar-day totals and Codex local-baseline windows may differ. The interface shows the observation coverage for every source directly. AI summaries are local trend measurements, not account billing for ChatGPT, Codex, or any API Provider.

## Analysis views

Network and AI usage share the same observation structure:

- Fixed summary: today’s observation, historical or billable total, and collection coverage;
- Time ranges: Today, 7 days, 30 days, and All history;
- Composition: Agent, model, domain, route, or VPS breakdowns;
- Rate trends: calculated from actual sampling time without presenting backfilled usage as a real-time spike;
- Daily history: stacked bars that preserve both total usage and composition.

Historical queries run through a dedicated read-only channel and do not wait for the 5-second network sampling cycle. The UI consumes bounded query results and never accesses SQLite or arbitrary files directly.

## Local facility discovery

Participating services publish a short-lived, owner-only
[`infra.discovery.registration@20260810.1`](https://github.com/glenzli/infra-protocol) lease. Sentinel
discovers these leases without scanning ports or guessing process names, intersects exact protocol
versions and bindings, and connects to the selected service. Discovery carries no metrics, Console
URL, request envelope, or control authority.

Current verified integrations are
[Paged Context Protocol (PCP)](https://github.com/glenzli/paged-context-protocol) through
`pcp.runtime.observer@20260810.1`, and
[Infer Runtime](https://github.com/glenzli/infer-runtime) through
`infer-runtime.status@20260810.1`. These application protocols remain independent; Infra Protocol
only standardizes discovery. Sentinel implements one adapter for each and normalizes only bounded
status, metrics, issues, observation time, and an optional loopback **Open Console** link into its
private UI projection. Every discovered facility is presented as its own first-class module and
detail view. Unknown protocols are ignored rather than guessed compatible. See
[facility discovery](docs/facility-discovery.md).

## Architecture

```text
Mihomo / VPS / Xray / OpenCode / Codex → Collectors → SQLite metrics ┐
Local facilities → Infra Protocol discovery → provider adapters ─────┤
                                                                     ↓
                                              Versioned Projection + commands
                                                                     ↓
                                                 Tauri UI / notifications
```

- The Python Agent is the sole owner of sampling, accounting, storage, policies, and Projection generation;
- SQLite WAL stores interval counters with stable identity keys to prevent duplicate accounting after restart or backfill;
- The Tauri WebView can only read a versioned Projection and submit allowlisted commands;
- The Rust bridge exposes no arbitrary file, shell, or SQL access;
- Collector failures are isolated so one unavailable source cannot block other resources;
- Facility discovery and provider-protocol I/O have their own lifecycle and never block resource sampling.

The current local Projection protocol version is `20260810.1`. Metric queries support minute, 5-minute, hourly, and daily aggregation, with bounded time ranges and result counts.

## Support matrix

Officially supported:

- macOS 13 or later;
- A Mihomo / Clash Meta-compatible read-only `/connections` API;
- Sockets created by Clash Verge service mode under the controlled `/tmp/verge` directory;
- Linux VPS hosts accessed through OpenSSH Host aliases;
- Optional Xray StatsService per-user statistics;
- The OpenCode Desktop local session database or compatible CLI;
- The Codex local state database;
- Local facilities publishing compatible Infra Protocol discovery offers for the supported
  [PCP](https://github.com/glenzli/paged-context-protocol) or
  [Infer Runtime](https://github.com/glenzli/infer-runtime) protocol.

Not currently supported:

- sing-box, Surge, or arbitrary TCP controllers;
- Remote interface accounting on non-Linux systems;
- Per-user server statistics from services other than Xray;
- ChatGPT/Codex subscription limits or generic API account balances;
- Screen time, prompt analysis, or full-disk file scanning.

Accidental interface compatibility does not imply official support.

## Installation and build

The project does not require an Apple Developer certificate. Prebuilt apps use ad-hoc signing and are not notarized by Apple. On first launch, macOS may require you to right-click and select **Open**, or confirm the app under **System Settings → Privacy & Security**.

Building from source requires:

- macOS 13+ and Xcode Command Line Tools;
- The Rust toolchain;
- Node.js LTS and npm;
- Python 3.11+ and PyInstaller.

```sh
git clone git@github.com:glenzli/infra-sentinel.git
cd infra-sentinel
python3 -m pip install pyinstaller
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

The build script installs frontend dependencies from `package-lock.json`, packages the Python Agent as a sidecar for the current Mac architecture, produces the Tauri `.app` and DMG, and verifies the ad-hoc signature.

Maintainers can generate a release ZIP and SHA-256 checksum with:

```sh
./bin/package-release.sh
```

## Configuration

The first launch creates a default configuration. Alerts and remote hosts are then managed from the app’s **Settings** page:

```text
~/Library/Application Support/Infra Sentinel/config.toml
```

Local Mihomo discovery requires no address configuration. For a remote host, enter only a Host alias already defined in `~/.ssh/config`. The app does not store private keys, passwords, or real host addresses.

```sshconfig
Host edge-a
  HostName vps.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519
```

To enable Xray user statistics, assign a unique `email` label to every client, enable per-user uplink and downlink statistics, and keep StatsService bound to the remote loopback address. Exact fields follow Xray’s own configuration format; Infra Sentinel never modifies server configuration automatically.

Configuration contracts use `YYYYMMDD.revision`. The current version is `20260808.4`. The default configuration contains no real host or account.

## Local data and privacy

Runtime data is stored under:

```text
~/Library/Application Support/Infra Sentinel/state/
```

This includes the SQLite metric store, counter checkpoints, health state, bounded command results, rolling logs, and necessary legacy network JSONL. State may contain aggregate domains, Xray client labels, model names, and user-defined display names, but does not store:

- Prompts, responses, message bodies, or command content;
- URL paths, query parameters, request headers, or network payloads;
- Project paths, file contents, Git metadata, or task titles;
- SSH private keys, passwords, API keys, or authentication credentials;
- Packet captures.

All collection, storage, and analysis happen locally by default.

## Development verification

```sh
python3 -m unittest discover -s tests -v
cd ui && npm run build
cd src-tauri && cargo test --offline
```

See [ROADMAP.md](ROADMAP.md) for architectural evolution and the admission criteria for new metrics. Release history is available in [CHANGELOG.md](CHANGELOG.md).
