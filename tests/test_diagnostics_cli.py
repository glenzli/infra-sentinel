from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.app.agent import main as agent_main  # noqa: E402
from infra_sentinel.cli.diagnostics import main  # noqa: E402
from infra_sentinel.metrics.diagnostics import DiagnosticStore  # noqa: E402


class DiagnosticCliTests(unittest.TestCase):
    def test_missing_history_is_reported_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "absent"
            output = StringIO()
            with redirect_stdout(output):
                result = main(["--state-dir", str(state)])
            self.assertEqual(result, 1)
            self.assertFalse(json.loads(output.getvalue())["available"])
            self.assertFalse(state.exists())

    def test_cli_reports_persisted_events_without_mutating_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            epoch = datetime(2026, 10, 22, 8, tzinfo=timezone.utc).timestamp()
            path = state / "diagnostics.sqlite3"
            store = DiagnosticStore(path)
            store.observe_projection({"epoch": epoch, "infra": {"sources": [{
                "id": "codex", "kind": "ai.codex", "resource_id": "ai_usage",
                "enabled": True, "status": "error", "updated_at": "2026-10-22T08:00:00+00:00",
                "checked_at": "2026-10-22T08:00:00+00:00",
            }]}}, now_epoch=epoch)
            before = path.read_bytes()
            output = StringIO()
            with patch("infra_sentinel.cli.diagnostics.time.time", return_value=epoch + 5), redirect_stdout(output):
                result = main(["--state-dir", str(state), "--category", "source", "--limit", "1"])
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(len(payload["events"]), 1)
            self.assertEqual(payload["events"][0]["transition"], "raised")
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_queries_fail_before_any_state_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "absent"
            for extra in (["--limit", "201"], ["--limit", "0"], ["--since", "2026-10-23T00:00:00Z", "--until", "2026-10-22T00:00:00Z"]):
                with self.subTest(extra=extra), redirect_stderr(StringIO()):
                    self.assertEqual(main(["--state-dir", str(state), *extra]), 2)
                self.assertFalse(state.exists())
            for extra in (["--hours", "nan"], ["--hours", "721"], ["--since", "2026-10-22T08:00:00"]):
                with self.subTest(extra=extra), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as stopped:
                    main(["--state-dir", str(state), *extra])
                self.assertEqual(stopped.exception.code, 2)
                self.assertFalse(state.exists())

    def test_packaged_agent_read_mode_never_bootstraps_or_connects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "absent"
            output = StringIO()
            with patch.dict("os.environ", {"INFRA_SENTINEL_STATE_DIR": str(state)}), \
                 patch.object(sys, "argv", ["infra-agent", "--config", str(PROJECT_ROOT / "config.example.toml"), "--diagnostics"]), \
                 patch("infra_sentinel.app.agent.ensure_state_dir") as initialize, \
                 patch("infra_sentinel.app.agent.MihomoApiClient") as connect, redirect_stdout(output):
                result = agent_main()
            self.assertEqual(result, 1)
            self.assertFalse(json.loads(output.getvalue())["available"])
            initialize.assert_not_called()
            connect.assert_not_called()
            self.assertFalse(state.exists())

    def test_packaged_agent_rejects_non_query_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "absent"
            with patch.dict("os.environ", {"INFRA_SENTINEL_STATE_DIR": str(state)}), \
                 patch.object(sys, "argv", ["infra-agent", "--config", str(PROJECT_ROOT / "config.example.toml"), "--diagnostics", "--diagnostics-query", '{"sql":"DELETE FROM diagnostics"}']), \
                 redirect_stderr(StringIO()):
                self.assertEqual(agent_main(), 2)
            self.assertFalse(state.exists())

    def test_read_entry_points_do_not_parse_or_migrate_old_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            contents = 'schema_version = "20260808.4"\n'
            config.write_text(contents)
            with patch.dict("os.environ", {"INFRA_SENTINEL_STATE_DIR": ""}), \
                 patch("infra_sentinel.app.configuration.read_user_settings", side_effect=AssertionError("read must not migrate")), \
                 redirect_stdout(StringIO()):
                self.assertEqual(main(["--config", str(config)]), 1)
                with patch.object(sys, "argv", ["infra-agent", "--config", str(config), "--diagnostics"]):
                    self.assertEqual(agent_main(), 1)
            self.assertEqual(config.read_text(), contents)
            self.assertEqual([item.name for item in config.parent.iterdir()], ["config.toml"])


if __name__ == "__main__":
    unittest.main()
