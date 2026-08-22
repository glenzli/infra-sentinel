"""Canonical, privacy-safe snapshot contract for local AI usage providers.

Collectors own discovery and their provider-specific counters.  This module owns
the common projection vocabulary so that a new AI client only has to map its
facts here: current-day usage, cumulative usage, model totals, and optional
diagnostic metric groups.  No prompts, response content, paths, account rows,
or credentials belong in this contract.
"""

from __future__ import annotations

from typing import Any


AI_USAGE_SNAPSHOT_SCHEMA = "20260822.1"
AI_USAGE_PRICE_REFERENCE_SCHEMA = "20260822.1"


def localized(en: str, zh: str) -> dict[str, str]:
    """Return display copy that can be rendered without provider branches."""
    return {"en": en, "zh": zh}


def usage_window(
    tokens: int | None,
    *,
    method: str,
    detail: dict[str, str],
    started_at: str | None = None,
) -> dict[str, Any]:
    """Describe one honest token window, including its provenance."""
    payload: dict[str, Any] = {
        "available": tokens is not None,
        "tokens": max(0, int(tokens or 0)),
        "method": method,
        "detail": detail,
    }
    if started_at:
        payload["started_at"] = started_at
    return payload


def token_metric(
    identifier: str,
    label: dict[str, str],
    value: int | float,
    detail: dict[str, str],
    *,
    unit: str = "tokens",
) -> dict[str, Any]:
    """Create a provider-specific detail metric rendered by the generic UI."""
    return {
        "id": identifier,
        "label": label,
        "value": max(0, value),
        "unit": unit,
        "detail": detail,
    }


def detail_group(
    identifier: str,
    title: dict[str, str],
    metrics: list[dict[str, Any]],
    *,
    note: dict[str, str] | None = None,
    badge: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an optional, data-only provider detail group."""
    payload: dict[str, Any] = {"id": identifier, "title": title, "metrics": metrics}
    if note:
        payload["note"] = note
    if badge:
        payload["badge"] = badge
    return payload


def model_usage(
    identifier: str,
    *,
    today_tokens: int | None,
    cumulative_tokens: int | None,
    today_method: str = "source-window",
    today_detail: dict[str, str] | None = None,
    cumulative_method: str = "source-history",
    cumulative_detail: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a normalized per-model usage row."""
    return {
        "id": identifier,
        "today": usage_window(today_tokens, method=today_method, detail=today_detail or localized("provider model total", "提供方模型累计")),
        "cumulative": usage_window(cumulative_tokens, method=cumulative_method, detail=cumulative_detail or localized("readable local history", "可读本地历史")),
    }

def daily_usage(day: str, tokens: int, models: list[dict[str, Any]]) -> dict[str, Any]:
    """Create one provider-normalized calendar-day usage row.

    Providers may derive these rows differently, but consumers never need to
    know which local database or API supplied them. A missing daily history is
    distinct from an available history containing no rows.
    """
    return {
        "date": day,
        "tokens": max(0, int(tokens)),
        "models": [
            {"id": str(model.get("id") or "unknown"), "tokens": max(0, int(model.get("tokens") or 0))}
            for model in models
        ],
    }


def pricing_day(
    day: str,
    *,
    kind: str,
    cost_usd: float,
    priced_tokens: int,
    unpriced_tokens: int,
    models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a bounded daily price reference beside, never inside, usage.

    The ``kind`` records provenance: a local provider-reported amount, an
    explicit catalogue mapping, or a sampled projection. Consumers may sum
    only compatible references; no reference is a provider invoice.
    """
    rows: list[dict[str, Any]] = []
    for model in (models or [])[:32]:
        identifier = str(model.get("id") or "unknown")[:128]
        rows.append({
            "id": identifier,
            "cost_usd": max(0.0, float(model.get("cost_usd") or 0.0)),
            "priced_tokens": max(0, int(model.get("priced_tokens") or 0)),
            "unpriced_tokens": max(0, int(model.get("unpriced_tokens") or 0)),
        })
    return {
        "date": day,
        "reference": {
            "schema": AI_USAGE_PRICE_REFERENCE_SCHEMA,
            "kind": str(kind)[:96],
            "cost_usd": max(0.0, float(cost_usd)),
            "priced_tokens": max(0, int(priced_tokens)),
            "unpriced_tokens": max(0, int(unpriced_tokens)),
            "models": rows,
        },
    }


def ai_usage_snapshot(
    *,
    source_id: str,
    label: str,
    status: str,
    observed_at: str,
    collection_method: str,
    today: dict[str, Any],
    cumulative: dict[str, Any],
    models: list[dict[str, Any]],
    details: list[dict[str, Any]],
    confidence: str,
    privacy: str,
    daily_history: list[dict[str, Any]] | None = None,
    pricing_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the only snapshot shape consumed by AI projection and UI."""
    return {
        "schema": AI_USAGE_SNAPSHOT_SCHEMA,
        "available": True,
        "status": status,
        "source_id": source_id,
        "label": label,
        "observed_at": observed_at,
        "collection_method": collection_method,
        "usage": {"today": today, "cumulative": cumulative},
        "models": models,
        "history": {
            "daily_available": daily_history is not None,
            "daily": daily_history or [],
        },
        "pricing": {
            "daily_available": pricing_history is not None,
            "daily": pricing_history or [],
        },
        "details": details,
        "attribution_method": "local-reported",
        "confidence": confidence,
        "privacy": privacy,
    }


def window_tokens(snapshot: dict[str, Any], window: str) -> int | None:
    """Read an available normalized window without trusting provider fields."""
    usage = snapshot.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get(window)
    if not isinstance(value, dict) or not value.get("available"):
        return None
    try:
        return max(0, int(value.get("tokens") or 0))
    except (TypeError, ValueError):
        return None
