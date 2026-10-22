from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.diagnostics import (  # noqa: E402
    CODE_TEXT, MAX_NORMALIZED_CHECKS, iso_at, normalize_diagnostics,
)


def projection(epoch: float = 1000) -> dict:
    return {
        "epoch": epoch, "interval_kind": "realtime",
        "infra": {
            "sources": [{"id": "local-mihomo", "enabled": True, "status": "ok", "updated_at": iso_at(epoch)}],
            "network_diagnostics": {"traffic_alert": {"level": "none"}, "daily_usage_guards": []},
            "facilities": {"status": "disabled", "items": []},
        },
    }


class DiagnosticNormalizationTests(unittest.TestCase):
    def test_multiple_existing_categories_and_fixed_bilingual_labels(self) -> None:
        value = projection()
        infra = value["infra"]
        infra["sources"].extend([
            {"id": "vps:personal-host", "enabled": True, "status": "ok", "updated_at": iso_at(1000)},
            {"id": "codex", "enabled": True, "status": "error", "checked_at": iso_at(1000), "error": "private exception"},
        ])
        infra["network_diagnostics"] = {
            "traffic_alert": {"level": "warning"},
            "daily_usage_guards": [{"id": "daily-host", "source_id": "personal-host", "level": "critical"}],
        }
        infra["system"] = {
            "available": True, "status": "warning", "observed_at": iso_at(1000),
            "capabilities": ["memory.pressure", "disk.capacity"],
            "memory": {"pressure": "normal"},
            "disk": {"total_bytes": 2 * 1024**4, "free_bytes": 1000},
            "reasons": ["disk_space_low"],
        }
        infra["facilities"] = {"status": "degraded", "items": [{
            "id": "observer-instance", "protocol": "dev-mesh.observer.status",
            "status": "degraded", "observed_at": iso_at(1000),
            "snapshot": {"captured_at": iso_at(1000), "issues": [
                {"code": "dev_mesh.collection.failed", "severity": "warning"},
                {"code": "dev_mesh.contention.stalled", "severity": "critical"},
            ]},
        }]}
        infra["upstream_status"] = {"items": [{
            "id": "openai", "available": True, "status": "critical", "observed_at": iso_at(1000),
            "incidents": [{"name": "private incident text", "url": "https://secret.invalid/token"}],
        }]}

        observation = normalize_diagnostics(value, 1000)
        active = {check.code: check for check in observation.checks if check.severity}
        self.assertEqual({check.category for check in active.values()}, {"source", "network", "system", "facility", "upstream"})
        self.assertEqual(active["network.traffic_high"].severity, "warning")
        self.assertEqual(active["network.daily_usage_high"].severity, "critical")
        self.assertEqual(active["system.disk_space"].severity, "warning")
        self.assertEqual(active["dev_mesh.contention.stalled"].severity, "critical")
        self.assertEqual(active["upstream.service_incident"].source_label, "OpenAI")
        self.assertTrue(all(len(CODE_TEXT[check.code]) == 4 for check in observation.checks))
        serialized = json.dumps(asdict(observation))
        for private in ("personal-host", "private exception", "private incident text", "secret.invalid", "observer-instance"):
            self.assertNotIn(private, serialized)

    def test_only_confirmed_levels_create_conditions(self) -> None:
        value = projection()
        value["infra"]["network_diagnostics"]["traffic_alert"] = {
            "level": "none", "windows": {"warning": {"up_bytes": 10**12}},
        }
        value["infra"]["system"] = {
            "available": True, "status": "healthy", "observed_at": iso_at(1000),
            "capabilities": ["memory.pressure"], "memory": {"pressure": "critical"},
            "reasons": [], "health_confirmation": {"candidate_status": "critical"},
        }
        observation = normalize_diagnostics(value, 1000)
        self.assertFalse(any(check.severity for check in observation.checks))

    def test_missing_disabled_unknown_and_stale_evidence_is_not_clear(self) -> None:
        value = projection()
        value["infra"]["sources"][0].update(status="error")
        value["infra"]["network_diagnostics"]["traffic_alert"]["level"] = "none"
        value["infra"]["system"] = {
            "available": True, "status": "healthy", "observed_at": iso_at(1000),
            "disk": {"health": {"state": "unknown"}, "smart": {"state": "unknown"}},
            "reasons": [],
        }
        value["infra"]["upstream_status"] = {"items": [
            {"id": "openai", "available": False, "status": "unknown", "observed_at": iso_at(1000), "error_kind": "OSError"},
            {"id": "claude", "available": False, "status": "unknown", "observed_at": iso_at(1000), "error_kind": "waiting"},
        ]}
        checks = normalize_diagnostics(value, 1000).checks
        by_code = {check.code: check for check in checks if check.source_id != "upstream:claude"}
        self.assertEqual(by_code["network.traffic_high"].observation_status, "unknown")
        self.assertEqual(by_code["system.disk_health"].observation_status, "unknown")
        self.assertEqual(by_code["system.disk_smart"].observation_status, "unknown")
        self.assertEqual(by_code["upstream.service_incident"].observation_status, "unknown")
        self.assertEqual(by_code["upstream.status_unavailable"].severity, "warning")
        self.assertTrue(all(check.severity is None for check in checks if check.source_id == "upstream:claude"))

        value["infra"]["sources"][0].update(enabled=False, status="disabled")
        traffic = next(check for check in normalize_diagnostics(value, 1000).checks if check.code == "network.traffic_high")
        self.assertEqual(traffic.observation_status, "disabled")
        value["infra"]["sources"][0].update(enabled=True, status="ok", updated_at=iso_at(1))
        traffic = next(check for check in normalize_diagnostics(value, 1000).checks if check.code == "network.traffic_high")
        self.assertEqual(traffic.observation_status, "unknown")

    def test_failed_collector_survives_omission_from_source_snapshot(self) -> None:
        value = projection()
        value["infra"]["collectors"] = [{
            "capability": {"source_id": "opencode", "source_kind": "ai.opencode"},
            "status": "error", "checked_at": iso_at(1000), "error_kind": "/Users/private/secret.db",
        }]
        check = next(check for check in normalize_diagnostics(value, 1000).checks if check.source_id == "opencode")
        self.assertEqual(check.code, "source.collection_failed")
        self.assertEqual(check.severity, "warning")

    def test_background_source_errors_require_actual_attempt_time(self) -> None:
        value = projection()
        value["infra"]["sources"].extend([
            {"id": "codex", "enabled": True, "status": "error", "updated_at": iso_at(1000)},
            {"id": "antigravity", "enabled": True, "status": "error", "updated_at": iso_at(1000), "checked_at": iso_at(200)},
        ])
        checks = normalize_diagnostics(value, 1000).checks
        codex = next(check for check in checks if check.source_id == "codex")
        antigravity = next(check for check in checks if check.source_id == "antigravity")
        self.assertEqual(codex.observation_status, "unknown")
        self.assertIsNone(codex.severity)
        self.assertEqual(antigravity.observation_status, "observed")
        self.assertEqual(antigravity.observed_epoch, 200)
        self.assertEqual(antigravity.severity, "warning")
        value["infra"]["collectors"] = [{
            "capability": {"source_id": "codex", "source_kind": "ai.codex"},
            "status": "error", "checked_at": iso_at(999),
        }]
        codex = next(check for check in normalize_diagnostics(value, 1000).checks if check.source_id == "codex")
        self.assertEqual(codex.observation_status, "observed")
        self.assertEqual(codex.observed_epoch, 999)

    def test_unrecognized_facility_codes_are_aggregated_without_payload_copy(self) -> None:
        value = projection()
        secret = "prompt-secret-account@example.test"
        value["infra"]["facilities"] = {"items": [{
            "id": secret, "status": "healthy", "observed_at": iso_at(1000), "label": secret,
            "snapshot": {"issues": [
                {"code": secret, "subject_id": secret, "severity": "warning"},
                {"code": "another-private-code", "severity": "critical"},
            ]},
        }]}
        observation = normalize_diagnostics(value, 1000)
        active = [check for check in observation.checks if check.severity]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].code, "facility.reported_issue")
        self.assertEqual(active[0].severity, "critical")
        self.assertNotIn(secret, json.dumps(asdict(observation)))

    def test_bounded_input_does_not_assert_absent_facility_issues_recovered(self) -> None:
        value = projection()
        value["infra"]["facilities"] = {"items": [{
            "id": f"facility-{index}", "status": "healthy", "observed_at": iso_at(1000),
            "snapshot": {"issues": [{"code": "private", "severity": "info"}] * 129},
        } for index in range(129)]}
        observation = normalize_diagnostics(value, 1000)
        self.assertTrue(observation.truncated)
        self.assertLessEqual(len(observation.checks), MAX_NORMALIZED_CHECKS)
        issue = next(check for check in observation.checks if check.code == "facility.reported_issue")
        self.assertEqual(issue.observation_status, "unknown")


if __name__ == "__main__":
    unittest.main()
