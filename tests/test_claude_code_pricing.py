from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.core.collectors import CollectorContext
from infra_sentinel.metrics.usage_receipts import UsageReceiptStore, prepare_receipt
from infra_sentinel.resources.ai.claude_code_pricing import estimate_claude_code_text_api_cost
from infra_sentinel.resources.ai.pricing_catalog import bundled_pricing_catalog
from infra_sentinel.resources.ai.usage_receipts import CliUsageReceiptCollector


COUNTS = {"input_tokens": 60, "cached_input_tokens": 30, "cache_write_input_tokens": 20,
          "output_tokens": 40, "reasoning_output_tokens": 25, "total_tokens": 100}


class ClaudeCodePricingTests(unittest.TestCase):
    def test_context_reference_prices_known_legs_without_assuming_write_ttl(self):
        result = estimate_claude_code_text_api_cost("claude-opus-5-5[1m]", COUNTS, usage_date="2026-11-10")
        self.assertAlmostEqual(result.total_cost_usd, 846 / 1_000_000)
        self.assertEqual((result.priced_tokens, result.unpriced_tokens), (80, 20))
        self.assertEqual(result.reference["model"], "claude-opus-5-5")
        self.assertEqual(result.reference["marker_kind"], "context")
        sonnet = estimate_claude_code_text_api_cost("claude-sonnet-5-5[1m]", COUNTS, usage_date="2026-11-10")
        self.assertAlmostEqual(sonnet.total_cost_usd, 423 / 1_000_000)
        kimi = estimate_claude_code_text_api_cost("claude-kimi-k3.1-pro[1m]", COUNTS, usage_date="2026-11-10")
        self.assertAlmostEqual(kimi.total_cost_usd, 426 / 1_000_000)

    def test_missing_breakdowns_do_not_create_zero_cost_inputs(self):
        for bad in [{**COUNTS, "cached_input_tokens": None}, {**COUNTS, "cache_write_input_tokens": None},
                    {**COUNTS, "input_tokens": 49}, {**COUNTS, "cached_input_tokens": True}]:
            result = estimate_claude_code_text_api_cost("claude-opus-5-5[1m]", bad, usage_date="2026-11-10")
            self.assertEqual((result.total_cost_usd, result.priced_tokens, result.unpriced_tokens), (0, 0, 100))
        no_writes = {**COUNTS, "cache_write_input_tokens": 0}
        result = estimate_claude_code_text_api_cost("claude-opus-5-5[1m]", no_writes, usage_date="2026-11-10")
        self.assertEqual((result.priced_tokens, result.unpriced_tokens), (100, 0))

    def test_glm_current_rates_do_not_backdate_unverified_history(self):
        old = estimate_claude_code_text_api_cost("claude-glm-5.3[1m]", COUNTS, usage_date="2026-11-10")
        self.assertEqual((old.priced_tokens, old.unpriced_tokens), (0, 100))
        current = estimate_claude_code_text_api_cost("claude-glm-5.3[1m]", COUNTS, usage_date="2026-11-15")
        self.assertEqual((current.priced_tokens, current.unpriced_tokens), (100, 0))
        self.assertAlmostEqual(current.total_cost_usd, 225.8 / 1_000_000)

    def test_dedicated_scope_exact_variant_wins_and_unknown_variants_stay_unpriced(self):
        price = bundled_pricing_catalog().price_for("claude-code", "claude-opus-5-5", "2026-11-10")
        class ExactCatalog:
            def price_for(self, scope, model, _day):
                self_scope = scope
                if self_scope != "claude-code":
                    raise AssertionError("another client scope was borrowed")
                return replace(price, input_per_million=9) if model == "claude-opus-5-5-xhigh" else price
        exact = estimate_claude_code_text_api_cost("claude-opus-5-5-xhigh[1m]", COUNTS,
                                                  catalog=ExactCatalog(), usage_date="2026-11-10")
        self.assertEqual(exact.reference["model"], "claude-opus-5-5-xhigh")
        self.assertAlmostEqual(exact.total_cost_usd, 896 / 1_000_000)
        for model in ("claude-opus-5-5[2m]", "claude-kimi-k3.1-pro-highspeed", "claude-opus-5-5-preview", "unknown-high"):
            result = estimate_claude_code_text_api_cost(model, COUNTS, usage_date="2026-11-10")
            self.assertEqual((result.priced_tokens, result.unpriced_tokens), (0, 100))

    def test_launch_boundaries_and_persisted_receipt_projection(self):
        for model, before, first in [("claude-opus-5-5", "2026-09-21", "2026-09-22"),
                                     ("claude-sonnet-5-5", "2026-09-27", "2026-09-28"),
                                     ("claude-kimi-k3.1-pro", "2026-10-22", "2026-10-23")]:
            self.assertEqual(estimate_claude_code_text_api_cost(model, COUNTS, usage_date=before).unpriced_tokens, 100)
            self.assertEqual(estimate_claude_code_text_api_cost(model, COUNTS, usage_date=first).priced_tokens, 80)
        with tempfile.TemporaryDirectory() as directory:
            store = UsageReceiptStore(Path(directory))
            store.record(prepare_receipt({"schema": "infra-sentinel.cli-usage.v1", "event_id": "claude-price-fixture",
                "client": "claude-code", "model": "claude-opus-5-5[1m]", "model_basis": "reported",
                "coverage": "uncollected", "completed_at": "2026-11-10T01:00:00+00:00", "usage": COUNTS}))
            snapshot = CliUsageReceiptCollector(store.summary).collect(
                CollectorContext({"epoch": datetime.now().timestamp()}, {})).snapshot
            self.assertEqual(snapshot["usage"]["cumulative"]["tokens"], 100)
            reference = snapshot["pricing"]["daily"][0]["reference"]
            self.assertEqual((reference["priced_tokens"], reference["unpriced_tokens"]), (80, 20))
            self.assertAlmostEqual(reference["cost_usd"], 846 / 1_000_000)


if __name__ == "__main__":
    unittest.main()
