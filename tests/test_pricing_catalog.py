from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.ai.antigravity_pricing import estimate_antigravity_text_api_cost  # noqa: E402
from infra_sentinel.resources.ai.codex_pricing import estimate_standard_api_cost  # noqa: E402
from infra_sentinel.resources.ai.pricing_catalog import (  # noqa: E402
    LATEST_CATALOG_URL,
    LATEST_MANIFEST_URL,
    PricingCatalog,
    PricingCatalogManager,
    _fetch,
    bundled_pricing_catalog,
)


def manifest(raw: bytes, version: str) -> bytes:
    return json.dumps({
        "schema": "api-price-manifest/v1",
        "catalog_schema": "api-price/v1",
        "catalog_version": version,
        "asset": "api-price.toml",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }).encode()


class PricingCatalogTests(unittest.TestCase):
    def test_fetch_survives_packaged_ca_file_disappearing_after_startup(self) -> None:
        class Response(BytesIO):
            headers = {"Content-Length": "3"}

        with (
            patch("infra_sentinel.resources.ai.pricing_catalog.certifi.where", side_effect=FileNotFoundError),
            patch("infra_sentinel.resources.ai.pricing_catalog.urlopen", return_value=Response(b"abc")) as opener,
        ):
            self.assertEqual(_fetch(LATEST_MANIFEST_URL, 10), b"abc")

        self.assertIsNotNone(opener.call_args.kwargs["context"])

    def test_bundled_catalog_prices_new_exact_models_and_future_period(self) -> None:
        catalog = bundled_pricing_catalog()
        astra = estimate_standard_api_cost({
            "gpt-6-astra": {
                "input_tokens": 1_000_000,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
        }, catalog=catalog, usage_date="2026-09-06")
        current_flash = estimate_antigravity_text_api_cost({
            "gemini-3.8-flash": {"input_tokens": 1_000_000},
        }, catalog=catalog, usage_date="2026-12-31")
        future_flash = estimate_antigravity_text_api_cost({
            "gemini-3.8-flash": {"input_tokens": 1_000_000},
        }, catalog=catalog, usage_date="2027-01-01")

        self.assertEqual(astra.total_cost_usd, 60.0)
        self.assertEqual(astra.unpriced_tokens, 0)
        self.assertEqual(current_flash.total_cost_usd, 0.75)
        self.assertEqual(future_flash.total_cost_usd, 1.5)

    def test_effective_dates_do_not_reprice_usage_before_a_model_was_checked(self) -> None:
        estimate = estimate_standard_api_cost({
            "gpt-6-astra": {"input_tokens": 100, "total_tokens": 100},
        }, usage_date="2026-09-05")
        self.assertEqual(estimate.priced_tokens, 0)
        self.assertEqual(estimate.unpriced_tokens, 100)

    def test_catalog_rejects_overlapping_periods(self) -> None:
        raw = bundled_pricing_catalog().raw.replace(
            b'valid_from = "2027-01-01"',
            b'valid_from = "2026-12-31"',
            1,
        )
        with self.assertRaisesRegex(ValueError, "overlapping date ranges"):
            PricingCatalog.from_bytes(raw)

    def test_manager_installs_only_manifest_matching_catalog(self) -> None:
        base = bundled_pricing_catalog().raw
        updated = base.replace(b'catalog_version = "2026.09.06.1"', b'catalog_version = "2026.09.07.1"')
        updated = updated.replace(b'input_per_million = 10.00', b'input_per_million = 11.00', 1)
        payloads = {
            LATEST_MANIFEST_URL: manifest(updated, "2026.09.07.1"),
            LATEST_CATALOG_URL: updated,
        }
        calls: list[str] = []

        def fetch(url: str, _maximum: int) -> bytes:
            calls.append(url)
            return payloads[url]

        with tempfile.TemporaryDirectory() as temporary:
            manager = PricingCatalogManager(
                Path(temporary),
                fetcher=fetch,
                clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp(),
            )
            result = manager.refresh(force=True)
            installed = (Path(temporary) / "api-price.toml").read_bytes()

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["catalog_version"], "2026.09.07.1")
        self.assertEqual(installed, updated)
        self.assertEqual(calls, [LATEST_MANIFEST_URL, LATEST_CATALOG_URL])
        self.assertEqual(manager.price_for("codex", "gpt-6-astra", "2026-09-07").input_per_million, 11.0)

    def test_bad_download_preserves_bundled_catalog(self) -> None:
        base = bundled_pricing_catalog().raw
        bad_manifest = json.loads(manifest(base, "2026.09.06.1"))
        bad_manifest["sha256"] = "0" * 64

        def fetch(url: str, _maximum: int) -> bytes:
            return json.dumps(bad_manifest).encode() if url == LATEST_MANIFEST_URL else base

        with tempfile.TemporaryDirectory() as temporary:
            manager = PricingCatalogManager(Path(temporary), fetcher=fetch)
            result = manager.refresh(force=True)

        self.assertEqual(result["status"], "error")
        self.assertEqual(manager.catalog_version, "2026.09.06.1")

    def test_missing_model_check_sends_no_model_or_usage_data(self) -> None:
        current = bundled_pricing_catalog().raw
        checked = threading.Event()
        calls: list[str] = []

        def fetch(url: str, _maximum: int) -> bytes:
            calls.append(url)
            checked.set()
            return manifest(current, "2026.09.06.1")

        with tempfile.TemporaryDirectory() as temporary:
            manager = PricingCatalogManager(Path(temporary), fetcher=fetch)
            self.assertIsNone(manager.price_for("codex", "gpt-secret-local", "2026-09-06"))
            self.assertTrue(checked.wait(2))
            deadline = time.monotonic() + 2
            while manager.status()["last_remote_check_at"] is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(manager.status()["last_remote_check_at"])

        self.assertEqual(calls, [LATEST_MANIFEST_URL])
        self.assertNotIn("gpt-secret-local", calls[0])


if __name__ == "__main__":
    unittest.main()
