from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_collectors import CollectorContext  # noqa: E402
from opencode_usage import OpenCodeUsageCollector, parse_opencode_stats, read_opencode_desktop_stats  # noqa: E402


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
        self.assertEqual(first.snapshot["tokens"]["total"], 11_800)  # type: ignore[index]
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
        self.assertEqual(failed.snapshot["tokens"]["total"], 11_800)  # type: ignore[index]

    def test_desktop_database_reads_only_today_assistant_usage_metadata(self) -> None:
        epoch = datetime(2026, 8, 9, 9, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "opencode.db"
            with sqlite3.connect(database) as connection:
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


if __name__ == "__main__":
    unittest.main()
