#!/usr/bin/env python3
"""Mihomo domain attribution, alerts, and dashboard state for Traffic Sentinel."""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from typing import Any
import uuid

from mihomo_traffic import (
    MIHOMO_SAMPLE_SCHEMA,
    MihomoApiClient,
    MihomoTrafficTracker,
    classify_host,
    classify_route,
    collect_interval,
    load_tracker,
    save_tracker,
)
from sample_timing import annotate_sample_timing, sample_is_realtime
from session import SessionMeter, consume_reset_request
from traffic_estimation import TrafficEstimationConfig
from vps import VpsConfig, VpsMonitor
from xray_stats import XrayStatsConfig, XrayStatsMonitor


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
FALLBACK_CONFIG = PROJECT_ROOT / "config.example.toml"
STATE_DIRECTORY_ENV = "CODEX_TRAFFIC_SENTINEL_STATE_DIR"
PARENT_PROCESS_ENV = "CODEX_TRAFFIC_SENTINEL_PARENT_PID"
APP_NOTIFICATIONS_ENV = "CODEX_TRAFFIC_SENTINEL_APP_NOTIFICATIONS"
SAMPLE_SCHEMA = 5


@dataclass(frozen=True)
class MonitorConfig:
    sample_seconds: int
    warning_window_seconds: int
    warning_bytes: int
    critical_window_seconds: int
    critical_bytes: int


@dataclass(frozen=True)
class StateConfig:
    max_log_bytes: int
    backups: int


@dataclass(frozen=True)
class Config:
    monitor: MonitorConfig
    state: StateConfig
    vps: VpsConfig
    xray_stats: XrayStatsConfig
    estimation: TrafficEstimationConfig
    state_dir: Path


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
    )
    if min(asdict(monitor).values()) <= 0:
        raise ValueError("所有 [monitor] 数值都必须大于 0")
    if monitor.sample_seconds < 2:
        raise ValueError("[monitor] sample_seconds 至少为 2 秒")

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
        ssh_host=str(vps_raw.get("ssh_host", "")).strip(),
        interface=str(vps_raw.get("interface", "auto")).strip(),
        poll_seconds=int(vps_raw.get("poll_seconds", 300)),
        billing_cycle_start_day=int(vps_raw.get("billing_cycle_start_day", 1)),
    )
    if vps.poll_seconds < 30:
        raise ValueError("[vps] poll_seconds 至少为 30 秒")
    if not 1 <= vps.billing_cycle_start_day <= 31:
        raise ValueError("[vps] billing_cycle_start_day 必须在 1 到 31 之间")
    if vps.enabled and not vps.ssh_host:
        raise ValueError("启用 VPS 监控时，[vps] ssh_host 不能为空")

    xray_raw = raw.get("xray_stats", {})
    expected_users_raw = xray_raw.get("users", [])
    flagged_users_raw = xray_raw.get("flagged_users", [])
    if not isinstance(expected_users_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in expected_users_raw
    ):
        raise ValueError("[xray_stats] users 必须是非空字符串数组")
    if not isinstance(flagged_users_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in flagged_users_raw
    ):
        raise ValueError("[xray_stats] flagged_users 必须是非空字符串数组")
    expected_users = tuple(dict.fromkeys(item.strip() for item in expected_users_raw))
    flagged_users = tuple(dict.fromkeys(item.strip() for item in flagged_users_raw))
    if any(">>>" in item or "\n" in item or "\r" in item for item in (*expected_users, *flagged_users)):
        raise ValueError("[xray_stats] 用户标签不能包含 >>> 或换行")
    xray_stats = XrayStatsConfig(
        enabled=bool(xray_raw.get("enabled", False)),
        ssh_host=str(xray_raw.get("ssh_host", "")).strip() or vps.ssh_host,
        api_server=str(xray_raw.get("api_server", "127.0.0.1:10085")).strip(),
        binary_path=str(xray_raw.get("binary_path", "/usr/local/bin/xray")).strip(),
        poll_seconds=int(xray_raw.get("poll_seconds", vps.poll_seconds)),
        expected_users=expected_users,
        flagged_users=flagged_users,
    )
    if xray_stats.poll_seconds < 30:
        raise ValueError("[xray_stats] poll_seconds 至少为 30 秒")
    if xray_stats.enabled and not xray_stats.ssh_host:
        raise ValueError("启用 Xray 用户统计时，[xray_stats] ssh_host 或 [vps] ssh_host 不能为空")

    estimation_raw = raw.get("estimation", {})
    estimation = TrafficEstimationConfig(
        vps_billing_legs=float(estimation_raw.get("vps_billing_legs", 2.0)),
    )
    if estimation.vps_billing_legs <= 0:
        raise ValueError("[estimation] vps_billing_legs 必须大于 0")

    configured_state_directory = os.environ.get(STATE_DIRECTORY_ENV)
    state_dir = (
        Path(configured_state_directory).expanduser()
        if configured_state_directory
        else selected.parent / "state"
    )
    return Config(monitor, state, vps, xray_stats, estimation, state_dir)


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{int(value)} B"


def ensure_state_dir(config: Config) -> None:
    (config.state_dir / "snapshots").mkdir(parents=True, exist_ok=True)


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
    archives = sorted(
        path.parent.glob(f"{path.stem}-*{path.suffix}"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in archives[state.backups:]:
        stale.unlink(missing_ok=True)


def append_jsonl(path: Path, record: dict[str, Any], state: StateConfig) -> None:
    rotate_before_append(path, state)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def latest_jsonl(path: Path) -> dict[str, Any] | None:
    latest = None
    for record in iter_jsonl(path):
        latest = record
    return latest


def latest_delta_event(path: Path) -> dict[str, Any] | None:
    latest = None
    for record in iter_jsonl(path):
        sample = record.get("sample", {})
        if (
            sample.get("schema") == SAMPLE_SCHEMA
            and sample_is_realtime(sample)
        ):
            latest = record
    return latest


def load_recent_samples(config: Config, now: float) -> deque[dict[str, Any]]:
    cutoff = now - config.monitor.critical_window_seconds
    return deque(
        record
        for record in iter_jsonl(config.state_dir / "samples.jsonl")
        if record.get("schema") == SAMPLE_SCHEMA
        and isinstance(record.get("epoch"), (int, float))
        and record["epoch"] >= cutoff
    )


def totals_for_window(
    samples: Iterable[dict[str, Any]],
    now: float,
    seconds: int,
    expected_interval_seconds: float = 5.0,
) -> dict[str, int]:
    cutoff = now - seconds
    result = {"up_bytes": 0, "down_bytes": 0}
    for sample in samples:
        if float(sample.get("epoch", 0)) < cutoff:
            continue
        if not sample_is_realtime(sample, expected_interval_seconds):
            continue
        kernel = sample.get("kernel", {})
        result["up_bytes"] += int(kernel.get("up_bytes", 0))
        result["down_bytes"] += int(kernel.get("down_bytes", 0))
    return result


class AlertEngine:
    def __init__(self) -> None:
        self.level = "none"

    def evaluate(
        self,
        warning: dict[str, int],
        critical: dict[str, int],
        config: Config,
    ) -> tuple[str, str] | None:
        if critical["up_bytes"] + critical["down_bytes"] > config.monitor.critical_bytes:
            next_level = "critical"
        elif max(warning["up_bytes"], warning["down_bytes"]) > config.monitor.warning_bytes:
            next_level = "warning"
        else:
            next_level = "none"
        if next_level == self.level:
            return None
        previous = self.level
        self.level = next_level
        if next_level == "none":
            return ("recovered", next_level)
        if previous == "critical" and next_level == "warning":
            return ("deescalated", next_level)
        return ("alert", next_level)


def busiest_service(sample: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        service
        for service in sample.get("services", [])
        if isinstance(service, dict) and service.get("id") != "unattributed"
    ]
    if not candidates:
        kernel = sample.get("kernel", {})
        return {
            "id": "mihomo",
            "label": "Mihomo",
            "up_bytes": int(kernel.get("up_bytes", 0)),
            "down_bytes": int(kernel.get("down_bytes", 0)),
            "total_bytes": int(kernel.get("total_bytes", 0)),
        }
    return max(candidates, key=lambda item: int(item.get("total_bytes", 0)))


def build_event(
    event_type: str,
    level: str,
    sample: dict[str, Any],
    warning: dict[str, int],
    critical: dict[str, int],
) -> dict[str, Any]:
    return {
        "schema": 2,
        "id": uuid.uuid4().hex,
        "timestamp": iso_now(),
        "type": event_type,
        "level": level,
        "alert_group": "Mihomo",
        "sample": sample,
        "windows": {"warning": warning, "critical": critical},
    }


def notify(
    event_type: str,
    level: str,
    warning: dict[str, int],
    critical: dict[str, int],
) -> None:
    if os.environ.get(APP_NOTIFICATIONS_ENV) == "1":
        return
    if event_type == "recovered":
        title, body = "Traffic Sentinel", "Mihomo 流量已回落到阈值以下"
    elif level == "critical":
        title = "Traffic Sentinel 严重告警"
        body = f"10 分钟累计 {format_bytes(critical['up_bytes'] + critical['down_bytes'])}"
    else:
        title = "Traffic Sentinel 流量告警"
        body = f"5 分钟 ↑{format_bytes(warning['up_bytes'])} ↓{format_bytes(warning['down_bytes'])}"
    script = 'display notification (system attribute "TS_BODY") with title (system attribute "TS_TITLE")'
    environment = os.environ.copy()
    environment.update({"TS_TITLE": title, "TS_BODY": body})
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )


def write_menubar_state(
    config: Config,
    sample: dict[str, Any],
    warning: dict[str, int],
    critical: dict[str, int],
    level: str,
    vps: dict[str, Any],
    xray_stats: dict[str, Any],
    session: dict[str, Any],
) -> None:
    state = {
        "schema": 5,
        "updated_at": sample["timestamp"],
        "epoch": sample["epoch"],
        "observed_seconds": sample["observed_seconds"],
        "interval_kind": sample.get("interval_kind", "realtime"),
        "level": level,
        "busiest_service": busiest_service(sample),
        "alert_group": {"id": "mihomo", "label": "Mihomo"},
        "windows": {"warning": warning, "critical": critical},
        "mihomo": {
            "kernel": sample.get("kernel", {}),
            "routes": sample.get("routes", {}),
            "attribution": sample.get("attribution", {}),
            "active_connections": sample.get("active_connections", 0),
        },
        "vps": vps,
        "xray_stats": xray_stats,
        "session": session,
        "last_event": latest_delta_event(config.state_dir / "events.jsonl"),
    }
    temporary = config.state_dir / ".menubar.json.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(config.state_dir / "menubar.json")


def write_health_state(config: Config, status: str, message: str | None = None) -> None:
    payload = {"schema": 1, "status": status, "updated_at": iso_now()}
    if message:
        payload["message"] = message
    temporary = config.state_dir / ".health.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(config.state_dir / "health.json")


def configure_logger(config: Config) -> logging.Logger:
    logger = logging.getLogger("net-traffic-sentinel")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(
        config.state_dir / "sentinel.log",
        maxBytes=config.state.max_log_bytes,
        backupCount=config.state.backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def handle_sample(
    config: Config,
    history: deque[dict[str, Any]],
    client: MihomoApiClient,
    tracker: MihomoTrafficTracker,
    alerts: AlertEngine,
    vps_monitor: VpsMonitor,
    xray_monitor: XrayStatsMonitor,
    session_meter: SessionMeter,
    logger: logging.Logger,
) -> dict[str, Any]:
    sample = collect_interval(client, tracker, config.monitor.sample_seconds)
    sample["schema"] = SAMPLE_SCHEMA
    annotate_sample_timing(sample, config.monitor.sample_seconds)
    save_tracker(config.state_dir / "mihomo-baseline.json", tracker)
    history.append(sample)
    cutoff = sample["epoch"] - config.monitor.critical_window_seconds
    while history and float(history[0].get("epoch", 0)) < cutoff:
        history.popleft()
    append_jsonl(config.state_dir / "samples.jsonl", sample, config.state)

    warning = totals_for_window(
        history,
        float(sample["epoch"]),
        config.monitor.warning_window_seconds,
        config.monitor.sample_seconds,
    )
    critical = totals_for_window(
        history,
        float(sample["epoch"]),
        config.monitor.critical_window_seconds,
        config.monitor.sample_seconds,
    )
    transition = alerts.evaluate(warning, critical, config)
    if transition is not None:
        event_type, level = transition
        event = build_event(event_type, level, sample, warning, critical)
        if event_type != "recovered":
            try:
                from snapshot import create_snapshot

                event["snapshot_path"] = str(create_snapshot(config, event))
            except Exception as exc:
                event["snapshot_error"] = str(exc)
        append_jsonl(config.state_dir / "events.jsonl", event, config.state)
        notify(event_type, level, warning, critical)

    reset_request = consume_reset_request(config.state_dir)
    if reset_request is not None:
        session_meter.reset(float(sample["epoch"]), "manual")
        vps_state = vps_monitor.maybe_poll(sample["epoch"], force=True)
        xray_state = xray_monitor.reset_session(float(sample["epoch"]))
        session_meter.set_vps_baseline(vps_state)
        logger.info("dashboard session reset id=%s", reset_request["id"])
    elif session_meter.started_epoch is None:
        vps_state = vps_monitor.maybe_poll(sample["epoch"])
        session_meter.reset(float(sample["epoch"]), "automatic")
        session_meter.set_vps_baseline(vps_state)
        xray_state = xray_monitor.reset_session(float(sample["epoch"]))
    else:
        vps_state = vps_monitor.maybe_poll(sample["epoch"])
        xray_state = xray_monitor.maybe_poll(sample["epoch"])
        session_meter.record(sample, vps_state)

    write_menubar_state(
        config,
        sample,
        warning,
        critical,
        alerts.level,
        vps_state,
        xray_state,
        session_meter.snapshot(
            config.vps.enabled,
            config.estimation,
            xray_state,
            now=float(sample["epoch"]),
        ),
    )
    write_health_state(config, "ok")
    return sample


def print_current(client: MihomoApiClient) -> None:
    payload = client.connections()
    upload = max(0, int(payload.get("uploadTotal", 0)))
    download = max(0, int(payload.get("downloadTotal", 0)))
    services: dict[str, dict[str, Any]] = {}
    for connection in payload.get("connections", []):
        if not isinstance(connection, dict):
            continue
        metadata = connection.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        service_id, label, _ = classify_host(metadata.get("host") or metadata.get("destinationIP"))
        service = services.setdefault(service_id, {"label": label, "bytes": 0})
        service["bytes"] += max(0, int(connection.get("upload", 0))) + max(0, int(connection.get("download", 0)))
        service["route"] = classify_route(connection.get("chains"))
    print(f"Mihomo 累计：↑ {format_bytes(upload)}  ↓ {format_bytes(download)}  合计 {format_bytes(upload + download)}")
    print(f"当前活跃连接：{len(payload.get('connections', []))}")
    for service in sorted(services.values(), key=lambda item: int(item["bytes"]), reverse=True)[:10]:
        print(f"{service['label']}（{service['route']}） {format_bytes(service['bytes'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 Mihomo 本机接口分析代理流量")
    parser.add_argument("--config", type=Path, help="TOML 配置；本机 Mihomo 无需配置")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="显示当前 Mihomo 累计与活跃域名")
    mode.add_argument("--watch", action="store_true", help="持续采样并更新 App")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        ensure_state_dir(config)
        client = MihomoApiClient()
        if args.once:
            print_current(client)
            return 0
        logger = configure_logger(config)
        history = load_recent_samples(config, time.time())
        tracker = load_tracker(config.state_dir / "mihomo-baseline.json")
        alerts = AlertEngine()
        vps_monitor = VpsMonitor(config.vps, config.state_dir, config.state)
        xray_monitor = XrayStatsMonitor(config.xray_stats, config.state_dir, config.state)
        session_meter = SessionMeter(
            config.state_dir,
            expected_interval_seconds=config.monitor.sample_seconds,
        )
        xray_monitor.align_session(session_meter.started_epoch)
        lock = acquire_watch_lock(config)
        if lock is None:
            raise RuntimeError("另一个监控实例已经在运行")
        logger.info(
            "monitor started local=%ss vps=%ss xray=%ss",
            config.monitor.sample_seconds,
            config.vps.poll_seconds,
            config.xray_stats.poll_seconds,
        )
        try:
            while not parent_process_exited():
                try:
                    handle_sample(
                        config,
                        history,
                        client,
                        tracker,
                        alerts,
                        vps_monitor,
                        xray_monitor,
                        session_meter,
                        logger,
                    )
                except Exception as exc:
                    logger.exception("sample failed")
                    write_health_state(config, "error", str(exc))
                    time.sleep(min(5, config.monitor.sample_seconds))
        finally:
            lock.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
