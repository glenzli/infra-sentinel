"""Explicit, reversible user-level opt-in; never called by collection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import tempfile
import tomllib

from .receiver import DEFAULT_PORT, SETTINGS_FILENAME, read_settings

BEGIN = "# BEGIN Infra Sentinel local Codex telemetry"
END = "# END Infra Sentinel local Codex telemetry"


def private_write(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + "-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(raw)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def managed_block(settings: dict) -> str:
    endpoint = f"http://127.0.0.1:{settings['port']}/v1/logs"
    return f'''{BEGIN}
[otel]
log_user_prompt = false
trace_exporter = "none"

[otel.exporter.otlp-http]
endpoint = {json.dumps(endpoint)}
protocol = "json"
headers = {{ Authorization = {json.dumps('Bearer ' + settings['token'])} }}
{END}
'''


def _base_config(raw: str, settings: dict | None) -> str:
    if BEGIN not in raw and END not in raw:
        if "otel" in tomllib.loads(raw):
            raise ValueError("Existing Codex telemetry configuration must be preserved; choose an explicit integration first")
        return raw
    if settings is None or raw.count(BEGIN) != 1 or raw.count(END) != 1:
        raise ValueError("Cannot identify the owned telemetry configuration")
    block = managed_block(settings)
    if block not in raw:
        raise ValueError("Owned telemetry configuration was changed; refusing to overwrite it")
    base = raw.replace(block, "", 1)
    if "otel" in tomllib.loads(base):
        raise ValueError("Another telemetry configuration is present")
    return base


def configure(action: str, state_dir: Path, codex_config: Path, *, port: int = DEFAULT_PORT) -> dict:
    """Change only our delimited block, preserving all other user settings."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid-listener-port")
    codex_config = codex_config.resolve()
    raw = codex_config.read_text(encoding="utf-8") if codex_config.exists() else ""
    if len(raw.encode()) > 1024 * 1024:
        raise ValueError("Codex configuration is too large")
    previous = read_settings(state_dir)
    if action == "status":
        return {"configured": previous is not None and managed_block(previous) in raw,
                "listener_enabled": previous is not None, "transport": "authenticated-loopback-otlp-json-logs"}
    base = _base_config(raw, previous)
    if action == "disable":
        if previous is not None:
            private_write(codex_config, base.encode())
            (state_dir / SETTINGS_FILENAME).unlink()
        return {"configured": False, "restart_required": True}
    if action != "enable":
        raise ValueError("unknown-telemetry-action")
    settings = previous or {"schema": 1, "port": port, "token": secrets.token_urlsafe(32)}
    if previous is not None and previous["port"] != port:
        raise ValueError("Existing listener uses another port")
    candidate = base.rstrip() + "\n\n" + managed_block(settings)
    tomllib.loads(candidate)
    # Save a private first-enable backup before touching the user's file.
    backup = codex_config.with_name(codex_config.name + ".infra-sentinel-before-telemetry")
    if previous is None:
        if backup.exists():
            backup = backup.with_name(backup.name + "-" + secrets.token_hex(4))
        private_write(backup, raw.encode())
    try:
        private_write(state_dir / SETTINGS_FILENAME, (json.dumps(settings) + "\n").encode())
        # Detect a concurrent edit between preparation and publication.
        current = codex_config.read_text(encoding="utf-8") if codex_config.exists() else ""
        if current != raw:
            raise ValueError("Codex configuration changed during setup")
        private_write(codex_config, candidate.encode())
    except Exception:
        if previous is None:
            (state_dir / SETTINGS_FILENAME).unlink(missing_ok=True)
        raise
    return {"configured": True, "restart_required": True,
            "transport": "authenticated-loopback-otlp-json-logs", "raw_payload_retention": False}
