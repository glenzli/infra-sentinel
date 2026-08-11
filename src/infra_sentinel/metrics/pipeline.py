"""Live metric buffering and low-frequency durable persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
import threading
import time
from typing import Any, Iterable

from infra_sentinel.core.model import MetricPoint
from infra_sentinel.metrics.aggregation import MetricAccumulator, MetricBucket, bucket_start
from infra_sentinel.metrics.store import HOT_RESOLUTION_SECONDS, MetricStore


@dataclass(frozen=True)
class MetricIngestResult:
    accepted_points: int
    flushed_buckets: int


class MetricPipeline:
    """Own the hot in-memory window, durable flushes, and query overlay.

    The sampler may call ``ingest`` every few seconds.  SQLite only receives a
    transaction when a 15-minute bucket completes, while queries merge the
    still-open bucket with persisted history.
    """

    def __init__(self, store: MetricStore, *, resolution_seconds: int = HOT_RESOLUTION_SECONDS) -> None:
        self.store = store
        self.resolution_seconds = int(resolution_seconds)
        self._accumulator = MetricAccumulator(self.resolution_seconds)
        self._lock = threading.RLock()
        self._last_flush_epoch = 0.0

    def ingest(
        self,
        points: Iterable[MetricPoint],
        now_epoch: float | None = None,
    ) -> MetricIngestResult:
        materialized = tuple(points)
        now = time.time() if now_epoch is None else float(now_epoch)
        with self._lock:
            self._accumulator.add_points(materialized)
            flushed = self._flush_completed_locked(now)
        return MetricIngestResult(len(materialized), flushed)

    def _flush_completed_locked(self, now_epoch: float) -> int:
        cutoff = bucket_start(now_epoch, self.resolution_seconds)
        buckets = self._accumulator.drain_before(cutoff)
        if not buckets:
            return 0
        try:
            self.store.write_buckets(buckets)
        except Exception:
            for bucket in buckets:
                self._accumulator.add_bucket(bucket)
            raise
        self._last_flush_epoch = now_epoch
        return len(buckets)

    def flush_completed(self, now_epoch: float | None = None) -> int:
        now = time.time() if now_epoch is None else float(now_epoch)
        with self._lock:
            return self._flush_completed_locked(now)

    def flush_all(self) -> int:
        """Persist the partial current bucket during an orderly shutdown."""
        with self._lock:
            buckets = self._accumulator.drain_all()
            if not buckets:
                return 0
            try:
                self.store.write_buckets(buckets)
            except Exception:
                for bucket in buckets:
                    self._accumulator.add_bucket(bucket)
                raise
            self._last_flush_epoch = time.time()
            return len(buckets)

    @staticmethod
    def _matches(
        bucket: MetricBucket,
        *,
        since_epoch: float,
        until_epoch: float,
        resource_id: str | None,
        source_id: str | None,
        metric: str | None,
        instrument: str | None,
    ) -> bool:
        return (
            since_epoch <= bucket.observed_epoch <= until_epoch
            and (resource_id is None or bucket.resource_id == resource_id)
            and (source_id is None or bucket.source_id == source_id)
            and (metric is None or bucket.metric == metric)
            and (instrument is None or bucket.instrument == instrument)
        )

    @staticmethod
    def _from_query_point(point: dict[str, Any], resolution_seconds: int) -> MetricBucket:
        value = float(point.get("value", 0))
        samples = max(1, int(point.get("sample_count", 1)))
        instrument = str(point.get("instrument", "counter"))
        dimensions = point.get("dimensions", {})
        return MetricBucket(
            observed_epoch=float(point.get("observed_epoch", 0)),
            resolution_seconds=max(resolution_seconds, int(point.get("resolution_seconds", 0))),
            metric=str(point.get("metric", "")),
            instrument=instrument,
            value_sum=value * samples if instrument == "gauge" else value,
            sample_count=samples,
            minimum_value=float(point.get("minimum_value", value)),
            maximum_value=float(point.get("maximum_value", value)),
            last_value=float(point.get("last_value", value)),
            last_epoch=float(point.get("last_epoch", point.get("observed_epoch", 0))),
            unit=str(point.get("unit", "")),
            source_id=str(point.get("source_id", "")),
            resource_id=str(point.get("resource_id", "")),
            dimensions=dimensions if isinstance(dimensions, dict) else {},
            attribution_method=str(point.get("attribution_method", "exact")),
            confidence=str(point.get("confidence", "high")),
            estimated=bool(point.get("estimated", False)),
        )

    @staticmethod
    def _as_query_point(bucket: MetricBucket) -> dict[str, Any]:
        return {
            "observed_epoch": bucket.observed_epoch,
            "metric": bucket.metric,
            "instrument": bucket.instrument,
            "value": bucket.value,
            "unit": bucket.unit,
            "source_id": bucket.source_id,
            "resource_id": bucket.resource_id,
            "dimensions": dict(bucket.dimensions),
            "attribution_method": bucket.attribution_method,
            "confidence": bucket.confidence,
            "estimated": bucket.estimated,
            "sample_count": bucket.sample_count,
            "minimum_value": bucket.minimum_value,
            "maximum_value": bucket.maximum_value,
            "last_value": bucket.last_value,
            "last_epoch": bucket.last_epoch,
            "resolution_seconds": bucket.resolution_seconds,
        }

    def query_points(
        self,
        *,
        since_epoch: float,
        until_epoch: float,
        resource_id: str | None = None,
        source_id: str | None = None,
        metric: str | None = None,
        instrument: str | None = None,
        bucket_seconds: int | None = None,
        bucket_offset_seconds: int = 0,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        requested_resolution = bucket_seconds or self.resolution_seconds
        # Keep the durable read and hot snapshot on one side of any flush.
        # Otherwise a bucket could move from memory to SQLite between the two
        # reads and be omitted (or counted twice) in one UI response.
        with self._lock:
            persisted = self.store.query_points(
                since_epoch=since_epoch,
                until_epoch=until_epoch,
                resource_id=resource_id,
                source_id=source_id,
                metric=metric,
                instrument=instrument,
                bucket_seconds=requested_resolution,
                bucket_offset_seconds=bucket_offset_seconds,
                limit=limit,
            )
            buffered = self._accumulator.buckets()
        accumulator = MetricAccumulator(requested_resolution, offset_seconds=bucket_offset_seconds)
        for point in persisted:
            accumulator.add_bucket(self._from_query_point(point, requested_resolution))
        for bucket in buffered:
            if self._matches(
                bucket,
                since_epoch=since_epoch,
                until_epoch=until_epoch,
                resource_id=resource_id,
                source_id=source_id,
                metric=metric,
                instrument=instrument,
            ):
                accumulator.add_bucket(bucket)
        points = [self._as_query_point(bucket) for bucket in accumulator.buckets()]
        points.sort(key=lambda item: (
            float(item["observed_epoch"]),
            str(item["metric"]),
            str(item["source_id"]),
            json.dumps(item["dimensions"], sort_keys=True, separators=(",", ":")),
        ))
        return points[:limit]
