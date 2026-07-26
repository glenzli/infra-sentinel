"""Low-frequency, short-lived SSH polling for a VPS network interface."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable


VPS_SAMPLE_SCHEMA = 2
VPS_BASELINE_SCHEMA = 2
SUPPORTED_VPS_SAMPLE_SCHEMAS = {1, VPS_SAMPLE_SCHEMA}
HOST_ALIAS_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
INTERFACE_RE = re.compile(r"[A-Za-z0-9_.:-]+\Z")

# This is deliberately static. The configured interface is passed as a shell
# argument only after local validation, so no configuration value is executed.
REMOTE_COUNTER_SCRIPT = r"""set -eu
requested_interface=$1
if [ "$requested_interface" = "auto" ]; then
  ip_command=""
  for candidate in /sbin/ip /usr/sbin/ip /usr/bin/ip ip; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ip_command=$candidate
      break
    fi
  done
  [ -n "$ip_command" ] || { echo "ip command unavailable" >&2; exit 2; }
  interface=$("$ip_command" route show default | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
else
  interface=$requested_interface
fi
case "$interface" in
  ""|*[!A-Za-z0-9_.:-]*) echo "invalid interface" >&2; exit 3 ;;
esac
rx_path=/sys/class/net/$interface/statistics/rx_bytes
tx_path=/sys/class/net/$interface/statistics/tx_bytes
[ -r "$rx_path" ] && [ -r "$tx_path" ] || { echo "interface counters unavailable" >&2; exit 4; }
printf '%s\t%s\t%s\n' "$interface" "$(cat "$rx_path")" "$(cat "$tx_path")"
"""

@dataclass(frozen=True)
class VpsConfig:
    enabled: bool
    ssh_host: str
    interface: str
    poll_seconds: int
    billing_cycle_start_day: int


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def billing_cycle_start_epoch(day: int, now: float | None = None) -> float:
    """Return the local-time billing-cycle start, including short months."""
    current = datetime.fromtimestamp(now or time.time()).astimezone()
    current_day = min(day, calendar.monthrange(current.year, current.month)[1])
    start = current.replace(day=current_day, hour=0, minute=0, second=0, microsecond=0)
    if current >= start:
        return start.timestamp()
    year, month = current.year, current.month - 1
    if month == 0:
        year, month = year - 1, 12
    prior_day = min(day, calendar.monthrange(year, month)[1])
    return current.replace(year=year, month=month, day=prior_day, hour=0, minute=0, second=0, microsecond=0).timestamp()


def read_vps_counters(config: VpsConfig) -> dict[str, Any]:
    """Read one counter snapshot through the user's existing SSH host alias."""
    if not HOST_ALIAS_RE.fullmatch(config.ssh_host):
        raise ValueError("[vps] ssh_host 必须是 ssh config 中的主机别名")
    if config.interface != "auto" and not INTERFACE_RE.fullmatch(config.interface):
        raise ValueError("[vps] interface 只能是 auto 或合法网卡名")
    command = [
        "/usr/bin/ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "ControlMaster=no",
        "-o", "ControlPersist=no",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        config.ssh_host,
        "/bin/sh", "-s", "--", config.interface,
    ]
    try:
        completed = subprocess.run(
            command,
            input=REMOTE_COUNTER_SCRIPT,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"VPS SSH 无法完成：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        raise RuntimeError(f"VPS SSH 退出码 {completed.returncode}：{detail[:300]}")
    for line in completed.stdout.splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.:-]+)\t(\d+)\t(\d+)", line.strip())
        if match:
            epoch = time.time()
            return {
                "timestamp": iso_now(epoch),
                "epoch": epoch,
                "interface": match.group(1),
                "in_bytes": int(match.group(2)),
                "out_bytes": int(match.group(3)),
            }
    raise RuntimeError("VPS SSH 未返回可识别的网卡计数")


class VpsCounterTracker:
    """Turn VPS interface counters into non-overlapping polling deltas."""

    def __init__(self, previous: dict[str, Any] | None = None) -> None:
        self.previous = previous

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        prior = self.previous
        same_interface = prior is not None and prior.get("interface") == raw["interface"]
        in_delta = raw["in_bytes"] - int(prior["in_bytes"]) if same_interface and raw["in_bytes"] >= int(prior["in_bytes"]) else 0
        out_delta = raw["out_bytes"] - int(prior["out_bytes"]) if same_interface and raw["out_bytes"] >= int(prior["out_bytes"]) else 0
        try:
            interval_started_epoch = float(prior["epoch"]) if same_interface else None
        except (KeyError, TypeError, ValueError):
            interval_started_epoch = None
        self.previous = {
            "interface": raw["interface"],
            "in_bytes": int(raw["in_bytes"]),
            "out_bytes": int(raw["out_bytes"]),
            "epoch": float(raw["epoch"]),
        }
        return {
            "schema": VPS_SAMPLE_SCHEMA,
            "timestamp": raw["timestamp"],
            "epoch": raw["epoch"],
            "interface": raw["interface"],
            "in_bytes": in_delta,
            "out_bytes": out_delta,
            "interval_started_epoch": interval_started_epoch,
        }

    def serialize(self) -> dict[str, Any]:
        return {"schema": VPS_BASELINE_SCHEMA, "counter": self.previous}


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def iter_vps_samples(state_dir: Path) -> Iterable[dict[str, Any]]:
    for path in sorted(state_dir.glob("vps_samples*.jsonl"), key=lambda item: item.stat().st_mtime):
        yield from _iter_jsonl(path)


def sum_vps_samples(samples: Iterable[dict[str, Any]], since: float) -> dict[str, int]:
    result = {"in_bytes": 0, "out_bytes": 0}
    for sample in samples:
        if sample.get("schema") not in SUPPORTED_VPS_SAMPLE_SCHEMAS or float(sample.get("epoch", 0)) < since:
            continue
        result["in_bytes"] += int(sample.get("in_bytes", 0))
        result["out_bytes"] += int(sample.get("out_bytes", 0))
    result["total_bytes"] = result["in_bytes"] + result["out_bytes"]
    return result


class VpsMonitor:
    """Own the VPS polling schedule, baseline, rolling records, and bill total."""

    def __init__(
        self,
        config: VpsConfig,
        state_dir: Path,
        state: Any,
        reader: Callable[[VpsConfig], dict[str, Any]] = read_vps_counters,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.log_state = state
        self.reader = reader
        self.tracker = self._load_tracker()
        self.next_poll_at = 0.0
        self.public_state = self._initial_public_state()

    def _load_tracker(self) -> VpsCounterTracker:
        path = self.state_dir / "vps_counter_baseline.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return VpsCounterTracker()
        counter = payload.get("counter") if payload.get("schema") in {1, VPS_BASELINE_SCHEMA} else None
        if not isinstance(counter, dict):
            return VpsCounterTracker()
        try:
            restored = {
                "interface": str(counter["interface"]),
                "in_bytes": int(counter["in_bytes"]),
                "out_bytes": int(counter["out_bytes"]),
            }
            if payload.get("schema") == VPS_BASELINE_SCHEMA:
                restored["epoch"] = float(counter["epoch"])
            return VpsCounterTracker(restored)
        except (KeyError, TypeError, ValueError):
            return VpsCounterTracker()

    def _cycle(self, now: float) -> dict[str, Any]:
        start = billing_cycle_start_epoch(self.config.billing_cycle_start_day, now)
        eligible = [sample for sample in iter_vps_samples(self.state_dir) if sample.get("schema") in SUPPORTED_VPS_SAMPLE_SCHEMAS and float(sample.get("epoch", 0)) >= start]
        totals = sum_vps_samples(eligible, start)
        coverage_started_at = min((float(sample["epoch"]) for sample in eligible), default=None)
        return {
            "started_at": iso_now(start),
            "coverage_started_at": iso_now(coverage_started_at) if coverage_started_at is not None else None,
            **totals,
        }

    def _initial_public_state(self) -> dict[str, Any]:
        now = time.time()
        if not self.config.enabled:
            return {"enabled": False, "status": "disabled", "ssh_host": self.config.ssh_host, "cycle": self._cycle(now)}
        latest = None
        for record in iter_vps_samples(self.state_dir):
            if record.get("schema") in SUPPORTED_VPS_SAMPLE_SCHEMAS:
                latest = record
        return {
            "enabled": True,
            "status": "waiting" if latest is None else "ok",
            "ssh_host": self.config.ssh_host,
            "updated_at": latest.get("timestamp") if latest else None,
            "interface": latest.get("interface") if latest else None,
            "last_sample": latest,
            "cycle": self._cycle(now),
        }

    def _save_tracker(self) -> None:
        temporary = self.state_dir / ".vps_counter_baseline.json.tmp"
        temporary.write_text(json.dumps(self.tracker.serialize(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_dir / "vps_counter_baseline.json")

    def _append_sample(self, sample: dict[str, Any]) -> None:
        self._append_record("vps_samples", sample)

    def _append_record(self, prefix: str, record: dict[str, Any]) -> None:
        path = self.state_dir / f"{prefix}.jsonl"
        if path.exists() and path.stat().st_size >= self.log_state.max_log_bytes:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"{prefix}-{timestamp}.jsonl"))
            archives = sorted(self.state_dir.glob(f"{prefix}-*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
            for stale in archives[self.log_state.backups:]:
                stale.unlink(missing_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def maybe_poll(self, now: float | None = None, force: bool = False) -> dict[str, Any]:
        current = now or time.time()
        if not self.config.enabled:
            self.public_state = {"enabled": False, "status": "disabled", "ssh_host": self.config.ssh_host, "cycle": self._cycle(current)}
            return self.public_state
        if force or current >= self.next_poll_at:
            self.next_poll_at = current + self.config.poll_seconds
            try:
                raw = self.reader(self.config)
                was_uninitialized = self.tracker.previous is None
                sample = self.tracker.apply(raw)
                self._save_tracker()  # A crash may lose an interval, never replay one.
                self._append_sample(sample)
                self.public_state = {
                    "enabled": True,
                    "status": "baseline" if was_uninitialized else "ok",
                    "ssh_host": self.config.ssh_host,
                    "updated_at": sample["timestamp"],
                    "interface": sample["interface"],
                    "last_sample": sample,
                    "cycle": self._cycle(float(sample["epoch"])),
                }
            except Exception as exc:
                previous = dict(self.public_state)
                previous.update({
                    "enabled": True,
                    "status": "error",
                    "error": str(exc)[:500],
                    "cycle": self._cycle(current),
                })
                self.public_state = previous
        return self.public_state
