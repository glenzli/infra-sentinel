"""Low-frequency, per-user Xray traffic accounting over loopback-only Stats API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import time
from typing import Any, Callable

from infra_sentinel.core.status_stability import StatusDecision, StatusStabilizer
from infra_sentinel.resources.network.remote_ssh import run_read_only_script


XRAY_SAMPLE_SCHEMA = 1
XRAY_BASELINE_SCHEMA = 1
XRAY_SESSION_SCHEMA = 1
API_SERVER_RE = re.compile(r"127\.0\.0\.1:([1-9][0-9]{0,4})\Z")
BINARY_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+\Z")
USER_STAT_RE = re.compile(r"user>>>(.+?)>>>traffic>>>(uplink|downlink)\Z")

REMOTE_STATS_SCRIPT = r"""set -eu
api_server=$1
xray_binary=$2
case "$api_server" in
  127.0.0.1:[0-9]*) ;;
  *) echo "invalid loopback API address" >&2; exit 2 ;;
esac
case "$xray_binary" in
  /*) ;;
  *) echo "invalid Xray binary path" >&2; exit 3 ;;
esac
[ -x "$xray_binary" ] || { echo "Xray binary is not executable" >&2; exit 4; }
exec "$xray_binary" api statsquery --server="$api_server" -pattern 'user>>>'
"""


@dataclass(frozen=True)
class XrayStatsConfig:
    enabled: bool
    ssh_host: str
    api_server: str
    binary_path: str
    poll_seconds: int
    expected_users: tuple[str, ...] = ()
    flagged_users: tuple[str, ...] = ()
    server_id: str = "default"
    label: str = "VPS"
    ssh_executable: str | None = None


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def parse_xray_stats(output: str) -> dict[str, dict[str, int]]:
    """Extract only per-user byte counters from one StatsService response."""
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Xray Stats 未返回 JSON")
    try:
        payload = json.loads(output[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Xray Stats JSON 无法解析：{exc}") from exc
    records = payload.get("stat", [])
    if not isinstance(records, list):
        raise ValueError("Xray Stats 的 stat 字段不是数组")
    users: dict[str, dict[str, int]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        match = USER_STAT_RE.fullmatch(str(record.get("name", "")))
        if not match:
            continue
        label, direction = match.groups()
        if not label or "\n" in label or "\r" in label:
            continue
        try:
            value = max(0, int(record.get("value", 0)))
        except (TypeError, ValueError):
            continue
        traffic = users.setdefault(label, {"up_bytes": 0, "down_bytes": 0})
        traffic["up_bytes" if direction == "uplink" else "down_bytes"] = value
    return users


def read_xray_stats(config: XrayStatsConfig) -> dict[str, Any]:
    """Read cumulative user counters through the existing SSH host alias."""
    match = API_SERVER_RE.fullmatch(config.api_server)
    if not match or int(match.group(1)) > 65_535:
        raise ValueError("[xray_stats] api_server 必须是 127.0.0.1:端口")
    if not BINARY_PATH_RE.fullmatch(config.binary_path):
        raise ValueError("[xray_stats] binary_path 必须是不含空格的绝对路径")
    try:
        completed = run_read_only_script(
            config.ssh_host,
            REMOTE_STATS_SCRIPT,
            (config.api_server, config.binary_path),
            timeout=20,
            ssh_executable=config.ssh_executable,
        )
    except (ValueError, RuntimeError) as exc:
        raise type(exc)(f"[xray_stats] {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        raise RuntimeError(f"Xray Stats SSH 退出码 {completed.returncode}：{detail[:300]}")
    epoch = time.time()
    return {
        "timestamp": iso_now(epoch),
        "epoch": epoch,
        "users": parse_xray_stats(completed.stdout),
    }


class XrayStatsTracker:
    """Convert cumulative Xray counters into non-overlapping poll deltas."""

    def __init__(self, previous: dict[str, dict[str, int]] | None = None, previous_epoch: float | None = None) -> None:
        self.previous = previous or {}
        self.previous_epoch = previous_epoch

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        deltas: dict[str, dict[str, int]] = {}
        next_previous = {
            label: {"up_bytes": int(value["up_bytes"]), "down_bytes": int(value["down_bytes"])}
            for label, value in self.previous.items()
        }
        for label, current in raw.get("users", {}).items():
            current_up = int(current.get("up_bytes", 0))
            current_down = int(current.get("down_bytes", 0))
            prior = self.previous.get(label)
            up_delta = current_up - int(prior["up_bytes"]) if prior and current_up >= int(prior["up_bytes"]) else 0
            down_delta = current_down - int(prior["down_bytes"]) if prior and current_down >= int(prior["down_bytes"]) else 0
            deltas[label] = {"up_bytes": up_delta, "down_bytes": down_delta}
            next_previous[label] = {"up_bytes": current_up, "down_bytes": current_down}
        interval_started_epoch = self.previous_epoch
        self.previous = next_previous
        self.previous_epoch = float(raw["epoch"])
        return {
            "schema": XRAY_SAMPLE_SCHEMA,
            "timestamp": raw["timestamp"],
            "epoch": raw["epoch"],
            "interval_started_epoch": interval_started_epoch,
            "users": deltas,
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "schema": XRAY_BASELINE_SCHEMA,
            "epoch": self.previous_epoch,
            "users": self.previous,
        }


class XrayStatsMonitor:
    """Own polling, baselines, aligned session totals, and public user rows."""

    def __init__(
        self,
        config: XrayStatsConfig,
        state_dir: Path,
        state: Any,
        reader: Callable[[XrayStatsConfig], dict[str, Any]] = read_xray_stats,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.log_state = state
        self.reader = reader
        self.tracker = self._load_tracker()
        self.session_started_epoch: float | None = None
        self.coverage_started_epoch: float | None = None
        self.session_totals: dict[str, dict[str, int]] = {}
        self.session_intervals = 0
        self.next_poll_at = 0.0
        self._load_session()
        self.public_state = self._build_public_state("disabled" if not config.enabled else "waiting")
        self._status_stability = StatusStabilizer(
            str(self.public_state["status"]),
            {"disabled": 0, "waiting": 0, "baseline": 0, "ok": 0, "error": 1},
            worsen_after=2,
            recover_after=2,
        )

    @staticmethod
    def _confirmation(decision: StatusDecision) -> dict[str, Any] | None:
        if not decision.pending_status:
            return None
        return {
            "candidate_status": decision.pending_status,
            "consecutive": decision.pending_count,
            "required": decision.required_count,
        }

    @property
    def baseline_path(self) -> Path:
        return self.state_dir / "xray_stats_baseline.json"

    @property
    def session_path(self) -> Path:
        return self.state_dir / "xray_stats_session.json"

    def _load_tracker(self) -> XrayStatsTracker:
        try:
            payload = json.loads(self.baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return XrayStatsTracker()
        if payload.get("schema") != XRAY_BASELINE_SCHEMA or not isinstance(payload.get("users"), dict):
            return XrayStatsTracker()
        try:
            users = {
                str(label): {
                    "up_bytes": int(traffic.get("up_bytes", 0)),
                    "down_bytes": int(traffic.get("down_bytes", 0)),
                }
                for label, traffic in payload["users"].items()
                if isinstance(traffic, dict)
            }
            epoch = payload.get("epoch")
            return XrayStatsTracker(users, float(epoch) if epoch is not None else None)
        except (AttributeError, TypeError, ValueError):
            return XrayStatsTracker()

    def _load_session(self) -> None:
        try:
            payload = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("schema") != XRAY_SESSION_SCHEMA:
            return
        try:
            started = payload.get("started_epoch")
            coverage = payload.get("coverage_started_epoch")
            self.session_started_epoch = float(started) if started is not None else None
            self.coverage_started_epoch = float(coverage) if coverage is not None else None
            self.session_intervals = int(payload.get("intervals", 0))
            self.session_totals = {
                str(label): {
                    "up_bytes": int(traffic.get("up_bytes", 0)),
                    "down_bytes": int(traffic.get("down_bytes", 0)),
                }
                for label, traffic in payload.get("users", {}).items()
                if isinstance(traffic, dict)
            }
        except (AttributeError, TypeError, ValueError):
            self.session_started_epoch = None
            self.coverage_started_epoch = None
            self.session_totals = {}
            self.session_intervals = 0

    def _save_tracker(self) -> None:
        self._atomic_json(self.baseline_path, self.tracker.serialize())

    def _save_session(self) -> None:
        self._atomic_json(self.session_path, {
            "schema": XRAY_SESSION_SCHEMA,
            "started_epoch": self.session_started_epoch,
            "coverage_started_epoch": self.coverage_started_epoch,
            "intervals": self.session_intervals,
            "users": self.session_totals,
        })

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _append_sample(self, sample: dict[str, Any]) -> None:
        path = self.state_dir / "xray_user_samples.jsonl"
        if path.exists() and path.stat().st_size >= self.log_state.max_log_bytes:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"xray_user_samples-{timestamp}.jsonl"))
            archives = sorted(self.state_dir.glob("xray_user_samples-*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
            for stale in archives[self.log_state.backups:]:
                stale.unlink(missing_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")

    def align_session(self, started_epoch: float | None) -> None:
        if started_epoch is None:
            return
        if self.session_started_epoch is None or abs(self.session_started_epoch - float(started_epoch)) > 0.5:
            self.session_started_epoch = float(started_epoch)
            self.coverage_started_epoch = None
            self.session_totals = {}
            self.session_intervals = 0
            self._save_session()
            self.public_state = self._build_public_state(self.public_state.get("status", "waiting"))

    def reset_session(self, started_epoch: float) -> dict[str, Any]:
        self.session_started_epoch = float(started_epoch)
        self.coverage_started_epoch = None
        self.session_totals = {}
        self.session_intervals = 0
        self._save_session()
        return self.maybe_poll(started_epoch, force=True)

    def _record_session(self, sample: dict[str, Any]) -> None:
        interval_started = sample.get("interval_started_epoch")
        if self.session_started_epoch is None or interval_started is None:
            return
        if float(interval_started) < self.session_started_epoch:
            return
        if self.coverage_started_epoch is None:
            self.coverage_started_epoch = float(interval_started)
        for label, delta in sample.get("users", {}).items():
            total = self.session_totals.setdefault(label, {"up_bytes": 0, "down_bytes": 0})
            total["up_bytes"] += int(delta.get("up_bytes", 0))
            total["down_bytes"] += int(delta.get("down_bytes", 0))
        self.session_intervals += 1
        self._save_session()

    def _ordered_labels(self) -> list[str]:
        configured = list(dict.fromkeys((*self.config.expected_users, *self.config.flagged_users)))
        remaining = [label for label in self.session_totals if label not in configured]
        remaining.sort(
            key=lambda label: (
                self.session_totals[label]["up_bytes"] + self.session_totals[label]["down_bytes"],
                label,
            ),
            reverse=True,
        )
        return configured + remaining

    def _build_public_state(
        self,
        status: str,
        *,
        updated_at: str | None = None,
        last_sample: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        flagged = set(self.config.flagged_users)
        users = []
        for label in self._ordered_labels():
            traffic = self.session_totals.get(label, {"up_bytes": 0, "down_bytes": 0})
            users.append({
                "id": label,
                "label": label,
                "up_bytes": int(traffic["up_bytes"]),
                "down_bytes": int(traffic["down_bytes"]),
                "total_bytes": int(traffic["up_bytes"]) + int(traffic["down_bytes"]),
                "flagged": label in flagged,
            })
        result = {
            "enabled": self.config.enabled,
            "status": status,
            "server_id": self.config.server_id,
            "label": self.config.label,
            "ssh_host": self.config.ssh_host,
            "api_server": self.config.api_server,
            "poll_seconds": self.config.poll_seconds,
            "updated_at": updated_at,
            "session_started_at": iso_now(self.session_started_epoch) if self.session_started_epoch is not None else None,
            "coverage_started_at": iso_now(self.coverage_started_epoch) if self.coverage_started_epoch is not None else None,
            "intervals": self.session_intervals,
            "ready": self.session_intervals > 0,
            "users": users,
            "total_bytes": sum(user["total_bytes"] for user in users),
            "last_sample": last_sample,
        }
        if error:
            result["error"] = error[:500]
        return result

    def maybe_poll(self, now: float | None = None, force: bool = False) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        if not self.config.enabled:
            self.public_state = self._build_public_state("disabled")
            self._status_stability.observe("disabled", immediate=True)
            return self.public_state
        if not force and current < self.next_poll_at:
            return self.public_state
        self.next_poll_at = current + self.config.poll_seconds
        try:
            raw = self.reader(self.config)
            was_uninitialized = self.tracker.previous_epoch is None
            sample = self.tracker.apply(raw)
            self._save_tracker()
            self._append_sample(sample)
            self._record_session(sample)
            candidate_status = "baseline" if was_uninitialized else "ok"
            decision = self._status_stability.observe(
                candidate_status,
                immediate=self._status_stability.status in {"waiting", "baseline"},
            )
            self.public_state = self._build_public_state(
                decision.status,
                updated_at=sample["timestamp"],
                last_sample=sample,
            )
            confirmation = self._confirmation(decision)
            if confirmation:
                self.public_state["confirmation"] = confirmation
        except Exception as exc:
            previous = self.public_state
            decision = self._status_stability.observe("error")
            self.public_state = self._build_public_state(
                decision.status,
                updated_at=previous.get("updated_at"),
                last_sample=previous.get("last_sample"),
                error=str(exc) if decision.status == "error" else None,
            )
            confirmation = self._confirmation(decision)
            if confirmation:
                self.public_state["confirmation"] = confirmation
        return self.public_state
