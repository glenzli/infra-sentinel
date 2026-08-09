"""Independent remote-server polling and aggregate state projection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from traffic_estimation import TrafficEstimationConfig
from vps import VpsConfig, VpsMonitor, read_vps_counters
from xray_stats import XrayStatsConfig, XrayStatsMonitor, read_xray_stats


@dataclass(frozen=True)
class RemoteServerConfig:
    id: str
    label: str
    vps: VpsConfig
    xray: XrayStatsConfig
    estimation: TrafficEstimationConfig


class RemoteFleetMonitor:
    """Own one isolated VPS/Xray monitor pair per configured remote server."""

    def __init__(
        self,
        configs: tuple[RemoteServerConfig, ...],
        state_dir: Path,
        state: Any,
        vps_reader: Callable[[VpsConfig], dict[str, Any]] = read_vps_counters,
        xray_reader: Callable[[XrayStatsConfig], dict[str, Any]] = read_xray_stats,
    ) -> None:
        self.configs = configs
        self.state_dir = state_dir
        self.log_state = state
        self.monitors: list[tuple[RemoteServerConfig, VpsMonitor, XrayStatsMonitor]] = []
        for config in configs:
            server_state_dir = state_dir / "remote" / config.id
            server_state_dir.mkdir(parents=True, exist_ok=True)
            self.monitors.append((
                config,
                VpsMonitor(config.vps, server_state_dir, state, reader=vps_reader),
                XrayStatsMonitor(config.xray, server_state_dir, state, reader=xray_reader),
            ))
        self.public_state = self._build_public_state()

    def align_session(self, started_epoch: float | None) -> None:
        for _, _, xray_monitor in self.monitors:
            xray_monitor.align_session(started_epoch)

    def reset_session(self, started_epoch: float) -> dict[str, Any]:
        for _, vps_monitor, xray_monitor in self.monitors:
            vps_monitor.maybe_poll(started_epoch, force=True)
            xray_monitor.reset_session(started_epoch)
        self.public_state = self._build_public_state()
        return self.public_state

    @staticmethod
    def _status(rows: list[dict[str, Any]]) -> str:
        active = [row for row in rows if row.get("vps", {}).get("enabled", False)]
        if not active:
            return "disabled"
        statuses = [row["vps"].get("status") for row in active]
        if any(status == "error" for status in statuses):
            return "error"
        if any(status in {"waiting", "baseline"} for status in statuses):
            return "waiting"
        return "ok"

    def _build_public_state(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        total_cycle = {"in_bytes": 0, "out_bytes": 0, "total_bytes": 0, "interface_total_bytes": 0}
        updated: list[str] = []
        for config, vps_monitor, xray_monitor in self.monitors:
            vps_state = vps_monitor.public_state
            xray_state = xray_monitor.public_state
            cycle = vps_state.get("cycle", {})
            incoming = int(cycle.get("in_bytes", 0))
            outgoing = int(cycle.get("out_bytes", 0))
            total_cycle["in_bytes"] += incoming
            total_cycle["out_bytes"] += outgoing
            total_cycle["interface_total_bytes"] += int(cycle.get("total_bytes", incoming + outgoing))
            total_cycle["total_bytes"] += config.estimation.billable_bytes(incoming, outgoing)
            if vps_state.get("updated_at"):
                updated.append(str(vps_state["updated_at"]))
            rows.append({
                "id": config.id,
                "label": config.label,
                "billing_mode": config.estimation.billing_mode,
                "vps": vps_state,
                "xray_stats": xray_state,
            })
        return {
            "enabled": any(bool(row.get("vps", {}).get("enabled")) for row in rows),
            "status": self._status(rows),
            "updated_at": max(updated) if updated else None,
            "cycle": total_cycle,
            "servers": rows,
        }

    def maybe_poll(self, now: float | None = None, force: bool = False) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        for _, vps_monitor, xray_monitor in self.monitors:
            vps_monitor.maybe_poll(current, force=force)
            xray_monitor.maybe_poll(current, force=force)
        self.public_state = self._build_public_state()
        return self.public_state
