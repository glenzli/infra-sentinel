"""Versioned local contract between the Infra Agent and any desktop UI.

The protocol is intentionally file-backed for this first transport: it is
local-only, dependency-free, and works wherever Python can create an atomic
replacement.  A future loopback IPC transport can expose these same payloads
without changing collectors, storage, or projections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable


PROJECTION_SCHEMA = "20260808.4"
COMMAND_SCHEMA = PROJECTION_SCHEMA
PROJECTION_FILENAME = "projection.json"
COMMANDS_DIRECTORY = "commands"
COMMAND_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds") if epoch is not None else datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def write_projection(state_dir: Path, payload: dict[str, Any]) -> Path:
    """Publish a whole, versioned projection for one local UI refresh."""
    document = dict(payload)
    document["schema"] = PROJECTION_SCHEMA
    document["protocol"] = {
        "schema": PROJECTION_SCHEMA,
        "transport": "local-file",
        "command_schema": COMMAND_SCHEMA,
    }
    path = state_dir / PROJECTION_FILENAME
    _atomic_json(path, document)
    return path


def read_projection(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((state_dir / PROJECTION_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != PROJECTION_SCHEMA:
        return None
    return payload


@dataclass(frozen=True)
class AgentCommand:
    id: str
    type: str
    payload: dict[str, Any]
    requested_at: str
    processing_path: Path


def _request_path(commands_dir: Path, command_id: str) -> Path:
    return commands_dir / f"{command_id}.request.json"


def _processing_path(commands_dir: Path, command_id: str) -> Path:
    return commands_dir / f"{command_id}.processing.json"


def _result_path(commands_dir: Path, command_id: str) -> Path:
    return commands_dir / f"{command_id}.result.json"


def _decode_command(path: Path) -> tuple[str, str, dict[str, Any], str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != COMMAND_SCHEMA:
        return None
    command_id = payload.get("id")
    command_type = payload.get("type")
    requested_at = payload.get("requested_at")
    body = payload.get("payload", {})
    if (
        not isinstance(command_id, str)
        or not COMMAND_ID_RE.fullmatch(command_id)
        or not isinstance(command_type, str)
        or not command_type
        or not isinstance(requested_at, str)
        or not isinstance(body, dict)
    ):
        return None
    return command_id, command_type, body, requested_at


def consume_commands(state_dir: Path) -> Iterable[AgentCommand]:
    """Claim valid commands; uncompleted claims are safely retried on restart."""
    commands_dir = state_dir / COMMANDS_DIRECTORY
    commands_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted((*commands_dir.glob("*.processing.json"), *commands_dir.glob("*.request.json")))
    for path in paths:
        decoded = _decode_command(path)
        if decoded is None:
            path.unlink(missing_ok=True)
            continue
        command_id, command_type, payload, requested_at = decoded
        processing_path = _processing_path(commands_dir, command_id)
        if path.name not in {f"{command_id}.request.json", processing_path.name}:
            path.unlink(missing_ok=True)
            continue
        if path != processing_path:
            try:
                path.replace(processing_path)
            except OSError:
                continue
        yield AgentCommand(command_id, command_type, payload, requested_at, processing_path)


def complete_command(command: AgentCommand, *, status: str, message: str | None = None) -> None:
    if status not in {"ok", "rejected", "error"}:
        raise ValueError("invalid command completion status")
    result = {
        "schema": COMMAND_SCHEMA,
        "id": command.id,
        "type": command.type,
        "status": status,
        "completed_at": iso_now(),
    }
    if message:
        result["message"] = message
    _atomic_json(_result_path(command.processing_path.parent, command.id), result)
    command.processing_path.unlink(missing_ok=True)
