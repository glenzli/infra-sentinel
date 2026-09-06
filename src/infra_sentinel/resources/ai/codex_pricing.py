"""Standard API reference pricing for explicitly sampled Codex text usage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from infra_sentinel.resources.ai.pricing_catalog import (
    PriceCatalogLookup,
    TextTokenPrice,
    bundled_pricing_catalog,
)


OPENAI_STANDARD_TEXT_PRICES_URL = "https://developers.openai.com/api/docs/models"
StandardTextPrice = TextTokenPrice


@dataclass(frozen=True)
class ModelCostEstimate:
    model: str
    tokens: int
    cost_usd: float


@dataclass(frozen=True)
class StandardApiEstimate:
    total_cost_usd: float
    priced_tokens: int
    unpriced_tokens: int
    models: tuple[ModelCostEstimate, ...]


def estimate_standard_api_cost(
    model_compositions: Mapping[str, Mapping[str, int]],
    *,
    catalog: PriceCatalogLookup | None = None,
    usage_date: str | None = None,
) -> StandardApiEstimate:
    """Price exact text-token fields without inventing model coverage.

    ``input_tokens`` includes the cached and cache-write subsets reported by
    Codex. They are removed from the ordinary-input leg before applying their
    individual rates. ``reasoning_output_tokens`` is already included in
    ``output_tokens`` and is intentionally not added a second time.
    """
    lookup = catalog or bundled_pricing_catalog()
    estimates: list[ModelCostEstimate] = []
    unpriced_tokens = 0
    for model, raw in model_compositions.items():
        tokens = _tokens(raw.get("total_tokens"))
        if tokens <= 0:
            continue
        price = lookup.price_for("codex", model, usage_date)
        if price is None:
            unpriced_tokens += tokens
            continue
        input_tokens = _tokens(raw.get("input_tokens"))
        cached_tokens = min(input_tokens, _tokens(raw.get("cached_input_tokens")))
        remaining_input = input_tokens - cached_tokens
        cache_write_tokens = (
            min(remaining_input, _tokens(raw.get("cache_write_input_tokens")))
            if price.cache_write_per_million is not None else 0
        )
        uncached_input = remaining_input - cache_write_tokens
        output_tokens = _tokens(raw.get("output_tokens"))
        cost = (
            uncached_input * price.input_per_million
            + cached_tokens * price.cached_input_per_million
            + cache_write_tokens * (price.cache_write_per_million or 0)
            + output_tokens * price.output_per_million
        ) / 1_000_000
        estimates.append(ModelCostEstimate(model=model, tokens=tokens, cost_usd=cost))
    estimates.sort(key=lambda item: (-item.cost_usd, item.model))
    return StandardApiEstimate(
        total_cost_usd=sum(item.cost_usd for item in estimates),
        priced_tokens=sum(item.tokens for item in estimates),
        unpriced_tokens=unpriced_tokens,
        models=tuple(estimates),
    )


def _tokens(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)
