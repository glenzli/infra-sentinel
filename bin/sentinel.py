#!/usr/bin/env python3
"""Local process attribution, alerts, and menu-bar state for Traffic Sentinel."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import sys
import time
import tomllib
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from codex_activity import CodexActivityConfig, CodexActivityMeter, drain_hook_inbox
from proxy_segments import ProxyCycleMeter, ProxySegmentTracker, load_tracker as load_proxy_tracker, save_tracker as save_proxy_tracker
from session import SessionMeter, consume_reset_request
from traffic_estimation import TrafficEstimationConfig
from vps import VpsConfig, VpsMonitor, billing_cycle_start_epoch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
FALLBACK_CONFIG = PROJECT_ROOT / "config.example.toml"
STATE_DIRECTORY_ENV = "CODEX_TRAFFIC_SENTINEL_STATE_DIR"
PARENT_PROCESS_ENV = "CODEX_TRAFFIC_SENTINEL_PARENT_PID"
SAMPLE_SCHEMA = 4
COUNTER_BASELINE_SCHEMA = 4
# Version 2 deliberately starts a fresh cycle after replacing the old
# all-sockets proxy comparison with interface-scoped accounting.
LOCAL_CYCLE_SCHEMA = 2
GROUP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True)
class MonitorConfig:
    sample_seconds: int
    warning_window_seconds: int
    warning_bytes: int
    critical_window_seconds: int
    critical_bytes: int
    alert_group: str


@dataclass(frozen=True)
class GroupConfig:
    id: str
    label: str
    role: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class StateConfig:
    max_log_bytes: int
    backups: int


@dataclass(frozen=True)
class Config:
    monitor: MonitorConfig
    groups: tuple[GroupConfig, ...]
    codex_activity: CodexActivityConfig
    state: StateConfig
    vps: VpsConfig
    estimation: TrafficEstimationConfig
    state_dir: Path

    def group(self, group_id: str) -> GroupConfig:
        for group in self.groups:
            if group.id == group_id:
                return group
        raise KeyError(group_id)


def _parse_groups(raw: dict[str, Any]) -> tuple[GroupConfig, ...]:
    configured = raw.get("process_groups", [])
    parsed: list[GroupConfig] = []
    if isinstance(configured, list):
        entries = configured
    elif isinstance(configured, dict):
        # Retain read compatibility with the original two-table configuration.
        entries = [
            {
                "id": key,
                "label": "Codex" if key == "codex" else ("本地代理" if key == "proxy" else key),
                "role": "attribution" if key == "codex" else "observer",
                "patterns": value,
            }
            for key, value in configured.items()
        ]
    else:
        raise ValueError("process_groups 必须是 [[process_groups]] 列表")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("每个 [[process_groups]] 必须是一个表")
        group_id = str(entry.get("id", ""))
        label = str(entry.get("label", group_id)).strip()
        role = str(entry.get("role", "attribution")).strip()
        raw_patterns = entry.get("patterns", ())
        if not GROUP_ID_RE.fullmatch(group_id):
            raise ValueError("进程组 id 只能使用小写字母、数字、_、-")
        if not label:
            raise ValueError(f"进程组 {group_id} 的 label 不能为空")
        if role not in {"attribution", "observer"}:
            raise ValueError(f"进程组 {group_id} 的 role 必须是 attribution 或 observer")
        if not isinstance(raw_patterns, list) or not raw_patterns or not all(str(item).strip() for item in raw_patterns):
            raise ValueError(f"进程组 {group_id} 至少要有一个非空 patterns 项")
        if any(group.id == group_id for group in parsed):
            raise ValueError(f"进程组 id 重复：{group_id}")
        parsed.append(GroupConfig(group_id, label, role, tuple(str(item) for item in raw_patterns)))
    if not parsed:
        raise ValueError("至少需要一个 [[process_groups]]")
    if not any(group.role == "attribution" for group in parsed):
        raise ValueError("至少需要一个 role = attribution 的进程组")
    return tuple(parsed)


def read_config(path: Path | None) -> Config:
    selected = path or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else FALLBACK_CONFIG)
    with selected.open("rb") as handle:
        raw = tomllib.load(handle)

    monitor_raw = raw.get("monitor", {})
    monitor = MonitorConfig(
        sample_seconds=int(monitor_raw.get("sample_seconds", 5)),
        warning_window_seconds=int(monitor_raw.get("warning_window_seconds", 300)),
        warning_bytes=int(monitor_raw.get("warning_bytes", 268_435_456)),
        critical_window_seconds=int(monitor_raw.get("critical_window_seconds", 600)),
        critical_bytes=int(monitor_raw.get("critical_bytes", 1_073_741_824)),
        alert_group=str(monitor_raw.get("alert_group", "codex")),
    )
    positive_monitor_values = asdict(monitor)
    positive_monitor_values.pop("alert_group")
    if min(positive_monitor_values.values()) <= 0:
        raise ValueError("所有 [monitor] 数值都必须大于 0")

    groups = _parse_groups(raw)
    attribution_ids = {group.id for group in groups if group.role == "attribution"}
    if monitor.alert_group not in attribution_ids:
        raise ValueError("[monitor] alert_group 必须指向一个 attribution 进程组")

    estimation_raw = raw.get("estimation", {})
    if not isinstance(estimation_raw, dict):
        raise ValueError("[estimation] 必须是一个表")
    # Read the old reference key once so existing App installations continue
    # monitoring the configured proxy before the on-launch migration runs.
    legacy_reconciliation = raw.get("reconciliation", {})
    legacy_reference = legacy_reconciliation.get("reference_group", "proxy") if isinstance(legacy_reconciliation, dict) else "proxy"
    proxy_group = str(estimation_raw.get("proxy_group", legacy_reference)).strip() or None
    estimation = TrafficEstimationConfig(
        proxy_group=proxy_group,
        vps_billing_legs=float(estimation_raw.get("vps_billing_legs", 2.0)),
        link_overhead_ratio=float(estimation_raw.get("link_overhead_ratio", 0.20)),
    )
    if proxy_group is not None:
        matched_proxy = next((group for group in groups if group.id == proxy_group), None)
        if matched_proxy is None or matched_proxy.role != "observer":
            raise ValueError("[estimation] proxy_group 必须指向一个 observer 进程组")
    if estimation.vps_billing_legs <= 0:
        raise ValueError("[estimation] vps_billing_legs 必须大于 0")
    if estimation.link_overhead_ratio < 0:
        raise ValueError("[estimation] link_overhead_ratio 不能小于 0")

    state_raw = raw.get("state", {})
    state = StateConfig(
        max_log_bytes=int(state_raw.get("max_log_bytes", 10 * 1024 * 1024)),
        backups=int(state_raw.get("backups", 5)),
    )
    if state.max_log_bytes <= 0 or state.backups < 1:
        raise ValueError("[state] max_log_bytes 必须大于 0，backups 至少为 1")

    vps_raw = raw.get("vps", {})
    vps = VpsConfig(
        enabled=bool(vps_raw.get("enabled", False)),
        ssh_host=str(vps_raw.get("ssh_host", "")),
        interface=str(vps_raw.get("interface", "auto")),
        poll_seconds=int(vps_raw.get("poll_seconds", 300)),
        billing_cycle_start_day=int(vps_raw.get("billing_cycle_start_day", 1)),
    )
    if vps.poll_seconds < 30:
        raise ValueError("[vps] poll_seconds 至少为 30 秒")
    if not 1 <= vps.billing_cycle_start_day <= 31:
        raise ValueError("[vps] billing_cycle_start_day 必须在 1 到 31 之间")
    if vps.enabled and not vps.ssh_host:
        raise ValueError("启用 VPS 监控时，[vps] ssh_host 不能为空")

    activity_raw = raw.get("codex_activity", {})
    if not isinstance(activity_raw, dict):
        raise ValueError("[codex_activity] 必须是一个表")
    codex_activity = CodexActivityConfig(
        enabled=bool(activity_raw.get("enabled", True)),
        process_group=str(activity_raw.get("process_group", monitor.alert_group)),
        warning_active_subagents=int(activity_raw.get("warning_active_subagents", 4)),
        warning_total_subagents=int(activity_raw.get("warning_total_subagents", 10)),
    )
    activity_group = next((group for group in groups if group.id == codex_activity.process_group), None)
    if activity_group is None or activity_group.role != "attribution":
        raise ValueError("[codex_activity] process_group 必须指向一个 attribution 进程组")
    if codex_activity.warning_active_subagents < 1 or codex_activity.warning_total_subagents < 1:
        raise ValueError("[codex_activity] 子 Agent 告警阈值必须至少为 1")

    configured_state_directory = os.environ.get(STATE_DIRECTORY_ENV)
    state_directory = Path(configured_state_directory).expanduser() if configured_state_directory else selected.parent / "state"
    return Config(
        monitor=monitor,
        groups=groups,
        codex_activity=codex_activity,
        state=state,
        vps=vps,
        estimation=estimation,
        state_dir=state_directory,
    )


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or time.time(), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def find_column(fieldnames: Iterable[str] | None, aliases: set[str]) -> str | None:
    return next((fieldname for fieldname in fieldnames or () if normalize_header(fieldname) in aliases), None)


def find_column_index(fieldnames: Iterable[str], aliases: set[str]) -> int | None:
    return next((index for index, fieldname in enumerate(fieldnames) if normalize_header(fieldname) in aliases), None)


def parse_counter(value: str | None) -> int:
    """Parse raw nettop counters, tolerating a few older nettop spellings."""
    if value is None:
        return 0
    clean = value.strip().replace(",", "").lstrip("+")
    if not clean or clean == "-":
        return 0
    try:
        return int(float(clean))
    except ValueError:
        match = re.fullmatch(r"([0-9.]+)\s*([kmgt]?i?b?)", clean.casefold())
        if not match:
            return 0
        multipliers = {"": 1, "b": 1, "k": 1000, "kb": 1000, "kib": 1024,
                       "m": 1000**2, "mb": 1000**2, "mib": 1024**2,
                       "g": 1000**3, "gb": 1000**3, "gib": 1024**3,
                       "t": 1000**4, "tb": 1000**4, "tib": 1024**4}
        return int(float(match.group(1)) * multipliers.get(match.group(2), 1))


def _parse_nettop_sections(output: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return process summaries plus socket rows associated with their owner.

    macOS's non-``-P`` CSV format emits a process summary first, followed by
    its socket rows.  The latter do not repeat the process name, so retaining
    the immediately preceding summary is essential for proxy attribution.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    name_aliases = {"processname", "process", "name", "procname", "proc", "command"}
    down_aliases = {"bytesin", "rxbytes", "rxbyte", "inbytes", "bytesreceived"}
    up_aliases = {"bytesout", "txbytes", "txbyte", "outbytes", "bytessent"}

    def supported_header(line: str) -> bool:
        fields = next(csv.reader([line]), [])
        unnamed_process = len(fields) > 1 and not fields[1].strip()
        return (find_column(fields, name_aliases) is not None or unnamed_process) and find_column(fields, down_aliases) is not None and find_column(fields, up_aliases) is not None

    header_index = next((index for index, line in enumerate(lines) if supported_header(line)), None)
    if header_index is None:
        candidates = " | ".join([line[:240] for line in lines if "," in line][:3]) or "无 CSV 行"
        raise ValueError(f"未能在 nettop CSV 中找到进程和字节计数列（候选表头：{candidates}）")
    headers = next(csv.reader([lines[header_index]]), [])
    name_column = find_column_index(headers, name_aliases)
    if name_column is None and len(headers) > 1 and not headers[1].strip():
        name_column = 1
    pid_column = find_column_index(headers, {"pid", "processid"})
    interface_column = find_column_index(headers, {"interface", "ifname", "networkinterface"})
    down_column = find_column_index(headers, down_aliases)
    up_column = find_column_index(headers, up_aliases)
    if name_column is None or down_column is None or up_column is None:
        raise ValueError(f"nettop CSV 缺少进程名或收发字节列（得到：{', '.join(headers)}）")

    def at(values: list[str], index: int | None) -> str:
        return values[index] if index is not None and index < len(values) else ""

    blank_process_column = name_column == 1 and len(headers) > 1 and not headers[1].strip()
    rows: list[dict[str, Any]] = []
    connections: list[dict[str, Any]] = []
    current_owner: tuple[str, int | None] | None = None
    for values in csv.reader(lines[header_index + 1:]):
        raw_name = at(values, name_column).strip()
        if not raw_name:
            continue
        name, raw_pid = raw_name, at(values, pid_column).strip()
        embedded = None
        if not raw_pid and blank_process_column:
            embedded = re.fullmatch(r"(.+)\.(\d+)", raw_name)
            if embedded:
                name, raw_pid = embedded.groups()
        # In the detailed macOS format a `tcp4 ...` / `udp6 ...` row belongs
        # to the preceding `process.pid` summary.  It is not a process itself.
        interface = at(values, interface_column).strip()
        if blank_process_column and embedded is None:
            if current_owner is not None and interface and re.match(r"(?:tcp|udp)\d", raw_name, re.IGNORECASE):
                connections.append({
                    "name": current_owner[0],
                    "pid": current_owner[1],
                    "interface": interface,
                    "connection": raw_name,
                    "down_bytes": parse_counter(at(values, down_column)),
                    "up_bytes": parse_counter(at(values, up_column)),
                })
            continue
        pid_match = re.search(r"\d+", raw_pid)
        pid = int(pid_match.group()) if pid_match else None
        rows.append({
            "name": name,
            "pid": pid,
            "down_bytes": parse_counter(at(values, down_column)),
            "up_bytes": parse_counter(at(values, up_column)),
        })
        if blank_process_column:
            current_owner = (name, pid)
    return rows, connections


def parse_nettop_csv(output: str) -> list[dict[str, Any]]:
    """Extract process summary rows without assuming one macOS header version."""
    return _parse_nettop_sections(output)[0]


def parse_nettop_connections_csv(output: str) -> list[dict[str, Any]]:
    """Extract detailed socket counters, with their owning process restored."""
    return _parse_nettop_sections(output)[1]


def matches_process(name: str, patterns: tuple[str, ...]) -> bool:
    candidate = name.casefold()
    return any(pattern.casefold() in candidate for pattern in patterns)


def aggregate_groups(rows: Iterable[dict[str, Any]], groups: Iterable[GroupConfig]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Assign each process to its first matching group so it cannot be counted twice."""
    configured = tuple(groups)
    by_group: dict[str, dict[tuple[int | None, str], dict[str, Any]]] = {group.id: {} for group in configured}
    for row in rows:
        group = next((item for item in configured if matches_process(row["name"], item.patterns)), None)
        if group is None:
            continue
        key = (row["pid"], row["name"])
        process = by_group[group.id].setdefault(key, {"pid": row["pid"], "name": row["name"], "up_bytes": 0, "down_bytes": 0})
        process["up_bytes"] += int(row["up_bytes"])
        process["down_bytes"] += int(row["down_bytes"])
    totals: dict[str, dict[str, Any]] = {}
    processes: dict[str, list[dict[str, Any]]] = {}
    for group in configured:
        group_processes = sorted(by_group[group.id].values(), key=lambda item: item["up_bytes"] + item["down_bytes"], reverse=True)
        processes[group.id] = group_processes
        totals[group.id] = {
            "label": group.label,
            "role": group.role,
            "up_bytes": sum(item["up_bytes"] for item in group_processes),
            "down_bytes": sum(item["down_bytes"] for item in group_processes),
        }
    return totals, processes


def collect_proxy_segment_counters(proxy_processes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read non-overlapping proxy totals for macOS interface classes.

    Per-socket snapshots lose short connections when they close before the
    next read. `nettop -t` instead filters the proxy process summary itself;
    three restricted reads run in parallel so the monitor still has roughly a
    five-second cadence.
    """
    pids = sorted({int(process["pid"]) for process in proxy_processes if isinstance(process.get("pid"), int)})
    if not pids:
        return []
    interface_types = {"external": "external", "loopback": "loopback", "other": "undefined"}
    running: dict[str, subprocess.Popen[str]] = {}
    try:
        for category, interface_type in interface_types.items():
            command = ["/usr/bin/nettop"]
            for pid in pids:
                command.extend(["-p", str(pid)])
            command.extend(["-P", "-t", interface_type, "-x", "-n", "-s", "1", "-L", "1"])
            running[category] = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        for process in running.values():
            process.kill()
        return []

    counters: list[dict[str, Any]] = []
    for category, process in running.items():
        try:
            stdout, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            continue
        if process.returncode != 0:
            continue
        for row in parse_nettop_csv(stdout):
            if row.get("pid") not in pids:
                continue
            counters.append({"category": category, **row})
    return counters


def collect_raw_counters(config: Config) -> dict[str, Any]:
    # Reserve one second for the proxy-only detail query below. The usual
    # configuration still has an approximately `sample_seconds` cadence.
    summary_delay = max(1, config.monitor.sample_seconds - 1)
    command = ["/usr/bin/nettop", "-P", "-x", "-n", "-s", str(summary_delay), "-L", "1"]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=config.monitor.sample_seconds + 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"nettop 无法运行：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        raise RuntimeError(f"nettop 退出码 {completed.returncode}：{detail[:400]}")
    totals, processes = aggregate_groups(parse_nettop_csv(completed.stdout), config.groups)
    reference_id = config.estimation.proxy_group
    proxy_segment_counters = collect_proxy_segment_counters(processes.get(reference_id, [])) if reference_id is not None else []
    epoch = time.time()
    return {
        "timestamp": iso_now(epoch),
        "epoch": epoch,
        "sample_seconds": config.monitor.sample_seconds,
        "groups": totals,
        "processes": processes,
        "proxy_segment_counters": proxy_segment_counters,
    }


def counter_key(group: str, process: dict[str, Any]) -> tuple[str, int | None, str]:
    return group, process.get("pid"), str(process.get("name", ""))


class ProcessDeltaTracker:
    """Turn monotonic nettop counters into non-overlapping, per-group deltas."""

    def __init__(self, previous: dict[tuple[str, int | None, str], dict[str, int]] | None = None, previous_epoch: float | None = None) -> None:
        self.previous = previous or {}
        self.previous_epoch = previous_epoch

    def apply(self, raw_sample: dict[str, Any]) -> dict[str, Any]:
        next_previous: dict[tuple[str, int | None, str], dict[str, int]] = {}
        delta_processes: dict[str, list[dict[str, Any]]] = {}
        delta_groups: dict[str, dict[str, Any]] = {}
        for group_id, raw_processes in raw_sample["processes"].items():
            group_deltas: list[dict[str, Any]] = []
            for raw_process in raw_processes:
                key = counter_key(group_id, raw_process)
                current_up, current_down = int(raw_process["up_bytes"]), int(raw_process["down_bytes"])
                prior = self.previous.get(key)
                up_delta = current_up - prior["up_bytes"] if prior and current_up >= prior["up_bytes"] else 0
                down_delta = current_down - prior["down_bytes"] if prior and current_down >= prior["down_bytes"] else 0
                group_deltas.append({"pid": raw_process.get("pid"), "name": raw_process["name"], "up_bytes": up_delta, "down_bytes": down_delta})
                next_previous[key] = {"up_bytes": current_up, "down_bytes": current_down}
            delta_processes[group_id] = sorted(group_deltas, key=lambda item: item["up_bytes"] + item["down_bytes"], reverse=True)
            metadata = raw_sample["groups"].get(group_id, {})
            delta_groups[group_id] = {
                "label": metadata.get("label", group_id),
                "role": metadata.get("role", "attribution"),
                "up_bytes": sum(item["up_bytes"] for item in group_deltas),
                "down_bytes": sum(item["down_bytes"] for item in group_deltas),
            }
        current_epoch = float(raw_sample["epoch"])
        observed_seconds = current_epoch - self.previous_epoch if self.previous_epoch is not None else float(raw_sample["sample_seconds"])
        self.previous = next_previous
        self.previous_epoch = current_epoch
        return {"schema": SAMPLE_SCHEMA, "timestamp": raw_sample["timestamp"], "epoch": raw_sample["epoch"], "sample_seconds": raw_sample["sample_seconds"], "observed_seconds": max(observed_seconds, 0.001), "groups": delta_groups, "processes": delta_processes}

    def serialize(self) -> dict[str, Any]:
        processes: dict[str, list[dict[str, Any]]] = {}
        for (group, pid, name), counters in self.previous.items():
            processes.setdefault(group, []).append({"pid": pid, "name": name, "up_bytes": counters["up_bytes"], "down_bytes": counters["down_bytes"]})
        return {"schema": COUNTER_BASELINE_SCHEMA, "epoch": self.previous_epoch, "processes": processes}


def load_delta_tracker(config: Config) -> ProcessDeltaTracker:
    path = config.state_dir / "counter_baseline.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ProcessDeltaTracker()
    if payload.get("schema") != COUNTER_BASELINE_SCHEMA:
        return ProcessDeltaTracker()
    known_groups = {group.id for group in config.groups}
    previous: dict[tuple[str, int | None, str], dict[str, int]] = {}
    for group, entries in payload.get("processes", {}).items():
        if group not in known_groups or not isinstance(entries, list):
            continue
        for process in entries:
            if not isinstance(process, dict) or not isinstance(process.get("name"), str):
                continue
            try:
                previous[counter_key(group, process)] = {"up_bytes": int(process["up_bytes"]), "down_bytes": int(process["down_bytes"])}
            except (KeyError, TypeError, ValueError):
                continue
    try:
        previous_epoch = float(payload["epoch"])
    except (KeyError, TypeError, ValueError):
        previous_epoch = None
    return ProcessDeltaTracker(previous, previous_epoch)


def save_delta_tracker(config: Config, tracker: ProcessDeltaTracker) -> None:
    temporary = config.state_dir / ".counter_baseline.json.tmp"
    temporary.write_text(json.dumps(tracker.serialize(), ensure_ascii=False), encoding="utf-8")
    temporary.replace(config.state_dir / "counter_baseline.json")


def ensure_state_dir(config: Config) -> None:
    (config.state_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "codex-hook-inbox").mkdir(parents=True, exist_ok=True)


def acquire_watch_lock(config: Config) -> Any | None:
    handle = (config.state_dir / "sentinel.lock").open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def parent_process_exited() -> bool:
    raw_parent = os.environ.get(PARENT_PROCESS_ENV)
    if not raw_parent:
        return False
    try:
        return os.getppid() != int(raw_parent)
    except ValueError:
        return False


def rotate_before_append(path: Path, state: StateConfig) -> None:
    if not path.exists() or path.stat().st_size < state.max_log_bytes:
        return
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = path.with_name(f"{path.stem}-{timestamp}{path.suffix}")
    path.rename(archived)
    archives = sorted(path.parent.glob(f"{path.stem}-*{path.suffix}"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in archives[state.backups:]:
        stale.unlink(missing_ok=True)


def append_jsonl(path: Path, record: dict[str, Any], state: StateConfig) -> None:
    rotate_before_append(path, state)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


class LocalCycleMeter:
    """Persist local per-group totals for the VPS billing period without log rescans."""

    def __init__(self, config: Config, now: float | None = None) -> None:
        self.config = config
        self.cycle_start_epoch = billing_cycle_start_epoch(config.vps.billing_cycle_start_day, now)
        self.coverage_started_epoch: float | None = None
        self.totals: dict[str, dict[str, int]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self.config.state_dir / "local_cycle.json"

    def _empty_totals(self) -> dict[str, dict[str, int]]:
        return {group.id: {"up_bytes": 0, "down_bytes": 0} for group in self.config.groups}

    def _load(self) -> None:
        self.totals = self._empty_totals()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("schema") != LOCAL_CYCLE_SCHEMA or int(payload.get("cycle_start_epoch", -1)) != int(self.cycle_start_epoch):
            return
        try:
            coverage = payload.get("coverage_started_epoch")
            self.coverage_started_epoch = float(coverage) if coverage is not None else None
        except (TypeError, ValueError):
            self.coverage_started_epoch = None
        saved_totals = payload.get("groups", {})
        for group in self.config.groups:
            item = saved_totals.get(group.id, {}) if isinstance(saved_totals, dict) else {}
            try:
                self.totals[group.id] = {"up_bytes": int(item.get("up_bytes", 0)), "down_bytes": int(item.get("down_bytes", 0))}
            except (AttributeError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        payload = {
            "schema": LOCAL_CYCLE_SCHEMA,
            "cycle_start_epoch": self.cycle_start_epoch,
            "coverage_started_epoch": self.coverage_started_epoch,
            "groups": self.totals,
        }
        temporary = self.config.state_dir / ".local_cycle.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def record(self, sample: dict[str, Any]) -> None:
        current_cycle_start = billing_cycle_start_epoch(self.config.vps.billing_cycle_start_day, float(sample["epoch"]))
        if int(current_cycle_start) != int(self.cycle_start_epoch):
            self.cycle_start_epoch = current_cycle_start
            self.coverage_started_epoch = None
            self.totals = self._empty_totals()
        if self.coverage_started_epoch is None:
            self.coverage_started_epoch = float(sample["epoch"])
        for group in self.config.groups:
            traffic = traffic_for_group(sample, group.id)
            self.totals[group.id]["up_bytes"] += traffic["up_bytes"]
            self.totals[group.id]["down_bytes"] += traffic["down_bytes"]
        self._save()

    def snapshot(self) -> dict[str, Any]:
        groups = []
        for group in self.config.groups:
            traffic = self.totals[group.id]
            groups.append({"id": group.id, "label": group.label, "role": group.role, **traffic, "total_bytes": traffic["up_bytes"] + traffic["down_bytes"]})
        return {
            "started_at": iso_now(self.cycle_start_epoch),
            "coverage_started_at": iso_now(self.coverage_started_epoch) if self.coverage_started_epoch is not None else None,
            "coverage_started_epoch": self.coverage_started_epoch,
            "groups": groups,
        }


def load_recent_samples(config: Config, now: float) -> deque[dict[str, Any]]:
    cutoff = now - config.monitor.critical_window_seconds
    return deque(record for record in iter_jsonl(config.state_dir / "samples.jsonl") if record.get("schema") == SAMPLE_SCHEMA and isinstance(record.get("epoch"), (int, float)) and record["epoch"] >= cutoff)


def traffic_for_group(sample: dict[str, Any], group_id: str) -> dict[str, int]:
    group = sample.get("groups", {}).get(group_id, {})
    return {"up_bytes": int(group.get("up_bytes", 0)), "down_bytes": int(group.get("down_bytes", 0))}


def totals_for_window(samples: Iterable[dict[str, Any]], now: float, seconds: int, group_id: str) -> dict[str, int]:
    cutoff = now - seconds
    totals = {"up_bytes": 0, "down_bytes": 0}
    for sample in samples:
        if float(sample.get("epoch", 0)) >= cutoff:
            traffic = traffic_for_group(sample, group_id)
            totals["up_bytes"] += traffic["up_bytes"]
            totals["down_bytes"] += traffic["down_bytes"]
    return totals


class AlertEngine:
    ranks = {"none": 0, "warning": 1, "critical": 2}

    def __init__(self) -> None:
        self.level = "none"

    def evaluate(self, warning: dict[str, int], critical: dict[str, int], config: Config) -> tuple[str, str] | None:
        if critical["up_bytes"] + critical["down_bytes"] > config.monitor.critical_bytes:
            next_level = "critical"
        elif max(warning["up_bytes"], warning["down_bytes"]) > config.monitor.warning_bytes:
            next_level = "warning"
        else:
            next_level = "none"
        previous, self.level = self.level, next_level
        if previous == next_level:
            return None
        if next_level == "none":
            return "recovered", next_level
        if self.ranks[next_level] > self.ranks[previous]:
            return "alert", next_level
        return "deescalated", next_level


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return f"{number:.1f} TiB"


def busiest_attribution_group(sample: dict[str, Any], config: Config) -> dict[str, Any]:
    candidates = [group for group in config.groups if group.role == "attribution"]
    winner = max(candidates, key=lambda group: traffic_for_group(sample, group.id)["up_bytes"] + traffic_for_group(sample, group.id)["down_bytes"])
    traffic = traffic_for_group(sample, winner.id)
    return {"id": winner.id, "label": winner.label, **traffic}


def write_menubar_state(config: Config, sample: dict[str, Any], warning: dict[str, int], critical: dict[str, int], level: str, vps: dict[str, Any], local_cycle: dict[str, Any], proxy_segments: dict[str, Any], session: dict[str, Any], codex_activity: dict[str, Any]) -> None:
    groups = []
    for group in config.groups:
        traffic = traffic_for_group(sample, group.id)
        groups.append({"id": group.id, "label": group.label, "role": group.role, **traffic})
    payload = {
        "updated_at": sample["timestamp"],
        "level": level,
        "observed_seconds": sample["observed_seconds"],
        "groups": groups,
        "busiest_group": busiest_attribution_group(sample, config),
        "alert_group": {"id": config.monitor.alert_group, "label": config.group(config.monitor.alert_group).label},
        "warning_window": warning,
        "critical_window": critical,
        "vps": vps,
        "local_cycle": local_cycle,
        "proxy_segments": proxy_segments,
        "session": session,
        "codex_activity": codex_activity,
        "last_event": latest_delta_event(config.state_dir / "events.jsonl"),
    }
    temporary = config.state_dir / ".menubar.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(config.state_dir / "menubar.json")


def write_health_state(config: Config, status: str, message: str | None = None) -> None:
    payload = {"updated_at": iso_now(), "status": status}
    if message:
        payload["message"] = message[:500]
    temporary = config.state_dir / ".health.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(config.state_dir / "health.json")


def latest_jsonl(path: Path) -> dict[str, Any] | None:
    last = None
    for record in iter_jsonl(path):
        last = record
    return last


def latest_delta_event(path: Path) -> dict[str, Any] | None:
    last = None
    for record in iter_jsonl(path):
        if record.get("sample", {}).get("schema") == SAMPLE_SCHEMA:
            last = record
    return last


def notify(event_type: str, level: str, warning: dict[str, int], critical: dict[str, int], group_label: str) -> None:
    if event_type == "alert":
        title = f"{group_label} 流量严重告警" if level == "critical" else f"{group_label} 流量告警"
        message = f"10 分钟累计 {format_bytes(critical['up_bytes'] + critical['down_bytes'])}" if level == "critical" else f"5 分钟 ↑{format_bytes(warning['up_bytes'])} ↓{format_bytes(warning['down_bytes'])}"
    elif event_type == "deescalated":
        title, message = f"{group_label} 流量降级", "严重阈值已回落，仍处于警告范围。"
    else:
        title, message = f"{group_label} 流量恢复", "流量已回落到告警阈值以下。"
    try:
        subprocess.run(["/usr/bin/osascript", "-e", f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"], capture_output=True, text=True, timeout=10, check=False)
    except OSError:
        pass


def build_event(event_type: str, level: str, sample: dict[str, Any], warning: dict[str, int], critical: dict[str, int], alert_group: str) -> dict[str, Any]:
    return {"schema": 1, "id": uuid.uuid4().hex, "timestamp": iso_now(), "type": event_type, "level": level, "alert_group": alert_group, "sample": sample, "windows": {"warning": warning, "critical": critical}}


def configure_logger(config: Config) -> logging.Logger:
    logger = logging.getLogger("codex-traffic-sentinel")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(config.state_dir / "sentinel.log", maxBytes=config.state.max_log_bytes, backupCount=config.state.backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def handle_sample(config: Config, history: deque[dict[str, Any]], tracker: ProcessDeltaTracker, proxy_tracker: ProxySegmentTracker, alerts: AlertEngine, vps_monitor: VpsMonitor, local_cycle_meter: LocalCycleMeter, proxy_cycle_meter: ProxyCycleMeter, session_meter: SessionMeter, activity_meter: CodexActivityMeter, logger: logging.Logger) -> dict[str, Any]:
    raw_sample = collect_raw_counters(config)
    sample = tracker.apply(raw_sample)
    save_delta_tracker(config, tracker)
    hook_events = drain_hook_inbox(config.state_dir)
    if config.codex_activity.enabled:
        activity_meter.record(sample, hook_events)
    reference_id = config.estimation.proxy_group
    reference_group = config.group(reference_id) if reference_id is not None else None
    proxy_counters = [
        counter for counter in raw_sample["proxy_segment_counters"]
        if reference_group is not None and matches_process(counter["name"], reference_group.patterns)
    ]
    proxy_sample = proxy_tracker.apply(raw_sample["timestamp"], float(raw_sample["epoch"]), proxy_counters)
    save_proxy_tracker(config.state_dir, proxy_tracker)
    history.append(sample)
    cutoff = sample["epoch"] - config.monitor.critical_window_seconds
    while history and float(history[0].get("epoch", 0)) < cutoff:
        history.popleft()
    append_jsonl(config.state_dir / "samples.jsonl", sample, config.state)
    local_cycle_meter.record(sample)
    proxy_cycle_meter.record(proxy_sample, billing_cycle_start_epoch(config.vps.billing_cycle_start_day, float(sample["epoch"])))
    warning = totals_for_window(history, sample["epoch"], config.monitor.warning_window_seconds, config.monitor.alert_group)
    critical = totals_for_window(history, sample["epoch"], config.monitor.critical_window_seconds, config.monitor.alert_group)
    transition = alerts.evaluate(warning, critical, config)
    if transition:
        event_type, level = transition
        event = build_event(event_type, level, sample, warning, critical, config.monitor.alert_group)
        if event_type == "alert":
            try:
                from snapshot import create_snapshot
                event["snapshot_path"] = str(create_snapshot(config, event))
            except Exception as exc:
                event["snapshot_error"] = str(exc)
                logger.exception("生成证据快照失败")
        append_jsonl(config.state_dir / "events.jsonl", event, config.state)
        if os.environ.get("CODEX_TRAFFIC_SENTINEL_APP_NOTIFICATIONS") != "1":
            notify(event_type, level, warning, critical, config.group(config.monitor.alert_group).label)
        logger.warning("event=%s level=%s id=%s", event_type, level, event["id"])
    reset_request = consume_reset_request(config.state_dir)
    if reset_request is not None:
        session_meter.reset(float(sample["epoch"]), "manual")
        activity_meter.reset(float(sample["epoch"]))
        vps_state = vps_monitor.maybe_poll(sample["epoch"], force=True)
        session_meter.set_vps_baseline(vps_state)
        logger.info("dashboard session reset id=%s", reset_request["id"])
    elif session_meter.started_epoch is None:
        vps_state = vps_monitor.maybe_poll(sample["epoch"])
        session_meter.reset(float(sample["epoch"]), "automatic")
        activity_meter.reset(float(sample["epoch"]))
        session_meter.set_vps_baseline(vps_state)
    else:
        vps_state = vps_monitor.maybe_poll(sample["epoch"])
        session_meter.record(sample, proxy_sample, vps_state)
    labels = {group.id: group.label for group in config.groups}
    roles = {group.id: group.role for group in config.groups}
    write_menubar_state(
        config,
        sample,
        warning,
        critical,
        alerts.level,
        vps_state,
        local_cycle_meter.snapshot(),
        proxy_cycle_meter.snapshot(),
        session_meter.snapshot(
            labels,
            roles,
            config.vps.enabled,
            config.estimation,
        ),
        activity_meter.snapshot(float(sample["epoch"])),
    )
    write_health_state(config, "ok")
    return sample


def print_sample(sample: dict[str, Any], heading: str = "本周期增量") -> None:
    print(f"采样时间：{sample['timestamp']}（{heading}）")
    for group_id, traffic in sample["groups"].items():
        role = "项目" if traffic["role"] == "attribution" else "独立观察"
        print(f"{traffic['label']}（{role}） 发送 ↑ {format_bytes(traffic['up_bytes'])}  接收 ↓ {format_bytes(traffic['down_bytes'])}")
        for process in sample["processes"].get(group_id, []):
            print(f"  PID {process['pid'] or '?'}  {process['name']}: ↑{format_bytes(process['up_bytes'])} ↓{format_bytes(process['down_bytes'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="监控本机 AI 进程的 nettop 增量流量")
    parser.add_argument("--config", type=Path, help="TOML 配置文件；默认优先使用 config.toml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="采样一次并打印")
    mode.add_argument("--watch", action="store_true", help="持续采样、告警、写入状态")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        ensure_state_dir(config)
        logger = configure_logger(config)
        if args.once:
            print_sample(collect_raw_counters(config), "当前原始计数；不计入窗口")
            return 0
        logger.info("Sentinel started; local_interval=%ss vps_interval=%ss", config.monitor.sample_seconds, config.vps.poll_seconds)
        history, tracker, alerts = load_recent_samples(config, time.time()), load_delta_tracker(config), AlertEngine()
        proxy_tracker = load_proxy_tracker(config.state_dir)
        vps_monitor = VpsMonitor(config.vps, config.state_dir, config.state)
        local_cycle_meter = LocalCycleMeter(config)
        proxy_cycle_meter = ProxyCycleMeter(config.state_dir, config.state, billing_cycle_start_epoch(config.vps.billing_cycle_start_day))
        session_meter = SessionMeter(config.state_dir, tuple(group.id for group in config.groups))
        activity_meter = CodexActivityMeter(config.state_dir, config.codex_activity)
        activity_meter.align_started_epoch(session_meter.started_epoch)
        lock = acquire_watch_lock(config)
        if lock is None:
            print("错误：已有 Sentinel watcher 正在写入此状态目录", file=sys.stderr)
            return 3
        try:
            while True:
                if parent_process_exited():
                    logger.info("宿主 App 已退出，停止内置 Sentinel helper")
                    return 0
                started = time.monotonic()
                try:
                    handle_sample(config, history, tracker, proxy_tracker, alerts, vps_monitor, local_cycle_meter, proxy_cycle_meter, session_meter, activity_meter, logger)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.exception("采样失败：%s", exc)
                    write_health_state(config, "error", str(exc))
                remaining = config.monitor.sample_seconds - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
