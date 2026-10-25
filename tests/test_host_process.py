from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.platform.host_process import background_subprocess_options, process_is_running  # noqa: E402


class HostProcessTests(unittest.TestCase):
    def test_live_child_probe_is_non_destructive_and_observes_exit(self) -> None:
        # A real child catches the Windows os.kill(pid, 0) regression: it
        # terminates a process instead of probing it on that platform.
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **background_subprocess_options(),
        )
        try:
            with patch("infra_sentinel.platform.host_process.os.kill", wraps=os.kill) as signal:
                self.assertTrue(process_is_running(child.pid))
                self.assertIsNone(child.poll())
                if os.name == "nt":
                    signal.assert_not_called()
            child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertFalse(process_is_running(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt", "Windows process creation")
    def test_background_child_retains_utf8_pipes_without_a_console(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write('本机监控'.encode('utf-8'))"],
            capture_output=True,
            check=True,
            **background_subprocess_options(),
        )
        self.assertEqual(completed.stdout.decode("utf-8"), "本机监控")
