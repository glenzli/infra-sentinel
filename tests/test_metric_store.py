from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_model import MetricPoint  # noqa: E402
from metric_store import MetricStore, STORE_SCHEMA  # noqa: E402
from network_metrics import local_sample_metrics, remote_state_metrics  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
