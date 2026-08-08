# Infra Sentinel Tauri UI

This directory owns the cross-platform desktop shell only.

- `src/` renders Agent Projection data and submits typed commands.
- `src-tauri/src/agent_bridge.rs` is the sole native bridge. It can read the
  versioned Projection and write only allowlisted Agent command documents.
- Python `infra_agent.py` remains the owner of collectors, SQLite, policies,
  notifications, state, and the Projection contract.

During this first migration slice the existing macOS app continues to own the
Agent process lifecycle, so `npm run tauri dev` attaches to the same local
Agent state. The next slice packages the Python Agent as a per-platform
sidecar and transfers lifecycle ownership to Tauri; the frontend contract will
not change.

```text
npm install
npm run tauri dev
```

The bridge intentionally does not grant the WebView filesystem or shell
permissions. It exposes only `read_projection`, `session.reset`, and
`metrics.query` through the existing local Agent protocol.
