#!/usr/bin/env python3
"""Privacy-safe Codex lifecycle hook capture and one-click hook installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any


HOOK_EVENT_SCHEMA = 1
HOOK_MARKER = "--traffic-sentinel-capture"
HOOK_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
)
READ_COMMANDS = {
    "cat",
    "find",
    "grep",
    "head",
    "less",
    "ls",
    "rg",
    "sed",
    "stat",
    "tail",
    "wc",
}
READ_TOOL_TERMS = ("fetch", "find", "get", "list", "open", "read", "search", "view")
DEFAULT_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Codex Traffic Sentinel"
DEFAULT_CODEX_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
CODEX_EXECUTABLE_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)


def compact_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8", errors="replace")


def command_is_read_like(command: str) -> bool:
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        tokens = command.strip().split()
    if not tokens:
        return False
    executable = Path(tokens[0]).name.casefold()
    if executable in READ_COMMANDS:
        return True
    return executable == "git" and len(tokens) > 1 and tokens[1].casefold() in {"diff", "log", "show", "status"}


def tool_is_read_like(tool_name: str, tool_input: Any) -> bool:
    normalized = tool_name.casefold()
    if normalized == "bash" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        return isinstance(command, str) and command_is_read_like(command)
    return any(term in normalized for term in READ_TOOL_TERMS)


def build_privacy_safe_record(payload: dict[str, Any], epoch: float | None = None) -> dict[str, Any]:
    """Reduce one hook payload to counters and opaque identifiers only.

    Prompt text, command text, paths, tool arguments, tool responses, and
    assistant messages are intentionally never copied into the returned value.
    """
    event = str(payload.get("hook_event_name", "")).strip()
    record: dict[str, Any] = {
        "schema": HOOK_EVENT_SCHEMA,
        "epoch": float(time.time() if epoch is None else epoch),
        "event": event,
        "session_id": str(payload.get("session_id", "")),
        "model": str(payload.get("model", "")),
    }
    for key in ("turn_id", "agent_id", "agent_type", "source", "trigger", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            record[key] = value
    if event == "PostToolUse":
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input")
        tool_response = payload.get("tool_response")
        input_bytes = compact_json_bytes(tool_input)
        response_bytes = compact_json_bytes(tool_response)
        read_like = tool_is_read_like(tool_name, tool_input)
        record.update({
            "tool_name": tool_name,
            "tool_input_bytes": len(input_bytes),
            "tool_response_bytes": len(response_bytes),
            "read_like": read_like,
        })
        if read_like:
            fingerprint_source = tool_name.encode("utf-8", errors="replace") + b"\0" + input_bytes
            record["input_fingerprint"] = hashlib.sha256(fingerprint_source).hexdigest()
    return record


def sentinel_is_live(state_dir: Path, now: float | None = None) -> bool:
    health = state_dir / "health.json"
    try:
        age = (time.time() if now is None else now) - health.stat().st_mtime
    except OSError:
        return False
    return age <= 30.0


def capture_from_stdin(state_dir: Path, *, require_live_sentinel: bool = True) -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if require_live_sentinel and not sentinel_is_live(state_dir):
        return 0
    record = build_privacy_safe_record(payload)
    inbox = state_dir / "codex-hook-inbox"
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        final_path = inbox / f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}.json"
        temporary = inbox / f".{final_path.name}.tmp"
        temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(final_path)
    except OSError:
        # Hooks must never disrupt Codex work if the monitor is unavailable.
        return 0
    return 0


def _is_our_handler(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and HOOK_MARKER in str(handler.get("command", ""))
    )


def _without_existing_sentinel_handlers(groups: Any) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        next_group = dict(group)
        handlers = [handler for handler in group.get("hooks", []) if not _is_our_handler(handler)]
        if handlers:
            next_group["hooks"] = handlers
            cleaned.append(next_group)
    return cleaned


def install_hooks(
    support_dir: Path = DEFAULT_SUPPORT_DIR,
    codex_dir: Path = DEFAULT_CODEX_DIR,
    source_script: Path | None = None,
) -> dict[str, Any]:
    """Install stable-path command hooks while preserving unrelated hook config."""
    support_dir.mkdir(parents=True, exist_ok=True)
    installed_script = support_dir / "codex_event_hook.py"
    source = (source_script or Path(__file__)).resolve()
    if source != installed_script.resolve():
        shutil.copy2(source, installed_script)
    installed_script.chmod(0o755)

    hooks_path = codex_dir / "hooks.json"
    codex_dir.mkdir(parents=True, exist_ok=True)
    if hooks_path.exists():
        try:
            root = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Existing hooks.json is invalid JSON: {exc}") from exc
        if not isinstance(root, dict):
            raise ValueError("Existing hooks.json must contain a JSON object")
    else:
        root = {}
    hooks = root.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks.json field 'hooks' must be an object")

    command = f"/usr/bin/python3 {shlex.quote(str(installed_script))} {HOOK_MARKER}"
    for event in HOOK_EVENTS:
        groups = _without_existing_sentinel_handlers(hooks.get(event, []))
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 3}]})
        hooks[event] = groups

    temporary = hooks_path.with_name(f".{hooks_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(hooks_path)
    return {
        "status": "installed",
        "hooks_path": str(hooks_path),
        "script_path": str(installed_script),
        "events": list(HOOK_EVENTS),
    }


def hooks_status(codex_dir: Path = DEFAULT_CODEX_DIR) -> dict[str, Any]:
    hooks_path = codex_dir / "hooks.json"
    try:
        root = json.loads(hooks_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "not_installed", "hooks_path": str(hooks_path)}
    except (json.JSONDecodeError, OSError) as exc:
        return {"status": "error", "hooks_path": str(hooks_path), "error": str(exc)}
    hooks = root.get("hooks", {}) if isinstance(root, dict) else {}
    installed_events = [
        event
        for event in HOOK_EVENTS
        if any(_is_our_handler(handler) for group in hooks.get(event, []) if isinstance(group, dict) for handler in group.get("hooks", []))
    ]
    return {
        "status": "installed" if len(installed_events) == len(HOOK_EVENTS) else "partial",
        "hooks_path": str(hooks_path),
        "events": installed_events,
    }


def summarize_runtime_hooks(response: dict[str, Any]) -> dict[str, Any]:
    matching: list[dict[str, Any]] = []
    errors: list[Any] = []
    warnings: list[Any] = []
    result = response.get("result", {})
    for entry in result.get("data", []) if isinstance(result, dict) else []:
        if not isinstance(entry, dict):
            continue
        errors.extend(entry.get("errors", []) if isinstance(entry.get("errors"), list) else [])
        warnings.extend(entry.get("warnings", []) if isinstance(entry.get("warnings"), list) else [])
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            if isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", "")):
                matching.append(hook)
    statuses = [str(hook.get("trustStatus", "untrusted")) for hook in matching]
    trusted = sum(status in {"trusted", "managed"} for status in statuses)
    review_required = sum(status in {"untrusted", "modified"} for status in statuses)
    if not matching:
        status = "not_discovered"
    elif review_required > 0:
        status = "review_required"
    elif len(matching) < len(HOOK_EVENTS):
        status = "partial"
    else:
        status = "trusted"
    return {
        "status": status,
        "discovered": len(matching),
        "trusted": trusted,
        "review_required": review_required,
        "errors": errors,
        "warnings": warnings,
    }


def find_codex_executable() -> Path | None:
    configured = os.environ.get("CODEX_EXECUTABLE")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
    for candidate in CODEX_EXECUTABLE_CANDIDATES:
        if candidate.is_file():
            return candidate
    resolved = shutil.which("codex")
    return Path(resolved) if resolved else None


def runtime_hooks_status(cwd: Path, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Ask the bundled Codex app-server whether installed hooks are trusted."""
    executable = find_codex_executable()
    if executable is None:
        return {"status": "codex_unavailable", "error": "Codex executable was not found"}
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [str(executable), "app-server"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            raise OSError("Codex app-server did not provide standard streams")
        messages = (
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "traffic_sentinel",
                        "title": "Codex Traffic Sentinel",
                        "version": "1.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"method": "initialized", "params": {}},
            {"method": "hooks/list", "id": 1, "params": {"cwds": [str(cwd)]}},
        )
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            ready = selector.select(timeout=min(0.5, max(0.0, deadline - time.monotonic())))
            if not ready:
                continue
            line = process.stdout.readline()
            if not line:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and response.get("id") == 1:
                if isinstance(response.get("error"), dict):
                    return {"status": "error", "error": str(response["error"].get("message", "hooks/list failed"))}
                return summarize_runtime_hooks(response)
        return {"status": "error", "error": "Timed out while checking Codex Hook trust"}
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Traffic Sentinel Codex event integration")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(HOOK_MARKER, action="store_true", dest="capture")
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--runtime-status", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_SUPPORT_DIR / "state")
    parser.add_argument("--support-dir", type=Path, default=DEFAULT_SUPPORT_DIR)
    parser.add_argument("--codex-dir", type=Path, default=DEFAULT_CODEX_DIR)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.capture:
        return capture_from_stdin(args.state_dir)
    try:
        if args.install:
            result = install_hooks(args.support_dir, args.codex_dir)
        elif args.runtime_status:
            result = runtime_hooks_status(args.cwd)
        else:
            result = hooks_status(args.codex_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
