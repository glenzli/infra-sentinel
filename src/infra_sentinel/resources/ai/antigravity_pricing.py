"""Pinned text-token price references for local Antigravity metadata.

Antigravity individual plans expose quota, not a per-request invoice. This
module computes an API-price reference only for explicitly mapped local model
identifiers. It excludes unknown or opaque aliases and non-token charges, and
is never used as a subscription bill or remaining-quota calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ANTIGRAVITY_TEXT_PRICE_REFERENCES_EFFECTIVE_DATE = "2026-08-22"
GEMINI_DEVELOPER_API_PRICES_URL = "https://ai.google.dev/gemini-api/docs/pricing"
GOOGLE_CLOUD_ANTHROPIC_PRICES_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"


@dataclass(frozen=True)
class TextTokenPrice:
    """Documented USD prices per million text tokens."""

    input_per_million: float
    cached_input_per_million: float
    output_per_million: float


# Sources: Google Gemini Developer API and Google Cloud partner-model pricing,
# checked on the effective date. Mappings are deliberately literal: opaque
# aliases are not priced. `gemini-3.1-pro-low` is the Antigravity local alias
# for the documented Gemini 3.1 Pro Preview <=200k-context tier.
ANTIGRAVITY_TEXT_PRICE_REFERENCES: Mapping[str, TextTokenPrice] = {
    "gemini-3.7-flash": TextTokenPrice(0.75, 0.075, 3.75),
    "gemini-3.6-flash": TextTokenPrice(0.75, 0.075, 3.75),
    "gemini-3.1-pro-low": TextTokenPrice(2.00, 0.20, 12.00),
    "claude-opus-4-6-thinking": TextTokenPrice(5.00, 0.50, 25.00),
}


@dataclass(frozen=True)
class AntigravityApiEstimate:
    total_cost_usd: float
    priced_tokens: int
    unpriced_tokens: int
    model_costs: tuple[tuple[str, float, int], ...]


def estimate_antigravity_text_api_cost(model_totals: Mapping[str, object]) -> AntigravityApiEstimate:
    """Return an explicit-model text API reference, never an Antigravity bill.

    ``input_tokens`` is fresh/system input in decoded metadata;
    ``cache_read_tokens`` is charged on the cache-read leg; output and reasoning
    are charged at the documented output rate. Cache storage, grounding, tools,
    multimodal charges, and Gemini 3.1 contexts above 200k are absent.
    """
    estimates: list[tuple[str, float, int]] = []
    unpriced_tokens = 0
    for identifier, totals in model_totals.items():
        input_tokens = _counter(totals, "input_tokens")
        cache_read_tokens = _counter(totals, "cache_read_tokens")
        output_tokens = _counter(totals, "output_tokens") + _counter(totals, "reasoning_tokens")
        total_tokens = input_tokens + cache_read_tokens + output_tokens
        price = ANTIGRAVITY_TEXT_PRICE_REFERENCES.get(identifier)
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
