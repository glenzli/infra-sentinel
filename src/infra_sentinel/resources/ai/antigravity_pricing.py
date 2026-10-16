"""Text API references with bounded, explicitly labeled Antigravity variants."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from infra_sentinel.resources.ai.pricing_catalog import (
    PriceCatalogLookup,
    TextTokenPrice,
    bundled_pricing_catalog,
)


GEMINI_DEVELOPER_API_PRICES_URL = "https://ai.google.dev/gemini-api/docs/pricing"
GOOGLE_CLOUD_ANTHROPIC_PRICES_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"

# Known access markers select only an exact, dated base-model catalog entry.
# They describe the name, not actual charges. Other pricing-relevant markers
# such as preview, lite, image, TTS, live and service tiers are not stripped.
_ACCESS_ID = re.compile(r"(?P<base>.+)(?P<marker>-(?:trial|trail|free))\Z")
_GEMINI_BASE = r"gemini-[0-9]+(?:\.[0-9]+)?-(?:argon|pro|flash(?:-lite)?)"
_ROLLOUT_ID = re.compile(rf"(?P<base>{_GEMINI_BASE})(?P<marker>-(?:[a-z]|exp(?:-[a-z0-9]+)?))\Z")
_EFFORT_ID = re.compile(
    rf"(?P<base>{_GEMINI_BASE}|claude-(?:opus|sonnet|haiku|fable)-[0-9]+(?:-[0-9]+)?)"
    r"(?P<marker>-(?:low|medium|high|thinking))\Z"
)


@dataclass(frozen=True)
class AntigravityModelReference:
    model: str
    match: str
    suspected_rollout: bool
    marker: str | None
    price: TextTokenPrice | None
    marker_kind: str | None = None

    def projection(self) -> dict[str, object]:
        return {
            "model": self.model,
            "match": self.match,
            "available": self.price is not None,
            "suspected_rollout": self.suspected_rollout,
            **({"marker": self.marker} if self.marker else {}),
            **({"marker_kind": self.marker_kind} if self.marker_kind else {}),
        }


def resolve_antigravity_reference(
    identifier: str,
    *,
    catalog: PriceCatalogLookup | None = None,
    usage_date: str | None = None,
) -> AntigravityModelReference:
    """Keep observed identity; exact dated prices win over a bounded base estimate.

    Trial/free access markers are separate from opaque rollout markers. Neither
    proves a server identity or billing contract, and free never implies zero API
    reference value. Unknown bases never borrow a neighboring model's price.
    """
    lookup = catalog or bundled_pricing_catalog()
    access = _ACCESS_ID.fullmatch(identifier)
    rollout = _ROLLOUT_ID.fullmatch(identifier)
    effort = _EFFORT_ID.fullmatch(identifier)
    variant = access or rollout or effort
    marker = variant.group("marker") if variant else None
    marker_kind = ("free" if marker == "-free" else "trial") if access else "rollout" if rollout else "effort" if effort else None
    price = lookup.price_for("antigravity", identifier, usage_date)
    if price is not None:
        return AntigravityModelReference(identifier, "exact", bool(rollout), marker, price, marker_kind)
    base = variant.group("base") if variant else identifier
    if base != identifier:
        price = lookup.price_for("antigravity", base, usage_date)
    return AntigravityModelReference(
        base, "base-reference" if price is not None else "unpriced", bool(rollout), marker, price, marker_kind,
    )


@dataclass(frozen=True)
class AntigravityApiEstimate:
    total_cost_usd: float
    priced_tokens: int
    unpriced_tokens: int
    model_costs: tuple[tuple[str, float, int], ...]
    base_reference_tokens: int = 0
    model_matches: tuple[tuple[str, AntigravityModelReference], ...] = ()


def estimate_antigravity_text_api_cost(
    model_totals: Mapping[str, object],
    *,
    catalog: PriceCatalogLookup | None = None,
    usage_date: str | None = None,
) -> AntigravityApiEstimate:
    """Return a dated text API reference, never an Antigravity bill."""
    lookup = catalog or bundled_pricing_catalog()
    estimates: list[tuple[str, float, int]] = []
    matches: list[tuple[str, AntigravityModelReference]] = []
    unpriced_tokens = 0
    base_reference_tokens = 0
    for identifier, totals in model_totals.items():
        input_tokens = _counter(totals, "input_tokens")
        cache_read_tokens = _counter(totals, "cache_read_tokens")
        output_tokens = _counter(totals, "output_tokens") + _counter(totals, "reasoning_tokens")
        total_tokens = input_tokens + cache_read_tokens + output_tokens
        if total_tokens <= 0:
            continue
        reference = resolve_antigravity_reference(identifier, catalog=lookup, usage_date=usage_date)
        matches.append((identifier, reference))
        price = reference.price
        if price is None:
            unpriced_tokens += total_tokens
            continue
        cost = (
            input_tokens * price.input_per_million
            + cache_read_tokens * price.cached_input_per_million
            + output_tokens * price.output_per_million
        ) / 1_000_000
        estimates.append((identifier, cost, total_tokens))
        if reference.match == "base-reference":
            base_reference_tokens += total_tokens
    estimates.sort(key=lambda item: (-item[1], item[0]))
    return AntigravityApiEstimate(
        total_cost_usd=sum(item[1] for item in estimates),
        priced_tokens=sum(item[2] for item in estimates),
        unpriced_tokens=unpriced_tokens,
        model_costs=tuple(estimates),
        base_reference_tokens=base_reference_tokens,
        model_matches=tuple(matches),
    )


def _counter(totals: object, name: str) -> int:
    value = totals.get(name) if isinstance(totals, dict) else getattr(totals, name, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
