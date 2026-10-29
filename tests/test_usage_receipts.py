from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.app.agent import apply_agent_commands, main as agent_main
from infra_sentinel.cli.usage_receipts import main as cli_main, submit_receipt
from infra_sentinel.core.collectors import CollectorContext, CollectorRegistry
from infra_sentinel.core.projection import build_infra_projection
from infra_sentinel.metrics.usage_receipts import (
    RECEIPT_FILENAME, RECEIPT_SCHEMA, UsageReceiptStore, prepare_receipt, validate_receipt,
)
from infra_sentinel.resources.ai.usage_receipts import CliUsageReceiptCollector
from infra_sentinel.resources.ai.pricing_catalog import bundled_pricing_catalog


def receipt(*, event_id: str = "fixture-attempt-0001", client: str = "codex", model: str = "gpt-6.1-terra", completed_at: str | None = None) -> dict:
    return {
        "schema": RECEIPT_SCHEMA, "event_id": event_id, "client": client,
        "model": model, "model_basis": "requested", "coverage": "isolated",
        "completed_at": completed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "usage": {"input_tokens": 9629, "output_tokens": 24415, "total_tokens": 34044,
                  "cached_input_tokens": 0, "cache_write_input_tokens": 0, "reasoning_output_tokens": 21606},
    }


def apply(state_dir: Path) -> None:
    apply_agent_commands(
        SimpleNamespace(state_dir=state_dir), state_dir / "config.toml",
        datetime.now().timestamp(), None, None, logging.getLogger("receipt-tests"),
    )


class UsageReceiptTests(unittest.TestCase):
    def test_receipt_effort_pricing_preserves_raw_model_and_includes_reasoning_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = UsageReceiptStore(Path(temporary))
            raw = receipt(client="agy", model="gemini-3.8-flash-high")
            raw["usage"] = {"input_tokens": 68372, "output_tokens": 74488, "total_tokens": 142860,
                            "reasoning_output_tokens": 68896, "cached_input_tokens": 0}
            store.record(prepare_receipt(raw))
            now = datetime.now().timestamp()
            result = CliUsageReceiptCollector(store.summary).collect(CollectorContext({"epoch": now}, {})).snapshot
            reference = result["models"][0]["price_reference"]
            self.assertEqual(result["models"][0]["id"], "gemini-3.8-flash-high")
            self.assertEqual(reference["model"], "gemini-3.8-flash")
            self.assertEqual(reference["marker_kind"], "effort")
            self.assertEqual(reference["marker"], "-high")
            priced = result["pricing"]["daily"][0]["reference"]
            self.assertEqual(priced["priced_tokens"], 142860)
            self.assertEqual(priced["unpriced_tokens"], 0)
            self.assertAlmostEqual(priced["cost_usd"], .330609)
            self.assertEqual(priced["models"][0]["id"], "gemini-3.8-flash-high")
            self.assertEqual(result["usage"]["today"]["tokens"], 142860)

    def test_partial_missing_cache_and_unknown_suffixes_remain_unpriced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = UsageReceiptStore(Path(temporary))
            for event, model, cached in [("known-cache-0001", "gemini-3.8-flash-high", 40),
                                          ("unknown-cache-0002", "gemini-3.8-flash-high", None),
                                          ("unknown-suffix-0003", "gemini-3.8-flash-turbo", 0)]:
                raw = receipt(event_id=event, client="agy", model=model)
                raw["usage"] = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
                if cached is not None:
                    raw["usage"]["cached_input_tokens"] = cached
                store.record(prepare_receipt(raw))
            summary = store.summary()
            self.assertEqual(len(summary["pricing"]), 3)
            result = CliUsageReceiptCollector(store.summary).collect(CollectorContext({"epoch": datetime.now().timestamp()}, {})).snapshot
            priced = result["pricing"]["daily"][0]["reference"]
            self.assertEqual(priced["priced_tokens"], 120)
            self.assertEqual(priced["unpriced_tokens"], 240)
            self.assertAlmostEqual(priced["cost_usd"], (60 * .75 + 40 * .075 + 20 * 3.75) / 1_000_000)
            self.assertEqual(result["usage"]["today"]["tokens"], 360)

    def test_dated_prices_and_codex_cache_subsets_use_shared_estimators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = UsageReceiptStore(Path(temporary))
            base = bundled_pricing_catalog().price_for("codex", "gpt-6.1-terra", "2026-10-29")
            calls = []
            class Catalog:
                def price_for(self, scope, model, usage_date=None):
                    calls.append((scope, model, usage_date))
                    return replace(base, input_per_million=1 if usage_date < "2026-10-20" else 2,
                                   cached_input_per_million=.1, cache_write_per_million=3, output_per_million=4)
            for day in ("2026-10-15", "2026-10-25"):
                completed = datetime.fromisoformat(f"{day}T12:00:00").astimezone().isoformat()
                raw = receipt(event_id=f"dated-cache-{day}", completed_at=completed)
                raw["usage"] = {"input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 20,
                                "output_tokens": 20, "reasoning_output_tokens": 15, "total_tokens": 120}
                store.record(prepare_receipt(raw))
            completed = datetime(2026, 10, 25, 12, 1).astimezone().isoformat()
            missing = receipt(event_id="missing-write-0003", completed_at=completed)
            missing["usage"] = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20, "total_tokens": 120}
            store.record(prepare_receipt(missing))
            result = CliUsageReceiptCollector(store.summary, pricing_catalog=Catalog()).collect(CollectorContext({"epoch": datetime.now().timestamp()}, {})).snapshot
            days = {row["date"]: row["reference"] for row in result["pricing"]["daily"]}
            self.assertAlmostEqual(days["2026-10-15"]["cost_usd"], (40 + 4 + 60 + 80) / 1_000_000)
            self.assertAlmostEqual(days["2026-10-25"]["cost_usd"], (80 + 4 + 60 + 80) / 1_000_000)
            self.assertEqual(days["2026-10-25"]["unpriced_tokens"], 120)
            self.assertIn(("codex", "gpt-6.1-terra", "2026-10-15"), calls)

    def test_strict_numeric_allowlist_and_subset_semantics(self) -> None:
        good = prepare_receipt(receipt())
        self.assertEqual(good["usage"]["total_tokens"], 34044)
        self.assertNotIn("event_id", good)
        cases = []
        for key, value in [("prompt", "private"), ("argv", ["private"]), ("duration_seconds", 10), ("coverage", "persistent")]:
            bad = deepcopy(good)
            bad[key] = value
            cases.append(bad)
        for key, value in [("input_tokens", True), ("output_tokens", -1), ("total_tokens", 55650), ("reasoning_output_tokens", 24416), ("cached_input_tokens", 9630), ("text", "private"), ("input_tokens", 1.2)]:
            bad = deepcopy(good)
            bad["usage"][key] = value
            cases.append(bad)
        for timestamp in ["2026-10-29T12:00:00", "nonsense", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()]:
            bad = deepcopy(good)
            bad["completed_at"] = timestamp
            cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_receipt(bad)

    def test_agent_ledger_is_idempotent_after_restart_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = receipt()
            safe = prepare_receipt(raw)
            first = UsageReceiptStore(root).record(safe)
            restarted = UsageReceiptStore(root)
            self.assertTrue(first["recorded"])
            self.assertTrue(restarted.record(safe)["duplicate"])
            conflict = deepcopy(safe)
            conflict["usage"]["input_tokens"] += 1
            conflict["usage"]["total_tokens"] += 1
            with self.assertRaises(ValueError):
                restarted.record(conflict)
            self.assertEqual(restarted.summary()["daily"][0]["total_tokens"], 34044)
            with sqlite3.connect(root / RECEIPT_FILENAME) as connection:
                rows = connection.execute("SELECT canonical_json FROM usage_receipts").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotIn(raw["event_id"], rows[0][0])
            self.assertEqual(json.loads(rows[0][0])["usage"]["reasoning_output_tokens"], 21606)
            if sys.platform != "win32":
                self.assertEqual((root / RECEIPT_FILENAME).stat().st_mode & 0o777, 0o600)

    def test_absent_optional_counts_stay_absent_and_reads_do_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "not-created"
            store = UsageReceiptStore(root)
            self.assertEqual(store.summary(), {"daily": [], "hourly": []})
            self.assertFalse(root.exists())
            raw = receipt(client="agy", model="gemini-3.8-flash-high")
            raw["usage"] = {"input_tokens": 68372, "output_tokens": 74488, "total_tokens": 142860}
            store.record(prepare_receipt(raw))
            first = store.summary()
            with patch("infra_sentinel.metrics.usage_receipts.sqlite3.connect") as connect:
                self.assertIs(store.summary(), first)
                connect.assert_not_called()
            with sqlite3.connect(store.path) as connection:
                stored = json.loads(connection.execute("SELECT canonical_json FROM usage_receipts").fetchone()[0])
            self.assertEqual(stored["usage"], raw["usage"])

    def test_completion_local_day_hour_and_offsets_are_not_import_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = (datetime.now().astimezone() - timedelta(days=2)).replace(hour=13, minute=27, second=0, microsecond=0)
            store = UsageReceiptStore(Path(temporary))
            safe = prepare_receipt(receipt(completed_at=completed.isoformat()))
            store.record(safe)
            same_instant = {**safe, "completed_at": completed.astimezone(timezone(timedelta(hours=-7))).isoformat()}
            self.assertTrue(store.record(same_instant)["duplicate"])
            summary = store.summary()
            self.assertEqual(summary["daily"][0]["date"], completed.date().isoformat())
            self.assertEqual(summary["hourly"], [])

    def test_command_queue_and_agent_replay_persist_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = receipt()
            first = submit_receipt(root, raw, wait_seconds=0)
            command = root / "commands" / f"{first['command_id']}.request.json"
            self.assertEqual(first["status"], "queued")
            self.assertNotIn(raw["event_id"], command.read_text())
            if sys.platform != "win32":
                self.assertEqual(command.stat().st_mode & 0o777, 0o600)
            apply(root)
            result = json.loads(command.with_name(f"{first['command_id']}.result.json").read_text())
            self.assertTrue(result["payload"]["recorded"])
            second = submit_receipt(root, raw, wait_seconds=0)
            # Simulate a crash after claiming a request, before acknowledging it.
            queued = root / "commands" / f"{second['command_id']}.request.json"
            queued.rename(queued.with_name(f"{second['command_id']}.processing.json"))
            apply(root)
            reply = json.loads(queued.with_name(f"{second['command_id']}.result.json").read_text())
            self.assertTrue(reply["payload"]["duplicate"])
            self.assertEqual(UsageReceiptStore(root).summary()["daily"][0]["receipts"], 1)

    def test_agent_revalidates_direct_commands_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = submit_receipt(root, receipt(), wait_seconds=0)
            command = root / "commands" / f"{queued['command_id']}.request.json"
            raw = json.loads(command.read_text())
            raw["payload"]["response"] = "must not enter the ledger"
            command.write_text(json.dumps(raw))
            apply(root)
            reply = json.loads(command.with_name(f"{queued['command_id']}.result.json").read_text())
            self.assertEqual(reply["status"], "rejected")
            self.assertFalse((root / RECEIPT_FILENAME).exists())
            self.assertNotIn("must not enter", json.dumps(reply))

    def test_receipts_project_into_unified_totals_without_performance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = UsageReceiptStore(Path(temporary))
            store.record(prepare_receipt(receipt()))
            flash = receipt(event_id="fixture-attempt-0002", client="agy", model="gemini-3.8-flash-high")
            flash["usage"] = {"input_tokens": 68372, "output_tokens": 74488, "total_tokens": 142860}
            store.record(prepare_receipt(flash))
            registry = CollectorRegistry([CliUsageReceiptCollector(store.summary)])
            now = datetime.now().astimezone()
            sample = {"epoch": now.timestamp(), "timestamp": now.isoformat()}
            runs = registry.collect(CollectorContext(sample, {}))
            result = build_infra_projection(sample, {"kernel": {}, "vps": {}}, {}, "none", runs)
            self.assertEqual(result["ai_usage"]["aggregate"]["today"]["tokens"], 176904)
            snapshot = result["ai_usage"]["sources"][0]
            self.assertEqual({row["id"] for row in snapshot["models"]}, {"gpt-6.1-terra", "gemini-3.8-flash-high"})
            self.assertEqual(sum(row["tokens"] for row in snapshot["history"]["hourly"]), 176904)
            self.assertNotIn("event_hash", json.dumps(result))
            self.assertFalse(any("performance" in group["id"] for group in snapshot["details"]))
            self.assertEqual(registry.collect(CollectorContext(sample, {}))[0].points, ())

    def test_cli_rejects_raw_output_without_retaining_or_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "bad.json"
            raw = receipt()
            raw["response"] = "PRIVATE_TEXT_DO_NOT_ECHO"
            path.write_text(json.dumps(raw))
            output = StringIO()
            with patch("sys.stdout", output):
                code = cli_main(["--state-dir", str(root / "state"), "--receipt", str(path)])
            self.assertEqual(code, 1)
            self.assertNotIn("PRIVATE_TEXT", output.getvalue())
            self.assertFalse((root / "state").exists())
            path.write_bytes(b"x" * 4097)
            with patch("sys.stdout", StringIO()):
                self.assertEqual(cli_main(["--state-dir", str(root / "state"), "--receipt", str(path)]), 1)

    def test_packaged_agent_dispatch_reaches_same_receipt_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = receipt()
            root = Path(temporary)
            with patch.object(sys, "argv", ["infra-agent", "--record-cli-usage", "-"]), \
                 patch("infra_sentinel.cli.usage_receipts.desktop_state_directory", return_value=root), \
                 patch("infra_sentinel.cli.usage_receipts.time.monotonic", side_effect=[0, 20]), \
                 patch("sys.stdin", SimpleNamespace(buffer=BytesIO(json.dumps(raw).encode()))), \
                 patch("sys.stdout", StringIO()):
                self.assertEqual(agent_main(), 0)
            requests = list((root / "commands").glob("*.request.json"))
            self.assertEqual(len(requests), 1)
            self.assertEqual(json.loads(requests[0].read_text())["type"], "ai.usage.receipt.record")


if __name__ == "__main__":
    unittest.main()
