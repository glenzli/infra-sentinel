# Infra Sentinel Architecture

[中文](architecture.zh-CN.md)

The repository separates portable resource semantics from operating-system
integration and executable packaging:

```text
bin/                         executable wrappers and release scripts only
src/infra_sentinel/
  app/                       Agent lifecycle, configuration, sidecar protocol
  core/                      collector, metric, registry, projection contracts
  metrics/                   SQLite persistence and bounded queries
  platform/                  narrow process/OS integration contracts
  resources/
    ai/                      local AI usage provider adapters
    facilities/              Infra Protocol discovery and provider adapters
    network/                 Mihomo, VPS, Xray, accounting, and policies
    system/                  capability-driven host collector
      backends/              one independently validated adapter per OS
    upstream/                official provider status observation
ui/                          platform-neutral WebView and native Tauri shell
tests/                       contract and behavior tests
```

`bin/infra_agent.py`, `bin/configuration.py`, `bin/report.py`, and
`bin/snapshot.py` contain no product logic. They only make the source package
available and invoke its typed entry point. PyInstaller packages the same
`infra_sentinel` package that tests import.

## Platform boundary

- Portable collectors consume protocols and declared capabilities; they do
  not branch on the host OS.
- Platform selection happens once at the adapter boundary. Unselected host
  backends are not imported.
- Missing capabilities stay missing. They are never converted to zero,
  healthy, or an inferred equivalent.
- A backend can be implemented in the shared tree, but it becomes supported
  only after its behavior, lifecycle, packaging, and sleep/resume path are
  validated on that target platform.
- User-selected executable or database locations enter through validated
  configuration fields. Resource collectors do not scan arbitrary disks.

The same rule applies to the native shell: Rust owns notifications, URL
opening, sidecar lifecycle, and target packaging; the WebView sees only the
versioned Projection and allowlisted commands.

## Adding a resource provider

1. Implement one source adapter under the resource family that owns its
   protocol, checkpoints, and error semantics.
2. Return canonical metric points and a privacy-safe current snapshot through
   the core collector contract.
3. Register the collector in the Agent composition root.
4. Extend Projection/UI only for stable resource semantics, not provider-
   specific transport details.
5. Add focused adapter tests and one projection-level integration test.

This keeps future Windows and Linux work target-local: those implementations
fill existing adapter contracts instead of adding platform conditionals to
accounting, policy, storage, or UI code.
