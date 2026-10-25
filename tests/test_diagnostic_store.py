from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.diagnostics import iso_at  # noqa: E402
from infra_sentinel.metrics.diagnostics import DiagnosticStore, RETENTION_SECONDS  # noqa: E402


def projection(epoch: float, level: str = "none", status: str = "ok") -> dict:
    return {
        "epoch": epoch, "interval_kind": "realtime",
        "infra": {
            "sources": [{"id": "local-mihomo", "enabled": True, "status": status, "updated_at": iso_at(epoch)}],
            "network_diagnostics": {"traffic_alert": {"level": level}, "daily_usage_guards": []},
            "facilities": {"status": "disabled", "items": []},
        },
    }


class DiagnosticStoreTests(unittest.TestCase):
    def test_read_only_missing_history_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing-state" / "diagnostics.sqlite3"
            store = DiagnosticStore(path)
            for snapshot in (store.snapshot(now_epoch=100), store.query({"limit": 1}, now_epoch=100)):
                self.assertFalse(snapshot["available"])
                self.assertEqual(snapshot["error_code"], "history_unavailable")
                self.assertEqual(snapshot["freshness"], "unknown")
            self.assertFalse(path.parent.exists())

    def test_transitions_restart_idempotency_and_repeat_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            store = DiagnosticStore(path)
            first = store.observe_projection(projection(100, "warning"))
            self.assertTrue(first["available"])
            condition_id = first["active"][0]["id"]
            self.assertEqual(first["events"][0]["transition"], "raised")
            store.observe_projection(projection(101, "warning"))
            store = DiagnosticStore(path)
            second = store.observe_projection(projection(102, "warning"))
            self.assertEqual(len(second["events"]), 1)
            self.assertEqual(second["active"][0]["id"], condition_id)
            changed = store.observe_projection(projection(103, "critical"))
            self.assertEqual([event["transition"] for event in changed["events"]], ["changed", "raised"])
            store.observe_projection(projection(104, "critical"))
            recovered = store.observe_projection(projection(105))
            self.assertEqual(recovered["active_count"], 0)
            self.assertEqual([event["transition"] for event in recovered["events"]], ["recovered", "changed", "raised"])
            self.assertTrue(all(event["started_at"] == iso_at(100) for event in recovered["events"]))
            self.assertEqual(len(store.observe_projection(projection(106))["events"]), 3)
            raised_again = store.observe_projection(projection(107, "warning"))
            self.assertEqual(raised_again["active"][0]["id"], condition_id)
            self.assertEqual(raised_again["active"][0]["started_at"], iso_at(107))
            self.assertEqual(len({event["id"] for event in raised_again["events"]}), 4)

    def test_disappearance_disabled_unknown_and_read_failure_never_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            store.observe_projection(projection(100, "warning"))
            missing = store.observe_projection({"epoch": 101, "infra": {}})
            condition = next(row for row in missing["active"] if row["code"] == "network.traffic_high")
            self.assertEqual(condition["observation_status"], "unobserved")
            value = projection(102)
            value["infra"]["sources"][0].update(enabled=False, status="disabled")
            disabled = store.observe_projection(value)
            self.assertEqual(disabled["active"][0]["observation_status"], "disabled")
            failed = store.observe_projection(projection(103, status="error"))
            traffic = next(row for row in failed["active"] if row["category"] == "network")
            self.assertEqual(traffic["observation_status"], "unknown")
            self.assertFalse(any(event["transition"] == "recovered" for event in failed["events"]))
            self.assertEqual({row["category"] for row in failed["active"]}, {"source", "network"})
            healthy = store.observe_projection(projection(104))
            self.assertEqual(healthy["active_count"], 0)
            self.assertEqual(sum(event["transition"] == "recovered" for event in healthy["events"]), 2)

    def test_upstream_failure_keeps_incident_until_explicit_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            value = projection(100)
            item = {"id": "openai", "available": True, "status": "critical", "observed_at": iso_at(100)}
            value["infra"]["upstream_status"] = {"items": [item]}
            store.observe_projection(value)
            item.update(available=False, status="unknown", error_kind="OSError", observed_at=iso_at(101))
            value["epoch"] = 101
            failed = store.observe_projection(value)
            self.assertEqual(failed["active_count"], 2)
            incident = next(row for row in failed["active"] if row["code"] == "upstream.service_incident")
            self.assertEqual(incident["observation_status"], "unknown")
            self.assertFalse(any(event["transition"] == "recovered" for event in failed["events"]))
            item.update(available=True, status="healthy", observed_at=iso_at(102))
            value["epoch"] = 102
            self.assertEqual(store.observe_projection(value)["active_count"], 0)

    def test_system_unknown_and_facility_disappearance_preserve_open_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            value = projection(100)
            system = {
                "available": True, "status": "warning", "observed_at": iso_at(100),
                "reasons": ["disk_smart_warning"],
                "disk": {"smart": {"state": "warning", "observed_at": iso_at(100), "interval_seconds": 600}},
            }
            facility = {
                "id": "observer", "status": "degraded", "observed_at": iso_at(100),
                "protocol": "dev-mesh.observer.status", "snapshot": {"issues": [
                    {"code": "dev_mesh.collection.failed", "severity": "warning"},
                ]},
            }
            value["infra"]["system"] = system
            value["infra"]["facilities"] = {"items": [facility]}
            initial = store.observe_projection(value)
            self.assertEqual(initial["active_count"], 3)
            value["epoch"] = 101
            system.update(status="healthy", reasons=[])
            system["disk"]["smart"].update(state="unknown", observed_at=iso_at(101))
            value["infra"]["facilities"]["items"] = []
            missing = store.observe_projection(value)
            self.assertEqual(missing["active_count"], 3)
            self.assertEqual({row["observation_status"] for row in missing["active"]}, {"unknown", "unobserved"})
            self.assertFalse(any(row["transition"] == "recovered" for row in missing["events"]))
            value["epoch"] = 102
            system["disk"]["smart"].update(state="healthy", observed_at=iso_at(102))
            facility.update(status="healthy", observed_at=iso_at(102), snapshot={"issues": []})
            value["infra"]["facilities"]["items"] = [facility]
            recovered = store.observe_projection(value)
            self.assertEqual(recovered["active_count"], 0)
            self.assertEqual(sum(row["transition"] == "recovered" for row in recovered["events"]), 3)

    def test_stale_reads_and_old_projection_replay_do_not_refresh_or_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            store.observe_projection(projection(100, "critical"))
            before = store.snapshot(now_epoch=200)
            self.assertEqual(before["freshness"], "stale")
            self.assertEqual(before["active"][0]["observation_status"], "unknown")
            self.assertEqual(before["observed_at"], iso_at(100))
            self.assertEqual(before["generated_at"], iso_at(200))
            store.observe_projection(projection(99))
            after = store.snapshot(now_epoch=200)
            self.assertEqual(before, after)
            old_source = projection(201)
            old_source["infra"]["sources"][0]["updated_at"] = iso_at(100)
            stale_source = store.observe_projection(old_source)
            self.assertEqual(stale_source["active_count"], 1)
            self.assertEqual(len(stale_source["events"]), 1)

    def test_restart_pending_confirmation_cannot_recover_existing_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            value = projection(100, "critical")
            value["infra"]["system"] = {
                "available": True, "status": "critical", "observed_at": iso_at(100),
                "capabilities": ["memory.pressure"], "memory": {"pressure": "critical"},
                "reasons": ["memory_pressure_critical"],
            }
            value["infra"]["facilities"] = {"checked_at": iso_at(100), "error_kind": "OSError", "items": []}
            first = DiagnosticStore(path).observe_projection(value)
            self.assertEqual(first["active_count"], 3)
            restarted = DiagnosticStore(path)
            value["epoch"] = 105
            value["infra"]["sources"][0]["updated_at"] = iso_at(105)
            value["infra"]["network_diagnostics"]["traffic_alert"] = {
                "level": "none", "confirmation": {"candidate_status": "critical", "consecutive": 1, "required": 2},
            }
            system = value["infra"]["system"]
            system.update(status="healthy", reasons=[], observed_at=iso_at(105), health_confirmation={
                "candidate_status": "critical", "candidate_reasons": ["memory_pressure_critical"],
                "consecutive": 1, "required": 3,
            })
            value["infra"]["facilities"] = {
                "checked_at": iso_at(105), "items": [],
                "confirmation": {"candidate_status": "error", "consecutive": 1, "required": 3},
            }
            pending = restarted.observe_projection(value)
            self.assertEqual(pending["active_count"], 3)
            self.assertEqual(len(pending["events"]), 3)
            self.assertEqual({row["observation_status"] for row in pending["active"]}, {"unknown"})
            value["epoch"] = 110
            value["infra"]["sources"][0]["updated_at"] = iso_at(110)
            value["infra"]["network_diagnostics"]["traffic_alert"] = {"level": "critical"}
            system.update(status="critical", reasons=["memory_pressure_critical"], observed_at=iso_at(110))
            system.pop("health_confirmation")
            value["infra"]["facilities"] = {"checked_at": iso_at(110), "error_kind": "OSError", "items": []}
            confirmed = restarted.observe_projection(value)
            self.assertEqual(confirmed["active_count"], 3)
            self.assertEqual(len(confirmed["events"]), 3)
            self.assertTrue(all(row["transition"] == "raised" for row in confirmed["events"]))

    def test_cached_facility_failures_do_not_gain_new_observation_times(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            value = projection(100)
            value["infra"]["facilities"] = {"checked_at": iso_at(100), "error_kind": "OSError", "items": [{
                "id": "pcp", "status": "unreachable", "checked_at": iso_at(100), "observed_at": iso_at(90),
            }]}
            initial = store.observe_projection(value)
            self.assertEqual(initial["active_count"], 2)
            value["epoch"] = 100000
            stale = store.observe_projection(value)
            self.assertEqual(stale["active_count"], 2)
            self.assertEqual(len(stale["events"]), 2)
            self.assertEqual({row["observed_at"] for row in stale["active"]}, {iso_at(100)})
            self.assertEqual({row["observation_status"] for row in stale["active"]}, {"unknown"})
            self.assertEqual({row["freshness"] for row in stale["coverage"] if row["code"] in {"facility.health", "facility.discovery_failed"}}, {"stale"})

    def test_fresh_facility_attempt_cannot_recover_from_an_old_provider_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            value = projection(100)
            facility = {"id": "pcp", "status": "unreachable", "checked_at": iso_at(100), "observed_at": iso_at(90)}
            value["infra"]["facilities"] = {"checked_at": iso_at(100), "items": [facility]}
            initial = store.observe_projection(value)
            self.assertEqual(initial["active_count"], 1)
            value["epoch"] = 1000
            value["infra"]["facilities"]["checked_at"] = iso_at(1000)
            facility.update(status="healthy", checked_at=iso_at(1000), observed_at=iso_at(10), snapshot={"captured_at": iso_at(10), "issues": []})
            old_snapshot = store.observe_projection(value)
            self.assertEqual(old_snapshot["active_count"], 1)
            self.assertEqual(old_snapshot["active"][0]["observation_status"], "unknown")
            self.assertEqual(len(old_snapshot["events"]), 1)
            facility.update(checked_at=iso_at(10), observed_at=iso_at(1000), snapshot={"captured_at": iso_at(1000), "issues": []})
            old_attempt = store.observe_projection(value)
            self.assertEqual(old_attempt["active_count"], 1)
            self.assertEqual(old_attempt["active"][0]["observation_status"], "unknown")
            facility["checked_at"] = iso_at(1000)
            recovered = store.observe_projection(value)
            self.assertEqual(recovered["active_count"], 0)
            self.assertEqual([row["transition"] for row in recovered["events"]], ["recovered", "raised"])

    def test_cached_background_failure_is_not_refreshed_by_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            value = projection(100)
            source = {"id": "codex", "enabled": True, "status": "error", "updated_at": iso_at(90), "checked_at": iso_at(100)}
            value["infra"]["sources"].append(source)
            initial = store.observe_projection(value)
            self.assertEqual(initial["active_count"], 1)
            value["epoch"] = 1000
            stale = store.observe_projection(value)
            self.assertEqual(stale["active_count"], 1)
            self.assertEqual(stale["active"][0]["observed_at"], iso_at(100))
            self.assertEqual(stale["active"][0]["observation_status"], "unknown")
            source.pop("checked_at")
            source["updated_at"] = iso_at(1000)
            missing_attempt = store.observe_projection(value)
            self.assertEqual(missing_attempt["active"][0]["observation_status"], "unknown")
            self.assertEqual(len(missing_attempt["events"]), 1)
            value["infra"]["collectors"] = [{
                "capability": {"source_id": "codex", "source_kind": "ai.codex"},
                "status": "error", "checked_at": iso_at(1000),
            }]
            attempted = store.observe_projection(value)
            self.assertEqual(attempted["active"][0]["observation_status"], "observed")
            self.assertEqual(attempted["active"][0]["observed_at"], iso_at(1000))
            self.assertEqual(len(attempted["events"]), 1)
            # This older success is still inside the TTL. Its ordering,
            # rather than age alone, must prevent recovery of the newer error.
            source.update(status="ok", updated_at=iso_at(999))
            value["infra"]["collectors"][0].update(status="ok")
            old_success = store.observe_projection(value)
            self.assertEqual(old_success["active_count"], 1)
            self.assertEqual(old_success["active"][0]["observation_status"], "unknown")
            source["updated_at"] = iso_at(1000)
            recovered = store.observe_projection(value)
            self.assertEqual(recovered["active_count"], 0)
            self.assertEqual([row["transition"] for row in recovered["events"]], ["recovered", "raised"])

    def test_older_source_evidence_cannot_change_a_newer_condition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            store.observe_projection(projection(100, "warning"))
            value = projection(105, "critical")
            value["infra"]["sources"][0]["updated_at"] = iso_at(99)
            older = store.observe_projection(value)
            self.assertEqual(len(older["events"]), 1)
            self.assertEqual(older["active"][0]["severity"], "warning")
            self.assertEqual(older["active"][0]["observed_at"], iso_at(100))
            self.assertEqual(older["active"][0]["observation_status"], "unknown")
            value["infra"]["sources"][0]["updated_at"] = iso_at(105)
            current = store.observe_projection(value)
            self.assertEqual(current["active"][0]["severity"], "critical")
            self.assertEqual([row["transition"] for row in current["events"]], ["changed", "raised"])

    def test_queries_filter_current_and_history_and_enforce_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            store = DiagnosticStore(path)
            store.observe_projection(projection(100, "warning"))
            store.observe_projection(projection(101, "critical"))
            store.observe_projection(projection(102, "critical", "error"))
            limited = store.query({"limit": 1}, now_epoch=102)
            self.assertEqual(len(limited["events"]), 1)
            self.assertTrue(limited["truncated"])
            critical = store.query({"category": "network", "severity": "critical", "source_id": "local-mihomo"}, now_epoch=102)
            self.assertEqual(critical["active_count"], 1)
            self.assertEqual({row["category"] for row in critical["coverage"]}, {"network"})
            self.assertEqual([row["transition"] for row in critical["events"]], ["changed"])
            changed = store.query({"transition": "changed", "since_epoch": 101, "until_epoch": 101}, now_epoch=102)
            self.assertEqual(len(changed["events"]), 1)
            self.assertEqual(changed["active_count"], 2)
            with closing(sqlite3.connect(path)) as connection, connection:
                counts_before = connection.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
            invalid = [
                {"sql": "DELETE FROM diagnostic_events"}, {"category": "arbitrary"},
                {"severity": "none"}, {"transition": "deleted"}, {"source_id": "/Users/secret"},
                {"limit": True}, {"limit": 0}, {"limit": 201}, {"since_epoch": float("nan")},
                {"until_epoch": True}, {"since_epoch": 103, "until_epoch": 102},
                {"since_epoch": 0, "until_epoch": RETENTION_SECONDS + 1}, None, [],
            ]
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    store.query(payload, now_epoch=102)
            with closing(sqlite3.connect(path)) as connection, connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0], counts_before)

    def test_retention_bounds_events_and_preserves_unresolved_old_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            store = DiagnosticStore(path)
            store.observe_projection(projection(100, "warning"))
            later = store.observe_projection({"epoch": 101 + RETENTION_SECONDS, "infra": {}})
            self.assertEqual(later["events"], [])
            self.assertEqual(later["active_count"], 1)
            self.assertEqual(later["active"][0]["observation_status"], "unobserved")
            self.assertTrue(later["history_truncated"])
            self.assertFalse(later["truncated"])
            with patch("infra_sentinel.metrics.diagnostics.MAX_EVENTS", 3), patch("infra_sentinel.metrics.diagnostics.MAX_ACTIVE", 2), patch("infra_sentinel.metrics.diagnostics.MAX_CHECKS", 10):
                for index in range(8):
                    value = {"epoch": 200 + RETENTION_SECONDS + index, "infra": {"sources": [{
                        "id": f"private-source-{index}", "status": "error", "enabled": True,
                        "checked_at": iso_at(200 + RETENTION_SECONDS + index),
                    }]}}
                    store.observe_projection(value)
            with closing(sqlite3.connect(path)) as connection, connection:
                self.assertLessEqual(connection.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0], 3)
                self.assertLessEqual(connection.execute("SELECT COUNT(*) FROM diagnostic_active").fetchone()[0], 2)
                self.assertLessEqual(connection.execute("SELECT COUNT(*) FROM diagnostic_checks").fetchone()[0], 10)

    def test_atomic_write_failure_does_not_advance_active_or_sample_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DiagnosticStore(Path(temporary) / "diagnostics.sqlite3")
            before = store.observe_projection(projection(100))
            with patch.object(store, "_event", side_effect=sqlite3.OperationalError("secret database error")):
                failed = store.observe_projection(projection(101, "warning"))
            self.assertFalse(failed["available"])
            self.assertEqual(failed["error_code"], "storage_unavailable")
            self.assertNotIn("secret", json.dumps(failed))
            after = store.snapshot(now_epoch=100)
            self.assertEqual(after, before)

    def test_corruption_is_isolated_and_fixed_metadata_does_not_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            content = b"private database error /Users/secret token=abc"
            path.write_bytes(content)
            store = DiagnosticStore(path)
            for snapshot in (store.snapshot(now_epoch=100), store.observe_projection(projection(100))):
                self.assertFalse(snapshot["available"])
                self.assertEqual(snapshot["error_code"], "storage_unavailable")
                self.assertNotIn("private", json.dumps(snapshot))
            self.assertEqual(path.read_bytes(), content)

    def test_queries_on_a_copied_main_database_create_no_directory_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.sqlite3"
            DiagnosticStore(original).observe_projection(projection(100, "warning"))
            copied_directory = root / "copied"
            copied_directory.mkdir()
            copied = copied_directory / "diagnostics.sqlite3"
            copied.write_bytes(original.read_bytes())
            before_names = {path.name for path in copied_directory.iterdir()}
            before_bytes = copied.read_bytes()
            result = DiagnosticStore(copied).query({"limit": 2}, now_epoch=100)
            self.assertTrue(result["available"])
            self.assertEqual(result["active_count"], 1)
            self.assertEqual({path.name for path in copied_directory.iterdir()}, before_names)
            self.assertEqual(copied.read_bytes(), before_bytes)

    def test_unexpected_wal_database_is_refused_without_creating_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.sqlite3"
            DiagnosticStore(original).observe_projection(projection(100, "warning"))
            copied = root / "copied.sqlite3"
            with closing(sqlite3.connect(original)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                copied.write_bytes(original.read_bytes())
                before_names = {path.name for path in root.iterdir()}
                result = DiagnosticStore(copied).snapshot(now_epoch=100)
                self.assertFalse(result["available"])
                self.assertEqual(result["error_code"], "storage_unavailable")
                self.assertEqual({path.name for path in root.iterdir()}, before_names)
            recovered = DiagnosticStore(copied).observe_projection(projection(101, "warning"))
            self.assertTrue(recovered["available"])
            self.assertEqual(len(recovered["events"]), 1)

    def test_persistence_does_not_copy_private_identifiers_or_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            secret = "private-prompt-/Users/alice/token=abc@example.test"
            value = projection(100)
            value["infra"]["sources"].append({"id": secret, "label": secret, "status": "error", "enabled": True, "checked_at": iso_at(100), "error": secret})
            value["infra"]["facilities"] = {"items": [{
                "id": secret, "label": secret, "status": "degraded", "observed_at": iso_at(100),
                "console_url": secret, "snapshot": {"issues": [{"code": secret, "severity": "warning", "subject_id": secret}], "prompt": secret},
            }]}
            value["infra"]["upstream_status"] = {"items": [{
                "id": secret, "label": secret, "status": "critical", "available": True,
                "observed_at": iso_at(100), "description": secret, "incidents": [{"name": secret, "url": secret}],
            }]}
            result = DiagnosticStore(path).observe_projection(value)
            self.assertTrue(result["available"])
            self.assertGreaterEqual(result["active_count"], 4)
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret.encode(), path.read_bytes())
            with closing(sqlite3.connect(path)) as connection, connection:
                for table in ("diagnostic_checks", "diagnostic_active", "diagnostic_events"):
                    self.assertNotIn(secret, repr(connection.execute(f"SELECT * FROM {table}").fetchall()))

    def test_parallel_readers_share_committed_state_without_connection_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.sqlite3"
            writer = DiagnosticStore(path)
            writer.observe_projection(projection(100, "warning"))
            reader = DiagnosticStore(path)

            def read_many() -> list[dict]:
                return [reader.query({"limit": 5}, now_epoch=130) for _ in range(12)]

            with ThreadPoolExecutor(max_workers=3) as executor:
                reads = [executor.submit(read_many) for _ in range(2)]
                for epoch in range(101, 111):
                    writer.observe_projection(projection(epoch, "critical" if epoch % 2 else "warning"))
                snapshots = [snapshot for future in reads for snapshot in future.result()]
            self.assertTrue(all(snapshot["available"] for snapshot in snapshots))
            self.assertTrue(all(snapshot["active_count"] == 1 for snapshot in snapshots))
            self.assertEqual(len(reader.snapshot(now_epoch=130)["events"]), 11)


if __name__ == "__main__":
    unittest.main()
