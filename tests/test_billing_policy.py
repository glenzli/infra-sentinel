from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from billing_policy import BillingBudgetEngine, BillingBudgetPolicy, billable_cycle_bytes  # noqa: E402


def remote_state(incoming: int, outgoing: int, *, billing_mode: str = "both") -> dict[str, object]:
    return {
        "servers": [{
            "id": "primary", "label": "Primary", "billing_mode": billing_mode,
            "vps": {"enabled": True, "cycle": {"in_bytes": incoming, "out_bytes": outgoing}},
        }],
    }


class BillingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = BillingBudgetPolicy(
            id="primary-billing-budget", source_id="primary", label="Primary",
            warning_bytes=100, critical_bytes=200,
        )

    def test_uses_the_server_billing_direction(self) -> None:
        self.assertEqual(billable_cycle_bytes(remote_state(60, 70)["servers"][0]), 130)
        self.assertEqual(billable_cycle_bytes(remote_state(60, 70, billing_mode="outbound")["servers"][0]), 70)

    def test_each_vps_budget_has_its_own_transition_state(self) -> None:
        engine = BillingBudgetEngine()
        self.assertEqual(engine.evaluate(remote_state(40, 50), (self.policy,)), [])
        warning = engine.evaluate(remote_state(60, 50), (self.policy,))
        self.assertEqual([(item.event_type, item.level, item.billable_bytes) for item in warning], [("alert", "warning", 110)])
        self.assertEqual(engine.level, "warning")
        critical = engine.evaluate(remote_state(120, 90), (self.policy,))
        self.assertEqual([(item.event_type, item.level) for item in critical], [("alert", "critical")])
        deescalated = engine.evaluate(remote_state(60, 50), (self.policy,))
        self.assertEqual([(item.event_type, item.level) for item in deescalated], [("deescalated", "warning")])
        recovered = engine.evaluate(remote_state(30, 40), (self.policy,))
        self.assertEqual([(item.event_type, item.level) for item in recovered], [("recovered", "none")])


if __name__ == "__main__":
    unittest.main()
