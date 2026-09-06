"""API price references for local Antigravity generation metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from infra_sentinel.resources.ai.pricing_catalog import (
    PriceCatalogLookup,
    TextTokenPrice,
    bundled_pricing_catalog,
)


GEMINI_DEVELOPER_API_PRICES_URL = "https://ai.google.dev/gemini-api/docs/pricing"
GOOGLE_CLOUD_ANTHROPIC_PRICES_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"


@dataclass(frozen=True)
class AntigravityApiEstimate:
    total_cost_usd: float
    priced_tokens: int
    unpriced_tokens: int
    model_costs: tuple[tuple[str, float, int], ...]


def estimate_antigravity_text_api_cost(
    model_totals: Mapping[str, object],
    *,
    catalog: PriceCatalogLookup | None = None,
    usage_date: str | None = None,
) -> AntigravityApiEstimate:
    """Return an exact-model text API reference, never an Antigravity bill."""
    lookup = catalog or bundled_pricing_catalog()
    estimates: list[tuple[str, float, int]] = []
    unpriced_tokens = 0
    for identifier, totals in model_totals.items():
        input_tokens = _counter(totals, "input_tokens")
        cache_read_tokens = _counter(totals, "cache_read_tokens")
        output_tokens = _counter(totals, "output_tokens") + _counter(totals, "reasoning_tokens")
        total_tokens = input_tokens + cache_read_tokens + output_tokens
        if total_tokens <= 0:
            continue
        price = lookup.price_for("antigravity", identifier, usage_date)
        if price is None:
            unpriced_tokens += total_tokens
            continue
        cost = (
            input_tokens * price.input_per_million
            + cache_read_tokens * price.cached_input_per_million
            + output_tokens * price.output_per_million
        ) / 1_000_000
        estimates.append((identifier, cost, total_tokens))
    estimates.sort(key=lambda item: (-item[1], item[0]))
    return AntigravityApiEstimate(
        total_cost_usd=sum(item[1] for item in estimates),
        priced_tokens=sum(item[2] for item in estimates),
        unpriced_tokens=unpriced_tokens,
        model_costs=tuple(estimates),
    )


def _counter(totals: object, name: str) -> int:
    value = totals.get(name) if isinstance(totals, dict) else getattr(totals, name, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
