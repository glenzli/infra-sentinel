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
    JsonlSampleState,
    composition_note,
    current_day_sample,
    load_jsonl_sample_state,
    sample_visible_rollouts,
    save_jsonl_sample_state,
)
from infra_sentinel.resources.ai.codex_pricing import estimate_standard_api_cost  # noqa: E402


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def record(record_type: str, payload: dict[str, object], timestamp: str = "2026-08-21T12:00:00Z") -> str:
    return json.dumps({"type": record_type, "timestamp": timestamp, "payload": payload}, separators=(",", ":")) + "\n"


def context(model: str) -> str:
    return record("turn_context", {"model": model})


def usage(*, input_tokens: int, cached_input_tokens: int, output_tokens: int, reasoning_output_tokens: int, total_tokens: int) -> str:
    return record("event_msg", {
        "type": "token_count",
        "info": {"last_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_input_tokens": 0,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_tokens": total_tokens,
        }},
    })


class CodexJsonlSamplingTests(unittest.TestCase):
    def test_uses_only_token_count_after_explicit_turn_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            (root / "rollout.jsonl").write_text(
                context("gpt-5.6-terra")
                + usage(input_tokens=100, cached_input_tokens=40, output_tokens=20, reasoning_output_tokens=8, total_tokens=120)
                + context("gpt-5.6-luna")
                + usage(input_tokens=30, cached_input_tokens=0, output_tokens=10, reasoning_output_tokens=2, total_tokens=40)
                + record("event_msg", {"type": "agent_message", "message": "never read"}),
                encoding="utf-8",
            )
            state = sample_visible_rollouts(root, JsonlSampleState(), now=NOW)

        sample = current_day_sample(state, NOW.timestamp())
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.events, 2)
        self.assertEqual(sample.total_tokens, 160)
        self.assertEqual(sample.input_tokens, 130)
        self.assertEqual(sample.cached_input_tokens, 40)
        self.assertEqual(sample.output_tokens, 30)
        self.assertEqual(sample.reasoning_output_tokens, 10)
        self.assertEqual(sample.models, {"gpt-5.6-luna": 40, "gpt-5.6-terra": 120})
        self.assertEqual(sample.model_compositions["gpt-5.6-terra"].cached_input_tokens, 40)

    def test_standard_api_estimate_uses_observed_model_breakdowns_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            (root / "rollout.jsonl").write_text(
                context("gpt-5.6-terra")
                + usage(input_tokens=100, cached_input_tokens=40, output_tokens=20, reasoning_output_tokens=8, total_tokens=120)
                + context("unknown-local-model")
                + usage(input_tokens=30, cached_input_tokens=0, output_tokens=10, reasoning_output_tokens=2, total_tokens=40),
                encoding="utf-8",
            )
            state = sample_visible_rollouts(root, JsonlSampleState(), now=NOW)
        sample = current_day_sample(state, NOW.timestamp())
        assert sample is not None
        estimate = estimate_standard_api_cost({identifier: item.as_model_payload() for identifier, item in sample.model_compositions.items()})
        self.assertEqual(estimate.priced_tokens, 120)
        self.assertEqual(estimate.unpriced_tokens, 40)
        self.assertEqual(estimate.models[0].model, "gpt-5.6-terra")
        self.assertAlmostEqual(estimate.models[0].cost_usd, 0.000368)
        self.assertAlmostEqual(estimate.total_cost_usd, 0.000368)

    def test_checkpoint_is_incremental_and_retains_aggregate_after_visible_file_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            rollout = root / "rollout.jsonl"
            rollout.write_text(record("event_msg", {"type": "user_message", "message": "private prompt must never persist"}) + context("gpt-5.6-sol") + usage(
                input_tokens=100, cached_input_tokens=80, output_tokens=10, reasoning_output_tokens=4, total_tokens=110,
            ), encoding="utf-8")
            checkpoint = Path(temporary) / "codex-session-events.json"
            state = sample_visible_rollouts(root, JsonlSampleState(), now=NOW)
            save_jsonl_sample_state(checkpoint, state)
            reloaded = load_jsonl_sample_state(checkpoint)
            repeated = sample_visible_rollouts(root, reloaded, now=NOW)
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(usage(input_tokens=50, cached_input_tokens=10, output_tokens=10, reasoning_output_tokens=0, total_tokens=60))
            updated = sample_visible_rollouts(root, repeated, now=NOW)
            rollout.unlink()
            retained = sample_visible_rollouts(root, updated, now=NOW)

            persisted = checkpoint.read_text(encoding="utf-8")

        sample = current_day_sample(retained, NOW.timestamp())
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.events, 2)
        self.assertEqual(sample.total_tokens, 170)
        self.assertNotIn("rollout.jsonl", persisted)
        self.assertNotIn("private prompt must never persist", persisted)

    def test_incomplete_or_malformed_records_are_not_counted_or_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            rollout = root / "rollout.jsonl"
            rollout.write_text(context("gpt-5.6-sol") + '{"type":"event_msg"', encoding="utf-8")
            initial = sample_visible_rollouts(root, JsonlSampleState(), now=NOW)
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(',"timestamp":"2026-08-21T12:00:00Z","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0,"total_tokens":2}}}}\n')
            completed = sample_visible_rollouts(root, initial, now=NOW)

        sample = current_day_sample(completed, NOW.timestamp())
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.total_tokens, 2)

    def test_note_exposes_estimate_boundary_without_claiming_billing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            (root / "rollout.jsonl").write_text(context("gpt-5.6-sol") + usage(
                input_tokens=100, cached_input_tokens=50, output_tokens=20, reasoning_output_tokens=5, total_tokens=120,
            ), encoding="utf-8")
            state = sample_visible_rollouts(root, JsonlSampleState(), now=NOW)

        sample = current_day_sample(state, NOW.timestamp())
        assert sample is not None
        note = composition_note(sample, partial=False)
        self.assertIn("visible rollout sample only", note["en"])
        self.assertIn("不会改写 SQLite 工作负载总量", note["zh"])
        self.assertIn("不是账单", note["zh"])


if __name__ == "__main__":
    unittest.main()
