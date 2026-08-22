"""Offline reference pricing for explicitly sampled Codex text-token usage.

This is deliberately a small, versioned catalogue rather than an API client.
The local rollout JSONL format is not a billing interface, so these values are
only an equivalent *standard API text-price* reference.  Unknown models and
pricing dimensions the sample cannot establish are excluded rather than
estimated from names or neighbouring model tiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


OPENAI_STANDARD_TEXT_PRICES_EFFECTIVE_DATE = "2026-08-21"
OPENAI_STANDARD_TEXT_PRICES_URL = "https://platform.openai.com/pricing"


@dataclass(frozen=True)
class StandardTextPrice:
    """Official standard API text prices in USD per million tokens."""

    input_per_million: float
    cached_input_per_million: float
    cache_write_per_million: float
    output_per_million: float


# Source: OpenAI's official model pages and pricing table, checked on the
# effective date above.  This is not a subscription, Codex plan, Batch,
# Priority, regional-processing, long-context, tool, or multimodal price.
STANDARD_TEXT_PRICES: Mapping[str, StandardTextPrice] = {
    "gpt-5.6-sol": StandardTextPrice(5.00, 0.50, 6.25, 30.00),
    "gpt-5.6-terra": StandardTextPrice(2.00, 0.20, 2.50, 12.00),
    "gpt-5.6-luna": StandardTextPrice(0.20, 0.02, 0.25, 1.20),
}


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


def estimate_standard_api_cost(model_compositions: Mapping[str, Mapping[str, int]]) -> StandardApiEstimate:
    """Price explicitly sampled text-token fields without inventing coverage.

    ``input_tokens`` includes the cached and cache-write subsets reported by
    Codex.  They are removed from the ordinary-input leg before applying their
    individual rates.  ``reasoning_output_tokens`` is intentionally not added:
    it is already part of ``output_tokens`` in the sampled usage object.
    """
    estimates: list[ModelCostEstimate] = []
    unpriced_tokens = 0
    for model, raw in model_compositions.items():
        tokens = _tokens(raw.get("total_tokens"))
        price = STANDARD_TEXT_PRICES.get(model)
        if price is None:
            unpriced_tokens += tokens
            continue
        input_tokens = _tokens(raw.get("input_tokens"))
        cached_tokens = min(input_tokens, _tokens(raw.get("cached_input_tokens")))
        remaining_input = input_tokens - cached_tokens
        cache_write_tokens = min(remaining_input, _tokens(raw.get("cache_write_input_tokens")))
        uncached_input = remaining_input - cache_write_tokens
        output_tokens = _tokens(raw.get("output_tokens"))
        cost = (
            uncached_input * price.input_per_million
            + cached_tokens * price.cached_input_per_million
            + cache_write_tokens * price.cache_write_per_million
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
