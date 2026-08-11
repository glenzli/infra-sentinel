# Infra Sentinel Tauri UI

This directory owns the cross-platform desktop shell only.

- `src/` renders Agent Projection data and submits typed commands.
- `src-tauri/src/agent_bridge.rs` is the sole native bridge. It can read the
  versioned Projection and command results, and write only allowlisted Agent
  command documents.
- Python `src/infra_sentinel/app/agent.py` remains the composition root for
  collectors, SQLite, policies, event state, and the Projection contract;
  `bin/infra_agent.py` is only the packaged executable wrapper.
- Rust/Tauri owns desktop lifecycle, URL opening, menu-bar behavior, and native
  notifications. Platform branches stay in this native shell rather than the
  WebView or resource projections.

Tauri owns the lifetime of a packaged Agent sidecar. On first launch it creates
the local `config.toml` from the bundled example, starts the Agent, and restarts
it after a successful configuration update. The frontend contract does not
change: it still sees only the versioned Projection and typed command results.

```text
npm install
npm run tauri dev
```

The bridge intentionally does not grant the WebView filesystem or shell
permissions. It exposes only `read_projection` plus the public
`session.reset`, `metrics.query`, `configuration.get`, and
`configuration.update` Agent commands. Configuration writes are validated by
the Python configuration owner; on success, the Agent exits cleanly so its
supervisor can restart it with the new runtime configuration.

The UI is capability-driven. A system backend may publish only the metrics its
platform can verify; unsupported fields are omitted from the view instead of
being rendered as zero, healthy, or inferred equivalents.
