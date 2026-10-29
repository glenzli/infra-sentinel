"""Submit one minimal receipt to the Agent, without opening its durable ledger."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Sequence
import uuid

from infra_sentinel.app.configuration import desktop_state_directory
from infra_sentinel.app.protocol import COMMAND_SCHEMA, COMMANDS_DIRECTORY, iso_now
from infra_sentinel.metrics.usage_receipts import MAX_RECEIPT_BYTES, prepare_receipt


def submit_receipt(state_dir: Path, raw: Any, *, wait_seconds: float = 15) -> dict[str, Any]:
    receipt = prepare_receipt(raw)
    command_id = str(uuid.uuid4())
    commands = state_dir / COMMANDS_DIRECTORY
    commands.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = commands / f"{command_id}.request.json"
    temporary = commands / f".{command_id}.tmp"
    document = {"schema": COMMAND_SCHEMA, "id": command_id, "type": "ai.usage.receipt.record", "requested_at": iso_now(), "payload": receipt}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, separators=(",", ":"))
    temporary.replace(path)
    deadline = time.monotonic() + wait_seconds
    result_path = commands / f"{command_id}.result.json"
    while True:
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                return {"status": "queued", "command_id": command_id}
            time.sleep(0.1)
            continue
        if result.get("id") != command_id or result.get("type") != document["type"]:
            raise ValueError("receipt acknowledgement does not match this request")
        result_path.unlink(missing_ok=True)
        return {key: value for key, value in result.items() if key in {"status", "message", "payload"}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record isolated CLI token usage; no prompt, response, timing or credentials")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--config", type=Path)
    location.add_argument("--state-dir", type=Path)
    parser.add_argument("--receipt", type=Path, help="counts-only JSON file; default reads stdin")
    parser.add_argument("--wait-seconds", type=float, default=15, help="0 to 30 seconds for Agent acknowledgement; queued is not yet recorded")
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 30:
        parser.error("--wait-seconds must be between 0 and 30")
    try:
        if args.receipt and str(args.receipt) != "-":
            with args.receipt.open("rb") as stream:
                data = stream.read(MAX_RECEIPT_BYTES + 1)
        else:
            data = sys.stdin.buffer.read(MAX_RECEIPT_BYTES + 1)
        if len(data) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt exceeds 4096 bytes; submit only numeric usage metadata")
        raw = json.loads(data)
        state_dir = args.state_dir.expanduser() if args.state_dir else desktop_state_directory(args.config)
        result = submit_receipt(state_dir, raw, wait_seconds=args.wait_seconds)
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result["status"] in {"ok", "queued"} else 1
    except (OSError, ValueError, TypeError):
        # Never echo user input or arbitrary filesystem/provider diagnostics.
        print(json.dumps({"status": "error", "reason": "receipt rejected or local Agent unavailable; no raw input was retained"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
