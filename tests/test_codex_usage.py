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


class CodexUsageTests(unittest.TestCase):
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
