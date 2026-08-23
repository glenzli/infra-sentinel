from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import ANY


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import CollectorContext  # noqa: E402
from infra_sentinel.resources.ai.infer_runtime import InferRuntimeUsageCollector  # noqa: E402


EPOCH = datetime(2026, 8, 13, 12, 0).astimezone().timestamp()


def facilities(*models: dict[str, object], version: str = "20260813.3") -> dict[str, object]:
    return {
        "items": [{
            "id": "infer-runtime:local",
            "kind": "infer-runtime",
            "status": "healthy",
            "snapshot": {"extensions": {"infer-runtime": {"usage_daily": {
                "schema": "infer-runtime.usage.daily",
                "schema_version": version,
                "calendar": "host_local",
                "days": [{"date": "2026-08-13", "models": list(models)}],
            }}}},
        }],
    }


def model(
    identifier: str,
    origin: str,
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost_usd: float,
) -> dict[str, object]:
    return {
        "id": identifier,
        "execution_origin": origin,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }


class InferRuntimeUsageTests(unittest.TestCase):
    def test_both_origins_are_persisted_and_each_poll_replaces_the_current_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "infer-runtime-usage-daily.json"
            collector = InferRuntimeUsageCollector(checkpoint_path=checkpoint, clock=lambda: EPOCH)
            first = collector.collect(CollectorContext({"epoch": EPOCH}, {}, facilities(
                model("gpt-5.6-sol", "codex", input_tokens=90, output_tokens=9, total_tokens=99, cost_usd=1.0),
                model("deepseek-v4-flash", "other", input_tokens=17, output_tokens=3, total_tokens=20, cost_usd=0.02),
                model("luna", "other", input_tokens=9, output_tokens=1, total_tokens=10, cost_usd=0.01),
            )))

            snapshot = first.snapshot
            self.assertEqual(first.status, "ok")
            self.assertEqual(snapshot["usage"]["today"]["tokens"], 129)  # type: ignore[index]
            self.assertEqual(snapshot["usage"]["cumulative"]["tokens"], 129)  # type: ignore[index]
            self.assertEqual({item["id"] for item in snapshot["models"]}, {"gpt-5.6-sol", "deepseek-v4-flash", "luna"})  # type: ignore[index]
            stored = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(set(stored["days"]["2026-08-13"]), {"codex:gpt-5.6-sol", "other:deepseek-v4-flash", "other:luna"})

            repeated = collector.collect(CollectorContext({"epoch": EPOCH + 15}, {}, facilities(
                model("gpt-5.6-sol", "codex", input_tokens=90, output_tokens=9, total_tokens=99, cost_usd=1.0),
                model("deepseek-v4-flash", "other", input_tokens=17, output_tokens=3, total_tokens=20, cost_usd=0.02),
                model("luna", "other", input_tokens=9, output_tokens=1, total_tokens=10, cost_usd=0.01),
            )))
            self.assertEqual(repeated.snapshot["usage"]["today"]["tokens"], 129)  # type: ignore[index]

            replacement = collector.collect(CollectorContext({"epoch": EPOCH + 30}, {}, facilities(
                model("gpt-5.6-sol", "codex", input_tokens=100, output_tokens=10, total_tokens=110, cost_usd=1.1),
                model("deepseek-v4-flash", "other", input_tokens=21, output_tokens=4, total_tokens=25, cost_usd=0.025),
            )))
            self.assertEqual(replacement.snapshot["usage"]["today"]["tokens"], 135)  # type: ignore[index]
            self.assertEqual(replacement.snapshot["usage"]["cumulative"]["tokens"], 135)  # type: ignore[index]
            self.assertEqual(
                replacement.snapshot["history"]["daily"],  # type: ignore[index]
                [{"date": "2026-08-13", "tokens": 135, "models": [
                    {"id": "deepseek-v4-flash", "tokens": 25}, {"id": "gpt-5.6-sol", "tokens": 110},
                ]}],
            )
            reference = replacement.snapshot["pricing"]["daily"]  # type: ignore[index]
            self.assertEqual(reference[0]["reference"]["kind"], "runtime-origin-aware-price-reference")
            self.assertEqual(reference[0]["reference"]["priced_tokens"], 135)
            self.assertAlmostEqual(reference[0]["reference"]["cost_usd"], 0.025 + (100 * 4 + 10 * 20) / 1_000_000)

    def test_same_model_from_two_origins_is_upserted_separately_and_displayed_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "infer-runtime-usage-daily.json"
            collector = InferRuntimeUsageCollector(checkpoint_path=checkpoint, clock=lambda: EPOCH)
            result = collector.collect(CollectorContext({"epoch": EPOCH}, {}, facilities(
                model("gpt-5.6-luna", "codex", input_tokens=4, output_tokens=1, total_tokens=5, cost_usd=0),
                model("gpt-5.6-luna", "other", input_tokens=2, output_tokens=1, total_tokens=3, cost_usd=0.01),
            )))
            self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 8)  # type: ignore[index]
            self.assertEqual(result.snapshot["models"], [  # type: ignore[index]
                {"id": "gpt-5.6-luna", "today": ANY, "cumulative": ANY},
            ])
            self.assertEqual(result.snapshot["history"]["daily"], [  # type: ignore[index]
                {"date": "2026-08-13", "tokens": 8, "models": [{"id": "gpt-5.6-luna", "tokens": 8}]},
            ])
            stored = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(set(stored["days"]["2026-08-13"]), {"codex:gpt-5.6-luna", "other:gpt-5.6-luna"})
            reference = result.snapshot["pricing"]["daily"][0]["reference"]  # type: ignore[index]
            self.assertEqual(reference["priced_tokens"], 8)
            self.assertAlmostEqual(reference["cost_usd"], 0.01 + (4 * 0.20 + 1 * 1.20) / 1_000_000)

    def test_other_zero_cost_is_not_mistaken_for_a_provider_price(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = InferRuntimeUsageCollector(
                checkpoint_path=Path(temporary) / "infer-runtime-usage-daily.json",
                clock=lambda: EPOCH,
            )
            result = collector.collect(CollectorContext({"epoch": EPOCH}, {}, facilities(
                model("opaque-provider-model", "other", input_tokens=4, output_tokens=2, total_tokens=6, cost_usd=0),
            )))
            self.assertFalse(result.snapshot["pricing"]["daily_available"])  # type: ignore[index]
            self.assertEqual(result.snapshot["pricing"]["daily"], [])  # type: ignore[index]

    def test_missing_or_old_origin_contract_is_not_projected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = InferRuntimeUsageCollector(
                checkpoint_path=Path(temporary) / "infer-runtime-usage-daily.json",
                clock=lambda: EPOCH,
            )
            for version in ("20260813.1", "20260813.2"):
                unsupported = collector.collect(CollectorContext({"epoch": EPOCH}, {}, facilities(
                    model("deepseek-v4-flash", "other", input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0.01),
                    version=version,
                )))
                self.assertEqual(unsupported.status, "unavailable")
                self.assertFalse(unsupported.snapshot["available"])  # type: ignore[index]

            originless = {
                "id": "gpt-5.6-sol", "input_tokens": 1, "output_tokens": 1,
                "total_tokens": 2, "cost_usd": 0.01,
            }
            safe = collector.collect(CollectorContext({"epoch": EPOCH + 15}, {}, facilities(originless)))
            self.assertEqual(safe.status, "ok")
            self.assertEqual(safe.snapshot["usage"]["today"]["tokens"], 0)  # type: ignore[index]
            self.assertEqual(safe.snapshot["models"], [])  # type: ignore[index]

    def test_history_is_local_and_survives_a_new_collector_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "infer-runtime-usage-daily.json"
            first = InferRuntimeUsageCollector(checkpoint_path=checkpoint, clock=lambda: EPOCH)
            first.collect(CollectorContext({"epoch": EPOCH}, {}, facilities(
                model("luna", "other", input_tokens=4, output_tokens=2, total_tokens=6, cost_usd=0.03),
            )))
            restarted = InferRuntimeUsageCollector(checkpoint_path=checkpoint, clock=lambda: EPOCH)
            result = restarted.collect(CollectorContext({"epoch": EPOCH + 15}, {}, facilities(
                model("luna", "other", input_tokens=4, output_tokens=2, total_tokens=6, cost_usd=0.03),
            )))
            self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 6)  # type: ignore[index]
            groups = result.snapshot["details"]  # type: ignore[index]
            self.assertEqual(groups[0]["metrics"][2]["unit"], "usd")
            self.assertEqual(groups[1]["metrics"][0]["value"], 0.03)

    def test_current_snapshot_removes_legacy_zero_token_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "infer-runtime-usage-daily.json"
            checkpoint.write_text(json.dumps({
                "schema": "20260813.1",
                "days": {
                    "2026-08-13": {
                        "other:luna": {
                            "id": "luna", "execution_origin": "other",
                            "input_tokens": 4, "output_tokens": 2, "total_tokens": 6, "cost_usd": 0.03,
                        },
                        "other:sha256:legacy-build": {
                            "id": "sha256:legacy-build", "execution_origin": "other",
                            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0,
                        },
                    },
                },
            }), encoding="utf-8")
            collector = InferRuntimeUsageCollector(checkpoint_path=checkpoint, clock=lambda: EPOCH)
            result = collector.collect(CollectorContext({"epoch": EPOCH}, {}, facilities(
                model("luna", "other", input_tokens=5, output_tokens=2, total_tokens=7, cost_usd=0.035),
            )))
            self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 7)  # type: ignore[index]
            self.assertEqual([item["id"] for item in result.snapshot["models"]], ["luna"])  # type: ignore[index]
            stored = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(set(stored["days"]["2026-08-13"]), {"other:luna"})

    def test_non_current_runtime_day_is_rejected_instead_of_rewriting_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = InferRuntimeUsageCollector(
                checkpoint_path=Path(temporary) / "infer-runtime-usage-daily.json",
                clock=lambda: EPOCH,
            )
            bad = facilities(model("luna", "other", input_tokens=4, output_tokens=2, total_tokens=6, cost_usd=0.03))
            bad["items"][0]["snapshot"]["extensions"]["infer-runtime"]["usage_daily"]["days"][0]["date"] = "2026-08-12"  # type: ignore[index]
            with self.assertRaisesRegex(ValueError, "current host-local day"):
                collector.collect(CollectorContext({"epoch": EPOCH}, {}, bad))
