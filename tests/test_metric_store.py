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
    CODEX_JSONL_HISTORY_MIGRATION,
    DAILY_RESOLUTION_SECONDS,
    HOURLY_RESOLUTION_SECONDS,
    HOT_RESOLUTION_SECONDS,
    MetricStore,
    STORE_SCHEMA,
)
from infra_sentinel.resources.network.metrics import (  # noqa: E402
    local_sample_metrics,
    remote_state_metrics,
    vps_sample_metrics,
    xray_sample_metrics,
)


class MetricStoreTests(unittest.TestCase):
    def test_new_codex_rebuild_marker_replaces_history_after_prior_migration(self) -> None:
        old_migration = "codex-jsonl-history-20260824.1"
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            old_point = MetricPoint(
                observed_at="2026-08-22T12:00:00+08:00", observed_epoch=100,
                metric="ai.tokens.total", instrument="counter", value=42, unit="tokens",
                source_id="codex", resource_id="ai_usage",
            )
            later_point = MetricPoint(
                observed_at="2026-08-22T12:05:00+08:00", observed_epoch=200,
                metric="ai.tokens.total", instrument="counter", value=9, unit="tokens",
                source_id="codex", resource_id="ai_usage",
            )
            store.write((old_point,))
            store.replace_source_history_once("codex", migration_key=old_migration, points=(later_point,))
            store.write((old_point,))
            report = store.replace_source_history_once(
                "codex", migration_key=CODEX_JSONL_HISTORY_MIGRATION,
            )
            self.assertEqual(report["status"], "replaced")
            self.assertEqual(report["deleted"], 2)
            self.assertEqual(store.metadata(old_migration)["inserted"], 1)
            self.assertEqual(store.metadata(CODEX_JSONL_HISTORY_MIGRATION)["inserted"], 0)

    def test_source_history_replacement_is_scoped_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            codex = MetricPoint(
                observed_at="2026-08-22T12:00:00+08:00", observed_epoch=100,
                metric="ai.tokens.total", instrument="counter", value=42, unit="tokens",
                source_id="codex", resource_id="ai_usage",
            )
            other = MetricPoint(
                observed_at="2026-08-22T12:00:00+08:00", observed_epoch=100,
                metric="ai.tokens.total", instrument="counter", value=7, unit="tokens",
                source_id="opencode", resource_id="ai_usage",
            )
            replacement = MetricPoint(
                observed_at="2026-08-22T12:05:00+08:00", observed_epoch=200,
                metric="ai.tokens.total", instrument="counter", value=9, unit="tokens",
                source_id="codex", resource_id="ai_usage",
            )
            store.write((codex, other))
            report = store.replace_source_history_once(
                "codex", migration_key=CODEX_JSONL_HISTORY_MIGRATION, points=(replacement,),
            )
            repeated = store.replace_source_history_once(
                "codex", migration_key=CODEX_JSONL_HISTORY_MIGRATION,
            )
            self.assertEqual(report["status"], "replaced")
            self.assertEqual(report["deleted"], 1)
            self.assertEqual(report["inserted"], 1)
            self.assertEqual(repeated["status"], "current")
            self.assertEqual(store.metadata(CODEX_JSONL_HISTORY_MIGRATION)["inserted"], 1)
            codex_rows = store.query_points(since_epoch=0, until_epoch=1_000, source_id="codex")
            other_rows = store.query_points(since_epoch=0, until_epoch=1_000, source_id="opencode")
            self.assertEqual([row["value"] for row in codex_rows], [9])
            self.assertEqual([row["value"] for row in other_rows], [7])

    def test_source_history_replacement_rejects_cross_source_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            point = MetricPoint(
                observed_at="2026-08-22T12:00:00+08:00", observed_epoch=100,
                metric="ai.tokens.total", instrument="counter", value=7, unit="tokens",
                source_id="opencode", resource_id="ai_usage",
            )
            with self.assertRaisesRegex(ValueError, "source"):
                store.replace_source_history_once(
                    "codex", migration_key=CODEX_JSONL_HISTORY_MIGRATION, points=(point,),
                )

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

    def test_xray_client_deltas_use_the_same_calendar_queries_as_other_network_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            yesterday = 1_786_406_400.0
            today = yesterday + 86_400
            store.write((
                *xray_sample_metrics("primary", {
                    "timestamp": "2026-08-11T00:05:00+08:00", "epoch": yesterday,
                    "users": {"mac": {"up_bytes": 4, "down_bytes": 6}},
                }),
                *xray_sample_metrics("primary", {
                    "timestamp": "2026-08-12T00:05:00+08:00", "epoch": today,
                    "users": {"mac": {"up_bytes": 7, "down_bytes": 9}},
                }),
            ))

            rows = store.query_points(
                since_epoch=today, until_epoch=today + 86_399,
                resource_id="network", metric="network.logical_bytes", source_id="xray:primary",
                instrument="counter", bucket_seconds=DAILY_RESOLUTION_SECONDS,
            )

            self.assertEqual(sum(row["value"] for row in rows), 16)
            self.assertEqual({row["dimensions"]["client"] for row in rows}, {"mac"})

    def test_remote_history_rebuild_replaces_polluted_window_from_exact_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            remote_dir = state_dir / "remote" / "primary"
            remote_dir.mkdir(parents=True)
            epoch = 1_786_473_000.0
            vps_sample = {
                "schema": 3, "timestamp": "2026-08-12T02:30:00+08:00", "epoch": epoch,
                "interval_started_epoch": epoch - 300, "in_bytes": 3, "out_bytes": 4,
            }
            xray_sample = {
                "schema": 1, "timestamp": "2026-08-12T02:30:00+08:00", "epoch": epoch,
                "interval_started_epoch": epoch - 300,
                "users": {"mac": {"up_bytes": 5, "down_bytes": 6}},
            }
            (remote_dir / "vps_samples.jsonl").write_text(json.dumps(vps_sample) + "\n", encoding="utf-8")
            (remote_dir / "xray_user_samples.jsonl").write_text(json.dumps(xray_sample) + "\n", encoding="utf-8")
            store = MetricStore(state_dir)
            store.initialize()
            with store._transaction(write=True) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    ("20260812.1", "2026-08-11 18:28:28"),
                )
            polluted = MetricAccumulator(HOT_RESOLUTION_SECONDS)
            for _ in range(12):
                polluted.add_points(vps_sample_metrics("primary", vps_sample))
                polluted.add_points(xray_sample_metrics("primary", xray_sample))
            store.write_buckets(polluted.buckets())
            local = MetricPoint(
                observed_at="2026-08-12T02:30:00+08:00", observed_epoch=epoch,
                metric="network.bytes", instrument="counter", value=99, unit="bytes",
                source_id="local-mihomo", resource_id="network", dimensions={"direction": "up"},
            )
            store.write((local,))

            report = store.rebuild_remote_history()

            remote_rows = store.query_points(
                since_epoch=epoch - 1, until_epoch=epoch + 1, resource_id="network",
                instrument="counter",
            )
            remote_rows = [row for row in remote_rows if row["source_id"] != "local-mihomo"]
            self.assertEqual(report["status"], "rebuilt")
            self.assertEqual(sum(row["value"] for row in remote_rows), 18)
            self.assertEqual({row["sample_count"] for row in remote_rows}, {1})
            self.assertEqual(store.rebuild_remote_history(), {"status": "current"})
            self.assertTrue(store.summary()["remote_history_rebuild_complete"])
            self.assertEqual(store.query_points(
                since_epoch=epoch - 1, until_epoch=epoch + 1, source_id="local-mihomo",
            )[0]["value"], 99)

    def test_remote_history_rebuild_fails_closed_without_matching_raw_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MetricStore(Path(temporary))
            store.initialize()
            with store._transaction(write=True) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    ("20260812.1", "2026-08-11 18:28:28"),
                )
            point = MetricPoint(
                observed_at="2026-08-12T02:30:00+08:00", observed_epoch=1_786_473_000.0,
                metric="network.billable_bytes", instrument="counter", value=120, unit="bytes",
                source_id="vps:missing", resource_id="network", dimensions={"direction": "out"},
            )
            polluted = MetricAccumulator(HOT_RESOLUTION_SECONDS)
            polluted.add_point(point)
            store.write_buckets(polluted.buckets())

            report = store.rebuild_remote_history()

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["missing"], [{
                "metric": "network.billable_bytes", "source_id": "vps:missing",
            }])
            self.assertEqual(store.query_points(
                since_epoch=0, until_epoch=2_000_000_000,
                source_id="vps:missing", metric="network.billable_bytes",
            )[0]["value"], 120)

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
