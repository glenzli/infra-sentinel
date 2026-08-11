"""Classify sampled byte deltas as live intervals or delayed catch-up."""

from __future__ import annotations

from typing import Any, MutableMapping


DEFAULT_EXPECTED_INTERVAL_SECONDS = 5.0
REALTIME_GRACE_MULTIPLIER = 2.0
REALTIME_INTERVAL = "realtime"
CATCH_UP_INTERVAL = "catch_up"


def classify_interval(observed_seconds: Any, expected_interval_seconds: Any) -> str:
    """Treat deltas spanning more than two sample periods as delayed catch-up."""
    try:
        observed = max(0.0, float(observed_seconds))
    except (TypeError, ValueError):
        observed = 0.0
    try:
        expected = max(0.001, float(expected_interval_seconds))
    except (TypeError, ValueError):
        expected = DEFAULT_EXPECTED_INTERVAL_SECONDS
    return (
        CATCH_UP_INTERVAL
        if observed > expected * REALTIME_GRACE_MULTIPLIER
        else REALTIME_INTERVAL
    )


def annotate_sample_timing(
    sample: MutableMapping[str, Any],
    expected_interval_seconds: float,
) -> str:
    """Persist the timing decision so every downstream consumer agrees."""
    kind = classify_interval(
        sample.get("observed_seconds"),
        expected_interval_seconds,
    )
    sample["interval_kind"] = kind
    sample["expected_interval_seconds"] = float(expected_interval_seconds)
    return kind


def sample_is_realtime(
    sample: dict[str, Any],
    fallback_expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
) -> bool:
    """Read a persisted timing decision, with a safe fallback for old records."""
    kind = sample.get("interval_kind")
    if kind in (REALTIME_INTERVAL, CATCH_UP_INTERVAL):
        return kind == REALTIME_INTERVAL
    return (
        classify_interval(
            sample.get("observed_seconds"),
            sample.get(
                "expected_interval_seconds",
                fallback_expected_interval_seconds,
            ),
        )
        == REALTIME_INTERVAL
    )
