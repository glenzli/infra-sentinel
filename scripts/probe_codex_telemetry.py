#!/usr/bin/env python3
"""Opt-in developer probe: controlled Codex request through the real receiver.

Runs one ephemeral synthetic turn, including a two-second tool wait. A private
temporary user-level profile is removed afterwards. No raw OTLP bodies, Codex
messages, credentials, or upstream identifiers are written to the report.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from infra_sentinel.resources.ai.codex_telemetry.protocol import parse_logs
from infra_sentinel.resources.ai.codex_telemetry.receiver import CodexTelemetryService, SETTINGS_FILENAME
from infra_sentinel.resources.ai.codex_telemetry.setup import managed_block


def probe(binary: Path) -> dict:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    profile_name = "infra-sentinel-probe-" + secrets.token_hex(6)
    profile = codex_home / (profile_name + ".config.toml")
    observed = []
    observed_lock = threading.Lock()
    def parse(raw, secret, now):
        events, count = parse_logs(raw, secret, now)
        with observed_lock:
            observed.extend(events)
        return events, count
    with tempfile.TemporaryDirectory(prefix="infra-codex-telemetry-probe-") as directory:
        state_dir = Path(directory)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        settings = {"schema": 1, "port": port, "token": secrets.token_urlsafe(32)}
        listener = state_dir / SETTINGS_FILENAME
        listener.write_text(json.dumps(settings)); listener.chmod(0o600)
        text = managed_block(settings)
        with profile.open("x", encoding="utf-8") as stream:
            profile.chmod(0o600)
            stream.write(text)
        service = CodexTelemetryService(state_dir)
        process = None
        try:
            with patch("infra_sentinel.resources.ai.codex_telemetry.receiver.parse_logs", side_effect=parse):
                service.start()
                if service.status != "waiting":
                    raise RuntimeError("probe listener unavailable")
                command = [str(binary), "exec", "--profile", profile_name,
                           "--disable", "hooks", "--disable", "plugins", "--disable", "apps", "--disable", "multi_agent",
                           "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--cd", directory,
                           "--model", "gpt-6-luna", "--json", "-c", 'model_reasoning_effort="low"',
                           "-c", "mcp_servers.node_repl.enabled=false", "-c", "mcp_servers.computer-use.enabled=false",
                           "-c", "mcp_servers.openaiDeveloperDocs.enabled=false",
                           'Use exactly one shell command: python3 -c "import time; time.sleep(2)". '
                           'After it finishes, reply with the numbers 1 through 40 separated by spaces. '
                           'No other tools and no file changes.']
                environment = os.environ.copy()
                for key in ("NO_PROXY", "no_proxy"):
                    environment[key] = ",".join(filter(None, [environment.get(key, ""), "127.0.0.1", "localhost"]))
                process = subprocess.run(command, capture_output=True, timeout=120, env=environment)
                time.sleep(.5)
                details = service.details()
        finally:
            service.stop()
            if profile.exists() and profile.read_text() == text:
                profile.unlink()
        if process is None or process.returncode:
            raise RuntimeError("Codex probe failed; raw client output omitted")
        reported_output = None
        for line in process.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                reported_output = event.get("usage", {}).get("output_tokens")
        completions = [event for event in observed if event.kind == "complete" and event.output_tokens]
        request_groups = [group for group in details if group["id"] == "request-performance"]
        assert reported_output is not None and sum(event.output_tokens for event in completions) == reported_output
        assert len(completions) >= 2 and service.state.snapshot()["matched_requests"] >= 2
        assert request_groups
        rows = []
        starts = {}
        for event in sorted(observed, key=lambda event: event.epoch):
            key = (event.session, event.model)
            if event.kind == "start":
                starts[key] = event
            elif event.kind == "complete":
                start = starts.pop(key, None)
                if start and event.output_tokens:
                    elapsed = (event.epoch - start.epoch) * 1000 + start.duration_ms
                    rows.append({"model": event.model, "output_tokens": event.output_tokens,
                                 "duration_ms": elapsed, "reported_ttft_ms": event.first_token_ms,
                                 "request_tokens_per_second": event.output_tokens * 1000 / elapsed})
        return {"status": "ok", "transport": "authenticated-loopback-otlp-json-logs", "trace_export": False,
                "turn_output_tokens": reported_output, "matched_requests": rows,
                "receiver": service.state.snapshot(), "raw_payload_retention": False,
                "user_config_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = probe(args.codex)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
