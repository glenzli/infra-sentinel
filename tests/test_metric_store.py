from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.model import MetricPoint  # noqa: E402
from infra_sentinel.metrics.aggregation import MetricAccumulator  # noqa: E402
from infra_sentinel.metrics.store import (  # noqa: E402
    DAILY_RESOLUTION_SECONDS,
    HOURLY_RESOLUTION_SECONDS,
    HOT_RESOLUTION_SECONDS,
    MetricStore,
    STORE_SCHEMA,
)
from infra_sentinel.resources.network.metrics import local_sample_metrics, remote_state_metrics  # noqa: E402


class MetricStoreTests(unittest.TestCase):
    def test_idempotent_metric_writes_keep_one_fact_per_source_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            point = MetricPoint(
                observed_at="2026-08-08T12:00:00+08:00", observed_epoch=1_786_083_200.0,
                metric="network.bytes", instrument="counter", value=42, unit="bytes",
                source_id="local-mihomo", resource_id="network", dimensions={"direction": "up"},
            )
            self.assertEqual(store.write([point]), 1)
            self.assertEqual(store.write([point]), 0)
            self.assertEqual(store.summary()["metric_points"], 1)
            self.assertEqual(store.summary()["schema"], STORE_SCHEMA)

    def test_legacy_import_is_atomic_and_only_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            sample = {
                "schema": 5, "timestamp": "2026-08-08T12:00:00+08:00", "epoch": 1_786_083_200.0,
                "kernel": {"up_bytes": 12, "down_bytes": 30}, "routes": {},
            }
            (state_dir / "samples.jsonl").write_text(json.dumps(sample) + "\n", encoding="utf-8")
            store = MetricStore(state_dir)
            self.assertEqual(store.import_legacy_network(), 2)
            self.assertEqual(store.import_legacy_network(), 0)
            summary = store.summary()
            self.assertTrue(summary["legacy_import_complete"])
            self.assertEqual(summary["metric_points"], 2)

    def test_network_adapter_keeps_remote_identities_separate(self) -> None:
        local = local_sample_metrics({
            "timestamp": "2026-08-08T12:00:00+08:00", "epoch": 1.0,
            "kernel": {"up_bytes": 1, "down_bytes": 2}, "routes": {},
        })
        remote = remote_state_metrics({"servers": [{
            "id": "primary",
            "vps": {"last_sample": {"timestamp": "2026-08-08T12:05:00+08:00", "epoch": 2.0, "in_bytes": 3, "out_bytes": 4}},
            "xray_stats": {"last_sample": {"timestamp": "2026-08-08T12:05:00+08:00", "epoch": 2.0, "users": {"mac": {"up_bytes": 5, "down_bytes": 6}}}},
        }]})
        self.assertEqual({point.source_id for point in local}, {"local-mihomo"})
        self.assertEqual({point.source_id for point in remote}, {"vps:primary", "xray:primary"})

    def test_local_adapter_records_service_dimensions_without_connection_identity(self) -> None:
        points = local_sample_metrics({
            "timestamp": "2026-08-08T12:00:00+08:00", "epoch": 1.0,
            "kernel": {"up_bytes": 1, "down_bytes": 2}, "routes": {},
            "services": [{"id": "chatgpt", "label": "ChatGPT", "up_bytes": 5, "down_bytes": 7}],
        })
        services = [point for point in points if point.metric == "network.service_bytes"]

        self.assertEqual(len(services), 2)
        self.assertEqual({point.dimensions["service"] for point in services}, {"chatgpt"})
        self.assertEqual({point.dimensions["label"] for point in services}, {"ChatGPT"})

    def test_history_maintenance_preserves_counter_sums_in_15_minute_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            points = (
                MetricPoint(
                    observed_at="2026-08-08T12:00:00+08:00", observed_epoch=100,
                    metric="network.bytes", instrument="counter", value=10, unit="bytes",
                    source_id="local", resource_id="network",
                ),
                MetricPoint(
                    observed_at="2026-08-08T12:01:00+08:00", observed_epoch=200,
                    metric="network.bytes", instrument="counter", value=20, unit="bytes",
                    source_id="local", resource_id="network",
                ),
            )
            store.write(points)

            report = store.maintain_history(now_epoch=1_800, force=True)

            self.assertEqual(report["raw_to_15m"]["source_points"], 2)
            rows = store.query_points(
                since_epoch=0, until_epoch=899, metric="network.bytes",
                instrument="counter", bucket_seconds=HOT_RESOLUTION_SECONDS,
            )
            self.assertEqual([(row["value"], row["sample_count"]) for row in rows], [(30.0, 2)])

    def test_history_maintenance_rolls_older_buckets_to_hour_and_day(self) -> None:
        day = DAILY_RESOLUTION_SECONDS
        now = 200 * day + 4_000
        accumulator = MetricAccumulator(HOT_RESOLUTION_SECONDS)
        accumulator.add_points((
            MetricPoint(
                observed_at="2026-08-01T00:00:00+08:00", observed_epoch=now - 8 * day,
                metric="network.bytes", instrument="counter", value=11, unit="bytes",
                source_id="local", resource_id="network",
            ),
            MetricPoint(
                observed_at="2026-05-01T00:00:00+08:00", observed_epoch=now - 91 * day,
                metric="network.bytes", instrument="counter", value=13, unit="bytes",
                source_id="local", resource_id="network",
            ),
        ))
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            store.write_buckets(accumulator.buckets())

            report = store.maintain_history(now_epoch=now, force=True)

            self.assertEqual(report["15m_to_hour"]["source_points"], 2)
            self.assertEqual(report["hour_to_day"]["source_points"], 1)
            with store._transaction() as connection:  # verify the durable tier contract
                resolutions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT resolution_seconds FROM metric_points ORDER BY observed_epoch"
                    )
                ]
            self.assertEqual(resolutions, [DAILY_RESOLUTION_SECONDS, HOURLY_RESOLUTION_SECONDS])


if __name__ == "__main__":
    unittest.main()
