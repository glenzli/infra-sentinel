from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.core.collectors import CollectorContext  # noqa: E402
from infra_sentinel.resources.system.backends.windows import WindowsSystemBackend  # noqa: E402
from infra_sentinel.resources.system.collector import SystemResourceCollector  # noqa: E402


@unittest.skipUnless(os.name == "nt", "native Windows resource APIs")
class WindowsSystemTests(unittest.TestCase):
    def test_native_backend_projects_real_capacity_and_cpu_without_fabricated_capabilities(self) -> None:
        collector = SystemResourceCollector(WindowsSystemBackend())
        result = None
        for _ in range(2):
            epoch = time.time()
            result = collector.collect(CollectorContext({
                "epoch": epoch,
                "timestamp": datetime.fromtimestamp(epoch).astimezone().isoformat(),
            }, {}))
        snapshot = result.snapshot
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["platform"], "windows")
        self.assertEqual(set(snapshot["capabilities"]), {"cpu.utilization", "memory.capacity", "disk.capacity"})
        self.assertGreater(snapshot["memory"]["total_bytes"], 0)
        self.assertGreater(snapshot["disk"]["total_bytes"], 0)
        self.assertGreaterEqual(snapshot["cpu"]["percent"], 0)
        self.assertLessEqual(snapshot["cpu"]["percent"], 100)
