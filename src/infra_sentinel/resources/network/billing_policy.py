"""Per-VPS daily-usage guardrails and transition state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class BillingBudgetPolicy:
    """A single VPS's local daily-usage limits."""

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
    usage_bytes: int
    threshold_bytes: int
    cycle: dict[str, Any]


def daily_usage_bytes(server: dict[str, Any]) -> int:
    """Use this host's configured billing direction for the daily guardrail."""
    cycle = server.get("vps", {}).get("cycle", {})
    incoming = max(0, int(cycle.get("in_bytes", 0)))
    outgoing = max(0, int(cycle.get("out_bytes", 0)))
    return outgoing if server.get("billing_mode") == "outbound" else incoming + outgoing


class BillingBudgetEngine:
    """Own persistent alert state independently for each VPS daily-usage policy."""

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
            value = daily_usage_bytes(server)
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
                usage_bytes=value,
                threshold_bytes=threshold,
                cycle=dict(server.get("vps", {}).get("cycle", {})),
            ))
        for policy_id in set(self.levels) - active_ids:
            del self.levels[policy_id]
        return transitions

    def snapshots(
        self,
        remote_state: dict[str, Any],
        policies: Iterable[BillingBudgetPolicy],
    ) -> list[dict[str, Any]]:
        """Expose durable per-host daily-usage state for the public Projection."""
        servers = {
            str(server.get("id")): server
            for server in remote_state.get("servers", [])
            if isinstance(server, dict)
        }
        rows: list[dict[str, Any]] = []
        for policy in policies:
            server = servers.get(policy.source_id)
            if not isinstance(server, dict) or not server.get("vps", {}).get("enabled"):
                continue
            rows.append({
                "id": policy.id,
                "source_id": policy.source_id,
                "label": policy.label,
                "level": self.levels.get(policy.id, "none"),
                "usage_bytes": daily_usage_bytes(server),
                "warning_bytes": policy.warning_bytes,
                "critical_bytes": policy.critical_bytes,
                "cycle": dict(server.get("vps", {}).get("cycle", {})),
            })
        return rows
