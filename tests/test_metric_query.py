from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.model import MetricPoint  # noqa: E402
from infra_sentinel.metrics.query import MetricQuery, execute_metric_query  # noqa: E402
from infra_sentinel.metrics.store import MetricStore  # noqa: E402


def point(epoch: float, value: int, *, source: str = "local-mihomo") -> MetricPoint:
    return MetricPoint(
        observed_at=f"2026-08-08T12:{int(epoch) // 60:02d}:{int(epoch) % 60:02d}+08:00",
        observed_epoch=epoch,
        metric="network.bytes",
        instrument="counter",
        value=value,
        unit="bytes",
        source_id=source,
        resource_id="network",
        dimensions={"direction": "up"},
    )


def gauge(epoch: float, value: int) -> MetricPoint:
    return MetricPoint(
        observed_at=f"2026-08-08T12:{int(epoch) // 60:02d}:{int(epoch) % 60:02d}+08:00",
        observed_epoch=epoch,
        metric="system.cpu.percent",
        instrument="gauge",
        value=value,
        unit="percent",
        source_id="local-system",
        resource_id="system",
    )


class MetricQueryTests(unittest.TestCase):
    def test_query_aggregates_counter_intervals_by_minute_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            store.write((point(121.0, 10), point(151.0, 20), point(181.0, 7), point(151.0, 4, source="vps:primary")))
            query = MetricQuery.from_payload({
                "since_epoch": 120,
                "until_epoch": 239,
                "resource_id": "network",
                "metric": "network.bytes",
                "bucket_seconds": 60,
            })

            result = execute_metric_query(store, query)

            self.assertFalse(result["truncated"])
            self.assertEqual(
                [(item["observed_epoch"], item["source_id"], item["value"]) for item in result["points"]],
                [(120.0, "local-mihomo", 30.0), (120.0, "vps:primary", 4.0), (180.0, "local-mihomo", 7.0)],
            )

    def test_query_rejects_unsafe_shape_before_accessing_store(self) -> None:
        with self.assertRaisesRegex(ValueError, "90 days"):
            MetricQuery.from_payload({"since_epoch": 0, "until_epoch": 91 * 86_400})
        self.assertEqual(
            MetricQuery.from_payload({"since_epoch": 0, "until_epoch": 365 * 86_400, "bucket_seconds": 86_400}).bucket_seconds,
            86_400,
        )
        with self.assertRaisesRegex(ValueError, "730 days"):
            MetricQuery.from_payload({"since_epoch": 0, "until_epoch": 731 * 86_400, "bucket_seconds": 86_400})
        with self.assertRaisesRegex(ValueError, "unsupported query fields"):
            MetricQuery.from_payload({"sql": "DROP TABLE metric_points"})
        with self.assertRaisesRegex(ValueError, "counter or gauge"):
            MetricQuery.from_payload({"instrument": "histogram"})

    def test_gauge_query_averages_samples_instead_of_summing_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            store.write((gauge(121.0, 20), gauge(151.0, 40), gauge(181.0, 80)))
            query = MetricQuery.from_payload({
                "since_epoch": 120,
                "until_epoch": 239,
                "resource_id": "system",
                "instrument": "gauge",
                "bucket_seconds": 60,
            })

            result = execute_metric_query(store, query)

            self.assertEqual(
                [(item["observed_epoch"], item["value"]) for item in result["points"]],
                [(120.0, 30.0), (180.0, 80.0)],
            )

    def test_daily_query_aligns_buckets_to_the_requested_local_day_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            store.write((point(143_999.0, 10), point(144_001.0, 20)))
            query = MetricQuery.from_payload({
                "since_epoch": 0,
                "until_epoch": 172_799,
                "resource_id": "network",
                "metric": "network.bytes",
                "bucket_seconds": 86_400,
                "bucket_offset_seconds": 57_600,
            })

            result = execute_metric_query(store, query)

            self.assertEqual(
                [(item["observed_epoch"], item["value"]) for item in result["points"]],
                [(57_600.0, 10.0), (144_000.0, 20.0)],
            )

    def test_bucket_offset_must_fit_inside_the_bucket(self) -> None:
        with self.assertRaisesRegex(ValueError, "within the bucket"):
            MetricQuery.from_payload({
                "since_epoch": 0, "until_epoch": 60,
                "bucket_seconds": 60, "bucket_offset_seconds": 60,
            })

    def test_fifteen_minute_bucket_is_supported(self) -> None:
        query = MetricQuery.from_payload({
            "since_epoch": 0,
            "until_epoch": 900,
            "bucket_seconds": 900,
        })

        self.assertEqual(query.bucket_seconds, 900)

if __name__ == "__main__":
    unittest.main()
