#!/usr/bin/env python3
"""Current Traffic Sentinel settings schema, validation, and persistence."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tomllib
from typing import Any

from remote_ssh import validate_ssh_host
from traffic_estimation import BILLING_MODES, TrafficEstimationConfig
from vps import VpsConfig
from xray_stats import XrayStatsConfig


STATE_DIRECTORY_ENV = "TRAFFIC_SENTINEL_STATE_DIR"
SETTINGS_SCHEMA = 1
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
SOURCE_EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.toml"
BUNDLED_EXAMPLE_CONFIG = Path(__file__).with_name("config.example.toml")

SAMPLE_SECONDS = 5
REMOTE_POLL_SECONDS = 300
VPS_INTERFACE = "auto"
XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_BINARY_PATH = "/usr/local/bin/xray"
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5

TOP_LEVEL_KEYS = {"monitor", "remote"}
MONITOR_KEYS = {
    "warning_window_minutes",
    "warning_mib",
    "critical_window_minutes",
    "critical_mib",
}
REMOTE_KEYS = {
    "enabled",
    "ssh_host",
    "xray_stats_enabled",
    "billing_cycle_start_day",
    "billing_mode",
}
@dataclass(frozen=True)
class MonitorConfig:
    sample_seconds: int
    warning_window_seconds: int
    warning_bytes: int
    critical_window_seconds: int
    critical_bytes: int


@dataclass(frozen=True)
class StateConfig:
    max_log_bytes: int = MAX_LOG_BYTES
    backups: int = LOG_BACKUPS


@dataclass(frozen=True)
class Config:
    monitor: MonitorConfig
    state: StateConfig
    vps: VpsConfig
    xray_stats: XrayStatsConfig
    estimation: TrafficEstimationConfig
    state_dir: Path


@dataclass(frozen=True)
class UserSettings:
    warning_window_minutes: int
    warning_mib: int
    critical_window_minutes: int
    critical_mib: int
    remote_enabled: bool
    ssh_host: str
    xray_stats_enabled: bool
    billing_cycle_start_day: int
    billing_mode: str


def default_user_settings() -> UserSettings:
    return UserSettings(
        warning_window_minutes=5,
        warning_mib=250,
        critical_window_minutes=10,
        critical_mib=1024,
        remote_enabled=False,
        ssh_host="",
        xray_stats_enabled=False,
        billing_cycle_start_day=1,
        billing_mode="both",
    )


def _require_table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"缺少 [{name}] 配置")
    return value


def _require_exact_keys(raw: dict[str, Any], expected: set[str], context: str) -> None:
    unknown = set(raw) - expected
    missing = expected - set(raw)
    if unknown:
        raise ValueError(f"{context} 包含不支持的字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{context} 缺少字段：{', '.join(sorted(missing))}")


def _integer(raw: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _boolean(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是 true 或 false")
    return value


def parse_settings(raw: dict[str, Any]) -> UserSettings:
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是表")
    _require_exact_keys(raw, TOP_LEVEL_KEYS, "配置")
    monitor = _require_table(raw, "monitor")
    remote = _require_table(raw, "remote")
    _require_exact_keys(monitor, MONITOR_KEYS, "[monitor]")
    _require_exact_keys(remote, REMOTE_KEYS, "[remote]")

    remote_enabled = _boolean(remote, "enabled")
    xray_enabled = _boolean(remote, "xray_stats_enabled")
    ssh_host = remote.get("ssh_host")
    if not isinstance(ssh_host, str):
        raise ValueError("ssh_host 必须是字符串")
    ssh_host = ssh_host.strip()
    if remote_enabled:
        validate_ssh_host(ssh_host)
    if xray_enabled and not remote_enabled:
        raise ValueError("启用 Xray 统计前必须启用远端对账")
    billing_mode = remote.get("billing_mode")
    if billing_mode not in BILLING_MODES:
        raise ValueError("billing_mode 只能是 both 或 outbound")

    return UserSettings(
        warning_window_minutes=_integer(monitor, "warning_window_minutes", 1, 120),
        warning_mib=_integer(monitor, "warning_mib", 1, 1_048_576),
        critical_window_minutes=_integer(monitor, "critical_window_minutes", 1, 240),
        critical_mib=_integer(monitor, "critical_mib", 1, 1_048_576),
        remote_enabled=remote_enabled,
        ssh_host=ssh_host,
        xray_stats_enabled=xray_enabled,
        billing_cycle_start_day=_integer(remote, "billing_cycle_start_day", 1, 31),
        billing_mode=str(billing_mode),
    )


def settings_payload(settings: UserSettings) -> dict[str, Any]:
    return {
        "schema": SETTINGS_SCHEMA,
        "monitor": {
            "warning_window_minutes": settings.warning_window_minutes,
            "warning_mib": settings.warning_mib,
            "critical_window_minutes": settings.critical_window_minutes,
            "critical_mib": settings.critical_mib,
        },
        "remote": {
            "enabled": settings.remote_enabled,
            "ssh_host": settings.ssh_host,
            "xray_stats_enabled": settings.xray_stats_enabled,
            "billing_cycle_start_day": settings.billing_cycle_start_day,
            "billing_mode": settings.billing_mode,
        },
    }


def serialize_settings(settings: UserSettings) -> str:
    return (
        "# Managed by Traffic Sentinel Settings.\n"
        "# Local Mihomo discovery, sampling frequency, remote endpoints, and log\n"
        "# retention are fixed product behavior rather than user configuration.\n"
        "\n"
        "[monitor]\n"
        f"warning_window_minutes = {settings.warning_window_minutes}\n"
        f"warning_mib = {settings.warning_mib}\n"
        f"critical_window_minutes = {settings.critical_window_minutes}\n"
        f"critical_mib = {settings.critical_mib}\n"
        "\n"
        "[remote]\n"
        f"enabled = {'true' if settings.remote_enabled else 'false'}\n"
        f"ssh_host = {json.dumps(settings.ssh_host, ensure_ascii=False)}\n"
        f"xray_stats_enabled = {'true' if settings.xray_stats_enabled else 'false'}\n"
        f"billing_cycle_start_day = {settings.billing_cycle_start_day}\n"
        f"billing_mode = {json.dumps(settings.billing_mode)}\n"
    )


def read_user_settings(path: Path) -> UserSettings:
    with path.open("rb") as handle:
        return parse_settings(tomllib.load(handle))


def write_user_settings(path: Path, payload: dict[str, Any]) -> UserSettings:
    if payload.get("schema") != SETTINGS_SCHEMA:
        raise ValueError("设置数据版本无效")
    settings = parse_settings({
        "monitor": payload.get("monitor"),
        "remote": payload.get("remote"),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(serialize_settings(settings), encoding="utf-8")
    temporary.replace(path)
    return settings


def runtime_config(settings: UserSettings, state_dir: Path) -> Config:
    monitor = MonitorConfig(
        sample_seconds=SAMPLE_SECONDS,
        warning_window_seconds=settings.warning_window_minutes * 60,
        warning_bytes=settings.warning_mib * 1024 * 1024,
        critical_window_seconds=settings.critical_window_minutes * 60,
        critical_bytes=settings.critical_mib * 1024 * 1024,
    )
    vps = VpsConfig(
        enabled=settings.remote_enabled,
        ssh_host=settings.ssh_host,
        interface=VPS_INTERFACE,
        poll_seconds=REMOTE_POLL_SECONDS,
        billing_cycle_start_day=settings.billing_cycle_start_day,
    )
    xray_stats = XrayStatsConfig(
        enabled=settings.remote_enabled and settings.xray_stats_enabled,
        ssh_host=settings.ssh_host,
        api_server=XRAY_API_SERVER,
        binary_path=XRAY_BINARY_PATH,
        poll_seconds=REMOTE_POLL_SECONDS,
        expected_users=(),
        flagged_users=(),
    )
    return Config(
        monitor=monitor,
        state=StateConfig(),
        vps=vps,
        xray_stats=xray_stats,
        estimation=TrafficEstimationConfig(settings.billing_mode),
        state_dir=state_dir,
    )


def default_config_path() -> Path:
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    if SOURCE_EXAMPLE_CONFIG.exists():
        return SOURCE_EXAMPLE_CONFIG
    return BUNDLED_EXAMPLE_CONFIG


def read_config(path: Path | None) -> Config:
    selected = path or default_config_path()
    settings = read_user_settings(selected)
    configured_state_directory = os.environ.get(STATE_DIRECTORY_ENV)
    state_dir = (
        Path(configured_state_directory).expanduser()
        if configured_state_directory
        else selected.parent / "state"
    )
    return runtime_config(settings, state_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Traffic Sentinel settings bridge")
    parser.add_argument("command", choices=("defaults", "export", "write"))
    parser.add_argument("config", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "defaults":
            settings = default_user_settings()
        elif args.config is None:
            raise ValueError("export 和 write 需要配置文件路径")
        elif args.command == "export":
            settings = read_user_settings(args.config)
        else:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("设置数据必须是 JSON 对象")
            settings = write_user_settings(args.config, payload)
        print(json.dumps(settings_payload(settings), ensure_ascii=False))
        return 0
    except (OSError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
