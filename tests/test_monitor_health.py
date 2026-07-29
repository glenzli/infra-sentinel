from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MonitorHealthNativeTests(unittest.TestCase):
    def test_health_projection_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "monitor-health-tests"
            module_cache = Path(temporary) / "module-cache"
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
                    str(PROJECT_ROOT / "app" / "MonitorHealth.m"),
                    str(PROJECT_ROOT / "tests" / "MonitorHealthTests.m"),
                    "-o",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [str(output)],
                check=True,
                capture_output=True,
                text=True,
            )
