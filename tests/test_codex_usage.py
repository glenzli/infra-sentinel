from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from codex_usage import CodexUsageCollector, read_codex_state_stats  # noqa: E402
from infra_collectors import CollectorContext  # noqa: E402


def create_state_database(path: Path, rows: list[tuple[object, ...]]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""
            CREATE TABLE threads (
                model TEXT,
                tokens_used INTEGER,
                thread_source TEXT,
                source TEXT,
                updated_at_ms INTEGER
            )
        """)
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?)", rows)
        connection.commit()


class CodexUsageTests(unittest.TestCase):
    def test_state_stats_aggregate_tokens_models_and_safe_topology(self) -> None:
        epoch = datetime(2026, 8, 9, 12, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            create_state_database(database, [
                ("gpt-5.6-sol", 120, "user", "vscode", int(epoch * 1000)),
                ("gpt-5.6-sol", 80, "subagent", json.dumps({"subagent": {"thread_spawn": {"depth": 2, "agent_path": "never returned"}}}), int(epoch * 1000)),
                ("gpt-5.6-terra", 50, "subagent", json.dumps({"subagent": {"thread_spawn": {"depth": 3, "agent_nickname": "never returned"}}}), 0),
            ])
            stats = read_codex_state_stats(database, epoch)

        self.assertEqual(stats.total_tokens, 120)
        self.assertEqual(stats.threads, 3)
        self.assertEqual(stats.user_threads, 1)
        self.assertEqual(stats.subagents, 2)
        self.assertEqual(stats.subagent_tokens, 130)
        self.assertEqual(stats.recent_threads, 2)
        self.assertEqual(stats.recent_subagents, 1)
        self.assertEqual(stats.maximum_depth, 3)
        self.assertEqual(stats.models, (
            {"id": "gpt-5.6-sol", "threads": 1, "total_tokens": 120},
        ))

    def test_subagent_context_does_not_inflate_user_token_counter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            checkpoint = Path(temporary) / "codex-usage-day.json"
            create_state_database(database, [
                ("gpt-5.6-sol", 100, "user", "vscode", 1_000_000),
                ("gpt-5.6-sol", 500, "subagent", json.dumps({"subagent": {}}), 1_000_000),
            ])
            collector = CodexUsageCollector(database_finder=lambda: database, poll_seconds=10, checkpoint_path=checkpoint)
            collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE threads SET tokens_used = 2_000 WHERE thread_source = 'subagent'")
                connection.commit()
            later = collector.collect(CollectorContext({"epoch": 1_010.0}, {}))

        self.assertEqual(later.points, ())
        self.assertEqual(later.snapshot["usage"]["cumulative"]["tokens"], 100)  # type: ignore[index]

    def test_first_observation_does_not_replay_lifetime_as_an_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            checkpoint = Path(temporary) / "codex-usage-day.json"
            create_state_database(database, [("gpt-5.6-sol", 100, "user", "vscode", 1_000_000)])
            collector = CodexUsageCollector(database_finder=lambda: database, poll_seconds=10, checkpoint_path=checkpoint)
            first = collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE threads SET tokens_used = 145")
                connection.commit()
            later = collector.collect(CollectorContext({"epoch": 1_010.0}, {}))

        self.assertEqual(first.points, ())
        self.assertEqual(first.snapshot["usage"]["cumulative"]["tokens"], 100)  # type: ignore[index]
        self.assertEqual(first.snapshot["usage"]["today"]["tokens"], 0)  # type: ignore[index]
        self.assertEqual(len(later.points), 2)
        self.assertEqual({point.metric for point in later.points}, {"ai.tokens.total"})
        self.assertEqual({point.value for point in later.points}, {45})
        self.assertEqual({tuple(sorted(point.dimensions.items())) for point in later.points}, {
            (("scope", "local-state"),), (("model", "gpt-5.6-sol"),),
        })
        self.assertEqual(later.snapshot["usage"]["today"]["tokens"], 45)  # type: ignore[index]

    def test_obsolete_or_incompatible_daily_checkpoint_rebaselines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            checkpoint = Path(temporary) / "codex-usage-day.json"
            create_state_database(database, [("gpt-5.6-sol", 100, "user", "vscode", 1_000_000)])
            checkpoint.write_text(json.dumps({
                "schema": "20260809.2", "day": "1970-01-01", "baseline_tokens": 1_500,
                "baseline_models": {"gpt-5.6-sol": 1_500}, "started_at": "old",
            }), encoding="utf-8")
            collector = CodexUsageCollector(database_finder=lambda: database, checkpoint_path=checkpoint)
            first = collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE threads SET tokens_used = 130")
                connection.commit()
            later = collector.collect(CollectorContext({"epoch": 1_020.0}, {}))

            persisted = json.loads(checkpoint.read_text(encoding="utf-8"))

        self.assertEqual(first.snapshot["usage"]["today"]["tokens"], 0)  # type: ignore[index]
        self.assertEqual(later.snapshot["usage"]["today"]["tokens"], 30)  # type: ignore[index]
        self.assertEqual(persisted["schema"], "20260809.3")
        self.assertEqual(persisted["baseline_tokens"], 100)

    def test_missing_database_is_unavailable(self) -> None:
        collector = CodexUsageCollector(database_finder=lambda: None)
        result = collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.snapshot, {"available": False, "status": "unavailable"})
