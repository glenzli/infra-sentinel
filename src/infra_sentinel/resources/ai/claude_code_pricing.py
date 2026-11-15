"""Dated API references for native Claude Code labels, without backend claims."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from infra_sentinel.resources.ai.pricing_catalog import PriceCatalogLookup, bundled_pricing_catalog
from infra_sentinel.resources.ai.model_reference import effort_variant


@dataclass(frozen=True)
class ClaudeCodeApiEstimate:
    total_cost_usd: float
    priced_tokens: int
    unpriced_tokens: int
    reference: dict[str, object]


def estimate_claude_code_text_api_cost(
    model: str, usage: Mapping[str, Any], *, catalog: PriceCatalogLookup | None = None,
    usage_date: str | None = None,
) -> ClaudeCodeApiEstimate:
    """Price known disjoint legs; an unknown cache-write tariff stays unpriced.

    Only the dedicated scope may define a native or proxy label. A context
    selector is not a tariff; exact effort variants still win before a base
    lookup. The catalog must explicitly cover the resulting dated identity.
    """
    lookup = catalog or bundled_pricing_catalog()
    name = model[:-4] if model.endswith("[1m]") else model
    price_model = name
    marker_kind = "context" if name != model else None
    price = lookup.price_for("claude-code", name, usage_date)
    effort = effort_variant(name)
    if price is None and effort:
        price_model = effort.base
        marker_kind = "effort"
        price = lookup.price_for("claude-code", price_model, usage_date)
    reference = {"model": price_model, "match": "unpriced" if price is None else (
        "exact" if price_model == model else "base-reference"), "available": price is not None,
        **({"marker_kind": marker_kind} if marker_kind else {})}
    total = usage["total_tokens"]
    required = [usage.get(key) for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")]
    if price is None or any(type(value) is not int or value < 0 for value in required):
        return ClaudeCodeApiEstimate(0, 0, total, reference)
    inputs, cached, written, output = required
    if cached + written > inputs or inputs + output != total:
        return ClaudeCodeApiEstimate(0, 0, total, reference)
    cost = ((inputs - cached - written) * price.input_per_million
            + cached * price.cached_input_per_million + output * price.output_per_million)
    unpriced = written if price.cache_write_per_million is None else 0
    if price.cache_write_per_million is not None:
        cost += written * price.cache_write_per_million
    return ClaudeCodeApiEstimate(cost / 1_000_000, total - unpriced, unpriced, reference)
