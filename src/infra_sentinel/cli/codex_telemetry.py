"""Human-invoked opt-in/status/disable for the local Codex telemetry source."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from infra_sentinel.app.configuration import desktop_state_directory
from infra_sentinel.platform.process_lock import acquire_process_lock
from infra_sentinel.resources.ai.codex_telemetry.receiver import DEFAULT_PORT
from infra_sentinel.resources.ai.codex_telemetry.setup import configure


def telemetry_state_directory(config_path: Path | None = None) -> Path:
    """Match the desktop app's data_local_dir, never a bundle extraction path."""
    return desktop_state_directory(config_path)


def run_action(action: str, state_dir: Path, codex_config: Path | None = None, *, port: int = DEFAULT_PORT) -> dict:
    codex_config = codex_config or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if action == "status":
        return configure(action, state_dir, codex_config, port=port)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = acquire_process_lock(state_dir / "codex-telemetry-setup.lock")
    if lock is None:
        raise ValueError("Another telemetry setup is in progress")
    try:
        return configure(action, state_dir, codex_config, port=port)
    finally:
        lock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Infra Sentinel's local Codex request telemetry")
    parser.add_argument("action", choices=("enable", "disable", "status"))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--codex-config", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        result = run_action(args.action, args.state_dir or telemetry_state_directory(), args.codex_config, port=args.port)
    except (OSError, ValueError):
        # Config parse errors can contain snippets or credentials; never echo them.
        print(json.dumps({"status": "error", "reason": "setup-rejected; existing configuration preserved"}))
        return 1
    print(json.dumps({"status": "ok", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
