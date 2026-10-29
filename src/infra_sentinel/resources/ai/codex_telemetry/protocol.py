"""Reduce bounded OTLP/JSON logs to numerical request events at admission.

Raw bodies, resources, prompts, tool records, account attributes and upstream
identifiers never leave this parser. Conversation IDs become keyed digests
used only in the receiver's bounded, volatile correlation state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import math
import re

MAX_BODY_BYTES = 1024 * 1024
MAX_RECORDS = 512
MAX_ATTRIBUTES = 96
MAX_DURATION_MS = 60 * 60 * 1000
MODEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,95}\Z")
_COUNTER_RE = re.compile(r"[0-9]{1,16}\Z")
_KEYS = frozenset({
    "event.name", "event.kind", "event.timestamp", "conversation.id", "model",
    "success", "duration_ms", "output_token_count", "reasoning_token_count", "ttft_ms",
    "error.message",
})


@dataclass(frozen=True)
class RequestEvent:
    kind: str
    session: str
    model: str
    epoch: float
    duration_ms: float = 0
    output_tokens: int | None = None
    first_token_ms: float | None = None


def _scalar(raw: object) -> object:
    if not isinstance(raw, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        value = raw.get(key)
        if type(value) in (str, int, float, bool):
            return value
    return None


def number(raw: object, maximum: float = MAX_DURATION_MS) -> float | None:
    if type(raw) not in (str, int, float) or (isinstance(raw, str) and len(raw) > 24):
        return None
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and 0 <= value <= maximum else None


def counter(raw: object) -> int | None:
    if type(raw) is int:
        return raw if 0 <= raw <= 2**53 - 1 else None
    if isinstance(raw, str) and _COUNTER_RE.fullmatch(raw):
        value = int(raw)
        return value if value <= 2**53 - 1 else None
    return None


def parse_logs(raw: bytes, secret: bytes, now: float) -> tuple[list[RequestEvent], int]:
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise ValueError("body-size")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid-json") from exc
    if not isinstance(document, dict) or not isinstance(document.get("resourceLogs"), list):
        raise ValueError("not-otlp-logs")
    events: list[RequestEvent] = []
    records = 0
    # Cardinality limits apply before iteration, including empty outer envelopes.
    if len(document["resourceLogs"]) > MAX_RECORDS:
        raise ValueError("too-many-resources")
    for resource in document["resourceLogs"]:
        scopes = resource.get("scopeLogs", []) if isinstance(resource, dict) else []
        if not isinstance(scopes, list) or len(scopes) > MAX_RECORDS:
            raise ValueError("too-many-scopes")
        for scope in scopes:
            logs = scope.get("logRecords", []) if isinstance(scope, dict) else []
            if not isinstance(logs, list) or records + len(logs) > MAX_RECORDS:
                raise ValueError("too-many-records")
            records += len(logs)
            for record in logs:
                if not isinstance(record, dict):
                    continue
                attributes = record.get("attributes", [])
                if not isinstance(attributes, list) or len(attributes) > MAX_ATTRIBUTES:
                    continue
                values: dict[str, object] = {}
                duplicated = False
                for attribute in attributes:
                    if not isinstance(attribute, dict):
                        continue
                    key = attribute.get("key")
                    if not isinstance(key, str) or key not in _KEYS:
                        continue
                    if key in values:
                        duplicated = True
                        break
                    # Error content is deliberately never inspected or retained.
                    values[key] = True if key == "error.message" else _scalar(attribute.get("value"))
                if duplicated:
                    continue
                event = _event(values, secret, now)
                if event is not None:
                    events.append(event)
    return sorted(events, key=lambda event: event.epoch), records


def _event(values: dict[str, object], secret: bytes, now: float) -> RequestEvent | None:
    name = values.get("event.name")
    if name not in {"codex.websocket_request", "codex.sse_event"}:
        return None
    if name == "codex.sse_event" and values.get("event.kind") not in {"response.completed", "response.failed"}:
        return None
    model, session, stamp = (values.get(key) for key in ("model", "conversation.id", "event.timestamp"))
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model) or model in {"unknown", "mixed"}:
        return None
    if not isinstance(session, str) or not 1 <= len(session) <= 128 or not isinstance(stamp, str) or len(stamp) > 48:
        return None
    try:
        date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        epoch = date.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    if not math.isfinite(epoch) or not now - 3600 <= epoch <= now + 30:
        return None
    digest = hmac.new(secret, session.encode(), hashlib.sha256).hexdigest()
    if "error.message" in values or values.get("event.kind") == "response.failed":
        return RequestEvent("failed", digest, model, epoch)
    if name == "codex.websocket_request":
        success = values.get("success")
        if success is not True and success != "true":
            return RequestEvent("failed", digest, model, epoch)
        duration = number(values.get("duration_ms"))
        return RequestEvent("start", digest, model, epoch, duration) if duration is not None else None
    output = counter(values.get("output_token_count"))
    reasoning = counter(values.get("reasoning_token_count"))
    if output is None or (reasoning is not None and reasoning > output):
        return None
    return RequestEvent("complete", digest, model, epoch, output_tokens=output,
                        first_token_ms=number(values.get("ttft_ms")))
