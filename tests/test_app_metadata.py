from __future__ import annotations

from pathlib import Path
import plistlib
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppMetadataTests(unittest.TestCase):
    def test_source_build_metadata_is_the_1_0_contract(self) -> None:
        with (PROJECT_ROOT / "app" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)

        self.assertEqual(info["CFBundleShortVersionString"], "1.0.0")
        self.assertEqual(info["CFBundleVersion"], "1")
        self.assertEqual(info["LSMinimumSystemVersion"], "13.0")


if __name__ == "__main__":
    unittest.main()
