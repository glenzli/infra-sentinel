"""Qualify the packaged Windows Agent against native pipe fixtures and Codex.

All Sentinel state and other client profiles are isolated below .build. By
default Codex is synthetic too. --codex-home explicitly opts into read-only
real rollout metadata; only aggregate audit and Projection data are saved.
The pipe fixtures implement the three wire contracts, not the actual products.
"""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, time as day_time, timedelta
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from infra_sentinel.platform.host_process import background_subprocess_options
from infra_sentinel.resources.ai.codex_sampling import audit_codex_rollout_ledger, discover_codex_rollout_roots, load_codex_rollout_ledger
from infra_sentinel.resources.facilities.protocols import DEFAULT_ADAPTERS
from smoke_windows import command, fixture
from test_facility_protocols import snapshot
from test_windows_facilities import private_runtime, publish, registered
from windows_facility_fixtures import PipeServer


def source_metadata(roots):
    return {path: (info.st_size, info.st_mtime_ns) for root in roots for path in root.rglob("*.jsonl")
            if (info := path.stat())}


def codex_source(document):
    return next(source for source in document["infra"]["ai_usage"]["sources"] if source["source_id"] == "codex")


def check_codex(document, roots, state):
    source = codex_source(document)
    ledger = load_codex_rollout_ledger(state / "codex-rollout-ledger.json")
    audit = audit_codex_rollout_ledger(roots, ledger, timezone=datetime.now().astimezone().tzinfo)
    assert not audit.read_errors, "captured Codex source prefixes are unavailable"
    expected_models = Counter()
    for usage in audit.days.values():
        expected_models.update(usage.composition.models)
    assert source["usage"]["cumulative"]["tokens"] == sum(expected_models.values()), "cumulative token mismatch"
    assert {model["id"]: model["cumulative"]["tokens"] for model in source["models"]} == dict(expected_models), "model token mismatch"
    expected_days = {day: value.composition.total_tokens for day, value in audit.days.items() if value.composition.total_tokens}
    assert {row["date"]: row["tokens"] for row in source["history"]["daily"]} == expected_days, "daily history mismatch"
    for day, value in audit.days.items():
        assert ledger.days[day].composition.as_payload() == value.composition.as_payload(), "ledger composition mismatch"
    return audit, ledger.partial


def query_windows(state, now):
    result = {}
    midnight = datetime.combine(now.date(), day_time(), now.tzinfo)
    for name, days in (("today", 0), ("7d", 6), ("30d", 29), ("recorded", 729)):
        bucket = 3600 if name == "today" else 86400
        result[name] = command(state, "metrics.query", {
            "since_epoch": (midnight - timedelta(days=days)).timestamp(), "until_epoch": now.timestamp(),
            "resource_id": "ai_usage", "source_id": "codex", "metric": "ai.tokens.total",
            "bucket_seconds": bucket, "bucket_offset_seconds": int(midnight.timestamp()) % bucket if bucket == 86400 else 0,
        })
        assert not result[name].get("truncated"), "metric query was truncated"
    return result


def run_once(executable, directory, environment, roots, *, generation):
    state = directory / "state"
    runtime = directory / "facilities"
    private_runtime(runtime)
    environment["INFRA_PROTOCOL_RUNTIME_DIR"] = str(runtime)
    frames = queue.Queue()
    with ExitStack() as stack:
        for adapter, kind in zip(DEFAULT_ADAPTERS, ("pcp", "infer-runtime", "dev-mesh-observer")):
            value = registered(runtime, adapter, "", generation=generation)
            value = replace(value, kind=kind, path=runtime / "registrations" / f"{kind}--local.json")
            response = snapshot(adapter, value)
            response["extensions"][adapter.extension_key]["private_test_marker"] = "PRIVATE_NOT_PROJECTED"
            server = stack.enter_context(PipeServer(json.dumps(response).encode() + b"\n"))
            publish(replace(value, offers=(replace(value.offers[0], endpoint=server.endpoint),)))
        parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                  stdin=subprocess.PIPE, **background_subprocess_options())
        environment["INFRA_SENTINEL_PARENT_PID"] = str(parent.pid)
        try:
            child = subprocess.Popen([str(executable), "--config", str(directory / "config.toml"), "--watch"],
                                     env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     **background_subprocess_options())
        except BaseException:
            parent.communicate(timeout=10)
            raise

        def read_frames():
            for line in child.stdout:
                try:
                    frames.put(json.loads(line))
                except ValueError:
                    frames.put({"invalid_projection": True})

        reader = threading.Thread(target=read_frames, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 45
            document = None
            while time.monotonic() < deadline:
                candidate = frames.get(timeout=max(0.01, deadline - time.monotonic()))
                facilities = candidate["infra"]["facilities"]
                if facilities["healthy"] == 3:
                    document = candidate
                    break
            assert document, "packaged Agent did not observe all three pipe fixtures"
            assert all(item["binding"] == "infra.local.windows-named-pipe" for item in document["infra"]["facilities"]["items"])
            assert "PRIVATE_NOT_PROJECTED" not in json.dumps(document), "provider extension leaked"
            audit, partial = check_codex(document, roots, state)
            queries = query_windows(state, datetime.now().astimezone())
            # A normal settings update requests graceful Agent shutdown. The
            # second invocation must resume its ledger without replaying usage.
            settings = command(state, "configuration.get")["settings"]
            settings["policies"][0]["warning_mib"] += 1
            command(state, "configuration.update", settings)
            assert child.wait(timeout=20) == 0
            return document, queries, audit, partial
        finally:
            parent.communicate(timeout=10)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.terminate()
            if child.poll() is None:
                child.wait(timeout=10)
            reader.join(timeout=5)
            child.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, type=Path)
    parser.add_argument("--codex-home", type=Path, help="explicit read-only access to real Codex rollout metadata")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Run on native Windows")
    executable = args.agent.resolve(strict=True)
    directory = ROOT / ".build" / f"windows-integration-{uuid.uuid4().hex[:8]}"
    environment = fixture(directory)
    codex_home = args.codex_home.resolve(strict=True) if args.codex_home else Path(environment["CODEX_HOME"])
    environment["CODEX_HOME"] = str(codex_home)
    roots = discover_codex_rollout_roots((codex_home / "sessions", codex_home / "archived_sessions"))
    if not roots:
        parser.error("No readable Codex rollout roots")
    before = source_metadata(roots)
    document, queries, audit, partial = run_once(executable, directory, environment, roots, generation="first")
    restarted, _, restarted_audit, _ = run_once(executable, directory, environment, roots, generation="restarted")
    grew = before != source_metadata(roots)
    if not grew:
        assert codex_source(document)["usage"] == codex_source(restarted)["usage"], "restart replayed usage"
    for name, contents in (("audit.json", audit.as_payload()), ("projection.json", document), ("queries.json", queries)):
        (directory / name).write_text(json.dumps(contents, ensure_ascii=False), encoding="utf-8")
    ui = subprocess.run(["node", str(ROOT / "ui" / "scripts" / "verify-codex-projection.mjs"), str(directory)],
                        cwd=ROOT / "ui", capture_output=True, text=True,
                        **background_subprocess_options())
    if ui.returncode:
        raise AssertionError(ui.stderr)
    report = {
        "status": "ok", "codex_data": "real-read-only" if args.codex_home else "synthetic",
        "coverage": audit.as_payload()["coverage"],
        "tokens": codex_source(document)["usage"]["cumulative"]["tokens"],
        "models": len(codex_source(document)["models"]),
        "turn_timing_models": sorted({group["sample"]["model"] for group in codex_source(document).get("details", [])
                                      if group["id"] == "turn-performance"}),
        "reference": "independent-replay-of-checkpoint-file-prefixes", "source_grew_during_check": grew,
        "capture_had_pending_tail": partial,
        "restart_captured_tokens": sum(day.composition.total_tokens for day in restarted_audit.days.values()),
        "restart_no_replay": True, "pipe_contract_fixtures": len(DEFAULT_ADAPTERS),
        "ui": json.loads(ui.stdout),
    }
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(directory / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
