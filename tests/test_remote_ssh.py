from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.network.remote_ssh import resolve_ssh_executable, run_read_only_script  # noqa: E402


class RemoteSshTests(unittest.TestCase):
    def test_rejects_host_alias_that_can_be_parsed_as_an_option(self) -> None:
        with patch("infra_sentinel.resources.network.remote_ssh.subprocess.run") as runner:
            with self.assertRaisesRegex(ValueError, "主机别名"):
                run_read_only_script("-V", "exit 0")
        runner.assert_not_called()

    def test_preserves_legitimate_host_alias_and_hardened_options(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with patch("infra_sentinel.resources.network.remote_ssh.subprocess.run", return_value=completed) as runner:
            result = run_read_only_script(
                "my-vps",
                "printf '%s' \"$1\"",
                ("eth0",),
            )
        self.assertIs(result, completed)
        command = runner.call_args.args[0]
        host_index = command.index("ClearAllForwardings=yes") + 1
        self.assertEqual(command[host_index], "my-vps")
        self.assertEqual(command[-4:], ["/bin/sh", "-s", "--", "eth0"])
        self.assertEqual(runner.call_args.kwargs["input"], "printf '%s' \"$1\"")
        self.assertTrue(runner.call_args.kwargs["text"])
        self.assertTrue(runner.call_args.kwargs["capture_output"])

    def test_explicit_ssh_executable_is_used_for_portable_installations(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        executable = str(Path("/portable/OpenSSH/ssh.exe"))
        with patch("infra_sentinel.resources.network.remote_ssh.subprocess.run", return_value=completed) as runner:
            run_read_only_script("my-vps", "exit 0", ssh_executable=executable)
        self.assertEqual(runner.call_args.args[0][0], executable)

    def test_automatic_discovery_fails_with_a_clear_error(self) -> None:
        with patch("infra_sentinel.resources.network.remote_ssh.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "SSH 客户端"):
                resolve_ssh_executable()


if __name__ == "__main__":
    unittest.main()
