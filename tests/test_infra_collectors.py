from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import (  # noqa: E402
    CallableCollector,
    CollectorCapability,
    CollectorContext,
    CollectorRegistry,
    collected_points,
)
from infra_sentinel.core.model import MetricPoint  # noqa: E402
from infra_sentinel.resources.network.metrics import network_collector_registry, network_metrics  # noqa: E402


def point_identity(point: MetricPoint) -> str:
    return json.dumps(point.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CollectorRegistryTests(unittest.TestCase):
    def test_one_failed_virtual_collector_does_not_block_other_collectors(self) -> None:
        good_capability = CollectorCapability(
            id="test.good", source_id="test:good", source_kind="test", resource_id="test",
            metrics=("test.counter",),
        )
        bad_capability = CollectorCapability(
            id="test.bad", source_id="test:bad", source_kind="test", resource_id="test",
            metrics=("test.counter",),
        )
        point = MetricPoint(
            observed_at="2026-08-08T12:00:00+08:00", observed_epoch=1.0,
            metric="test.counter", instrument="counter", value=42, unit="items",
            source_id="test:good", resource_id="test",
        )
        registry = CollectorRegistry((
            CallableCollector(good_capability, lambda _: (point,)),
            CallableCollector(bad_capability, lambda _: (_ for _ in ()).throw(RuntimeError("expected"))),
        ))

        runs = registry.collect(CollectorContext({}, {}))

        self.assertEqual([run.status for run in runs], ["ok", "error"])
        self.assertEqual(runs[1].error_kind, "RuntimeError")
        self.assertEqual(list(collected_points(runs)), [point])
        self.assertEqual([item["id"] for item in registry.capabilities()], ["test.good", "test.bad"])

    def test_network_collectors_match_the_existing_network_metric_fixture(self) -> None:
        local = {
            "timestamp": "2026-08-08T12:00:00+08:00", "epoch": 1.0,
            "kernel": {"up_bytes": 12, "down_bytes": 30},
            "routes": {"proxy": {"up_bytes": 10, "down_bytes": 20}, "unattributed": {"up_bytes": 2, "down_bytes": 10}},
        }
        remote = {
            "servers": [{
                "id": "primary",
                "vps": {"last_sample": {
                    "timestamp": "2026-08-08T12:05:00+08:00", "epoch": 2.0,
                    "in_bytes": 100, "out_bytes": 120,
                }},
                "xray_stats": {"last_sample": {
                    "timestamp": "2026-08-08T12:05:00+08:00", "epoch": 2.0,
                    "users": {"mac": {"up_bytes": 25, "down_bytes": 75}},
                }},
            }],
        }

        legacy = sorted(point_identity(point) for point in network_metrics(local, remote))
        registry = network_collector_registry((("primary", "both"), ("unused", "both")))
        adapted = sorted(point_identity(point) for point in collected_points(
            registry.collect(CollectorContext(local, remote))
        ))

        self.assertEqual(adapted, legacy)

    def test_outbound_billing_collector_omits_incoming_interface_bytes(self) -> None:
        remote = {"servers": [{
            "id": "primary", "billing_mode": "outbound",
            "vps": {"last_sample": {
                "timestamp": "2026-08-09T12:05:00+08:00", "epoch": 2.0,
                "in_bytes": 100, "out_bytes": 120,
            }},
        }]}
        registry = network_collector_registry((("primary", "outbound"),))

        points = list(collected_points(registry.collect(CollectorContext({}, remote))))

        self.assertEqual([point.value for point in points if point.metric == "network.billable_bytes"], [120])
        self.assertEqual([point.dimensions["direction"] for point in points if point.metric == "network.billable_bytes"], ["out"])


if __name__ == "__main__":
    unittest.main()
