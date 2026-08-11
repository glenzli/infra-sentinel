from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_collectors import CollectorContext  # noqa: E402
from opencode_usage import (  # noqa: E402
    OPENCODE_COUNTER_SCHEMA, OpenCodeUsageCollector, discover_opencode,
    discover_opencode_desktop_database, parse_opencode_stats,
    read_opencode_desktop_daily_history, read_opencode_desktop_stats,
)


STATS_OUTPUT = """
┌────────────────────────────────────────────────────────┐
│                       OVERVIEW                         │
├────────────────────────────────────────────────────────┤
│Sessions                                              3 │
│Messages                                             12 │
│Days                                                  1 │
└────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────┐
│                    COST & TOKENS                       │
├────────────────────────────────────────────────────────┤
│Total Cost                                         $0.15 │
│Avg Cost/Day                                       $0.15 │
│Avg Tokens/Session                                 4.2K │
│Median Tokens/Session                               3.8K │
│Input                                              8.0K │
│Output                                             1.2K │
│Cache Read                                          2.0K │
│Cache Write                                         0.4K │
└────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────┐
│                      MODEL USAGE                       │
├────────────────────────────────────────────────────────┤
│ openai/gpt-5.6                                      │
│  Messages                                           8 │
│  Input Tokens                                    5.0K │
│  Output Tokens                                   1.1K │
│  Cache Read                                      1.5K │
│  Cache Write                                     0.2K │
│  Cost                                        $0.1200 │
├────────────────────────────────────────────────────────┤
│ deepseek/deepseek-chat                              │
│  Messages                                           4 │
│  Input Tokens                                    3.0K │
│  Output Tokens                                   0.3K │
│  Cache Read                                      0.5K │
│  Cache Write                                     0.2K │
│  Cost                                        $0.0300 │
└────────────────────────────────────────────────────────┘
"""


class OpenCodeStatsTests(unittest.TestCase):
    def test_explicit_portable_executable_and_database_take_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "opencode.exe"
            database = root / "portable.db"
            executable.touch()
            executable.chmod(executable.stat().st_mode | 0o100)
            database.touch()
            self.assertEqual(discover_opencode(executable), str(executable))
            self.assertEqual(discover_opencode_desktop_database(database), database)

    def test_stats_parser_uses_model_breakdown_and_marks_combined_reasoning(self) -> None:
        stats = parse_opencode_stats(STATS_OUTPUT)

        self.assertEqual(stats.sessions, 3)
        self.assertEqual(stats.messages, 12)
        self.assertEqual(stats.input_tokens, 8_000)
        self.assertEqual(stats.output_tokens, 1_400)
        self.assertEqual(stats.cache_read_tokens, 2_000)
        self.assertEqual(stats.cache_write_tokens, 400)
        self.assertEqual(stats.total_tokens, 11_800)
        self.assertAlmostEqual(stats.cost_usd, 0.15)
        self.assertTrue(stats.output_includes_reasoning)
        self.assertEqual([model["id"] for model in stats.models], ["openai/gpt-5.6", "deepseek/deepseek-chat"])

    def test_missing_rows_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required rows"):
            parse_opencode_stats("│Sessions 1│")

    def test_collector_paces_cli_and_writes_nonduplicated_model_intervals(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, STATS_OUTPUT, "")

        collector = OpenCodeUsageCollector(
            executable_finder=lambda: "/test/opencode",
            desktop_database_finder=lambda: None,
            runner=runner,
            poll_seconds=60,
        )
        first = collector.collect(CollectorContext({"epoch": 100.0}, {}))
        cached = collector.collect(CollectorContext({"epoch": 110.0}, {}))
        later = collector.collect(CollectorContext({"epoch": 160.0}, {}))

        self.assertEqual(calls, [["/test/opencode", "stats", "--days", "0", "--models"], ["/test/opencode", "stats", "--days", "0", "--models"]])
        self.assertEqual(first.status, "ok")
        self.assertEqual(first.snapshot["usage"]["today"]["tokens"], 11_800)  # type: ignore[index]
        self.assertEqual(sum(point.value for point in first.points if point.metric == "ai.tokens.total"), 11_800)
        self.assertEqual(sum(point.value for point in first.points if point.metric == "ai.tokens.input"), 8_000)
        self.assertEqual(cached.points, ())
        self.assertEqual(later.points, ())
        self.assertEqual(
            {point.dimensions.get("model") for point in first.points if point.metric == "ai.tokens.input"},
            {"openai/gpt-5.6", "deepseek/deepseek-chat"},
        )

    def test_missing_executable_is_absent_not_a_runtime_failure(self) -> None:
        collector = OpenCodeUsageCollector(executable_finder=lambda: None, desktop_database_finder=lambda: None)
        result = collector.collect(CollectorContext({"epoch": 100.0}, {}))

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.snapshot, {"available": False, "status": "unavailable"})

    def test_restart_reuses_same_day_counter_checkpoint_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "opencode-usage-counters.json"

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(command, 0, STATS_OUTPUT, "")

            first_collector = OpenCodeUsageCollector(
                executable_finder=lambda: "/test/opencode", desktop_database_finder=lambda: None,
                runner=runner, checkpoint_path=checkpoint,
            )
            first = first_collector.collect(CollectorContext({"epoch": 1_786_083_200.0}, {}))
            restarted = OpenCodeUsageCollector(
                executable_finder=lambda: "/test/opencode", desktop_database_finder=lambda: None,
                runner=runner, checkpoint_path=checkpoint,
            ).collect(CollectorContext({"epoch": 1_786_083_220.0}, {}))

        self.assertTrue(first.points)
        self.assertEqual(restarted.points, ())

    def test_checkpoint_envelope_upgrade_does_not_replay_stable_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "opencode-usage-counters.json"
            checkpoint.write_text(json.dumps({
                "schema": "20990101.9",
                "counter_schema": OPENCODE_COUNTER_SCHEMA,
                "day": "2026-08-09",
                "counters": {
                    "openai/gpt-5.6:ai.tokens.total": 7_800,
                    "openai/gpt-5.6:ai.tokens.input": 5_000,
                    "openai/gpt-5.6:ai.tokens.output": 1_100,
                    "openai/gpt-5.6:ai.tokens.cache_read": 1_500,
                    "openai/gpt-5.6:ai.tokens.cache_write": 200,
                    "openai/gpt-5.6:ai.tokens.reasoning": 0,
                    "openai/gpt-5.6:ai.cost.usd": 0.12,
                    "deepseek/deepseek-chat:ai.tokens.total": 4_000,
                    "deepseek/deepseek-chat:ai.tokens.input": 3_000,
                    "deepseek/deepseek-chat:ai.tokens.output": 300,
                    "deepseek/deepseek-chat:ai.tokens.cache_read": 500,
                    "deepseek/deepseek-chat:ai.tokens.cache_write": 200,
                    "deepseek/deepseek-chat:ai.tokens.reasoning": 0,
                    "deepseek/deepseek-chat:ai.cost.usd": 0.03,
                    "all:ai.messages": 12,
                },
            }), encoding="utf-8")

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(command, 0, STATS_OUTPUT, "")

            result = OpenCodeUsageCollector(
                executable_finder=lambda: "/test/opencode", desktop_database_finder=lambda: None,
                runner=runner, checkpoint_path=checkpoint,
            ).collect(CollectorContext({"epoch": datetime(2026, 8, 9, 12).timestamp()}, {}))

        self.assertEqual(result.points, ())

    def test_failed_later_poll_keeps_last_complete_snapshot_visible(self) -> None:
        outputs = iter([STATS_OUTPUT, "not a supported stats table"])

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, next(outputs), "")

        collector = OpenCodeUsageCollector(
            executable_finder=lambda: "/test/opencode",
            desktop_database_finder=lambda: None,
            runner=runner,
            poll_seconds=60,
        )
        collector.collect(CollectorContext({"epoch": 100.0}, {}))
        failed = collector.collect(CollectorContext({"epoch": 160.0}, {}))

        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.snapshot["usage"]["today"]["tokens"], 11_800)  # type: ignore[index]

    def test_desktop_database_reads_only_today_assistant_usage_metadata(self) -> None:
        epoch = datetime(2026, 8, 9, 9, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "opencode.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE message (session_id TEXT, time_created INTEGER, data TEXT)")
                connection.executemany("INSERT INTO message VALUES (?, ?, ?)", [
                    ("session-a", int(epoch * 1000), json.dumps({
                        "role": "assistant", "providerID": "openai", "modelID": "gpt-5.6",
                        "tokens": {"input": 100, "output": 20, "reasoning": 30, "cache": {"read": 40, "write": 5}},
                        "cost": 0.12, "text": "this must never be selected",
                    })),
                    ("session-b", int(epoch * 1000), json.dumps({
                        "role": "assistant", "providerID": "deepseek", "modelID": "deepseek-chat",
                        "tokens": {"input": 50, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        "cost": 0.01,
                    })),
                    ("session-a", int(epoch * 1000), json.dumps({"role": "user", "text": "never count user data"})),
                ])
                connection.commit()
            stats = read_opencode_desktop_stats(database, epoch)

        self.assertEqual(stats.sessions, 2)
        self.assertEqual(stats.messages, 2)
        self.assertEqual(stats.input_tokens, 150)
        self.assertEqual(stats.output_tokens, 30)
        self.assertEqual(stats.reasoning_tokens, 30)
        self.assertEqual(stats.cache_read_tokens, 40)
        self.assertEqual(stats.cache_write_tokens, 5)
        self.assertEqual(stats.total_tokens, 255)
        self.assertFalse(stats.output_includes_reasoning)
        self.assertEqual([item["id"] for item in stats.models], ["openai/gpt-5.6", "deepseek/deepseek-chat"])

    def test_desktop_database_exposes_exact_daily_model_history(self) -> None:
        today = datetime(2026, 8, 9, 9, tzinfo=timezone.utc).timestamp()
        yesterday = today - 86_400
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "opencode.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE message (session_id TEXT, time_created INTEGER, data TEXT)")
                connection.executemany("INSERT INTO message VALUES (?, ?, ?)", [
                    ("session-a", int(yesterday * 1000), json.dumps({
                        "role": "assistant", "providerID": "openai", "modelID": "gpt-5.6",
                        "tokens": {"input": 100, "output": 20, "reasoning": 30, "cache": {"read": 40, "write": 5}},
                    })),
                    ("session-b", int(today * 1000), json.dumps({
                        "role": "assistant", "providerID": "deepseek", "modelID": "deepseek-chat",
                        "tokens": {"input": 50, "output": 10, "reasoning": 0, "cache": {"read": 5, "write": 0}},
                    })),
                    ("session-b", int(today * 1000), json.dumps({"role": "user", "text": "never selected"})),
                ])
                connection.commit()
            history = read_opencode_desktop_daily_history(database)

        self.assertEqual([day.date for day in history], ["2026-08-08", "2026-08-09"])
        self.assertEqual([day.total_tokens for day in history], [195, 65])
        self.assertEqual(history[0].models, ({"id": "openai/gpt-5.6", "tokens": 195},))
        self.assertEqual(history[1].models, ({"id": "deepseek/deepseek-chat", "tokens": 65},))

    def test_snapshot_keeps_lifetime_models_when_the_desktop_database_is_available(self) -> None:
        stats = parse_opencode_stats(STATS_OUTPUT)
        collector = OpenCodeUsageCollector(desktop_database_finder=lambda: None)
        lifetime_models = [
            *collector._models_with_totals(stats),
            {"id": "historical/model", "total_tokens": 42},
        ]
        snapshot = collector._snapshot_for(
            stats, "2026-08-09T12:00:00+08:00", "desktop-session-metadata", 11_842, lifetime_models,
        )

        self.assertEqual(snapshot["usage"]["cumulative"]["tokens"], 11_842)
        self.assertEqual(
            [model["id"] for model in snapshot["models"]],
            ["openai/gpt-5.6", "deepseek/deepseek-chat", "historical/model"],
        )
        self.assertTrue(all(model["cumulative"]["available"] for model in snapshot["models"]))
        self.assertEqual(snapshot["models"][-1]["today"]["tokens"], 0)
        activity = next(group for group in snapshot["details"] if group["id"] == "activity")
        reported_cost = next(metric for metric in activity["metrics"] if metric["id"] == "reported-cost")
        self.assertAlmostEqual(reported_cost["value"], 0.15)
        self.assertEqual(reported_cost["unit"], "usd")

    def test_snapshot_distinguishes_available_daily_history_from_missing_history(self) -> None:
        stats = parse_opencode_stats(STATS_OUTPUT)
        collector = OpenCodeUsageCollector(desktop_database_finder=lambda: None)
        available = collector._snapshot_for(
            stats, "2026-08-09T12:00:00+08:00", "desktop-session-metadata", 11_800,
            collector._models_with_totals(stats), (),
        )
        unavailable = collector._snapshot_for(
            stats, "2026-08-09T12:00:00+08:00", "cli-session-summary", None, None,
        )

        self.assertEqual(available["history"], {"daily_available": True, "daily": []})
        self.assertEqual(unavailable["history"], {"daily_available": False, "daily": []})


if __name__ == "__main__":
    unittest.main()
