from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SettingsStoreNativeTests(unittest.TestCase):
    def test_native_python_settings_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            output = temporary_path / "settings-store-tests"
            module_cache = temporary_path / "module-cache"
            config = temporary_path / "config.toml"
            subprocess.run(
                [
                    "/usr/bin/clang",
                    "-fobjc-arc",
                    "-fmodules",
                    f"-fmodules-cache-path={module_cache}",
                    "-framework",
                    "Foundation",
                    "-I",
                    str(PROJECT_ROOT / "app"),
                    str(PROJECT_ROOT / "app" / "SettingsStore.m"),
                    str(PROJECT_ROOT / "tests" / "SettingsStoreTests.m"),
                    "-o",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    str(output),
                    str(PROJECT_ROOT / "bin" / "configuration.py"),
                    str(config),
                ],
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
