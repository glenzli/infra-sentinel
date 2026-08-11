"""Bounded read-only query contract for canonical Infra metrics."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Protocol


QUERY_SCHEMA = "20260811.1"
MAX_RANGE_SECONDS = 90 * 24 * 60 * 60
MAX_DAILY_RANGE_SECONDS = 730 * 24 * 60 * 60
MAX_POINTS = 10_000
BUCKET_SECONDS = {60, 300, 900, 3_600, 86_400}
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class MetricQuerySource(Protocol):
    def query_points(self, **kwargs: Any) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class MetricQuery:
    since_epoch: float
    until_epoch: float
    resource_id: str | None
    source_id: str | None
    metric: str | None
    instrument: str
    bucket_seconds: int
    bucket_offset_seconds: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any], now: float | None = None) -> "MetricQuery":
        supported = {
            "since_epoch", "until_epoch", "resource_id", "source_id", "metric", "instrument",
            "bucket_seconds", "bucket_offset_seconds",
        }
        unknown = set(payload) - supported
        if unknown:
            raise ValueError(f"unsupported query fields: {', '.join(sorted(unknown))}")
        current = time.time() if now is None else float(now)
        since = cls._epoch(payload.get("since_epoch", current - 3_600), "since_epoch")
        until = cls._epoch(payload.get("until_epoch", current), "until_epoch")
        if until < since:
            raise ValueError("until_epoch must not precede since_epoch")
        bucket = payload.get("bucket_seconds", 60)
        if isinstance(bucket, bool) or not isinstance(bucket, int) or bucket not in BUCKET_SECONDS:
            raise ValueError("bucket_seconds must be one of 60, 300, 900, 3600, 86400")
        offset = payload.get("bucket_offset_seconds", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < bucket:
            raise ValueError("bucket_offset_seconds must be an integer within the bucket")
        maximum_range = MAX_DAILY_RANGE_SECONDS if bucket == 86_400 else MAX_RANGE_SECONDS
        if until - since > maximum_range:
            raise ValueError(f"query range exceeds {int(maximum_range // 86_400)} days")
        instrument = payload.get("instrument", "counter")
        if instrument not in {"counter", "gauge"}:
            raise ValueError("instrument must be counter or gauge")
        return cls(
            since_epoch=since,
            until_epoch=until,
            resource_id=cls._identifier(payload.get("resource_id"), "resource_id"),
            source_id=cls._identifier(payload.get("source_id"), "source_id"),
            metric=cls._identifier(payload.get("metric"), "metric"),
            instrument=instrument,
            bucket_seconds=bucket,
            bucket_offset_seconds=offset,
        )

    @staticmethod
    def _epoch(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        return float(value)

    @staticmethod
    def _identifier(value: Any, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"{name} is invalid")
        return value

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "since_epoch": self.since_epoch,
            "until_epoch": self.until_epoch,
            "bucket_seconds": self.bucket_seconds,
            "bucket_offset_seconds": self.bucket_offset_seconds,
            "instrument": self.instrument,
        }
        for key in ("resource_id", "source_id", "metric"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


def execute_metric_query(store: MetricQuerySource, query: MetricQuery) -> dict[str, Any]:
    """Return a bounded aggregated view without exposing SQL to protocol clients."""
    rows = store.query_points(
        since_epoch=query.since_epoch,
        until_epoch=query.until_epoch,
        resource_id=query.resource_id,
        source_id=query.source_id,
        metric=query.metric,
        instrument=query.instrument,
        bucket_seconds=query.bucket_seconds,
        bucket_offset_seconds=query.bucket_offset_seconds,
        limit=MAX_POINTS + 1,
    )
    truncated = len(rows) > MAX_POINTS
    if truncated:
        rows = rows[:MAX_POINTS]
    return {
        "schema": QUERY_SCHEMA,
        "query": query.as_dict(),
        "points": rows,
        "truncated": truncated,
    }
