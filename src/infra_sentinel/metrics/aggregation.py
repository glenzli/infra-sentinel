"""Time-bucket aggregation contracts for durable Infra metrics.

Collectors emit high-frequency ``MetricPoint`` facts.  This owner keeps the
numeric rules for combining those facts independent from SQLite and from the
Agent lifecycle so live buffers, history compaction, and queries agree on the
same counter/gauge semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Iterable

from infra_sentinel.core.model import MetricPoint


COUNTER_INSTRUMENTS = {"counter"}
GAUGE_INSTRUMENTS = {"gauge"}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def bucket_start(epoch: float, seconds: int, offset_seconds: int = 0) -> float:
    """Return the stable epoch for one aligned bucket."""
    return float(int((float(epoch) - offset_seconds) // seconds) * seconds + offset_seconds)


def bucket_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class MetricBucket:
    """One mergeable aggregate with enough evidence for later rollups."""

    observed_epoch: float
    resolution_seconds: int
    metric: str
    instrument: str
    value_sum: float
    sample_count: int
    minimum_value: float
    maximum_value: float
    last_value: float
    last_epoch: float
    unit: str
    source_id: str
    resource_id: str
    dimensions: dict[str, str] = field(default_factory=dict)
    attribution_method: str = "exact"
    confidence: str = "high"
    estimated: bool = False

    @property
    def value(self) -> float:
        if self.instrument in GAUGE_INSTRUMENTS:
            return self.value_sum / max(1, self.sample_count)
        if self.instrument in COUNTER_INSTRUMENTS:
            return self.value_sum
        return self.last_value

    @property
    def observed_at(self) -> str:
        return bucket_timestamp(self.observed_epoch)

    def key(self) -> tuple[object, ...]:
        return (
            self.observed_epoch,
            self.metric,
            self.instrument,
            self.unit,
            self.source_id,
            self.resource_id,
            json.dumps(self.dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            self.attribution_method,
            self.confidence,
            self.estimated,
        )


@dataclass
class _MutableBucket:
    observed_epoch: float
    resolution_seconds: int
    metric: str
    instrument: str
    value_sum: float
    sample_count: int
    minimum_value: float
    maximum_value: float
    last_value: float
    last_epoch: float
    unit: str
    source_id: str
    resource_id: str
    dimensions: dict[str, str]
    attribution_method: str
    confidence: str
    estimated: bool

    def merge(self, bucket: MetricBucket) -> None:
        if self.instrument in COUNTER_INSTRUMENTS:
            self.value_sum += bucket.value
        elif self.instrument in GAUGE_INSTRUMENTS:
            self.value_sum += bucket.value * bucket.sample_count
        else:
            self.value_sum = bucket.value
        self.sample_count += bucket.sample_count
        self.minimum_value = min(self.minimum_value, bucket.minimum_value)
        self.maximum_value = max(self.maximum_value, bucket.maximum_value)
        if bucket.last_epoch >= self.last_epoch:
            self.last_value = bucket.last_value
            self.last_epoch = bucket.last_epoch
        self.estimated = self.estimated or bucket.estimated
        if CONFIDENCE_ORDER.get(bucket.confidence, 2) > CONFIDENCE_ORDER.get(self.confidence, 2):
            self.confidence = bucket.confidence
        if bucket.attribution_method != self.attribution_method:
            self.attribution_method = "inferred"

    def freeze(self) -> MetricBucket:
        return MetricBucket(
            observed_epoch=self.observed_epoch,
            resolution_seconds=self.resolution_seconds,
            metric=self.metric,
            instrument=self.instrument,
            value_sum=self.value_sum,
            sample_count=self.sample_count,
            minimum_value=self.minimum_value,
            maximum_value=self.maximum_value,
            last_value=self.last_value,
            last_epoch=self.last_epoch,
            unit=self.unit,
            source_id=self.source_id,
            resource_id=self.resource_id,
            dimensions=dict(self.dimensions),
            attribution_method=self.attribution_method,
            confidence=self.confidence,
            estimated=self.estimated,
        )


class MetricAccumulator:
    """Merge points or existing buckets into one target resolution."""

    def __init__(self, resolution_seconds: int, *, offset_seconds: int = 0) -> None:
        if resolution_seconds <= 0:
            raise ValueError("resolution_seconds must be positive")
        self.resolution_seconds = int(resolution_seconds)
        self.offset_seconds = int(offset_seconds)
        self._buckets: dict[tuple[object, ...], _MutableBucket] = {}

    @staticmethod
    def _point_bucket(point: MetricPoint) -> MetricBucket:
        epoch = float(point.observed_epoch if point.observed_epoch is not None else datetime.fromisoformat(point.observed_at).timestamp())
        value = float(point.value)
        return MetricBucket(
            observed_epoch=epoch,
            resolution_seconds=0,
            metric=point.metric,
            instrument=point.instrument,
            value_sum=value,
            sample_count=1,
            minimum_value=value,
            maximum_value=value,
            last_value=value,
            last_epoch=epoch,
            unit=point.unit,
            source_id=point.source_id,
            resource_id=point.resource_id,
            dimensions=dict(point.dimensions),
            attribution_method=point.attribution_method,
            confidence=point.confidence,
            estimated=point.estimated,
        )

    def add_point(self, point: MetricPoint) -> None:
        self.add_bucket(self._point_bucket(point))

    def add_bucket(self, bucket: MetricBucket) -> None:
        target_epoch = bucket_start(bucket.observed_epoch, self.resolution_seconds, self.offset_seconds)
        dimensions_json = json.dumps(bucket.dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = (
            target_epoch,
            bucket.metric,
            bucket.instrument,
            bucket.unit,
            bucket.source_id,
            bucket.resource_id,
            dimensions_json,
        )
        current = self._buckets.get(key)
        if current is None:
            weighted_sum = bucket.value * bucket.sample_count if bucket.instrument in GAUGE_INSTRUMENTS else bucket.value
            self._buckets[key] = _MutableBucket(
                observed_epoch=target_epoch,
                resolution_seconds=self.resolution_seconds,
                metric=bucket.metric,
                instrument=bucket.instrument,
                value_sum=weighted_sum,
                sample_count=bucket.sample_count,
                minimum_value=bucket.minimum_value,
                maximum_value=bucket.maximum_value,
                last_value=bucket.last_value,
                last_epoch=bucket.last_epoch,
                unit=bucket.unit,
                source_id=bucket.source_id,
                resource_id=bucket.resource_id,
                dimensions=dict(bucket.dimensions),
                attribution_method=bucket.attribution_method,
                confidence=bucket.confidence,
                estimated=bucket.estimated,
            )
        else:
            current.merge(bucket)

    def add_points(self, points: Iterable[MetricPoint]) -> None:
        for point in points:
            self.add_point(point)

    def buckets(self) -> tuple[MetricBucket, ...]:
        return tuple(item.freeze() for item in self._buckets.values())

    def drain_before(self, cutoff_epoch: float) -> tuple[MetricBucket, ...]:
        keys = [key for key, bucket in self._buckets.items() if bucket.observed_epoch < cutoff_epoch]
        buckets = tuple(self._buckets.pop(key).freeze() for key in keys)
        return buckets

    def drain_all(self) -> tuple[MetricBucket, ...]:
        buckets = self.buckets()
        self._buckets.clear()
        return buckets
