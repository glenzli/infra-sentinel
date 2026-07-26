"""A resettable, aligned observation session for the dashboard."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from proxy_segments import CATEGORIES
from traffic_estimation import TrafficEstimationConfig, estimate_traffic, minute_rate_trend
from vps import VPS_SAMPLE_SCHEMA


SESSION_SCHEMA = 2
RESET_REQUEST_SCHEMA = 1
HISTORY_LIMIT = 2_000
HISTORY_WINDOW_SECONDS = 15 * 60
RESET_REQUEST_NAME = "session-reset.request.json"


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def consume_reset_request(state_dir: Path) -> dict[str, Any] | None:
    """Consume a dashboard request exactly once; malformed requests are ignored."""
    path = state_dir / RESET_REQUEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        path.unlink()
    except OSError:
        return None
    if payload.get("schema") != RESET_REQUEST_SCHEMA or not isinstance(payload.get("id"), str):
        return None
    return payload


class SessionMeter:
    """Persist one user-started comparison session without changing bill cycles."""

    def __init__(self, state_dir: Path, group_ids: tuple[str, ...]) -> None:
        self.state_dir = state_dir
        self.group_ids = group_ids
        self.started_epoch: float | None = None
        self.started_reason = ""
        self.groups: dict[str, dict[str, int]] = {}
        self.proxy_categories: dict[str, dict[str, int]] = {}
        self.vps = {"in_bytes": 0, "out_bytes": 0}
        self.vps_baselined_at: float | None = None
        self.last_vps_sample_epoch: float | None = None
        self.vps_intervals = 0
        self.history: list[dict[str, Any]] = []
        self._clear()
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "session.json"

    def _clear(self) -> None:
        self.groups = {group_id: {"up_bytes": 0, "down_bytes": 0} for group_id in self.group_ids}
        self.proxy_categories = {category: {"up_bytes": 0, "down_bytes": 0} for category in CATEGORIES}
        self.vps = {"in_bytes": 0, "out_bytes": 0}
        self.vps_baselined_at = None
        self.last_vps_sample_epoch = None
        self.vps_intervals = 0
        self.history = []

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("schema") != SESSION_SCHEMA:
            return
        try:
            started = payload.get("started_epoch")
            self.started_epoch = float(started) if started is not None else None
            self.started_reason = str(payload.get("started_reason", ""))
            saved_groups = payload.get("groups", {})
            for group_id in self.group_ids:
                current = saved_groups.get(group_id, {})
                self.groups[group_id] = {"up_bytes": int(current.get("up_bytes", 0)), "down_bytes": int(current.get("down_bytes", 0))}
            saved_categories = payload.get("proxy_categories", {})
            for category in CATEGORIES:
                current = saved_categories.get(category, {})
                self.proxy_categories[category] = {"up_bytes": int(current.get("up_bytes", 0)), "down_bytes": int(current.get("down_bytes", 0))}
            saved_vps = payload.get("vps", {})
            self.vps = {"in_bytes": int(saved_vps.get("in_bytes", 0)), "out_bytes": int(saved_vps.get("out_bytes", 0))}
            default_intervals = 1 if self.vps["in_bytes"] + self.vps["out_bytes"] > 0 else 0
            self.vps_intervals = int(payload.get("vps_intervals", default_intervals))
            baseline = payload.get("vps_baselined_at")
            self.vps_baselined_at = float(baseline) if baseline is not None else None
            latest = payload.get("last_vps_sample_epoch")
            self.last_vps_sample_epoch = float(latest) if latest is not None else None
            raw_history = payload.get("history", [])
            self.history = [item for item in raw_history if isinstance(item, dict)][-HISTORY_LIMIT:]
        except (AttributeError, TypeError, ValueError):
            self.started_epoch = None
            self.started_reason = ""
            self._clear()

    def _save(self) -> None:
        payload = {
            "schema": SESSION_SCHEMA,
            "started_epoch": self.started_epoch,
            "started_reason": self.started_reason,
            "groups": self.groups,
            "proxy_categories": self.proxy_categories,
            "vps": self.vps,
            "vps_intervals": self.vps_intervals,
            "vps_baselined_at": self.vps_baselined_at,
            "last_vps_sample_epoch": self.last_vps_sample_epoch,
            "history": self.history,
        }
        temporary = self.state_dir / ".session.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def reset(self, epoch: float, reason: str) -> None:
        self.started_epoch = float(epoch)
        self.started_reason = reason
        self._clear()
        self._save()

    def set_vps_baseline(self, vps_state: dict[str, Any]) -> None:
        last = vps_state.get("last_sample", {})
        if vps_state.get("status") == "error" or last.get("schema") != VPS_SAMPLE_SCHEMA:
            self._save()
            return
        try:
            self.last_vps_sample_epoch = float(last["epoch"])
            self.vps_baselined_at = self.last_vps_sample_epoch
        except (KeyError, TypeError, ValueError):
            pass
        self._save()

    def record(self, sample: dict[str, Any], proxy_sample: dict[str, Any], vps_state: dict[str, Any]) -> None:
        if self.started_epoch is None:
            self.reset(float(sample["epoch"]), "automatic")
            self.set_vps_baseline(vps_state)
            return
        for group_id in self.group_ids:
            traffic = sample.get("groups", {}).get(group_id, {})
            self.groups[group_id]["up_bytes"] += int(traffic.get("up_bytes", 0))
            self.groups[group_id]["down_bytes"] += int(traffic.get("down_bytes", 0))
        for category in CATEGORIES:
            traffic = proxy_sample.get("categories", {}).get(category, {})
            self.proxy_categories[category]["up_bytes"] += int(traffic.get("up_bytes", 0))
            self.proxy_categories[category]["down_bytes"] += int(traffic.get("down_bytes", 0))

        last = vps_state.get("last_sample", {})
        try:
            vps_epoch = float(last["epoch"])
            interval_start = float(last["interval_started_epoch"])
        except (KeyError, TypeError, ValueError):
            vps_epoch = None
            interval_start = None
        if vps_epoch is not None and vps_epoch != self.last_vps_sample_epoch:
            self.last_vps_sample_epoch = vps_epoch
            if self.vps_baselined_at is None or (self.started_epoch is not None and interval_start is not None and interval_start >= self.started_epoch):
                self.vps["in_bytes"] += int(last.get("in_bytes", 0))
                self.vps["out_bytes"] += int(last.get("out_bytes", 0))
                self.vps_intervals += 1
            self.vps_baselined_at = vps_epoch

        external = proxy_sample.get("categories", {}).get("external", {})
        self.history.append({
            "epoch": float(sample["epoch"]),
            "observed_seconds": float(sample.get("observed_seconds", 0.0)),
            "groups": {group_id: int(sample.get("groups", {}).get(group_id, {}).get("up_bytes", 0)) + int(sample.get("groups", {}).get(group_id, {}).get("down_bytes", 0)) for group_id in self.group_ids},
            "proxy_external": int(external.get("up_bytes", 0)) + int(external.get("down_bytes", 0)),
        })
        cutoff = float(sample["epoch"]) - HISTORY_WINDOW_SECONDS
        self.history = [point for point in self.history[-HISTORY_LIMIT:] if float(point.get("epoch", 0)) >= cutoff]
        self._save()

    def snapshot(
        self,
        labels: dict[str, str],
        roles: dict[str, str],
        vps_enabled: bool,
        estimation_config: TrafficEstimationConfig,
        now: float | None = None,
    ) -> dict[str, Any]:
        groups = []
        for group_id in self.group_ids:
            traffic = self.groups[group_id]
            groups.append({
                "id": group_id,
                "label": labels.get(group_id, group_id),
                "role": roles.get(group_id, "attribution"),
                **traffic,
                "total_bytes": traffic["up_bytes"] + traffic["down_bytes"],
            })
        proxy_categories = []
        for category in CATEGORIES:
            traffic = self.proxy_categories[category]
            proxy_categories.append({"id": category, **traffic, "total_bytes": traffic["up_bytes"] + traffic["down_bytes"]})
        proxy_external_total = self.proxy_categories["external"]["up_bytes"] + self.proxy_categories["external"]["down_bytes"]
        vps_total = self.vps["in_bytes"] + self.vps["out_bytes"]
        project_groups = sorted((group for group in groups if group["role"] == "attribution"), key=lambda group: group["total_bytes"], reverse=True)
        project_total = sum(group["total_bytes"] for group in project_groups)
        visible_projects = project_groups[:3]
        if len(project_groups) > 3:
            remaining_total = sum(group["total_bytes"] for group in project_groups[3:])
            visible_projects.append({"id": "other_monitored", "label": "其他项目", "role": "attribution", "up_bytes": 0, "down_bytes": 0, "total_bytes": remaining_total})
        vps_ready = bool(vps_enabled and self.vps_intervals > 0)
        estimates = estimate_traffic(proxy_external_total, project_total, vps_total, vps_ready, estimation_config)
        duration_seconds = max(0, int((now if now is not None else time.time()) - self.started_epoch)) if self.started_epoch is not None else 0
        return {
            "started_at": iso_now(self.started_epoch) if self.started_epoch is not None else None,
            "started_epoch": self.started_epoch,
            "started_reason": self.started_reason,
            "groups": groups,
            "proxy_categories": proxy_categories,
            "proxy_external_total_bytes": proxy_external_total,
            "vps": {
                **self.vps,
                "total_bytes": vps_total,
                "baselined_at": iso_now(self.vps_baselined_at) if self.vps_baselined_at is not None else None,
                "intervals": self.vps_intervals,
            },
            "vps_ready": vps_ready,
            "duration_seconds": duration_seconds,
            "breakdown": {
                "visible_projects": visible_projects,
                **estimates,
            },
            "trend": minute_rate_trend(self.history, (group["id"] for group in project_groups)),
        }
