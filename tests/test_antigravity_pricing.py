from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.resources.ai.antigravity_pricing import estimate_antigravity_text_api_cost, resolve_antigravity_reference
from infra_sentinel.resources.ai.pricing_catalog import bundled_pricing_catalog


class AntigravityPricingTests(unittest.TestCase):
    def test_known_rollout_markers_use_only_the_same_dated_base(self):
        for marker in ("-n", "-trial", "-exp-a"):
            with self.subTest(marker=marker):
                result = resolve_antigravity_reference("gemini-3.8-flash" + marker, usage_date="2026-10-16")
                self.assertEqual(result.model, "gemini-3.8-flash")
                self.assertEqual(result.match, "base-reference")
                self.assertTrue(result.suspected_rollout)
                self.assertEqual(result.marker, marker)
                self.assertEqual(result.price.input_per_million, .75)
        exact = resolve_antigravity_reference("gemini-3.7-flash-exp-b", usage_date="2026-10-16")
        self.assertEqual(exact.match, "exact")
        self.assertTrue(exact.suspected_rollout)

    def test_argon_trial_respects_release_and_scheduled_price_boundaries(self):
        before = resolve_antigravity_reference("gemini-4-argon-trial", usage_date="2026-10-12")
        self.assertIsNone(before.price)
        self.assertTrue(before.suspected_rollout)
        for day, price in (("2026-10-13", 2), ("2026-11-30", 2), ("2026-12-01", 4)):
            with self.subTest(day=day):
                result = resolve_antigravity_reference("gemini-4-argon-trial", usage_date=day)
                self.assertEqual(result.price.input_per_million, price)
                self.assertEqual(result.price.cached_input_per_million, price * .05)
        old_flash = resolve_antigravity_reference("gemini-3.8-flash-n", usage_date="2026-12-31")
        new_flash = resolve_antigravity_reference("gemini-3.8-flash-n", usage_date="2027-01-01")
        self.assertEqual(old_flash.price.input_per_million, .75)
        self.assertEqual(new_flash.price.input_per_million, 1.5)

    def test_exact_variant_price_wins_even_if_a_base_reference_exists(self):
        base = bundled_pricing_catalog().price_for("antigravity", "gemini-3.8-flash", "2026-10-16")
        variant = replace(base, input_per_million=9)
        calls = []
        class Catalog:
            def price_for(self, scope, model, usage_date=None):
                calls.append((scope, model, usage_date))
                return variant if model == "gemini-3.8-flash-n" else base
        result = resolve_antigravity_reference("gemini-3.8-flash-n", catalog=Catalog(), usage_date="2026-10-16")
        self.assertEqual(result.match, "exact")
        self.assertEqual(result.model, "gemini-3.8-flash-n")
        self.assertEqual(result.price.input_per_million, 9)
        self.assertEqual(calls, [("antigravity", "gemini-3.8-flash-n", "2026-10-16")])

    def test_unknown_bases_and_pricing_relevant_suffixes_are_not_stripped(self):
        unknown = resolve_antigravity_reference("gemini-99-argon-trial", usage_date="2026-10-16")
        self.assertTrue(unknown.suspected_rollout)
        self.assertEqual(unknown.match, "unpriced")
        self.assertEqual(unknown.model, "gemini-99-argon")
        for model in ("gemini-3.8-flash-turbo", "gemini-3.8-flash-tts", "gemini-3.8-flash-image",
                      "gemini-3.8-flash-lite", "gemini-3.8-flash-n-extra", "gemini-3.8-flash-preview",
                      "codex-auto-review", "claude-opus-5-5-turbo"):
            with self.subTest(model=model):
                result = resolve_antigravity_reference(model, usage_date="2026-10-16")
                self.assertEqual(result.model, model)
                self.assertIsNone(result.price)
                self.assertFalse(result.suspected_rollout)
        preview = resolve_antigravity_reference("gemini-3-flash-preview", usage_date="2026-10-16")
        self.assertEqual(preview.match, "exact")
        self.assertFalse(preview.suspected_rollout)

    def test_reasoning_marker_is_a_labeled_base_reference_without_rollout_claim(self):
        result = resolve_antigravity_reference("claude-opus-5-5-high", usage_date="2026-10-16")
        self.assertEqual(result.model, "claude-opus-5-5")
        self.assertEqual(result.match, "base-reference")
        self.assertFalse(result.suspected_rollout)
        self.assertEqual(result.price.input_per_million, 4)

    def test_estimate_preserves_raw_ids_and_counts_reasoning_cache_and_unpriced_usage(self):
        model = "gemini-4-argon-trial"
        totals = {model: {"input_tokens": 1_000_000, "cache_read_tokens": 1_000_000,
                          "output_tokens": 100_000, "reasoning_tokens": 100_000},
                  "gemini-99-argon-trial": {"input_tokens": 50}}
        estimate = estimate_antigravity_text_api_cost(totals, usage_date="2026-10-16")
        self.assertAlmostEqual(estimate.total_cost_usd, 4.1)
        self.assertEqual(estimate.priced_tokens, 2_200_000)
        self.assertEqual(estimate.base_reference_tokens, 2_200_000)
        self.assertEqual(estimate.unpriced_tokens, 50)
        self.assertEqual([row[0] for row in estimate.model_costs], [model])
        reference = dict(estimate.model_matches)[model].projection()
        self.assertEqual(reference["model"], "gemini-4-argon")
        self.assertEqual(reference["match"], "base-reference")
        self.assertTrue(reference["available"])
        self.assertEqual(set(totals), {model, "gemini-99-argon-trial"})


if __name__ == "__main__":
    unittest.main()
