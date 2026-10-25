"""Exercise the real Windows Agent or desktop using isolated synthetic state.

Uses the production Projection/command contract. It never opens the user's
AI histories, SSH configuration, or local facility endpoints. Public status
and price-catalog requests retain their normal read-only behavior.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from infra_sentinel.app.protocol import COMMAND_SCHEMA, PROJECTION_SCHEMA  # noqa: E402
from infra_sentinel.platform.host_process import background_subprocess_options, process_is_running  # noqa: E402


def wait_for(read, *, timeout: float = 45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Windows smoke check timed out")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def command(state: Path, kind: str, payload: dict | None = None) -> dict:
    identifier = str(uuid.uuid4())
    directory = state / "commands"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{identifier}.tmp"
    temporary.write_text(json.dumps({
        "schema": COMMAND_SCHEMA, "id": identifier, "type": kind,
        "payload": payload or {}, "requested_at": datetime.now().astimezone().isoformat(),
    }), encoding="utf-8")
    temporary.replace(directory / f"{identifier}.request.json")
    result = wait_for(lambda: load_json(directory / f"{identifier}.result.json"))
    assert result["status"] == "ok", result
    return result["payload"]


def check_projection(document: dict) -> None:
    assert document["schema"] == PROJECTION_SCHEMA
    system = document["infra"]["system"]
    assert system["platform"] == "windows" and system["available"]
    assert set(system["capabilities"]) == {"cpu.utilization", "memory.capacity", "disk.capacity"}
    assert system["memory"]["total_bytes"] > 0 and system["disk"]["total_bytes"] > 0
    codex = next(item for item in document["infra"]["ai_usage"]["sources"] if item["source_id"] == "codex")
    assert codex["usage"]["cumulative"]["tokens"] == 140
    assert "SMOKE_PRIVATE_CONTENT" not in json.dumps(document)
    mihomo = next(item for item in document["infra"]["sources"] if item["id"] == "local-mihomo")
    assert not mihomo["enabled"] and mihomo["status"] == "disabled"
    assert all(item["source_id"] != "local-mihomo" for item in document["infra"]["diagnostics"]["active"])


def fixture(directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True)
    profile = directory / "profile"
    sessions = profile / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    stamp = datetime.now().astimezone().isoformat()
    usage = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 40,
             "reasoning_output_tokens": 0, "total_tokens": 140}
    rows = [
        ("session_meta", {"id": "smoke-session", "thread_source": "user"}),
        ("turn_context", {"model": "gpt-5.6-sol"}),
        ("event_msg", {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}}),
        ("response_item", {"type": "message", "content": "SMOKE_PRIVATE_CONTENT"}),
    ]
    (sessions / "rollout-smoke.jsonl").write_text("".join(
        json.dumps({"type": kind, "timestamp": stamp, "payload": payload}) + "\n" for kind, payload in rows
    ), encoding="utf-8")
    shutil.copyfile(ROOT / "config.example.toml", directory / "config.toml")
    environment = dict(os.environ)
    for name in ("INFRA_SENTINEL_STATIC_PROJECTION", "INFRA_SENTINEL_PARENT_PID"):
        environment.pop(name, None)
    environment.update({
        "USERPROFILE": str(profile), "HOME": str(profile),
        "LOCALAPPDATA": str(profile / "local"), "APPDATA": str(profile / "roaming"),
        "CODEX_HOME": str(profile / ".codex"), "DSH_HOME": str(profile / ".dsh"),
        "INFRA_PROTOCOL_RUNTIME_DIR": str(directory / "absent-facilities"),
        "INFRA_SENTINEL_STATE_DIR": str(directory / "state"),
        "INFRA_SENTINEL_APP_NOTIFICATIONS": "1",
        "INFRA_SENTINEL_PROJECTION_STREAM": "stdio",
        "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
        "TEMP": str(directory), "TMP": str(directory),
    })
    return environment


def agent_smoke(executable: Path, directory: Path, environment: dict[str, str]) -> None:
    parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                              stdin=subprocess.PIPE, **background_subprocess_options())
    environment["INFRA_SENTINEL_PARENT_PID"] = str(parent.pid)
    children = []
    frames: queue.Queue = queue.Queue()

    def read_frames(stream):
        for line in stream:
            frames.put(json.loads(line))

    def launch():
        child = subprocess.Popen([str(executable), "--config", str(directory / "config.toml"), "--watch"],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 **background_subprocess_options())
        children.append(child)
        threading.Thread(target=read_frames, args=(child.stdout,), daemon=True).start()
        return child

    try:
        first = launch()
        check_projection(frames.get(timeout=45))
        settings = command(directory / "state", "configuration.get")["settings"]
        settings["policies"][0]["warning_mib"] += 1
        command(directory / "state", "configuration.update", settings)
        assert first.wait(timeout=20) == 0 and parent.poll() is None
        while not frames.empty():
            frames.get_nowait()
        second = launch()
        check_projection(frames.get(timeout=45))
        assert command(directory / "state", "configuration.get")["settings"] == settings
        # Closing the recorded parent must stop the frozen bootstrap + Agent.
        parent.communicate(timeout=10)
        assert second.wait(timeout=20) == 0
        check_projection(load_json(directory / "state" / "projection.json"))
    finally:
        if parent.poll() is None:
            parent.communicate(timeout=10)
        for child in children:
            try:
                child.wait(timeout=20)
            finally:
                if child.poll() is None:
                    child.kill()
                if child.stdout:
                    child.stdout.close()


def lock_pid(state: Path) -> int | None:
    try:
        with (state / "infra-agent.lock").open("rb") as source:
            source.seek(1)  # Do not read Windows' locked byte.
            return int(source.read().strip())
    except (OSError, ValueError):
        return None


def desktop_smoke(executable: Path, directory: Path, environment: dict[str, str]) -> None:
    child = subprocess.Popen([str(executable)], env=environment, cwd=executable.parent,
                             **background_subprocess_options())
    agent_pid = None
    state = directory / "state"
    try:
        check_projection(wait_for(lambda: load_json(state / "projection.json")))
        assert child.poll() is None
        agent_pid = wait_for(lambda: lock_pid(state))
        duplicate = subprocess.Popen([str(executable)], env=environment, cwd=executable.parent,
                                     **background_subprocess_options())
        try:
            assert duplicate.wait(timeout=15) == 0
        finally:
            if duplicate.poll() is None:
                duplicate.kill()
                duplicate.wait(timeout=10)
        assert child.poll() is None and lock_pid(state) == agent_pid
        settings = command(state, "configuration.get")["settings"]
        settings["policies"][0]["warning_mib"] += 1
        command(state, "configuration.update", settings)
        agent_pid = wait_for(lambda: (pid if (pid := lock_pid(state)) != agent_pid else None))
        assert command(state, "configuration.get")["settings"] == settings
        check_projection(load_json(state / "projection.json"))
    finally:
        child.terminate()  # Exercise unexpected desktop loss as well as restart.
        child.wait(timeout=10)
        if agent_pid:
            wait_for(lambda: not process_is_running(agent_pid), timeout=25)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--agent", type=Path)
    mode.add_argument("--desktop", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Run this integration check on Windows")
    directory = ROOT / ".build" / f"windows-smoke-{uuid.uuid4().hex[:8]}"
    environment = fixture(directory)
    executable = (args.agent or args.desktop).resolve(strict=True)
    if args.agent:
        agent_smoke(executable, directory, environment)
    else:
        (directory / "config.toml").unlink()  # Exercise native first-run bootstrap.
        desktop_smoke(executable, directory, environment)
    print(json.dumps({"status": "ok", "mode": "agent" if args.agent else "desktop",
                      "tokens_after_restart": 140, "state": str(directory)}))


if __name__ == "__main__":
    main()
