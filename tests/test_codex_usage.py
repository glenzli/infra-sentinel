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
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.ai.codex import CodexUsageCollector, discover_codex_state_database, read_codex_state_stats  # noqa: E402
from infra_sentinel.core.collectors import CollectorContext  # noqa: E402


def create_state_database(path: Path, rows: list[tuple[object, ...]]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                model TEXT,
                tokens_used INTEGER,
                thread_source TEXT,
                source TEXT,
                updated_at_ms INTEGER
            )
        """)
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", [
            (f"thread-{index}", *row) for index, row in enumerate(rows)
        ])
        connection.commit()


class CodexUsageTests(unittest.TestCase):
    def test_explicit_portable_database_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "codex.sqlite"
            database.touch()
            self.assertEqual(discover_codex_state_database(database), database)

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
        self.assertIn("local calendar-day baseline", first.snapshot["usage"]["today"]["detail"]["en"])  # type: ignore[index]
        self.assertIn("统计日界线不同", first.snapshot["usage"]["today"]["detail"]["zh"])  # type: ignore[index]
        self.assertIn("root-thread counters", first.snapshot["usage"]["cumulative"]["detail"]["en"])  # type: ignore[index]
        self.assertIn("非账单口径", first.snapshot["usage"]["cumulative"]["detail"]["zh"])  # type: ignore[index]
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
                "schema": "20260809.3", "day": "1970-01-01", "baseline_tokens": 1_500,
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
        self.assertEqual(persisted["schema"], "20260809.4")
        self.assertEqual(persisted["baseline_tokens"], 100)

    def test_model_reclassification_cannot_create_phantom_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            checkpoint = Path(temporary) / "codex-usage-day.json"
            create_state_database(database, [
                ("gpt-5.6-terra", 100, "user", "vscode", 1_000_000),
                ("gpt-5.6-sol", 50, "user", "vscode", 1_000_000),
            ])
            collector = CodexUsageCollector(database_finder=lambda: database, poll_seconds=10, checkpoint_path=checkpoint)
            collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE threads SET model = 'gpt-5.6-sol' WHERE id = 'thread-0'")
                connection.execute("UPDATE threads SET tokens_used = 60 WHERE id = 'thread-1'")
                connection.commit()
            later = collector.collect(CollectorContext({"epoch": 1_010.0}, {}))

        total_point = next(point for point in later.points if point.dimensions.get("scope") == "local-state")
        model_points = [point for point in later.points if "model" in point.dimensions]
        self.assertEqual(total_point.value, 10)
        self.assertEqual(sum(point.value for point in model_points), total_point.value)
        self.assertEqual([(point.dimensions["model"], point.value) for point in model_points], [("gpt-5.6-sol", 10)])
        self.assertEqual(later.snapshot["usage"]["today"]["tokens"], 10)  # type: ignore[index]
        self.assertEqual(sum(model["today"]["tokens"] for model in later.snapshot["models"]), 10)  # type: ignore[index]

    def test_new_root_thread_after_baseline_counts_its_initial_tokens_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            checkpoint = Path(temporary) / "codex-usage-day.json"
            create_state_database(database, [("gpt-5.6-sol", 100, "user", "vscode", 1_000_000)])
            collector = CodexUsageCollector(database_finder=lambda: database, poll_seconds=10, checkpoint_path=checkpoint)
            collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (
                    "new-thread", "gpt-5.6-terra", 25, "user", "vscode", 1_010_000,
                ))
                connection.commit()
            later = collector.collect(CollectorContext({"epoch": 1_010.0}, {}))

        total_point = next(point for point in later.points if point.dimensions.get("scope") == "local-state")
        self.assertEqual(total_point.value, 25)
        self.assertEqual(later.snapshot["usage"]["today"]["tokens"], 25)  # type: ignore[index]

    def test_missing_database_is_unavailable(self) -> None:
        collector = CodexUsageCollector(database_finder=lambda: None)
        result = collector.collect(CollectorContext({"epoch": 1_000.0}, {}))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.snapshot, {"available": False, "status": "unavailable"})
