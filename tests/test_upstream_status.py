from __future__ import annotations

import logging
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.upstream.status import (  # noqa: E402
    DEFAULT_PROVIDERS,
    StatusProvider,
    UpstreamStatusMonitor,
    aggregate_snapshot,
    decode_flashduty_summary,
    normalize_summary,
)


PROVIDER = StatusProvider(
    "example",
    "Example AI",
    "https://status.example.com/api/v2/summary.json",
    "https://status.example.com/",
    ("api",),
)


def summary(component_status: str = "operational", *, incident: bool = False) -> dict[str, object]:
    incidents = []
    if incident:
        incidents.append({
            "id": "incident-1",
            "name": "Elevated API errors",
            "status": "investigating",
            "impact": "major",
            "updated_at": "2026-08-11T02:00:00Z",
            "components": [{"id": "api"}],
        })
    return {
        "page": {"updated_at": "2026-08-11T02:00:00Z"},
        "status": {"indicator": "none", "description": "All Systems Operational"},
        "components": [
            {"id": "api", "name": "Example API", "status": component_status},
            {"id": "chat", "name": "Consumer Chat", "status": "major_outage"},
        ],
        "incidents": incidents,
    }


def flashduty_page(*, active: bool = False) -> bytes:
    active_changes = []
    if active:
        active_changes.append({
            "change_id": 42,
            "title": "V4 Flash degraded",
            "status": "investigating",
            "start_at_seconds": 1786420000,
            "affected_components": [{"component_id": "flash", "status": "degraded"}],
        })
    record = ["$", "$L23", None, {
        "pageId": 1,
        "initialData": {
            "page": {
                "components": [
                    {"component_id": "legacy", "name": "Legacy API", "hide_all": True},
                    {"component_id": "pro", "name": "V4 Pro API"},
                    {"component_id": "flash", "name": "V4 Flash API"},
                    {"component_id": "search", "section_id": "chat", "name": "Search Service"},
                ],
                "sections": [{"section_id": "chat", "name": "Chat Service"}],
            },
            "active_changes": active_changes,
        },
        "initialDataUpdatedAt": 1786420184265,
    }]
    flight = "1e:" + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return f'<html><script>self.__next_f.push([1,{json.dumps(flight, ensure_ascii=False)}])</script></html>'.encode()


class UpstreamStatusTests(unittest.TestCase):
    def test_default_official_status_feeds_include_kimi_and_cursor(self) -> None:
        providers = {provider.id: provider for provider in DEFAULT_PROVIDERS}
        self.assertEqual(providers["moonshot"].summary_url, "https://status.moonshot.cn/api/v2/summary.json")
        self.assertEqual(providers["cursor"].summary_url, "https://status.cursor.com/api/v2/summary.json")
        self.assertNotIn("reference_pages", aggregate_snapshot([], "now"))

    def test_only_api_components_contribute_to_provider_status(self) -> None:
        item = normalize_summary(PROVIDER, summary(), "2026-08-11T10:00:00+08:00")
        self.assertEqual(item["status"], "healthy")
        self.assertEqual([component["id"] for component in item["components"]], ["api"])

    def test_wildcard_provider_preserves_all_public_components(self) -> None:
        provider = StatusProvider(
            "all", "All", "https://status.example.com/summary.json",
            "https://status.example.com/", ("*",),
        )
        item = normalize_summary(provider, summary(), "2026-08-11T10:00:00+08:00")
        self.assertEqual([component["id"] for component in item["components"]], ["api", "chat"])
        self.assertEqual(item["status"], "critical")

    def test_api_degradation_and_incident_are_preserved(self) -> None:
        item = normalize_summary(
            PROVIDER,
            summary("degraded_performance", incident=True),
            "2026-08-11T10:00:00+08:00",
        )
        self.assertEqual(item["status"], "warning")
        self.assertEqual(item["incidents"][0]["name"], "Elevated API errors")

    def test_flashduty_page_exposes_current_public_components_and_groups(self) -> None:
        payload = decode_flashduty_summary(flashduty_page())
        self.assertEqual([component["id"] for component in payload["components"]], ["pro", "flash", "search"])
        self.assertEqual(payload["components"][2]["group"], "Chat Service")
        self.assertTrue(all(component["status"] == "operational" for component in payload["components"]))

    def test_flashduty_active_change_updates_component_and_incident(self) -> None:
        payload = decode_flashduty_summary(flashduty_page(active=True))
        flash = next(component for component in payload["components"] if component["id"] == "flash")
        self.assertEqual(flash["status"], "degraded")
        self.assertEqual(payload["incidents"][0]["name"], "V4 Flash degraded")

    def test_transport_failure_is_unknown_not_an_outage(self) -> None:
        monitor = UpstreamStatusMonitor(
            logging.getLogger("upstream-test"),
            (PROVIDER,),
            fetch_json=lambda _url: (_ for _ in ()).throw(TimeoutError()),
        )
        snapshot = monitor.poll_once()
        self.assertEqual(snapshot["status"], "degraded")
        self.assertEqual(snapshot["attention"], 0)
        self.assertEqual(snapshot["unknown"], 1)
        self.assertEqual(snapshot["items"][0]["error_kind"], "TimeoutError")

    def test_monitor_emits_only_real_state_transitions(self) -> None:
        values = iter((summary(), summary("major_outage"), summary("major_outage"), summary()))
        monitor = UpstreamStatusMonitor(
            logging.getLogger("upstream-test"),
            (PROVIDER,),
            fetch_json=lambda _url: next(values),
        )
        monitor.poll_once()
        self.assertEqual(monitor.drain_transitions(), [])
        monitor.poll_once()
        transition = monitor.drain_transitions()
        self.assertEqual((transition[0]["type"], transition[0]["level"]), ("alert", "critical"))
        monitor.poll_once()
        self.assertEqual(monitor.drain_transitions(), [])
        monitor.poll_once()
        transition = monitor.drain_transitions()
        self.assertEqual((transition[0]["type"], transition[0]["level"]), ("recovered", "healthy"))

    def test_aggregate_counts_unknown_separately_from_attention(self) -> None:
        snapshot = aggregate_snapshot([
            {"status": "healthy"},
            {"status": "warning"},
            {"status": "unknown"},
        ], "now")
        self.assertEqual((snapshot["healthy"], snapshot["attention"], snapshot["unknown"]), (1, 1, 1))
        self.assertEqual(snapshot["status"], "warning")


if __name__ == "__main__":
    unittest.main()
