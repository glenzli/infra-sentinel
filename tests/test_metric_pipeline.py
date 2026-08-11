from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.model import MetricPoint  # noqa: E402
from infra_sentinel.metrics.pipeline import MetricPipeline  # noqa: E402
from infra_sentinel.metrics.store import MetricStore  # noqa: E402


def point(epoch: float, value: float, *, instrument: str = "counter") -> MetricPoint:
    return MetricPoint(
        observed_at="2026-08-12T12:00:00+08:00",
        observed_epoch=epoch,
        metric="test.value",
        instrument=instrument,  # type: ignore[arg-type]
        value=value,
        unit="items",
        source_id="test-source",
        resource_id="test-resource",
    )


class MetricPipelineTests(unittest.TestCase):
    def test_open_bucket_stays_in_memory_but_is_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            pipeline = MetricPipeline(store, resolution_seconds=900)

            result = pipeline.ingest((point(100, 10), point(200, 20)), now_epoch=200)

            self.assertEqual(result.accepted_points, 2)
            self.assertEqual(result.flushed_buckets, 0)
            self.assertEqual(store.summary()["metric_points"], 0)
            rows = pipeline.query_points(
                since_epoch=0,
                until_epoch=899,
                metric="test.value",
                instrument="counter",
                bucket_seconds=900,
            )
            self.assertEqual([(row["observed_epoch"], row["value"]) for row in rows], [(0.0, 30.0)])

    def test_completed_bucket_flushes_once_and_shutdown_merges_partial_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            store = MetricStore(state_dir)
            pipeline = MetricPipeline(store, resolution_seconds=900)
            pipeline.ingest((point(100, 10), point(200, 20)), now_epoch=200)

            result = pipeline.ingest((point(910, 7),), now_epoch=910)

            self.assertEqual(result.flushed_buckets, 1)
            persisted = store.query_points(
                since_epoch=0,
                until_epoch=899,
                metric="test.value",
                instrument="counter",
                bucket_seconds=900,
            )
            self.assertEqual([row["value"] for row in persisted], [30.0])
            self.assertEqual(pipeline.flush_all(), 1)

            restarted = MetricPipeline(MetricStore(state_dir), resolution_seconds=900)
            restarted.ingest((point(920, 8),), now_epoch=920)
            restarted.flush_all()
            merged = restarted.store.query_points(
                since_epoch=900,
                until_epoch=1799,
                metric="test.value",
                instrument="counter",
                bucket_seconds=900,
            )
            self.assertEqual([row["value"] for row in merged], [15.0])

    def test_gauge_keeps_weighted_mean_range_and_latest_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline = MetricPipeline(MetricStore(Path(temporary)), resolution_seconds=900)
            pipeline.ingest(
                (point(100, 10, instrument="gauge"), point(200, 40, instrument="gauge")),
                now_epoch=200,
            )

            rows = pipeline.query_points(
                since_epoch=0,
                until_epoch=899,
                metric="test.value",
                instrument="gauge",
                bucket_seconds=900,
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["value"], 25.0)
            self.assertEqual(rows[0]["minimum_value"], 10.0)
            self.assertEqual(rows[0]["maximum_value"], 40.0)
            self.assertEqual(rows[0]["last_value"], 40.0)
            self.assertEqual(rows[0]["sample_count"], 2)


if __name__ == "__main__":
    unittest.main()
