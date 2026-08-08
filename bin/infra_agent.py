#!/usr/bin/env python3
"""Infra Agent runtime: sample, store, project, and serve local commands."""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from billing_policy import BillingBudgetEngine, BillingBudgetTransition
from agent_protocol import PROJECTION_SCHEMA, complete_command, consume_commands, write_projection
from configuration import (
    Config,
    StateConfig,
    default_config_path,
    read_config,
    read_user_settings,
    settings_payload,
    write_user_settings,
)
from infra_collectors import CollectorContext, CollectorRegistry, CollectorRun, collected_points
from infra_projection import build_infra_projection
from metric_query import BUCKET_SECONDS, MAX_RANGE_SECONDS, QUERY_SCHEMA, MetricQuery, execute_metric_query
from metric_store import MetricStore
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
from network_metrics import network_collector_registry
from sample_timing import annotate_sample_timing, sample_is_realtime
from remote import RemoteFleetMonitor
from session import SessionMeter


PARENT_PROCESS_ENV = "INFRA_SENTINEL_PARENT_PID"
APP_NOTIFICATIONS_ENV = "INFRA_SENTINEL_APP_NOTIFICATIONS"
CONFIGURATION_READ_INTERVAL_SECONDS = 0.1
SAMPLE_SCHEMA = 5
AGENT_RUNTIME_SCHEMA = PROJECTION_SCHEMA


@dataclass(frozen=True)
class AgentCommandEffects:
    """Command results that alter Agent lifecycle after a sample completes."""

    remote_state: dict[str, Any] | None = None
    restart_requested: bool = False


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
    handle = (config.state_dir / "infra-agent.lock").open("a", encoding="utf-8")
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
        parent_pid = int(raw_parent)
    except ValueError:
        return False
    if parent_pid <= 1:
        return False
    try:
        # A PyInstaller one-file sidecar forks a bootstrap process before the
        # Python runtime starts. `getppid()` then points at that bootstrap,
        # not at the desktop process that originally launched the sidecar.
        # Test the recorded desktop PID directly so both the source runtime
        # and the frozen sidecar stop when their UI exits.
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
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
        ) or record.get("scope") == "vps_billing_cycle":
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
    config: Config,
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
        "window_seconds": {
            "warning": config.monitor.warning_window_seconds,
            "critical": config.monitor.critical_window_seconds,
        },
    }


def build_billing_event(transition: BillingBudgetTransition) -> dict[str, Any]:
    return {
        "schema": 3,
        "id": uuid.uuid4().hex,
        "timestamp": iso_now(),
        "type": transition.event_type,
        "level": transition.level,
        "scope": "vps_billing_cycle",
        "alert_group": transition.policy.label,
        "source_id": transition.policy.source_id,
        "policy_id": transition.policy.id,
        "billable_bytes": transition.billable_bytes,
        "threshold_bytes": transition.threshold_bytes,
        "cycle": transition.cycle,
    }


def notify(
    event_type: str,
    level: str,
    warning: dict[str, int],
    critical: dict[str, int],
    config: Config,
) -> None:
    if os.environ.get(APP_NOTIFICATIONS_ENV) == "1":
        return
    if event_type == "recovered":
        title, body = "Traffic Sentinel", "Mihomo 流量已回落到阈值以下"
    elif level == "critical":
        title = "Traffic Sentinel 严重告警"
        minutes = config.monitor.critical_window_seconds // 60
        body = f"{minutes} 分钟累计 {format_bytes(critical['up_bytes'] + critical['down_bytes'])}"
    else:
        title = "Traffic Sentinel 流量告警"
        minutes = config.monitor.warning_window_seconds // 60
        body = f"{minutes} 分钟 ↑{format_bytes(warning['up_bytes'])} ↓{format_bytes(warning['down_bytes'])}"
    send_native_notification(title, body)


def send_native_notification(title: str, body: str) -> None:
    """Deliver UTF-8 text as AppleScript arguments instead of environment variables.

    `system attribute` has been observed to decode non-ASCII environment values
    inconsistently under launchd/PyInstaller. Positional arguments preserve the
    Python process' UTF-8 text all the way to AppleScript without interpolating
    untrusted values into script source.
    """
    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script, title, body],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def notify_billing(transition: BillingBudgetTransition) -> None:
    """Deliver a compact CLI notification when the native app is not hosting it."""
    if os.environ.get(APP_NOTIFICATIONS_ENV) == "1":
        return
    label = transition.policy.label
    if transition.event_type == "recovered":
        title, body = f"{label} 账单恢复", "本计费周期账单已回落到警告阈值以下"
    elif transition.event_type == "deescalated":
        title, body = f"{label} 账单降级", "严重阈值已回落，仍处于警告范围"
    else:
        title = f"{label} {'严重' if transition.level == 'critical' else ''}账单告警"
        body = f"本计费周期 {format_bytes(transition.billable_bytes)}，阈值 {format_bytes(transition.threshold_bytes)}"
    send_native_notification(title, body)


def write_projection_state(
    config: Config,
    sample: dict[str, Any],
    warning: dict[str, int],
    critical: dict[str, int],
    level: str,
    remote: dict[str, Any],
    session: dict[str, Any],
    collector_runs: tuple[CollectorRun, ...] = (),
    storage: dict[str, Any] | None = None,
) -> None:
    remote_servers = remote.get("servers", [])
    xray_servers = [server.get("xray_stats", {}) for server in remote_servers]
    active_xray = [server for server in xray_servers if server.get("enabled")]
    if not active_xray:
        xray_status = "disabled"
    elif any(server.get("status") == "error" for server in active_xray):
        xray_status = "error"
    elif any(server.get("status") in {"waiting", "baseline"} for server in active_xray):
        xray_status = "waiting"
    else:
        xray_status = "ok"
    state = {
        "agent_runtime_schema": AGENT_RUNTIME_SCHEMA,
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
        "vps": {
            "enabled": remote.get("enabled", False),
            "status": remote.get("status", "disabled"),
            "updated_at": remote.get("updated_at"),
            "cycle": remote.get("cycle", {}),
            "servers": remote.get("servers", []),
        },
        "xray_stats": {
            "enabled": bool(active_xray),
            "status": xray_status,
            "servers": xray_servers,
            "users": [
                {
                    **user,
                    "id": f"{server.get('id', 'remote')}:{user.get('id', user.get('label', 'unknown'))}",
                    "label": f"{server.get('label', server.get('id', 'VPS'))} / {user.get('label', 'unknown')}",
                }
                for server in remote_servers
                for user in server.get("xray_stats", {}).get("users", [])
            ],
            "total_bytes": sum(int(server.get("total_bytes", 0)) for server in xray_servers),
        },
        "session": session,
        "storage": storage or {"schema": "20260808.2", "kind": "sqlite", "status": "waiting"},
        "query": {
            "schema": QUERY_SCHEMA,
            "command": "metrics.query",
            "instrument": "counter",
            "bucket_seconds": sorted(BUCKET_SECONDS),
            "max_range_seconds": MAX_RANGE_SECONDS,
        },
        "collectors": [run.as_dict() for run in collector_runs],
        # This is a derived, generic resource projection.  The legacy-shaped
        # network fields above remain facts for the detailed network panel.
        "infra": build_infra_projection(sample, session, remote, level, collector_runs),
        "last_event": latest_delta_event(config.state_dir / "events.jsonl"),
    }
    write_projection(config.state_dir, state)


def write_health_state(config: Config, status: str, message: str | None = None) -> None:
    payload = {"schema": 1, "status": status, "updated_at": iso_now()}
    if message:
        payload["message"] = message
    temporary = config.state_dir / ".health.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(config.state_dir / "health.json")


def configure_logger(config: Config) -> logging.Logger:
    logger = logging.getLogger("infra-agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(
        config.state_dir / "infra-agent.log",
        maxBytes=config.state.max_log_bytes,
        backupCount=config.state.backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def process_configuration_read_commands(config_path: Path, state_dir: Path, logger: logging.Logger) -> int:
    """Complete configuration reads without waiting for a sampling interval.

    Opening Settings is a local read and should not wait for Mihomo's five
    second observation window or a slow SSH poll. Mutating commands remain in
    ``apply_agent_commands`` so they retain the normal single-writer lifecycle.
    """
    completed = 0
    for command in consume_commands(state_dir, accepted_types={"configuration.get"}):
        try:
            complete_command(command, status="ok", payload={
                "settings": settings_payload(read_user_settings(config_path)),
            })
        except (OSError, ValueError) as exc:
            complete_command(command, status="error", message=str(exc))
            logger.warning("configuration read failed id=%s", command.id)
        completed += 1
    return completed


def run_configuration_read_service(
    config_path: Path,
    state_dir: Path,
    logger: logging.Logger,
    stop_event: threading.Event,
) -> None:
    """Serve the read-only settings command while the sampler is busy."""
    while not stop_event.is_set():
        process_configuration_read_commands(config_path, state_dir, logger)
        stop_event.wait(CONFIGURATION_READ_INTERVAL_SECONDS)


def apply_agent_commands(
    config: Config,
    config_path: Path,
    sample_epoch: float,
    metric_store: MetricStore,
    remote_monitor: RemoteFleetMonitor,
    session_meter: SessionMeter,
    logger: logging.Logger,
) -> AgentCommandEffects:
    """Apply idempotent local commands before recording the current interval."""
    reset_remote_state: dict[str, Any] | None = None
    restart_requested = False
    for command in consume_commands(config.state_dir):
        if command.type == "configuration.get":
            try:
                complete_command(command, status="ok", payload={"settings": settings_payload(read_user_settings(config_path))})
            except (OSError, ValueError) as exc:
                complete_command(command, status="error", message=str(exc))
            continue
        if command.type == "configuration.update":
            try:
                settings = write_user_settings(config_path, command.payload)
                complete_command(command, status="ok", payload={
                    "settings": settings_payload(settings),
                    "restart_required": True,
                })
                restart_requested = True
            except (OSError, ValueError) as exc:
                complete_command(command, status="rejected", message=str(exc))
            continue
        if command.type == "metrics.query":
            try:
                query = MetricQuery.from_payload(command.payload, now=sample_epoch)
                complete_command(command, status="ok", payload=execute_metric_query(metric_store, query))
            except (ValueError, TypeError) as exc:
                complete_command(command, status="rejected", message=str(exc))
            continue
        if command.type != "session.reset":
            complete_command(command, status="rejected", message="unsupported command")
            continue
        try:
            session_meter.reset(sample_epoch, "manual")
            remote_state = remote_monitor.reset_session(sample_epoch)
            session_meter.set_vps_baseline(remote_state)
        except Exception as exc:
            complete_command(command, status="error", message=type(exc).__name__)
            logger.warning("agent command failed id=%s type=%s", command.id, command.type)
            continue
        complete_command(command, status="ok")
        logger.info("agent command applied id=%s type=%s", command.id, command.type)
        reset_remote_state = remote_state
    return AgentCommandEffects(reset_remote_state, restart_requested)


def handle_sample(
    config: Config,
    config_path: Path,
    history: deque[dict[str, Any]],
    client: MihomoApiClient,
    tracker: MihomoTrafficTracker,
    alerts: AlertEngine,
    billing_alerts: BillingBudgetEngine,
    remote_monitor: RemoteFleetMonitor,
    session_meter: SessionMeter,
    metric_store: MetricStore,
    collector_registry: CollectorRegistry,
    logger: logging.Logger,
) -> tuple[dict[str, Any], bool]:
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
        event = build_event(event_type, level, sample, warning, critical, config)
        if event_type != "recovered":
            try:
                from snapshot import create_snapshot

                event["snapshot_path"] = str(create_snapshot(config, event))
            except Exception as exc:
                event["snapshot_error"] = str(exc)
        append_jsonl(config.state_dir / "events.jsonl", event, config.state)
        notify(event_type, level, warning, critical, config)

    command_effects = apply_agent_commands(
        config,
        config_path,
        float(sample["epoch"]),
        metric_store,
        remote_monitor,
        session_meter,
        logger,
    )
    if command_effects.remote_state is not None:
        remote_state = command_effects.remote_state
    elif session_meter.started_epoch is None:
        remote_state = remote_monitor.maybe_poll(sample["epoch"])
        session_meter.reset(float(sample["epoch"]), "automatic")
        session_meter.set_vps_baseline(remote_state)
    else:
        remote_state = remote_monitor.maybe_poll(sample["epoch"])
        session_meter.record(sample, remote_state)

    session_snapshot = session_meter.snapshot(
        remote_state,
        now=float(sample["epoch"]),
    )
    for transition in billing_alerts.evaluate(remote_state, config.remote_billing_policies):
        event = build_billing_event(transition)
        append_jsonl(config.state_dir / "events.jsonl", event, config.state)
        notify_billing(transition)
    collector_runs = collector_registry.collect(CollectorContext(sample, remote_state))
    for run in collector_runs:
        if run.status == "error":
            logger.warning("metric collector failed id=%s kind=%s", run.capability.id, run.error_kind)
    metric_store.write(collected_points(collector_runs))
    write_projection_state(
        config,
        sample,
        warning,
        critical,
        "critical" if "critical" in (alerts.level, billing_alerts.level) else (
            "warning" if "warning" in (alerts.level, billing_alerts.level) else "none"
        ),
        remote_state,
        session_snapshot,
        collector_runs,
        metric_store.summary(),
    )
    write_health_state(config, "ok")
    return sample, command_effects.restart_requested


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
    parser = argparse.ArgumentParser(description="Infra Sentinel local agent")
    parser.add_argument("--config", type=Path, help="TOML 配置；本机 Mihomo 无需配置")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="显示当前 Mihomo 累计与活跃域名")
    mode.add_argument("--watch", action="store_true", help="持续采样并发布本地 Projection")
    args = parser.parse_args()
    try:
        config_path = args.config or default_config_path()
        config = read_config(config_path)
        ensure_state_dir(config)
        client = MihomoApiClient()
        if args.once:
            print_current(client)
            return 0
        logger = configure_logger(config)
        history = load_recent_samples(config, time.time())
        tracker = load_tracker(config.state_dir / "mihomo-baseline.json")
        alerts = AlertEngine()
        billing_alerts = BillingBudgetEngine()
        remote_monitor = RemoteFleetMonitor(config.remote_servers, config.state_dir, config.state)
        session_meter = SessionMeter(
            config.state_dir,
            expected_interval_seconds=config.monitor.sample_seconds,
        )
        remote_monitor.align_session(session_meter.started_epoch)
        lock = acquire_watch_lock(config)
        if lock is None:
            raise RuntimeError("另一个监控实例已经在运行")
        metric_store = MetricStore(config.state_dir)
        collector_registry = network_collector_registry(server.id for server in config.remote_servers)
        imported = metric_store.import_legacy_network()
        if imported:
            logger.info("imported legacy network metric points=%s", imported)
        logger.info(
            "agent started local=%ss remote_servers=%s",
            config.monitor.sample_seconds,
            len(config.remote_servers),
        )
        configuration_read_stop = threading.Event()
        configuration_read_thread = threading.Thread(
            target=run_configuration_read_service,
            args=(config_path, config.state_dir, logger, configuration_read_stop),
            name="infra-configuration-read-service",
            daemon=True,
        )
        configuration_read_thread.start()
        try:
            while not parent_process_exited():
                try:
                    _sample, restart_requested = handle_sample(
                        config,
                        config_path,
                        history,
                        client,
                        tracker,
                        alerts,
                        billing_alerts,
                        remote_monitor,
                        session_meter,
                        metric_store,
                        collector_registry,
                        logger,
                    )
                    if restart_requested:
                        logger.info("configuration updated; Agent restart requested")
                        return 0
                except Exception as exc:
                    logger.exception("sample failed")
                    write_health_state(config, "error", str(exc))
                    time.sleep(min(5, config.monitor.sample_seconds))
        finally:
            configuration_read_stop.set()
            configuration_read_thread.join(timeout=1)
            lock.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
