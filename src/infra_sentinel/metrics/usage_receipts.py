"""Counts-only CLI receipt admission and Agent-owned, idempotent storage.

This ledger is separate from discovered session histories. Only an outer caller
that knows its run is isolated may submit a receipt. No raw event identifier,
source artifact, prompt, response, argv, or timing is retained.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any


RECEIPT_SCHEMA = "infra-sentinel.cli-usage.v1"
RECEIPT_FILENAME = "cli-usage.sqlite3"
MAX_RECEIPT_BYTES = 4096
MAX_TOKENS = 10**12
_FIELDS = {"schema", "event_hash", "client", "model", "model_basis", "coverage", "completed_at", "usage"}
_COUNTS = {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"}


def validate_receipt(raw: Any, *, now_epoch: float | None = None) -> dict[str, Any]:
    """Validate the same strict numeric allowlist at every trust boundary."""
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ValueError("receipt fields do not match the counts-only contract")
    if len(json.dumps(raw, separators=(",", ":")).encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt exceeds 4096 bytes")
    if raw["schema"] != RECEIPT_SCHEMA or raw["coverage"] != "isolated":
        raise ValueError("only isolated CLI usage receipts are supported")
    if raw["client"] not in ("codex", "agy") or raw["model_basis"] not in ("reported", "requested"):
        raise ValueError("invalid client or model provenance")
    if not isinstance(raw["event_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["event_hash"]):
        raise ValueError("receipt requires a hashed event identity")
    if not isinstance(raw["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", raw["model"]):
        raise ValueError("invalid model identifier")
    try:
        completed = datetime.fromisoformat(raw["completed_at"].replace("Z", "+00:00"))
        if completed.tzinfo is None:
            raise ValueError()
        epoch = completed.timestamp()
        if not 946684800 <= epoch <= (time.time() if now_epoch is None else now_epoch) + 300:
            raise ValueError()
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("completed_at must be a valid past timestamp with timezone") from exc
    usage = raw["usage"]
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens", "total_tokens"} <= set(usage) or set(usage) - _COUNTS:
        raise ValueError("usage requires input, output and total counts; unknown fields are rejected")
    if any(type(value) is not int or not 0 <= value <= MAX_TOKENS for value in usage.values()):
        raise ValueError("token counts must be nonnegative integers of at most 1e12")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise ValueError("total must equal input + output; reasoning is already included in output")
    if usage.get("reasoning_output_tokens", 0) > usage["output_tokens"]:
        raise ValueError("reasoning tokens must be a subset of output")
    if usage.get("cached_input_tokens", 0) + usage.get("cache_write_input_tokens", 0) > usage["input_tokens"]:
        raise ValueError("cached input counts must be subsets of input")
    return {**raw, "completed_at": completed.astimezone(timezone.utc).isoformat(), "usage": dict(usage)}


def prepare_receipt(raw: Any) -> dict[str, Any]:
    """Hash the caller's stable event ID before it enters the command queue."""
    if not isinstance(raw, dict) or "event_id" not in raw or "event_hash" in raw:
        raise ValueError("provide event_id for one stable billable attempt")
    event_id = raw["event_id"]
    if not isinstance(event_id, str) or not 8 <= len(event_id) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", event_id):
        raise ValueError("event_id must be an opaque 8 to 128 character stable identifier")
    safe = {key: value for key, value in raw.items() if key != "event_id"}
    safe["event_hash"] = hashlib.sha256(f"{raw.get('client')}:{event_id}".encode()).hexdigest()
    return validate_receipt(safe)


class UsageReceiptStore:
    """The Agent is the only writer; adapters receive aggregate read callbacks."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / RECEIPT_FILENAME
        self._signature: tuple[int, int, str] | None = None
        self._summary: dict[str, Any] = {}

    def record(self, raw: Any) -> dict[str, Any]:
        receipt = validate_receipt(raw)
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        completed = datetime.fromisoformat(receipt["completed_at"]).astimezone()
        usage = receipt["usage"]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS usage_receipts (
                event_hash TEXT PRIMARY KEY, canonical_json TEXT NOT NULL,
                completed_epoch REAL NOT NULL, local_day TEXT NOT NULL,
                local_hour REAL NOT NULL, client TEXT NOT NULL, model TEXT NOT NULL,
                model_basis TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL
            )""")
            existing = connection.execute("SELECT canonical_json FROM usage_receipts WHERE event_hash = ?", (receipt["event_hash"],)).fetchone()
            if existing is not None:
                if existing[0] != canonical:
                    raise ValueError("event identity already exists with different usage; no counters changed")
                return {"recorded": False, "duplicate": True, "total_tokens": usage["total_tokens"]}
            connection.execute("INSERT INTO usage_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                receipt["event_hash"], canonical, completed.timestamp(), completed.date().isoformat(),
                completed.replace(minute=0, second=0, microsecond=0).timestamp(), receipt["client"],
                receipt["model"], receipt["model_basis"], usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
            ))
        self._signature = None
        return {"recorded": True, "duplicate": False, "total_tokens": usage["total_tokens"]}

    def summary(self) -> dict[str, Any]:
        """Read grouped counts only; unchanged polls perform one stat, no SQL."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return {"daily": [], "hourly": []}
        current_day = datetime.now().astimezone().date().isoformat()
        signature = (stat.st_mtime_ns, stat.st_size, current_day)
        if signature == self._signature:
            return self._summary
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            fields = "client, model, model_basis, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(total_tokens) AS total_tokens, COUNT(*) AS receipts"
            daily = [dict(row) for row in connection.execute(f"SELECT local_day AS date, {fields} FROM usage_receipts GROUP BY local_day, client, model, model_basis ORDER BY local_day, client, model")]
            hourly = [dict(row) for row in connection.execute(f"SELECT local_hour AS epoch, {fields} FROM usage_receipts WHERE local_day = ? GROUP BY local_hour, client, model, model_basis ORDER BY local_hour", (current_day,))]
            # Keep unknown cache fields separate from known zero. This reads
            # the existing numeric receipt bodies without rewriting history.
            pricing = [dict(row) for row in connection.execute("""
                SELECT local_day AS date, client, model,
                    SUM(input_tokens) AS input_tokens,
                    SUM(output_tokens) AS output_tokens,
                    SUM(total_tokens) AS total_tokens,
                    SUM(json_extract(canonical_json, '$.usage.cached_input_tokens')) AS cached_input_tokens,
                    SUM(json_extract(canonical_json, '$.usage.cache_write_input_tokens')) AS cache_write_input_tokens
                FROM usage_receipts
                GROUP BY local_day, client, model,
                    json_type(canonical_json, '$.usage.cached_input_tokens'),
                    json_type(canonical_json, '$.usage.cache_write_input_tokens')
                ORDER BY local_day, client, model
            """)]
        finally:
            connection.close()
        self._summary = {"daily": daily, "hourly": hourly, "pricing": pricing}
        self._signature = signature
        return self._summary
