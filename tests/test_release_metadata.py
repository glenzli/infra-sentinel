from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELEASE_VERSION = "2.0.0"


class ReleaseMetadataTests(unittest.TestCase):
    def test_product_versions_are_aligned(self) -> None:
        pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        package = json.loads((PROJECT_ROOT / "ui/package.json").read_text())
        tauri = json.loads((PROJECT_ROOT / "ui/src-tauri/tauri.conf.json").read_text())
        cargo = tomllib.loads((PROJECT_ROOT / "ui/src-tauri/Cargo.toml").read_text())
        cargo_lock = tomllib.loads((PROJECT_ROOT / "ui/src-tauri/Cargo.lock").read_text())
        desktop_lock = next(
            item for item in cargo_lock["package"]
            if item["name"] == "infra-sentinel-desktop"
        )

        self.assertEqual(pyproject["project"]["version"], RELEASE_VERSION)
        self.assertEqual(package["version"], RELEASE_VERSION)
        self.assertEqual(tauri["version"], RELEASE_VERSION)
        self.assertEqual(cargo["package"]["version"], RELEASE_VERSION)
        self.assertEqual(desktop_lock["version"], RELEASE_VERSION)

    def test_release_notes_and_changelog_name_the_release(self) -> None:
        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text()
        release_notes = PROJECT_ROOT / "docs/releases/v2.0.0.md"

        self.assertIn(f"## {RELEASE_VERSION} — 2026-08-28", changelog)
        self.assertTrue(release_notes.is_file())
        self.assertIn(
            f"# Infra Sentinel {RELEASE_VERSION}",
            release_notes.read_text(),
        )


if __name__ == "__main__":
    unittest.main()
