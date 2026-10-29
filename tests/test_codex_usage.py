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
from infra_sentinel.resources.ai.codex_sampling import (  # noqa: E402
    load_codex_rollout_ledger, save_codex_rollout_ledger, update_codex_rollout_ledger,
)


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
    model: str = "gpt-5.6-sol",
    timestamp: str = "2026-08-22T12:00:00Z",
) -> str:
    return (
        record("event_msg", {"type": "task_started", "turn_id": turn_id}, timestamp)
        + record("turn_context", {"model": model, "turn_id": turn_id}, timestamp)
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


def model_timing_metrics(snapshot):
    return {group["sample"]["model"]: {m["id"]: m["value"] for m in group["metrics"]}
            for group in snapshot["details"] if group["id"] == "turn-performance"}


class CodexUsageTests(unittest.TestCase):
    def test_optional_telemetry_never_changes_rollout_usage_or_turn_timing(self):
        from infra_sentinel.resources.ai.performance import PerformanceSample, performance_details
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.jsonl").write_text(session("user") + usage(100, 100, "2026-08-22T12:00:00Z") + timed_turn())
            telemetry = performance_details(PerformanceSample(EPOCH, "gpt-5.6-sol", 100, 1000, 100), scope="codex-request")
            results = []
            def failed():
                raise OSError("private error details")
            for callback in (None, lambda: telemetry, failed):
                collector = CodexUsageCollector(rollout_roots_finder=lambda: (root,), telemetry_details=callback)
                results.append(collector.collect(CollectorContext({"epoch": EPOCH + 10}, {})))
        for result in results:
            self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 100)
            self.assertEqual(result.points, results[0].points)
            self.assertEqual(timing_metrics(result.snapshot), timing_metrics(results[0].snapshot))
        groups = {g["id"]: g for g in results[1].snapshot["details"]}
        self.assertEqual(groups["request-performance"]["metrics"][0]["value"], 100)
        self.assertEqual(groups["turn-performance"]["metrics"][0]["value"], 50)
        self.assertNotIn("private error", json.dumps(results[2].snapshot))

    def test_model_switch_in_one_session_keeps_each_latest_rate_across_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            path = root / "root.jsonl"
            path.write_text(session("user") + usage(100, 100, "2026-08-22T12:00:00Z")
                            + timed_turn("private-astra-1", model="gpt-6-astra", output=300)
                            + timed_turn("private-sol-1", model="gpt-6.1-sol", output=600, timestamp="2026-08-22T12:01:00Z")
                            + timed_turn("private-astra-2", model="gpt-6-astra", output=400, timestamp="2026-08-22T12:02:00Z"))
            checkpoint = Path(temporary) / "ledger.json"
            collector = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint)
            initial = collector.collect(CollectorContext({"epoch": EPOCH + 150}, {}))
            with path.open("a") as stream:
                stream.write("".join(timed_turn("private-sol-open", model="gpt-6.1-sol", output=999).splitlines(keepends=True)[:-1]))
            restarted = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint)
            after = restarted.collect(CollectorContext({"epoch": EPOCH + 180}, {}))
            saved = checkpoint.read_text()
        self.assertEqual({m: v["turn-output-rate"] for m, v in model_timing_metrics(initial.snapshot).items()},
                         {"gpt-6-astra": 40, "gpt-6.1-sol": 60})
        self.assertEqual(model_timing_metrics(initial.snapshot), model_timing_metrics(after.snapshot))
        self.assertEqual(after.snapshot["usage"]["cumulative"]["tokens"], 100)
        self.assertEqual(after.points, ())
        self.assertNotIn("private-astra", saved)
        self.assertNotIn("private-sol", saved)

    def test_old_checkpoint_backfills_models_without_replaying_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            path = root / "root.jsonl"
            path.write_text(session("user") + usage(100, 100, "2026-08-22T12:00:00Z")
                            + timed_turn("a", model="astra", output=300)
                            + timed_turn("s", model="sol", output=600, timestamp="2026-08-22T12:01:00Z"))
            checkpoint = Path(temporary) / "ledger.json"
            initial = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint)
            initial.collect(CollectorContext({"epoch": EPOCH + 150}, {}))
            old = json.loads(checkpoint.read_text())
            for entry in old["files"].values():
                entry.pop("latest_turn_timings", None)
            checkpoint.write_text(json.dumps(old))
            after = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint).collect(
                CollectorContext({"epoch": EPOCH + 180}, {}))
            saved = json.loads(checkpoint.read_text())
            path.unlink()
            deleted = CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint).collect(
                CollectorContext({"epoch": EPOCH + 210}, {}))
        self.assertEqual(set(model_timing_metrics(after.snapshot)), {"astra", "sol"})
        self.assertEqual(model_timing_metrics(deleted.snapshot), model_timing_metrics(after.snapshot))
        for field in ("days", "hours", "dedup_keys"):
            self.assertEqual(saved[field], old[field])
        self.assertEqual([e["offset"] for e in saved["files"].values()], [e["offset"] for e in old["files"].values()])
        self.assertEqual(after.points, ())
        self.assertFalse(any("timing_backfill" in e for e in saved["files"].values()))

    def test_bounded_timing_backfill_resumes_and_preserves_new_live_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            path = root / "root.jsonl"
            path.write_text(session("user") + usage(100, 100, "2026-08-22T12:00:00Z")
                            + timed_turn("a", model="astra", output=300)
                            + timed_turn("s", model="sol", output=600, timestamp="2026-08-22T12:01:00Z"))
            checkpoint = Path(temporary) / "ledger.json"
            CodexUsageCollector(rollout_roots_finder=lambda: (root,), ledger_path=checkpoint).collect(
                CollectorContext({"epoch": EPOCH + 150}, {}))
            old = json.loads(checkpoint.read_text())
            for entry in old["files"].values():
                entry.pop("latest_turn_timings", None)
            checkpoint.write_text(json.dumps(old))
            for index in range(30):
                ledger = load_codex_rollout_ledger(checkpoint)
                update = update_codex_rollout_ledger((root,), ledger, timezone=timezone.utc,
                    now=datetime.fromtimestamp(EPOCH + 300, timezone.utc), max_scan_bytes=600)
                self.assertEqual(update.increments, ())
                save_codex_rollout_ledger(checkpoint, ledger)
                if index == 0:
                    self.assertTrue(any("timing_backfill" in e for e in ledger.files.values()))
                    with path.open("a") as stream:
                        stream.write(timed_turn("s-new", model="sol", output=900, timestamp="2026-08-22T12:03:00Z"))
                elif not any("timing_backfill" in e for e in ledger.files.values()) and all(e["offset"] == path.stat().st_size for e in ledger.files.values()):
                    break
            else:
                self.fail("timing scan did not finish within bounded passes")
            rates = model_timing_metrics({"details": CodexUsageCollector._timing_details(ledger)})
        self.assertEqual(rates["astra"]["turn-output-rate"], 30)
        self.assertEqual(rates["sol"]["turn-output-rate"], 90)
        for field in ("days", "hours", "dedup_keys"):
            self.assertEqual(ledger.as_payload()[field], old[field])

    def test_latest_sample_is_selected_per_model_across_files_and_ignores_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, model, output, timestamp, source in (
                ("new-astra", "astra", 300, "2026-08-22T12:01:00Z", "user"),
                ("old-astra", "astra", 200, "2026-08-22T12:00:00Z", "user"),
                ("sol", "sol", 600, "2026-08-22T12:02:00Z", "user"),
                ("child", "astra", 9999, "2026-08-22T12:03:00Z", "subagent"),
            ):
                (root / (name + ".jsonl")).write_text(session(source) + timed_turn(model=model, output=output, timestamp=timestamp))
            result = CodexUsageCollector(rollout_roots_finder=lambda: (root,)).collect(CollectorContext({"epoch": EPOCH + 300}, {}))
        self.assertEqual({m: v["turn-output-rate"] for m, v in model_timing_metrics(result.snapshot).items()}, {"astra": 30, "sol": 60})

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
