from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_projection import build_infra_projection  # noqa: E402
from infra_collectors import CollectorCapability, CollectorRun  # noqa: E402


def sample() -> dict[str, object]:
    return {"timestamp": "2026-08-08T10:00:00+08:00"}


class InfraProjectionTests(unittest.TestCase):
    def test_local_network_projection_preserves_exact_counter(self) -> None:
        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 1234}, "vps": {"total_bytes": 0}},
            {"enabled": False, "status": "disabled", "servers": []},
            "none",
        )

        self.assertEqual(projection["schema"], "20260809.1")
        self.assertEqual(projection["overall"]["status"], "healthy")
        self.assertEqual(projection["resources"][0]["primary_metric"], "network.local_bytes")
        self.assertEqual(projection["resources"][0]["primary_value"], 1234)
        self.assertEqual(projection["metrics"][0]["value"], 1234)
        self.assertFalse(projection["metrics"][0]["estimated"])

    def test_remote_network_projection_exposes_separate_sources_and_billing(self) -> None:
        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 100}, "vps": {"total_bytes": 300}},
            {
                "enabled": True,
                "status": "ok",
                "servers": [{
                    "id": "primary",
                    "label": "Primary VPS",
                    "vps": {"enabled": True, "status": "ok", "updated_at": "now"},
                    "xray_stats": {"enabled": True, "status": "ok", "updated_at": "now"},
                }],
            },
            "warning",
        )

        self.assertEqual(projection["overall"]["status"], "warning")
        self.assertEqual(projection["resources"][0]["primary_metric"], "network.billable_bytes")
        self.assertEqual(projection["resources"][0]["primary_value"], 300)
        self.assertEqual(projection["resources"][0]["source_count"], 3)
        self.assertEqual(projection["resources"][0]["online_source_count"], 3)
        self.assertEqual({item["id"] for item in projection["sources"]}, {"local-mihomo", "vps:primary", "xray:primary"})

    def test_remote_collector_error_degrades_health_without_an_alert(self) -> None:
        projection = build_infra_projection(
            sample(),
            {"kernel": {}, "vps": {}},
            {"enabled": True, "status": "error", "servers": []},
            "none",
        )
        self.assertEqual(projection["overall"]["status"], "degraded")

    def test_metric_adapter_failure_is_exposed_as_source_health(self) -> None:
        run = CollectorRun(
            capability=CollectorCapability(
                id="network.mihomo", source_id="local-mihomo", source_kind="network.mihomo",
                resource_id="network", metrics=("network.bytes",),
            ),
            status="error",
            error_kind="ValueError",
        )

        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 1}, "vps": {"total_bytes": 0}},
            {"enabled": False, "status": "disabled", "servers": []},
            "none",
            (run,),
        )

        self.assertEqual(projection["overall"]["status"], "degraded")
        self.assertEqual(projection["sources"][0]["status"], "error")
        self.assertEqual(projection["collectors"][0]["error_kind"], "ValueError")

    def test_available_opencode_is_a_separate_ai_resource_with_no_network_source_changes(self) -> None:
        run = CollectorRun(
            capability=CollectorCapability(
                id="ai.opencode.session-usage", source_id="opencode", source_kind="ai.opencode",
                resource_id="ai_usage", metrics=("ai.tokens.input",),
            ),
            status="ok",
            snapshot={
                "available": True,
                "status": "ok",
                "label": "OpenCode",
                "observed_at": "2026-08-08T10:00:00+08:00",
                "tokens": {"total": 12_345},
            },
        )

        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 100}, "vps": {"total_bytes": 0}},
            {"enabled": False, "status": "disabled", "servers": []},
            "none",
            (run,),
        )

        ai_resource = next(item for item in projection["resources"] if item["id"] == "ai_usage")
        self.assertEqual(ai_resource["primary_value"], 12_345)
        self.assertEqual(ai_resource["primary_unit"], "tokens")
        self.assertEqual(projection["ai_usage"]["opencode"]["label"], "OpenCode")
        self.assertEqual({item["id"] for item in projection["sources"]}, {"local-mihomo", "opencode"})


if __name__ == "__main__":
    unittest.main()
