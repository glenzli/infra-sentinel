from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.ai.codex_sampling import (  # noqa: E402
    CodexRolloutLedger,
    audit_codex_rollouts,
    load_codex_rollout_ledger,
    rebuild_codex_rollout_ledger,
    save_codex_rollout_ledger,
    update_codex_rollout_ledger,
)
from infra_sentinel.resources.ai.codex_pricing import estimate_standard_api_cost  # noqa: E402


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def record(record_type: str, payload: dict[str, object], timestamp: str = "2026-08-21T12:00:00Z") -> str:
    return json.dumps({"type": record_type, "timestamp": timestamp, "payload": payload}, separators=(",", ":")) + "\n"


def context(model: str) -> str:
    return record("turn_context", {"model": model})


def cumulative_usage(last_total: int, cumulative_total: int, timestamp: str) -> str:
    def fields(total: int) -> dict[str, int]:
        return {
            "input_tokens": total,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": total,
        }

    return record("event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": fields(last_total),
            "total_token_usage": fields(cumulative_total),
        },
    }, timestamp)


class CodexJsonlSamplingTests(unittest.TestCase):
    def test_durable_ledger_rebuilds_once_then_ingests_only_appended_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            rollout = root / "rollout-2026-08-22-ledger.jsonl"
            rollout.write_text(
                record("session_meta", {"thread_source": "user"})
                + context("gpt-5.6-sol")
                + cumulative_usage(100, 100, "2026-08-22T01:00:00Z")
                + cumulative_usage(50, 150, "2026-08-22T01:01:00Z")
                + cumulative_usage(20, 20, "2026-08-22T01:02:00Z"),
                encoding="utf-8",
            )
            ledger = rebuild_codex_rollout_ledger([root], timezone=timezone.utc, now=NOW)
            checkpoint = Path(temporary) / "codex-rollout-ledger.json"
            save_codex_rollout_ledger(checkpoint, ledger)
            reloaded = load_codex_rollout_ledger(checkpoint)
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(cumulative_usage(30, 50, "2026-08-22T01:03:00Z"))
            update = update_codex_rollout_ledger(
                [root], reloaded, timezone=timezone.utc, now=NOW, max_scan_bytes=None,
            )
            repeated = update_codex_rollout_ledger(
                [root], reloaded, timezone=timezone.utc, now=NOW, max_scan_bytes=None,
            )
            rollout.unlink()
            retained = update_codex_rollout_ledger(
                [root], reloaded, timezone=timezone.utc, now=NOW, max_scan_bytes=None,
            )

        self.assertEqual(ledger.cumulative().total_tokens, 170)
        self.assertEqual([increment.usage["total_tokens"] for increment in update.increments], [30])
        self.assertEqual(reloaded.cumulative().total_tokens, 200)
        self.assertEqual(repeated.increments, ())
        self.assertEqual(retained.increments, ())
        self.assertEqual(reloaded.days["2026-08-22"].counter_resets, 1)
        self.assertFalse(reloaded.partial)

    def test_ledger_marker_survives_move_from_live_to_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            live = base / "sessions"
            archived = base / "archived_sessions"
            live.mkdir()
            archived.mkdir()
            rollout = live / "rollout-2026-08-22-moved.jsonl"
            rollout.write_text(
                cumulative_usage(40, 40, "2026-08-22T03:00:00Z"), encoding="utf-8",
            )
            ledger = rebuild_codex_rollout_ledger([live, archived], timezone=timezone.utc, now=NOW)
            moved = archived / rollout.name
            rollout.replace(moved)
            update = update_codex_rollout_ledger(
                [live, archived], ledger, timezone=timezone.utc, now=NOW, max_scan_bytes=None,
            )

        self.assertEqual(ledger.cumulative().total_tokens, 40)
        self.assertEqual(update.increments, ())
        self.assertEqual(len(ledger.files), 1)

    def test_invalid_ledger_schema_rebuilds_from_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "ledger.json"
            checkpoint.write_text('{"schema":"old","days":{"2026-08-22":{"total_tokens":99}}}', encoding="utf-8")
            ledger = load_codex_rollout_ledger(checkpoint)

        self.assertIsInstance(ledger, CodexRolloutLedger)
        self.assertEqual(ledger.days, {})
        self.assertEqual(ledger.files, {})

    def test_audit_uses_cumulative_deltas_suppresses_duplicates_and_counts_resets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            rollout = root / "2026" / "08" / "20" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                record("session_meta", {"session_id": "session-1", "thread_source": "user"})
                + context("gpt-5.6-sol")
                + cumulative_usage(100, 100, "2026-08-22T01:00:00Z")
                + cumulative_usage(100, 100, "2026-08-22T01:01:00Z")
                + cumulative_usage(50, 150, "2026-08-22T01:02:00Z")
                + cumulative_usage(20, 20, "2026-08-22T01:03:00Z"),
                encoding="utf-8",
            )
            audit = audit_codex_rollouts(
                [root], start_day=NOW.date().replace(day=22), end_day=NOW.date().replace(day=22), timezone=timezone.utc,
            )

        day = audit.days["2026-08-22"]
        self.assertEqual(day.composition.total_tokens, 170)
        self.assertEqual(day.composition.events, 3)
        self.assertEqual(day.token_records, 4)
        self.assertEqual(day.duplicate_snapshots, 1)
        self.assertEqual(day.counter_resets, 1)
        self.assertEqual(day.delta_last_mismatches, 0)
        self.assertEqual(day.source_tokens, {"user": 170})
        self.assertEqual(day.composition.models, {"gpt-5.6-sol": 170})

    def test_audit_first_record_uses_last_usage_instead_of_inherited_cumulative_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            (root / "rollout-2026-08-22-fork.jsonl").write_text(
                record("session_meta", {
                    "session_id": "fork-1", "thread_source": "subagent", "forked_from_id": "not-retained",
                })
                + context("gpt-5.6-terra")
                + cumulative_usage(25, 1_025, "2026-08-22T02:00:00Z")
                + cumulative_usage(30, 1_055, "2026-08-22T02:01:00Z"),
                encoding="utf-8",
            )
            audit = audit_codex_rollouts(
                [root], start_day=NOW.date().replace(day=22), end_day=NOW.date().replace(day=22), timezone=timezone.utc,
            )

        day = audit.days["2026-08-22"]
        self.assertEqual(day.composition.total_tokens, 55)
        self.assertEqual(day.source_tokens, {"subagent": 55})
        self.assertEqual(day.delta_last_mismatches, 0)

    def test_audit_includes_continuing_old_rollout_and_deduplicates_archived_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            live = base / "sessions"
            archived = base / "archived_sessions"
            live_rollout = live / "2026" / "08" / "20" / "rollout.jsonl"
            related_rollout = live / "2026" / "08" / "20" / "related.jsonl"
            archived_rollout = archived / "rollout-2026-08-20-copy.jsonl"
            live_rollout.parent.mkdir(parents=True)
            archived.mkdir()
            payload = record("session_meta", {"thread_source": "user"}) + cumulative_usage(
                40, 40, "2026-08-22T03:00:00Z",
            )
            live_rollout.write_text(payload, encoding="utf-8")
            archived_rollout.write_text(payload, encoding="utf-8")
            related_rollout.write_text(
                record("session_meta", {"thread_source": "subagent"})
                + cumulative_usage(60, 60, "2026-08-22T03:01:00Z"),
                encoding="utf-8",
            )
            audit = audit_codex_rollouts(
                [live, archived], start_day=NOW.date().replace(day=22), end_day=NOW.date().replace(day=23), timezone=timezone.utc,
            )

        self.assertEqual(audit.candidate_files, 3)
        self.assertEqual(audit.scanned_files, 2)
        self.assertEqual(audit.duplicate_files, 1)
        self.assertEqual(audit.days["2026-08-22"].composition.total_tokens, 100)

    def test_standard_api_estimate_uses_observed_model_breakdowns_only(self) -> None:
        estimate = estimate_standard_api_cost({
            "gpt-5.6-terra": {
                "input_tokens": 100, "cached_input_tokens": 40,
                "cache_write_input_tokens": 0, "output_tokens": 20,
                "reasoning_output_tokens": 8, "total_tokens": 120,
            },
            "unknown-local-model": {
                "input_tokens": 30, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 10,
                "reasoning_output_tokens": 2, "total_tokens": 40,
            },
        })
        self.assertEqual(estimate.priced_tokens, 120)
        self.assertEqual(estimate.unpriced_tokens, 40)
        self.assertEqual(estimate.models[0].model, "gpt-5.6-terra")
        self.assertAlmostEqual(estimate.models[0].cost_usd, 0.000368)
        self.assertAlmostEqual(estimate.total_cost_usd, 0.000368)

    def test_gpt_5_5_uses_its_published_input_cached_input_and_output_reference(self) -> None:
        estimate = estimate_standard_api_cost({
            "gpt-5.5": {
                "input_tokens": 1_000_000,
                "cached_input_tokens": 400_000,
                "cache_write_input_tokens": 200_000,
                "output_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
        })

        # GPT-5.5 had no separate cache-write price: such observed tokens stay
        # on the normal input leg instead of being treated as a zero-cost tier.
        self.assertEqual(estimate.priced_tokens, 2_000_000)
        self.assertAlmostEqual(estimate.total_cost_usd, 33.2)

if __name__ == "__main__":
    unittest.main()
