"""Per-VPS billing-cycle budget policies and transition state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class BillingBudgetPolicy:
    """A single VPS's billable-byte limits for its configured billing cycle."""

    id: str
    source_id: str
    label: str
    warning_bytes: int
    critical_bytes: int


@dataclass(frozen=True)
class BillingBudgetTransition:
    policy: BillingBudgetPolicy
    event_type: str
    level: str
    billable_bytes: int
    threshold_bytes: int
    cycle: dict[str, Any]


def billable_cycle_bytes(server: dict[str, Any]) -> int:
    """Apply this VPS's provider billing direction to its cycle counters."""
    cycle = server.get("vps", {}).get("cycle", {})
    incoming = max(0, int(cycle.get("in_bytes", 0)))
    outgoing = max(0, int(cycle.get("out_bytes", 0)))
    return outgoing if server.get("billing_mode") == "outbound" else incoming + outgoing


class BillingBudgetEngine:
    """Own alert state independently for each configured VPS billing policy."""

    def __init__(self) -> None:
        self.levels: dict[str, str] = {}

    @property
    def level(self) -> str:
        if "critical" in self.levels.values():
            return "critical"
        if "warning" in self.levels.values():
            return "warning"
        return "none"

    def evaluate(
        self,
        remote_state: dict[str, Any],
        policies: Iterable[BillingBudgetPolicy],
    ) -> list[BillingBudgetTransition]:
        servers = {
            str(server.get("id")): server
            for server in remote_state.get("servers", [])
            if isinstance(server, dict)
        }
        active_ids: set[str] = set()
        transitions: list[BillingBudgetTransition] = []
        for policy in policies:
            active_ids.add(policy.id)
            server = servers.get(policy.source_id)
            if not isinstance(server, dict) or not server.get("vps", {}).get("enabled"):
                continue
            value = billable_cycle_bytes(server)
            next_level = (
                "critical" if value >= policy.critical_bytes
                else "warning" if value >= policy.warning_bytes
                else "none"
            )
            previous = self.levels.get(policy.id, "none")
            self.levels[policy.id] = next_level
            if next_level == previous:
                continue
            event_type = (
                "recovered" if next_level == "none"
                else "deescalated" if previous == "critical" and next_level == "warning"
                else "alert"
            )
            threshold = policy.critical_bytes if next_level == "critical" else policy.warning_bytes
            transitions.append(BillingBudgetTransition(
                policy=policy,
                event_type=event_type,
                level=next_level,
                billable_bytes=value,
                threshold_bytes=threshold,
                cycle=dict(server.get("vps", {}).get("cycle", {})),
            ))
        for policy_id in set(self.levels) - active_ids:
            del self.levels[policy_id]
        return transitions
