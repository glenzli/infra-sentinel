from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.app.codex_migration import (  # noqa: E402
    BACKUP_DIRECTORY,
    LEDGER_FILENAME,
    prepare_codex_jsonl_migration,
)
from infra_sentinel.core.model import MetricPoint  # noqa: E402
from infra_sentinel.metrics.store import CODEX_JSONL_HISTORY_MIGRATION, MetricStore  # noqa: E402


NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def rollout_usage(total: int) -> str:
    usage = {
        "input_tokens": total,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": total,
    }
    rows = [
        {"type": "event_msg", "timestamp": "2026-08-22T12:00:00Z", "payload": {
            "type": "user_message", "message": "private prompt must not persist",
        }},
        {"type": "event_msg", "timestamp": "2026-08-22T12:00:01Z", "payload": {
            "type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage},
        }},
    ]
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


def metric(source: str, value: int) -> MetricPoint:
    return MetricPoint(
        observed_at="2026-08-22T12:00:00+00:00", observed_epoch=1_000,
        metric="ai.tokens.total", instrument="counter", value=value, unit="tokens",
        source_id=source, resource_id="ai_usage",
    )


class CodexMigrationTests(unittest.TestCase):
    def test_migration_rebuilds_ledger_backs_up_old_state_and_deletes_only_codex_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            root = Path(temporary) / "sessions"
            state_dir.mkdir()
            root.mkdir()
            (root / "rollout-2026-08-22.jsonl").write_text(rollout_usage(100), encoding="utf-8")
            (state_dir / "codex-usage-day.json").write_text('{"old":true}', encoding="utf-8")
            (state_dir / "codex-session-events.json").write_text('{"old":true}', encoding="utf-8")
            store = MetricStore(state_dir)
            store.write((metric("codex", 42), metric("opencode", 7)))

            report = prepare_codex_jsonl_migration(state_dir, store, roots=(root,), now=NOW)
            repeated = prepare_codex_jsonl_migration(state_dir, store, roots=(root,), now=NOW)
            backup = state_dir / "backups" / BACKUP_DIRECTORY
            with sqlite3.connect(backup / "infra.sqlite3") as connection:
                backed_up_codex = int(connection.execute(
                    "SELECT COUNT(*) FROM metric_points WHERE source_id = 'codex'"
                ).fetchone()[0])
            ledger_text = (state_dir / LEDGER_FILENAME).read_text(encoding="utf-8")

            self.assertEqual(report["status"], "migrated")
            self.assertEqual(report["ledger_tokens"], 100)
            self.assertEqual(report["store"]["deleted"], 1)
            self.assertEqual(repeated["status"], "current")
            self.assertEqual(backed_up_codex, 1)
            self.assertTrue((backup / "manifest.json").is_file())
            self.assertTrue((backup / "codex-usage-day.json").is_file())
            self.assertTrue((backup / "codex-session-events.json").is_file())
            self.assertEqual(store.query_points(since_epoch=0, until_epoch=2_000, source_id="codex"), [])
            self.assertEqual(store.query_points(since_epoch=0, until_epoch=2_000, source_id="opencode")[0]["value"], 7)
            self.assertEqual(store.metadata(CODEX_JSONL_HISTORY_MIGRATION)["deleted"], 1)
            self.assertNotIn("private prompt", ledger_text)
            self.assertNotIn("rollout-2026", ledger_text)

    def test_missing_rollouts_blocks_without_deleting_old_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            store = MetricStore(state_dir)
            store.write((metric("codex", 42),))
            report = prepare_codex_jsonl_migration(state_dir, store, roots=(), now=NOW)

            self.assertEqual(report, {"status": "blocked", "reason": "codex-rollout-roots-missing"})
            self.assertEqual(store.query_points(since_epoch=0, until_epoch=2_000, source_id="codex")[0]["value"], 42)
            self.assertIsNone(store.metadata(CODEX_JSONL_HISTORY_MIGRATION))


if __name__ == "__main__":
    unittest.main()
