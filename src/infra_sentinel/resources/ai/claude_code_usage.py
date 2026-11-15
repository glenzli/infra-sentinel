"""Normalize one fresh Claude Code terminal result into counts-only receipts.

No filesystem access, transcript persistence, model alias inference or timing
estimates belong here. The caller verifies that no other source counts the run.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from infra_sentinel.metrics.usage_receipts import MAX_TOKENS, RECEIPT_SCHEMA, prepare_receipt


def _counts(raw: Any, *, per_model: bool) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ValueError("Claude Code requires complete native usage")
    keys = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens") if per_model else (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
    )
    values = [raw.get(key) for key in keys]
    if any(type(value) is not int or not 0 <= value <= MAX_TOKENS for value in values):
        raise ValueError("Claude Code requires known nonnegative input, cache-read, cache-write and output counts")
    uncached, cached, written, output = values
    result = {"input_tokens": uncached + cached + written, "cached_input_tokens": cached,
              "cache_write_input_tokens": written, "output_tokens": output,
              "total_tokens": uncached + cached + written + output}
    details = raw.get("output_tokens_details")
    reasoning = raw.get("thinkingTokens") if per_model else (
        details.get("thinking_tokens") if isinstance(details, dict) else None
    )
    if reasoning is not None:
        if type(reasoning) is not int or not 0 <= reasoning <= output:
            raise ValueError("Claude Code thinking tokens must be a subset of output")
        result["reasoning_output_tokens"] = reasoning
    return result


def claude_code_receipts(result: Any, *, completed_at: str, fresh_session: bool) -> list[dict[str, Any]]:
    """Accept final counts for a fresh, otherwise uncollected native session.

    modelUsage is cumulative over the session. Resumed snapshots need a
    separately evidenced delta and are deliberately rejected by this helper.
    Each native model gets one stable session/model identity across retries.
    """
    if fresh_session is not True:
        raise ValueError("Claude Code resumed sessions require a verified delta, not a cumulative receipt")
    if not isinstance(result, dict) or result.get("type") != "result":
        raise ValueError("Claude Code requires a native terminal result")
    session = result.get("session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", session):
        raise ValueError("Claude Code requires a stable native session identity")
    total = _counts(result.get("usage"), per_model=False)
    models = result.get("modelUsage")
    if not total["total_tokens"] and not models:
        return []
    if not isinstance(models, dict) or not 1 <= len(models) <= 32:
        raise ValueError("Claude Code requires a bounded native per-model usage breakdown")
    normalized = {model: _counts(usage, per_model=True) for model, usage in models.items()}
    for field in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "total_tokens"):
        if sum(row[field] for row in normalized.values()) != total[field]:
            raise ValueError("Claude Code native total and per-model usage disagree")
    if "reasoning_output_tokens" in total and all("reasoning_output_tokens" in row for row in normalized.values()):
        if sum(row["reasoning_output_tokens"] for row in normalized.values()) != total["reasoning_output_tokens"]:
            raise ValueError("Claude Code native thinking breakdowns disagree")
    receipts = []
    for model, usage in normalized.items():
        if not isinstance(model, str):
            raise ValueError("Claude Code model identities must be strings")
        event_id = hashlib.sha256(b"infra-sentinel/claude-code-session-v1\0" + session.encode() + b"\0" + model.encode()).hexdigest()
        receipt = {"schema": RECEIPT_SCHEMA, "event_id": event_id,
                   "client": "claude-code", "model": model, "model_basis": "reported",
                   "coverage": "uncollected", "completed_at": completed_at, "usage": usage}
        prepare_receipt(receipt)
        if usage["total_tokens"]:
            receipts.append(receipt)
    return receipts
