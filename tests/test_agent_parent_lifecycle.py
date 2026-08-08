from __future__ import annotations

from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from infra_agent import PARENT_PROCESS_ENV, parent_process_exited  # noqa: E402


class AgentParentLifecycleTests(TestCase):
    def test_missing_parent_environment_keeps_agent_running(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(parent_process_exited())

    def test_existing_recorded_parent_keeps_agent_running(self) -> None:
        with patch.dict("os.environ", {PARENT_PROCESS_ENV: "4242"}, clear=True):
            with patch("infra_agent.os.kill") as probe:
                self.assertFalse(parent_process_exited())
        probe.assert_called_once_with(4242, 0)

    def test_missing_recorded_parent_stops_agent(self) -> None:
        with patch.dict("os.environ", {PARENT_PROCESS_ENV: "4242"}, clear=True):
            with patch("infra_agent.os.kill", side_effect=ProcessLookupError):
                self.assertTrue(parent_process_exited())

    def test_invalid_parent_value_is_ignored(self) -> None:
        with patch.dict("os.environ", {PARENT_PROCESS_ENV: "not-a-pid"}, clear=True):
            self.assertFalse(parent_process_exited())
