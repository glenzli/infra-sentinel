from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import CollectorContext  # noqa: E402
from infra_sentinel.resources.ai.codex import CodexUsageCollector  # noqa: E402


EPOCH = datetime(2026, 8, 22, 12, tzinfo=timezone.utc).timestamp()


def record(record_type: str, payload: dict[str, object], timestamp: str) -> str:
    return json.dumps({"type": record_type, "timestamp": timestamp, "payload": payload}, separators=(",", ":")) + "\n"


def session(source: str, timestamp: str = "2026-08-22T11:59:00Z") -> str:
    return record("session_meta", {"thread_source": source}, timestamp)


def context(model: str, timestamp: str = "2026-08-22T11:59:30Z") -> str:
    return record("turn_context", {"model": model}, timestamp)


def usage(last_total: int, cumulative_total: int, timestamp: str, *, cached: int = 0) -> str:
    def fields(total: int, cached_tokens: int) -> dict[str, int]:
        return {
            "input_tokens": total,
            "cached_input_tokens": min(total, cached_tokens),
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": total,
        }

    return record("event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": fields(last_total, cached),
            "total_token_usage": fields(cumulative_total, cached),
        },
    }, timestamp)


def timed_turn(
    turn_id: str = "private-turn-id", *, output: object = 500,
    duration: object = 10_000, ttft: object = 2_000,
    timestamp: str = "2026-08-22T12:00:00Z",
) -> str:
    return (
        record("event_msg", {"type": "task_started", "turn_id": turn_id}, timestamp)
        + record("turn_context", {"model": "gpt-5.6-sol", "turn_id": turn_id}, timestamp)
        + record("token_usage_record", {
            "turn_id": turn_id, "response_id": "private-response-id",
            "usage": {"output_tokens": 100},
            "turn_token_usage": {"output_tokens": output},
        }, timestamp)
        + record("event_msg", {
            "type": "task_complete", "turn_id": turn_id, "duration_ms": duration,
            "time_to_first_token_ms": ttft, "last_agent_message": "private-response-text",
        }, timestamp)
    )


def timing_metrics(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    group = next((group for group in snapshot["details"] if group["id"] == "turn-performance"), None)
    return {metric["id"]: metric for metric in group["metrics"]} if group else {}


class CodexUsageTests(unittest.TestCase):
    def test_reported_turn_timing_uses_turn_total_without_changing_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "sessions"
            root.mkdir()
            (root / "root.jsonl").write_text(
                session("user") + usage(100, 100, "2026-08-22T12:00:00Z") + timed_turn(),
                encoding="utf-8",
            )
            checkpoint = base / "ledger.json"
            collector = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint)
            result = collector.collect(CollectorContext({"epoch": EPOCH + 10}, {}))
            restarted = CodexUsageCollector(
                rollout_roots_finder=lambda: (root,), ledger_path=checkpoint,
            ).collect(CollectorContext({"epoch": EPOCH + 40}, {}))
            persisted = checkpoint.read_text(encoding="utf-8")

        group = next(group for group in result.snapshot["details"] if group["id"] == "turn-performance")
        self.assertEqual(group["sample"]["model"], "gpt-5.6-sol")
        self.assertEqual(datetime.fromisoformat(group["sample"]["recorded_at"]).timestamp(), EPOCH)
        metrics = timing_metrics(result.snapshot)
        self.assertEqual(metrics["turn-output-rate"]["value"], 50)
        self.assertEqual(metrics["turn-output-rate"]["unit"], "tokens/s")
        self.assertEqual(metrics["first-token-latency"]["value"], 2)
        self.assertEqual(metrics["turn-duration"]["value"], 10)
        self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 100)
        self.assertEqual(result.points, ())
        self.assertEqual(timing_metrics(restarted.snapshot), metrics)
        for private in ("private-turn-id", "private-response-id", "private-response-text"):
            self.assertNotIn(private, persisted)
            self.assertNotIn(private, json.dumps(result.snapshot))

    def test_timing_waits_for_completion_and_survives_midturn_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "sessions"
            root.mkdir()
            rollout = root / "root.jsonl"
            lines = timed_turn().splitlines(keepends=True)
            rollout.write_text(session("user") + "".join(lines[:-1]), encoding="utf-8")
            checkpoint = base / "ledger.json"
            before = CodexUsageCollector(
                rollout_roots_finder=lambda: (root,), ledger_path=checkpoint,
            ).collect(CollectorContext({"epoch": EPOCH}, {}))
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(lines[-1])
            restarted = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint)
            after = restarted.collect(CollectorContext({"epoch": EPOCH + 30}, {}))
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(lines[-1])
            repeated = restarted.collect(CollectorContext({"epoch": EPOCH + 60}, {}))

        self.assertEqual(timing_metrics(before.snapshot), {})
        self.assertEqual(timing_metrics(after.snapshot)["turn-output-rate"]["value"], 50)
        self.assertEqual(timing_metrics(repeated.snapshot), timing_metrics(after.snapshot))
        self.assertEqual(after.snapshot["usage"]["cumulative"]["tokens"], 0)
        self.assertEqual(after.points, ())

    def test_missing_invalid_or_unmatched_timing_never_guesses_a_rate(self) -> None:
        cases = [
            timed_turn(duration=0), timed_turn(duration=-1), timed_turn(duration=True),
            timed_turn(duration="10000"), timed_turn(output=None), timed_turn(output=True),
            timed_turn().replace('"turn_id":"private-turn-id","duration_ms"', '"turn_id":"unmatched","duration_ms"'),
            "".join(timed_turn().splitlines(keepends=True)[2:]),
            context("gpt-5.6-sol") + usage(100, 100, "2026-08-22T12:00:00Z"),
        ]
        for index, content in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "root.jsonl").write_text(session("user") + content, encoding="utf-8")
                result = CodexUsageCollector(rollout_roots_finder=lambda: (root,)).collect(
                    CollectorContext({"epoch": EPOCH}, {}),
                )
                self.assertEqual(result.status, "ok")
                self.assertEqual(timing_metrics(result.snapshot), {})

    def test_unavailable_latency_is_omitted_and_known_zero_values_are_kept(self) -> None:
        for ttft in (None, -1, 20_000, True):
            with self.subTest(ttft=ttft), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "root.jsonl").write_text(session("user") + timed_turn(ttft=ttft), encoding="utf-8")
                result = CodexUsageCollector(rollout_roots_finder=lambda: (root,)).collect(
                    CollectorContext({"epoch": EPOCH}, {}),
                )
                metrics = timing_metrics(result.snapshot)
                self.assertEqual(metrics["turn-output-rate"]["value"], 50)
                self.assertNotIn("first-token-latency", metrics)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "root.jsonl").write_text(session("user") + timed_turn(output=0, ttft=0), encoding="utf-8")
            result = CodexUsageCollector(rollout_roots_finder=lambda: (root,)).collect(CollectorContext({"epoch": EPOCH}, {}))
        self.assertEqual(timing_metrics(result.snapshot)["turn-output-rate"]["value"], 0)
        self.assertEqual(timing_metrics(result.snapshot)["first-token-latency"]["value"], 0)

    def test_subagents_and_fork_replay_do_not_replace_main_chat_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "root.jsonl").write_text(session("user") + timed_turn(), encoding="utf-8")
            (root / "child.jsonl").write_text(
                record("session_meta", {"id": "child", "forked_from_id": "parent", "thread_source": "subagent"}, "2026-08-22T12:01:00Z")
                + timed_turn(output=1_000, timestamp="2026-08-22T12:02:00Z"), encoding="utf-8",
            )
            (root / "subagent.jsonl").write_text(
                session("subagent") + timed_turn(output=2_000, timestamp="2026-08-22T12:03:00Z"), encoding="utf-8",
            )
            result = CodexUsageCollector(rollout_roots_finder=lambda: (root,)).collect(CollectorContext({"epoch": EPOCH + 300}, {}))
        self.assertEqual(timing_metrics(result.snapshot)["turn-output-rate"]["value"], 50)

    def test_latest_completion_is_selected_by_event_time_not_file_scan_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "newer.jsonl").write_text(
                session("user") + timed_turn(output=250, timestamp="2026-08-22T12:01:00Z"), encoding="utf-8",
            )
            (root / "older.jsonl").write_text(session("user") + timed_turn(), encoding="utf-8")
            result = CodexUsageCollector(rollout_roots_finder=lambda: (root,)).collect(CollectorContext({"epoch": EPOCH + 120}, {}))
        self.assertEqual(timing_metrics(result.snapshot)["turn-output-rate"]["value"], 25)

    def test_bootstrap_rebuilds_jsonl_history_without_replaying_it_as_live_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "sessions"
            root.mkdir()
            (root / "rollout-2026-08-22-root.jsonl").write_text(
                session("user") + context("gpt-5.6-terra") + usage(100, 100, "2026-08-22T12:00:00Z", cached=60),
                encoding="utf-8",
            )
            (root / "rollout-2026-08-22-subagent.jsonl").write_text(
                session("subagent") + context("codex-auto-review") + usage(40, 40, "2026-08-22T12:01:00Z", cached=20),
                encoding="utf-8",
            )
            checkpoint = base / "codex-rollout-ledger.json"
            result = CodexUsageCollector(
                rollout_roots_finder=lambda: (root,), ledger_path=checkpoint, poll_seconds=10,
            ).collect(CollectorContext({"epoch": EPOCH + 120}, {}))
            persisted = checkpoint.read_text(encoding="utf-8")

        self.assertEqual(result.points, ())
        self.assertEqual(result.snapshot["collection_method"], "Codex local rollout JSONL ledger")
        self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 140)
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 140)
        self.assertEqual(result.snapshot["usage"]["cumulative"]["method"], "local-rollout-ledger")
        self.assertEqual(result.snapshot["history"]["daily"], [{
            "date": "2026-08-22",
            "tokens": 140,
            "models": [
                {"id": "codex-auto-review", "tokens": 40},
                {"id": "gpt-5.6-terra", "tokens": 100},
            ],
        }])
        self.assertTrue(result.snapshot["history"]["hourly_available"])
        self.assertEqual(sum(row["tokens"] for row in result.snapshot["history"]["hourly"]), 140)
        self.assertTrue(result.snapshot["pricing"]["daily_available"])
        reference = result.snapshot["pricing"]["daily"][0]["reference"]
        self.assertEqual(reference["kind"], "local-rollout-standard-api-projection")
        self.assertEqual(reference["priced_tokens"], 100)
        self.assertEqual(reference["unpriced_tokens"], 40)
        groups = {group["id"]: group for group in result.snapshot["details"]}
        self.assertNotIn("cached-weight-comparison", groups)
        inherited = next(
            metric for metric in groups["rollout-ledger"]["metrics"]
            if metric["id"] == "inherited-snapshots"
        )
        self.assertEqual(inherited["value"], 0)
        self.assertNotIn("rollout-2026", persisted)

    def test_appended_reset_is_counted_once_and_emitted_at_request_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "sessions"
            root.mkdir()
            rollout = root / "rollout-2026-08-22-live.jsonl"
            rollout.write_text(
                session("user") + context("gpt-5.6-sol") + usage(100, 100, "2026-08-22T12:00:00Z"),
                encoding="utf-8",
            )
            collector = CodexUsageCollector(
                rollout_roots_finder=lambda: (root,), ledger_path=base / "ledger.json", poll_seconds=10,
            )
            collector.collect(CollectorContext({"epoch": EPOCH + 10}, {}))
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(usage(20, 20, "2026-08-22T12:02:00Z"))
            updated = collector.collect(CollectorContext({"epoch": EPOCH + 30}, {}))
            repeated = collector.collect(CollectorContext({"epoch": EPOCH + 50}, {}))

        total_points = [point for point in updated.points if point.metric == "ai.tokens.total"]
        self.assertEqual([(point.value, point.dimensions) for point in total_points], [
            (20, {"scope": "local-jsonl"}),
            (20, {"model": "gpt-5.6-sol"}),
        ])
        expected_event_time = datetime.fromtimestamp(EPOCH, tz=timezone.utc).astimezone().replace(
            hour=20, minute=0, second=0, microsecond=0,
        ).isoformat(timespec="seconds")
        self.assertEqual({point.observed_at for point in updated.points}, {expected_event_time})
        self.assertEqual(updated.snapshot["usage"]["today"]["tokens"], 120)
        groups = {group["id"]: group for group in updated.snapshot["details"]}
        reset_metric = next(metric for metric in groups["rollout-ledger"]["metrics"] if metric["id"] == "counter-resets")
        self.assertEqual(reset_metric["value"], 1)
        self.assertEqual(repeated.points, ())

    def test_checkpoint_retains_cumulative_history_after_rollout_deletion_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "sessions"
            root.mkdir()
            rollout = root / "rollout-2026-08-22-delete.jsonl"
            rollout.write_text(usage(75, 75, "2026-08-22T12:00:00Z"), encoding="utf-8")
            checkpoint = base / "ledger.json"
            CodexUsageCollector(
                rollout_roots_finder=lambda: (root,), ledger_path=checkpoint,
            ).collect(CollectorContext({"epoch": EPOCH + 1}, {}))
            rollout.unlink()
            restarted = CodexUsageCollector(
                rollout_roots_finder=lambda: (root,), ledger_path=checkpoint,
            ).collect(CollectorContext({"epoch": EPOCH + 30}, {}))

        self.assertEqual(restarted.snapshot["usage"]["cumulative"]["tokens"], 75)
        self.assertEqual(restarted.points, ())

    def test_missing_rollout_roots_is_unavailable(self) -> None:
        collector = CodexUsageCollector(rollout_roots_finder=lambda: ())
        result = collector.collect(CollectorContext({"epoch": EPOCH}, {}))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.snapshot, {"available": False, "status": "unavailable"})


if __name__ == "__main__":
    unittest.main()
