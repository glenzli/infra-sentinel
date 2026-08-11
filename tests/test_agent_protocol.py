from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.app.protocol import cleanup_command_results  # noqa: E402


class AgentProtocolTests(unittest.TestCase):
    def test_cleanup_only_removes_expired_transient_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            commands = state_dir / "commands"
            commands.mkdir()
            stale = commands / "old.result.json"
            recent = commands / "new.result.json"
            request = commands / "pending.request.json"
            for path in (stale, recent, request):
                path.write_bytes(b"1234")
            old = time.time() - 7_200
            os.utime(stale, (old, old))

            report = cleanup_command_results(state_dir, older_than_seconds=3_600)

            self.assertEqual(report, {"files": 1, "bytes": 4})
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(request.exists())


if __name__ == "__main__":
    unittest.main()
