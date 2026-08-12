from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.status_stability import StatusStabilizer  # noqa: E402


class StatusStabilizerTests(unittest.TestCase):
    def test_worsening_requires_consecutive_observations(self) -> None:
        stable = StatusStabilizer("healthy", {"healthy": 0, "warning": 1, "critical": 2})

        first = stable.observe("warning")
        interrupted = stable.observe("healthy")
        second_first = stable.observe("warning")
        second_second = stable.observe("warning")
        confirmed = stable.observe("warning")

        self.assertEqual((first.status, first.pending_count, first.required_count), ("healthy", 1, 3))
        self.assertEqual(interrupted.status, "healthy")
        self.assertEqual((second_first.pending_count, second_second.pending_count), (1, 2))
        self.assertEqual(confirmed.status, "warning")
        self.assertTrue(confirmed.changed)

    def test_recovery_uses_its_own_confirmation_window(self) -> None:
        stable = StatusStabilizer(
            "critical",
            {"healthy": 0, "warning": 1, "critical": 2},
            recover_after=2,
        )

        pending = stable.observe("healthy")
        recovered = stable.observe("healthy")

        self.assertEqual(pending.status, "critical")
        self.assertEqual((pending.pending_count, pending.required_count), (1, 2))
        self.assertEqual(recovered.status, "healthy")
        self.assertTrue(recovered.changed)

    def test_immediate_transition_bypasses_confirmation(self) -> None:
        stable = StatusStabilizer("healthy", {"healthy": 0, "critical": 2})

        decision = stable.observe("critical", immediate=True)

        self.assertEqual(decision.status, "critical")
        self.assertTrue(decision.changed)
        self.assertIsNone(decision.pending_status)


if __name__ == "__main__":
    unittest.main()
