from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from billing_policy import BillingBudgetEngine, BillingBudgetPolicy, daily_usage_bytes  # noqa: E402


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
            id="primary-daily-usage", source_id="primary", label="Primary",
            warning_bytes=100, critical_bytes=200,
        )

    def test_uses_full_interface_traffic_independent_of_billing_direction(self) -> None:
        self.assertEqual(daily_usage_bytes(remote_state(60, 70)["servers"][0]), 130)
        self.assertEqual(daily_usage_bytes(remote_state(60, 70, billing_mode="outbound")["servers"][0]), 130)

    def test_each_vps_budget_has_its_own_transition_state(self) -> None:
        engine = BillingBudgetEngine()
        self.assertEqual(engine.evaluate(remote_state(40, 50), (self.policy,)), [])
        warning = engine.evaluate(remote_state(60, 50), (self.policy,))
        self.assertEqual([(item.event_type, item.level, item.usage_bytes) for item in warning], [("alert", "warning", 110)])
        self.assertEqual(engine.level, "warning")
        critical = engine.evaluate(remote_state(120, 90), (self.policy,))
        self.assertEqual([(item.event_type, item.level) for item in critical], [("alert", "critical")])
        deescalated = engine.evaluate(remote_state(60, 50), (self.policy,))
        self.assertEqual([(item.event_type, item.level) for item in deescalated], [("deescalated", "warning")])
        recovered = engine.evaluate(remote_state(30, 40), (self.policy,))
        self.assertEqual([(item.event_type, item.level) for item in recovered], [("recovered", "none")])

    def test_snapshots_expose_current_daily_usage_and_persistent_level(self) -> None:
        engine = BillingBudgetEngine()
        state = remote_state(60, 50)
        engine.evaluate(state, (self.policy,))
        self.assertEqual(engine.snapshots(state, (self.policy,)), [{
            "id": "primary-daily-usage",
            "source_id": "primary",
            "label": "Primary",
            "level": "warning",
            "usage_bytes": 110,
            "warning_bytes": 100,
            "critical_bytes": 200,
            "cycle": {"in_bytes": 60, "out_bytes": 50},
        }])


if __name__ == "__main__":
    unittest.main()
