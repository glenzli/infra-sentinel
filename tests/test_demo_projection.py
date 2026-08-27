from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scripts.write_demo_projection import projection  # noqa: E402


class DemoProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.observed = datetime(
            2026, 8, 27, 20, 30,
            tzinfo=timezone(timedelta(hours=8)),
        )
        self.projection = projection(self.observed)

    def test_ai_fixture_matches_current_hourly_contract(self) -> None:
        ai = self.projection["infra"]["ai_usage"]
        sources = ai["sources"]

        self.assertEqual(
            [source["source_id"] for source in sources],
            ["opencode", "codex", "antigravity", "infer-runtime"],
        )
        self.assertEqual(self.projection["infra"]["resources"][1]["source_count"], 4)
        for source in sources:
            history = source["history"]
            self.assertTrue(history["hourly_available"])
            self.assertEqual(
                sum(row["tokens"] for row in history["hourly"]),
                source["usage"]["today"]["tokens"],
            )
            self.assertTrue(all(
                datetime.fromtimestamp(row["epoch"], tz=self.observed.tzinfo).date()
                == self.observed.date()
                for row in history["hourly"]
            ))

    def test_codex_fixture_uses_current_price_reference(self) -> None:
        sources = self.projection["infra"]["ai_usage"]["sources"]
        codex = next(source for source in sources if source["source_id"] == "codex")
        reference = codex["pricing"]["daily"][0]["reference"]

        self.assertEqual(reference["kind"], "local-rollout-standard-api-projection")
        self.assertGreater(reference["cost_usd"], 0)
        self.assertEqual(
            reference["priced_tokens"],
            codex["usage"]["today"]["tokens"],
        )

    def test_fixture_contains_no_machine_specific_paths(self) -> None:
        encoded = json.dumps(self.projection, ensure_ascii=False)

        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("Library/Application Support", encoded)
        self.assertNotIn("g4i", encoded)


if __name__ == "__main__":
    unittest.main()
