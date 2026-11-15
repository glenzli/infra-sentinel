from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.cli.usage_receipts import submit_claude_code_result
from infra_sentinel.app.agent import apply_agent_commands
from infra_sentinel.core.collectors import CollectorContext
from infra_sentinel.metrics.usage_receipts import UsageReceiptStore, prepare_receipt
from infra_sentinel.resources.ai.claude_code_usage import claude_code_receipts
from infra_sentinel.resources.ai.usage_receipts import CliUsageReceiptCollector


def terminal() -> dict:
    return {
        "type": "result", "session_id": "fixture-fresh-session-0001", "uuid": "fixture-result-0001",
        "is_error": False, "result": "private answer must not enter receipts",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 30,
                  "cache_creation_input_tokens": 20, "output_tokens": 40,
                  "output_tokens_details": {"thinking_tokens": 25}},
        "modelUsage": {"claude-opus-5-5[1m]": {
            "inputTokens": 10, "cacheReadInputTokens": 30, "cacheCreationInputTokens": 20,
            "outputTokens": 40, "thinkingTokens": 25, "costUSD": 123.45,
            "canonicalModel": "a-provider-claim", "provider": "unverified-provider",
        }},
    }


class ClaudeCodeUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.completed = datetime.now(timezone.utc).isoformat()

    def convert(self, result: dict) -> list[dict]:
        return claude_code_receipts(result, completed_at=self.completed, fresh_session=True)

    def test_cache_legs_are_disjoint_and_thinking_is_already_in_output(self) -> None:
        result = terminal()
        original = deepcopy(result)
        receipts = self.convert(result)
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt["usage"], {"input_tokens": 60, "cached_input_tokens": 30,
            "cache_write_input_tokens": 20, "output_tokens": 40, "total_tokens": 100,
            "reasoning_output_tokens": 25})
        self.assertEqual((receipt["client"], receipt["coverage"]), ("claude-code", "uncollected"))
        self.assertEqual((receipt["model"], receipt["model_basis"]), ("claude-opus-5-5[1m]", "reported"))
        self.assertEqual(result, original)
        saved = json.dumps(receipt)
        for private in [result["session_id"], result["uuid"], result["result"], "costUSD", "a-provider-claim"]:
            self.assertNotIn(private, saved)

    def test_native_models_split_without_counting_the_aggregate_again(self) -> None:
        result = terminal()
        result["modelUsage"]["claude-sonnet-5-5[1m]"] = result["modelUsage"].pop("claude-opus-5-5[1m]")
        result["modelUsage"]["claude-opus-5-5[1m]"] = {"inputTokens": 2, "cacheReadInputTokens": 3,
            "cacheCreationInputTokens": 4, "outputTokens": 5}
        result["usage"].update(input_tokens=12, cache_read_input_tokens=33,
                               cache_creation_input_tokens=24, output_tokens=45)
        receipts = self.convert(result)
        self.assertEqual(sum(row["usage"]["total_tokens"] for row in receipts), 114)
        self.assertEqual(len({row["event_id"] for row in receipts}), 2)

    def test_session_model_identity_survives_delivery_retries_and_conflicts_fail(self) -> None:
        result = terminal()
        one = self.convert(result)[0]
        result["uuid"] = "another-copy-of-the-same-session-result"
        self.assertEqual(self.convert(result)[0], one)
        with tempfile.TemporaryDirectory() as directory:
            store = UsageReceiptStore(Path(directory))
            self.assertTrue(store.record(prepare_receipt(one))["recorded"])
            self.assertTrue(store.record(prepare_receipt(self.convert(result)[0]))["duplicate"])
            result["usage"]["output_tokens"] += 1
            result["modelUsage"][one["model"]]["outputTokens"] += 1
            with self.assertRaises(ValueError):
                store.record(prepare_receipt(self.convert(result)[0]))
            self.assertEqual(store.summary()["daily"][0]["total_tokens"], 100)

    def test_resumes_partial_counts_mismatches_and_invalid_numbers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            claude_code_receipts(terminal(), completed_at=self.completed, fresh_session=False)
        bad = []
        for field, value in [("type", "assistant"), ("session_id", "short"), ("modelUsage", {})]:
            value_result = terminal(); value_result[field] = value; bad.append(value_result)
        value_result = terminal(); del value_result["usage"]["cache_read_input_tokens"]; bad.append(value_result)
        value_result = terminal(); value_result["usage"]["output_tokens"] = 39; bad.append(value_result)
        for field, value in [("inputTokens", True), ("cacheReadInputTokens", -1), ("outputTokens", 1.2), ("thinkingTokens", 41)]:
            value_result = terminal(); value_result["modelUsage"]["claude-opus-5-5[1m]"][field] = value; bad.append(value_result)
        for result in bad:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.convert(result)

    def test_failed_terminal_usage_counts_but_zero_usage_does_not_create_a_row(self) -> None:
        result = terminal(); result["is_error"] = True
        self.assertEqual(self.convert(result)[0]["usage"]["total_tokens"], 100)
        result["usage"] = {key: 0 for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}
        with self.assertRaises(ValueError):
            self.convert(result)  # Do not conceal a conflicting positive per-model total.
        result["modelUsage"] = {}
        self.assertEqual(self.convert(result), [])

    def test_receiver_model_and_coverage_rules_and_projection_remain_explicit(self) -> None:
        raw = self.convert(terminal())[0]
        for client, coverage, model in [("codex", "uncollected", "gpt-6.1-sol"),
                                        ("agy", "isolated", "claude-opus-5-5[1m]"),
                                        ("claude-code", "uncollected", "<unsafe>"),
                                        ("claude-code", "uncollected", "opus[unknown]")]:
            bad = {**raw, "client": client, "coverage": coverage, "model": model}
            with self.assertRaises(ValueError):
                prepare_receipt(bad)
        class NoBorrowedPrices:
            def price_for(self, scope, *_args):
                if scope != "claude-code":
                    raise AssertionError("Claude Code must not borrow another client's price scope")
                return None
        with tempfile.TemporaryDirectory() as directory:
            store = UsageReceiptStore(Path(directory))
            store.record(prepare_receipt(raw))
            collector = CliUsageReceiptCollector(store.summary, pricing_catalog=NoBorrowedPrices())
            snapshot = collector.collect(CollectorContext({"epoch": datetime.now().timestamp()}, {})).snapshot
            self.assertEqual(snapshot["usage"]["cumulative"]["tokens"], 100)
            price = snapshot["pricing"]["daily"][0]["reference"]
            self.assertEqual((price["priced_tokens"], price["unpriced_tokens"]), (0, 100))

    def test_outer_runner_helper_sends_only_normalized_receipts(self) -> None:
        with patch("infra_sentinel.cli.usage_receipts.submit_receipt", return_value={"status": "queued"}) as submit:
            result = submit_claude_code_result(Path("fixture-state"), terminal(), completed_at=self.completed,
                                              fresh_session=True, wait_seconds=0)
        self.assertEqual(result, [{"status": "queued"}])
        sent = submit.call_args.args[1]
        self.assertEqual(sent["client"], "claude-code")
        self.assertEqual(sent["usage"]["total_tokens"], 100)
        self.assertNotIn("private answer", json.dumps(sent))

    def test_agent_accepts_claude_receipts_and_replays_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for duplicate in (False, True):
                queued = submit_claude_code_result(root, terminal(), completed_at=self.completed,
                                                   fresh_session=True, wait_seconds=0)[0]
                command = root / "commands" / f"{queued['command_id']}.request.json"
                saved = command.read_text()
                self.assertNotIn(terminal()["session_id"], saved)
                self.assertNotIn(terminal()["result"], saved)
                apply_agent_commands(SimpleNamespace(state_dir=root), root / "config.toml",
                    datetime.now().timestamp(), None, None, logging.getLogger("claude-receipt-tests"))
                reply = json.loads(command.with_name(f"{queued['command_id']}.result.json").read_text())
                self.assertEqual(reply["status"], "ok")
                self.assertEqual(reply["payload"]["recorded"], not duplicate)
                self.assertEqual(reply["payload"]["duplicate"], duplicate)
            summary = UsageReceiptStore(root).summary()
            self.assertEqual(summary["daily"][0]["total_tokens"], 100)
            self.assertEqual(summary["daily"][0]["receipts"], 1)


if __name__ == "__main__":
    unittest.main()
