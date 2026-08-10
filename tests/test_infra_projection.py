from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_projection import build_infra_projection  # noqa: E402
from infra_collectors import CollectorCapability, CollectorRun  # noqa: E402
from ai_usage_contract import ai_usage_snapshot, localized, usage_window  # noqa: E402


def sample() -> dict[str, object]:
    return {"timestamp": "2026-08-08T10:00:00+08:00"}


class InfraProjectionTests(unittest.TestCase):
    @staticmethod
    def ai_snapshot(source_id: str, label: str, *, today: int | None, cumulative: int | None) -> dict[str, object]:
        return ai_usage_snapshot(
            source_id=source_id,
            label=label,
            status="ok",
            observed_at="2026-08-08T10:00:00+08:00",
            collection_method="fixture",
            today=usage_window(today, method="fixture-day", detail=localized("fixture", "测试")),
            cumulative=usage_window(cumulative, method="fixture-history", detail=localized("fixture", "测试")),
            models=[],
            details=[],
            confidence="high",
            privacy="aggregate-only",
        )

    def test_local_network_projection_preserves_exact_counter(self) -> None:
        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 1234}, "vps": {"total_bytes": 0}},
            {"enabled": False, "status": "disabled", "servers": []},
            "none",
        )

        self.assertEqual(projection["schema"], "20260811.1")
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
            snapshot=self.ai_snapshot("opencode", "OpenCode", today=12_345, cumulative=None),
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
        self.assertEqual(projection["ai_usage"]["sources"][0]["label"], "OpenCode")
        self.assertEqual({item["id"] for item in projection["sources"]}, {"local-mihomo", "opencode"})

    def test_codex_and_opencode_share_ai_resource_without_mixing_windows(self) -> None:
        open_code = CollectorRun(
            capability=CollectorCapability("ai.opencode.session-usage", "opencode", "ai.opencode", "ai_usage", ("ai.tokens.input",)),
            status="ok",
            snapshot=self.ai_snapshot("opencode", "OpenCode", today=100, cumulative=300),
        )
        codex = CollectorRun(
            capability=CollectorCapability("ai.codex.local-workload", "codex", "ai.codex", "ai_usage", ("ai.tokens.total",)),
            status="ok",
            snapshot=self.ai_snapshot("codex", "Codex", today=25, cumulative=200),
        )
        projection = build_infra_projection(
            sample(), {"kernel": {}, "vps": {}}, {"enabled": False, "status": "disabled", "servers": []}, "none", (open_code, codex),
        )

        ai_resource = next(item for item in projection["resources"] if item["id"] == "ai_usage")
        self.assertEqual(ai_resource["primary_source_id"], "ai_usage.aggregate")
        self.assertEqual(ai_resource["source_count"], 2)
        self.assertEqual(set(projection["ai_usage"]), {"schema", "sources", "aggregate"})
        self.assertEqual(projection["ai_usage"]["aggregate"]["today"]["tokens"], 125)
        self.assertEqual(projection["ai_usage"]["aggregate"]["cumulative"]["tokens"], 500)
        self.assertEqual({item["id"] for item in projection["sources"]}, {"local-mihomo", "opencode", "codex"})

    def test_future_ai_provider_is_projected_without_a_provider_specific_branch(self) -> None:
        future = CollectorRun(
            capability=CollectorCapability("ai.future.usage", "future-agent", "ai.future-agent", "ai_usage", ("ai.tokens.total",)),
            status="ok",
            snapshot=self.ai_snapshot("future-agent", "Future Agent", today=42, cumulative=420),
        )
        projection = build_infra_projection(
            sample(), {"kernel": {}, "vps": {}}, {"enabled": False, "status": "disabled", "servers": []}, "none", (future,),
        )

        resource = next(item for item in projection["resources"] if item["id"] == "ai_usage")
        self.assertEqual(resource["source_count"], 1)
        self.assertEqual(projection["ai_usage"]["aggregate"]["today"]["tokens"], 42)
        self.assertEqual(projection["ai_usage"]["sources"][0]["source_id"], "future-agent")
        self.assertIn("future-agent", {item["id"] for item in projection["sources"]})

    def test_facility_health_is_separate_from_resources_and_contributes_to_overall_status(self) -> None:
        facilities = {
            "schema": "20260810.1",
            "status": "degraded",
            "total": 2,
            "healthy": 1,
            "attention": 1,
            "items": [
                {"id": "pcp:default", "status": "healthy"},
                {"id": "infer-runtime:default", "status": "stale"},
            ],
        }

        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 100}, "vps": {"total_bytes": 0}},
            {"enabled": False, "status": "disabled", "servers": []},
            "none",
            (),
            facilities,
        )

        self.assertEqual({resource["id"] for resource in projection["resources"]}, {"network"})
        self.assertEqual(projection["facilities"], facilities)
        self.assertEqual(projection["overall"]["status"], "degraded")
        self.assertEqual(projection["overall"]["active_alerts"], 1)

    def test_upstream_status_is_an_independent_resource_and_only_confirmed_incidents_affect_health(self) -> None:
        upstream = {
            "schema": "20260811.1",
            "status": "warning",
            "total": 3,
            "healthy": 2,
            "attention": 1,
            "unknown": 0,
            "items": [
                {"id": "openai", "label": "OpenAI", "status": "healthy", "available": True, "observed_at": "now"},
                {"id": "claude", "label": "Claude", "status": "warning", "available": True, "observed_at": "now"},
                {"id": "deepseek", "label": "DeepSeek", "status": "healthy", "available": True, "observed_at": "now"},
            ],
        }
        projection = build_infra_projection(
            sample(),
            {"kernel": {"total_bytes": 100}, "vps": {"total_bytes": 0}},
            {"enabled": False, "status": "disabled", "servers": []},
            "none",
            (),
            None,
            upstream,
        )

        resources = {resource["id"]: resource for resource in projection["resources"]}
        self.assertEqual(resources["network"]["source_count"], 1)
        self.assertEqual(resources["upstream_status"]["primary_value"], 2)
        self.assertEqual(resources["upstream_status"]["online_source_count"], 3)
        self.assertEqual(projection["overall"]["status"], "warning")
        self.assertEqual(projection["overall"]["active_alerts"], 1)
        self.assertEqual(projection["upstream_status"], upstream)

    def test_unknown_upstream_reads_do_not_become_service_alerts(self) -> None:
        upstream = {
            "schema": "20260811.1", "status": "degraded", "total": 3, "healthy": 2,
            "attention": 0, "unknown": 1, "items": [
                {"id": "openai", "label": "OpenAI", "status": "unknown", "available": False},
                {"id": "claude", "label": "Claude", "status": "healthy", "available": True},
                {"id": "deepseek", "label": "DeepSeek", "status": "healthy", "available": True},
            ],
        }
        projection = build_infra_projection(
            sample(), {"kernel": {}, "vps": {}}, {"enabled": False, "status": "disabled", "servers": []},
            "none", (), None, upstream,
        )
        self.assertEqual(projection["overall"]["status"], "healthy")
        self.assertEqual(projection["overall"]["active_alerts"], 0)


if __name__ == "__main__":
    unittest.main()
