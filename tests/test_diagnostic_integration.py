from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from io import BytesIO
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.app.agent import AlertEngine, build_projection_state, handle_sample, process_read_only_commands  # noqa: E402
from infra_sentinel.app.configuration import read_config  # noqa: E402
from infra_sentinel.app.protocol import COMMAND_SCHEMA  # noqa: E402
from infra_sentinel.app.projection_publisher import ProjectionPublisher  # noqa: E402
from infra_sentinel.core.collectors import CollectorContext, CollectorRegistry  # noqa: E402
from infra_sentinel.metrics.diagnostics import DiagnosticStore  # noqa: E402
from infra_sentinel.resources.network.metrics import network_collector_registry  # noqa: E402
from infra_sentinel.resources.network.session import SessionMeter  # noqa: E402
from infra_sentinel.resources.system.collector import SystemResourceCollector  # noqa: E402
from infra_sentinel.resources.system.contract import HostReading, MEMORY_PRESSURE  # noqa: E402


class DiagnosticIntegrationTests(unittest.TestCase):
    def _command(self, state: Path, kind: str, payload: dict) -> tuple[str, Path]:
        identity = str(uuid.uuid4())
        commands = state / "commands"
        commands.mkdir(exist_ok=True)
        path = commands / f"{identity}.request.json"
        path.write_text(json.dumps({"schema": COMMAND_SCHEMA, "id": identity, "type": kind,
                                    "requested_at": "2026-10-22T08:00:00Z", "payload": payload}))
        return identity, path

    def test_query_uses_read_service_and_leaves_mutations_for_the_sampler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = DiagnosticStore(state / "diagnostics.sqlite3")
            now = datetime.now(timezone.utc).timestamp()
            store.observe_projection({"epoch": now, "infra": {"sources": [{
                "id": "codex", "resource_id": "ai_usage", "enabled": True, "status": "error",
                "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            }]}}, now_epoch=now)
            query_id, _ = self._command(state, "diagnostics.query", {"limit": 1})
            bad_id, _ = self._command(state, "diagnostics.query", {"path": "/private/config"})
            _, mutation = self._command(state, "session.reset", {})
            before = store.path.read_bytes()
            completed = process_read_only_commands(state / "config.toml", state, Mock(), logging.getLogger("test"), store)
            self.assertEqual(completed, 2)
            result = json.loads((state / "commands" / f"{query_id}.result.json").read_text())
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["payload"]["events"]), 1)
            rejected = json.loads((state / "commands" / f"{bad_id}.result.json").read_text())
            self.assertEqual(rejected["status"], "rejected")
            self.assertTrue(mutation.exists())
            self.assertEqual(store.path.read_bytes(), before)

    def test_missing_journal_is_explicit_in_the_command_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            query_id, _ = self._command(state, "diagnostics.query", {})
            process_read_only_commands(state / "config.toml", state, Mock(), logging.getLogger("test"))
            result = json.loads((state / "commands" / f"{query_id}.result.json").read_text())
            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["payload"]["available"])
            self.assertFalse((state / "diagnostics.sqlite3").exists())

    def _sample(self, state: Path, store: DiagnosticStore) -> dict:
        with patch.dict("os.environ", {"INFRA_SENTINEL_STATE_DIR": str(state)}):
            config = read_config(PROJECT_ROOT / "config.example.toml")
        stream = BytesIO()
        publisher = ProjectionPublisher(state, stream=stream)
        billing = Mock(level="none")
        billing.evaluate.return_value = []
        billing.snapshots.return_value = []
        remote = Mock()
        remote.maybe_poll.return_value = {"enabled": False, "status": "disabled", "servers": []}
        facilities = Mock()
        facilities.snapshot.return_value = {"status": "healthy", "items": []}
        pipeline = Mock()
        pipeline.ingest.return_value = SimpleNamespace(flushed_buckets=False)
        system = Mock()
        system.drain_transitions.return_value = []
        upstream = Mock()
        upstream.snapshot.return_value = {"status": "healthy", "items": []}
        upstream.drain_transitions.return_value = []
        with patch("infra_sentinel.app.agent.collect_interval", side_effect=RuntimeError("Mihomo absent")), \
             patch("infra_sentinel.app.agent.apply_agent_commands", return_value=SimpleNamespace(remote_state=None, restart_requested=False)):
            handle_sample(config, state / "config.toml", deque(), None, None, AlertEngine(), billing,
                          remote, SessionMeter(state), None, Mock(summary=lambda: {}), pipeline,
                          network_collector_registry(()), system, facilities, upstream, publisher,
                          logging.getLogger("test"), store)
        return json.loads(stream.getvalue().splitlines()[-1])

    def test_live_projection_and_read_query_share_the_same_persisted_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            store = DiagnosticStore(state / "diagnostics.sqlite3")
            first = self._sample(state, store)
            diagnostics = first["infra"]["diagnostics"]
            self.assertTrue(diagnostics["available"])
            self.assertTrue(diagnostics["events"])
            self.assertEqual(store.snapshot()["events"], diagnostics["events"])
            reopened = DiagnosticStore(store.path)
            second = self._sample(state, reopened)
            self.assertEqual(len(second["infra"]["diagnostics"]["events"]), len(diagnostics["events"]))

    def test_diagnostic_storage_failure_does_not_stop_existing_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = state / "diagnostics.sqlite3"
            path.mkdir()
            projection = self._sample(state, DiagnosticStore(path))
            self.assertFalse(projection["infra"]["diagnostics"]["available"])
            self.assertEqual(projection["mihomo"]["status"], "error")
            self.assertIn("resources", projection["infra"])

    def test_restarting_network_confirmation_does_not_recover_an_existing_alert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with patch.dict("os.environ", {"INFRA_SENTINEL_STATE_DIR": str(state)}):
                config = read_config(PROJECT_ROOT / "config.example.toml")
            store = DiagnosticStore(state / "diagnostics.sqlite3")
            high = {"up_bytes": config.monitor.critical_bytes * 2, "down_bytes": 0}
            low = {"up_bytes": 0, "down_bytes": 0}
            epoch = datetime.now(timezone.utc).timestamp()

            def observe(engine: AlertEngine, values: dict, offset: int) -> dict:
                now = epoch + offset
                engine.evaluate(values, values, config)
                projection = build_projection_state(config, {
                    "epoch": now, "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "observed_seconds": 5, "kernel": values,
                }, values, values, engine.level, {}, {}, traffic_alert_level=engine.level,
                    traffic_alert_confirmation=engine.confirmation)
                return store.observe_projection(projection, now_epoch=now)

            original = AlertEngine()
            observe(original, high, 0)
            confirmed = observe(original, high, 5)
            self.assertEqual([item["transition"] for item in confirmed["events"]], ["raised"])
            store = DiagnosticStore(store.path)
            restarted = AlertEngine()
            pending = observe(restarted, high, 10)
            self.assertEqual(pending["active_count"], 1)
            self.assertEqual(pending["active"][0]["observation_status"], "unknown")
            self.assertEqual(len(pending["events"]), 1)
            self.assertEqual(len(observe(restarted, high, 15)["events"]), 1)
            self.assertEqual(observe(restarted, low, 20)["active_count"], 1)
            recovered = observe(restarted, low, 25)
            self.assertEqual([item["transition"] for item in recovered["events"]], ["recovered", "raised"])

    def test_restarting_system_collector_keeps_persisted_pressure_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with patch.dict("os.environ", {"INFRA_SENTINEL_STATE_DIR": str(state)}):
                config = read_config(PROJECT_ROOT / "config.example.toml")
            store = DiagnosticStore(state / "diagnostics.sqlite3")
            epoch = datetime.now(timezone.utc).timestamp()
            backend = Mock(platform="macos", capabilities=(MEMORY_PRESSURE,))
            backend.read.side_effect = lambda observed_at, now: HostReading(
                observed_at, now, (1, 1, 8, 0), 100, 10, 0, "critical", True,
                0, 0, 0, 100, 50, 0, 0, 0, 0, False, None,
            )

            def observe(collector: SystemResourceCollector, offset: int) -> dict:
                now = epoch + offset
                sample = {"epoch": now, "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(), "observed_seconds": 5}
                runs = CollectorRegistry((collector,)).collect(CollectorContext(sample, {}))
                projection = build_projection_state(config, sample, {}, {}, "none", {}, {}, collector_runs=runs)
                return store.observe_projection(projection, now_epoch=now)

            original = SystemResourceCollector(backend)
            for offset in (0, 5, 10):
                observed = observe(original, offset)
            self.assertEqual([item["code"] for item in observed["active"]], ["system.memory_pressure"])
            store = DiagnosticStore(store.path)
            restarted = SystemResourceCollector(backend)
            for offset in (15, 20, 25):
                observed = observe(restarted, offset)
                self.assertEqual(observed["active_count"], 1)
                self.assertEqual([item["transition"] for item in observed["events"]], ["raised"])


if __name__ == "__main__":
    unittest.main()
