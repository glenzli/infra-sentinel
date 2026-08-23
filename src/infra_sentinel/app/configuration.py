#!/usr/bin/env python3
"""Infra Sentinel configuration contract, migration, and runtime mapping.

The persisted document is intentionally source/policy based.  A single
one-time reader migration rewrites the prior Traffic Sentinel document and
leaves a dated backup; no parallel configuration format is maintained.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

from infra_sentinel.resources.network.billing_policy import BillingBudgetPolicy, GIB
from infra_sentinel.resources.network.remote_ssh import validate_ssh_host
from infra_sentinel.resources.network.remote import RemoteServerConfig
from infra_sentinel.resources.network.traffic_estimation import BILLING_MODES, TrafficEstimationConfig
from infra_sentinel.resources.network.vps import VpsConfig
from infra_sentinel.resources.network.xray import XrayStatsConfig


STATE_DIRECTORY_ENV = "INFRA_SENTINEL_STATE_DIR"
CONFIG_SCHEMA = "20260811.1"
PREVIOUS_CONFIG_SCHEMA = "20260808.4"
SETTINGS_SCHEMA = CONFIG_SCHEMA
PROJECT_ROOT = Path(__file__).resolve().parents[3]
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

CONTRACT_TOP_LEVEL_KEYS = {"app", "integrations", "policies", "sources"}
APP_KEYS = {"menu_bar_mode"}
INTEGRATION_KEYS = {
    "ssh_executable", "opencode_executable", "opencode_database", "codex_database",
}
POLICY_KEYS = {
    "id", "kind", "resource_id", "warning_window_minutes", "warning_mib",
    "critical_window_minutes", "critical_mib",
}
REMOTE_USAGE_POLICY_KEYS = {"id", "kind", "source_id", "warning_gib", "critical_gib"}
LOCAL_SOURCE_KEYS = {"id", "kind", "enabled"}
REMOTE_SOURCE_KEYS = {
    "id", "kind", "label", "enabled", "ssh_host", "xray_stats_enabled",
    "billing_mode",
}
LEGACY_TOP_LEVEL_KEYS = {"monitor", "remote"}
MONITOR_KEYS = {
    "warning_window_minutes", "warning_mib", "critical_window_minutes", "critical_mib",
}
REMOTE_KEYS = {"servers"}
LEGACY_REMOTE_KEYS = {
    "enabled", "ssh_host", "xray_stats_enabled", "billing_mode",
}
LEGACY_SERVER_KEYS = {
    "id", "label", "enabled", "ssh_host", "xray_stats_enabled",
    "billing_mode",
}
SERVER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")


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
    remote_servers: tuple[RemoteServerConfig, ...]
    remote_billing_policies: tuple[BillingBudgetPolicy, ...]
    integrations: "LocalIntegrationPaths"
    state_dir: Path


@dataclass(frozen=True)
class UserSettings:
    warning_window_minutes: int
    warning_mib: int
    critical_window_minutes: int
    critical_mib: int
    remote_servers: tuple[dict[str, Any], ...]
    integrations: "LocalIntegrationPaths"


@dataclass(frozen=True)
class LocalIntegrationPaths:
    ssh_executable: Path | None = None
    opencode_executable: Path | None = None
    opencode_database: Path | None = None
    # Legacy schema field retained for round-trip compatibility only. Codex
    # usage is reconstructed from rollout JSONL and never reads this path.
    codex_database: Path | None = None

    def as_payload(self) -> dict[str, str]:
        return {
            "ssh_executable": str(self.ssh_executable or ""),
            "opencode_executable": str(self.opencode_executable or ""),
            "opencode_database": str(self.opencode_database or ""),
            "codex_database": str(self.codex_database or ""),
        }


def default_user_settings() -> UserSettings:
    return UserSettings(5, 250, 10, 1024, (), LocalIntegrationPaths())


def _require_exact_keys(raw: dict[str, Any], expected: set[str], context: str) -> None:
    unknown = set(raw) - expected
    missing = expected - set(raw)
    if unknown:
        raise ValueError(f"{context} 包含不支持的字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{context} 缺少字段：{', '.join(sorted(missing))}")


def _require_table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"缺少 [{name}] 配置")
    return value


def _integer(raw: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _boolean(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{key} 必须是 true 或 false")


def _optional_path(raw: dict[str, Any], key: str) -> Path | None:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    text = value.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() or "\n" in text or "\r" in text:
        raise ValueError(f"{key} 必须为空或绝对路径")
    return path


def _integration_paths(raw: dict[str, Any]) -> LocalIntegrationPaths:
    _require_exact_keys(raw, INTEGRATION_KEYS, "[integrations]")
    return LocalIntegrationPaths(**{
        key: _optional_path(raw, key)
        for key in INTEGRATION_KEYS
    })


def _parse_remote_sources(raw_sources: list[Any]) -> tuple[dict[str, Any], ...]:
    servers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    local_found = False
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict):
            raise ValueError(f"数据源 #{index + 1} 必须是表")
        kind = raw_source.get("kind")
        if kind == "network.mihomo":
            _require_exact_keys(raw_source, LOCAL_SOURCE_KEYS, f"数据源 #{index + 1}")
            if raw_source.get("id") != "local-mihomo" or not _boolean(raw_source, "enabled"):
                raise ValueError("local-mihomo 必须保持启用")
            local_found = True
            continue
        if kind != "network.linux-xray":
            raise ValueError(f"数据源 #{index + 1} 的 kind 不受支持")
        _require_exact_keys(raw_source, REMOTE_SOURCE_KEYS, f"数据源 #{index + 1}")
        server_id = raw_source.get("id")
        label = raw_source.get("label")
        ssh_host = raw_source.get("ssh_host")
        if not isinstance(server_id, str) or not SERVER_ID_RE.fullmatch(server_id) or server_id == "local-mihomo":
            raise ValueError(f"数据源 #{index + 1} 的 id 无效")
        if server_id in seen_ids:
            raise ValueError(f"数据源 id 重复：{server_id}")
        seen_ids.add(server_id)
        if not isinstance(label, str) or not label.strip() or "\n" in label or "\r" in label:
            raise ValueError(f"数据源 #{index + 1} 的 label 无效")
        if not isinstance(ssh_host, str):
            raise ValueError(f"数据源 #{index + 1} 的 ssh_host 必须是字符串")
        enabled = _boolean(raw_source, "enabled")
        xray_enabled = _boolean(raw_source, "xray_stats_enabled")
        ssh_host = ssh_host.strip()
        if enabled:
            validate_ssh_host(ssh_host)
        if xray_enabled and not enabled:
            raise ValueError(f"数据源 #{index + 1} 启用 Xray 前必须启用服务器")
        billing_mode = raw_source.get("billing_mode")
        if billing_mode not in BILLING_MODES:
            raise ValueError("billing_mode 只能是 both 或 outbound")
        servers.append({
            "id": server_id,
            "label": label.strip(),
            "enabled": enabled,
            "ssh_host": ssh_host,
            "xray_stats_enabled": xray_enabled,
            "billing_mode": str(billing_mode),
            "usage_alert_enabled": False,
            "usage_warning_gib": 0,
            "usage_critical_gib": 0,
        })
    if not local_found:
        raise ValueError("缺少 local-mihomo 数据源")
    return tuple(servers)


def _settings_from_contract(
    raw: dict[str, Any],
    version_key: str,
    expected_schema: str = CONFIG_SCHEMA,
) -> UserSettings:
    if not isinstance(raw, dict) or raw.get(version_key) != expected_schema:
        raise ValueError("配置版本无效")
    body = {key: value for key, value in raw.items() if key != version_key}
    _require_exact_keys(body, CONTRACT_TOP_LEVEL_KEYS, "配置")
    app = _require_table(body, "app")
    _require_exact_keys(app, APP_KEYS, "[app]")
    if app.get("menu_bar_mode") != "health":
        raise ValueError("menu_bar_mode 只能是 health")
    integrations = _integration_paths(_require_table(body, "integrations"))
    policies = body.get("policies")
    sources = body.get("sources")
    if not isinstance(policies, list) or not isinstance(sources, list):
        raise ValueError("policies 和 sources 必须是数组")
    traffic = next((item for item in policies if isinstance(item, dict) and item.get("id") == "network-traffic-alerts"), None)
    if traffic is None:
        raise ValueError("缺少 network-traffic-alerts 策略")
    _require_exact_keys(traffic, POLICY_KEYS, "流量策略")
    if traffic.get("kind") != "traffic.threshold" or traffic.get("resource_id") != "network":
        raise ValueError("network-traffic-alerts 策略无效")
    settings = UserSettings(
        warning_window_minutes=_integer(traffic, "warning_window_minutes", 1, 120),
        warning_mib=_integer(traffic, "warning_mib", 1, 1_048_576),
        critical_window_minutes=_integer(traffic, "critical_window_minutes", 1, 240),
        critical_mib=_integer(traffic, "critical_mib", 1, 1_048_576),
        remote_servers=_parse_remote_sources(sources),
        integrations=integrations,
    )
    servers = {server["id"]: server for server in settings.remote_servers}
    for policy in policies:
        if not isinstance(policy, dict):
            raise ValueError("策略必须是表")
        if policy is traffic:
            continue
        _require_exact_keys(policy, REMOTE_USAGE_POLICY_KEYS, "VPS 每日用量策略")
        source_id = policy.get("source_id")
        if policy.get("kind") != "network.daily.usage" or not isinstance(source_id, str) or source_id not in servers:
            raise ValueError("VPS 每日用量策略的数据源无效")
        if policy.get("id") != f"{source_id}-daily-usage":
            raise ValueError("VPS 每日用量策略 id 无效")
        server = servers[source_id]
        if server["usage_alert_enabled"]:
            raise ValueError(f"VPS 每日用量策略重复：{source_id}")
        warning_gib = _integer(policy, "warning_gib", 1, 1_048_576)
        critical_gib = _integer(policy, "critical_gib", 1, 1_048_576)
        if critical_gib <= warning_gib:
            raise ValueError("VPS 每日严重阈值必须大于警告阈值")
        server.update({
            "usage_alert_enabled": True,
            "usage_warning_gib": warning_gib,
            "usage_critical_gib": critical_gib,
        })
    return settings


def parse_settings(payload: dict[str, Any]) -> UserSettings:
    """Validate the date-versioned native settings bridge payload."""
    return _settings_from_contract(payload, "schema")


def settings_payload(settings: UserSettings) -> dict[str, Any]:
    sources: list[dict[str, Any]] = [{"id": "local-mihomo", "kind": "network.mihomo", "enabled": True}]
    sources.extend({"id": server["id"], "kind": "network.linux-xray", "label": server["label"],
                    "enabled": server["enabled"], "ssh_host": server["ssh_host"],
                    "xray_stats_enabled": server["xray_stats_enabled"],
                    "billing_mode": server["billing_mode"]}
                   for server in settings.remote_servers)
    policies: list[dict[str, Any]] = [{
        "id": "network-traffic-alerts", "kind": "traffic.threshold", "resource_id": "network",
        "warning_window_minutes": settings.warning_window_minutes, "warning_mib": settings.warning_mib,
        "critical_window_minutes": settings.critical_window_minutes, "critical_mib": settings.critical_mib,
    }]
    policies.extend({
        "id": f"{server['id']}-daily-usage", "kind": "network.daily.usage", "source_id": server["id"],
        "warning_gib": server["usage_warning_gib"], "critical_gib": server["usage_critical_gib"],
    } for server in settings.remote_servers if server["usage_alert_enabled"])
    return {
        "schema": SETTINGS_SCHEMA,
        "app": {"menu_bar_mode": "health"},
        "integrations": settings.integrations.as_payload(),
        "policies": policies,
        "sources": sources,
    }


def serialize_settings(settings: UserSettings) -> str:
    lines = [
        "# Managed by Infra Sentinel. Schema identifiers use YYYYMMDD.revision.",
        "# Local Mihomo discovery and sampling frequency are fixed product behavior.",
        f'schema_version = "{CONFIG_SCHEMA}"', "",
        "[app]", 'menu_bar_mode = "health"', "",
        "[integrations]",
        *(
            f"{key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in settings.integrations.as_payload().items()
        ),
        "",
        "[[policies]]", 'id = "network-traffic-alerts"', 'kind = "traffic.threshold"', 'resource_id = "network"',
        f"warning_window_minutes = {settings.warning_window_minutes}", f"warning_mib = {settings.warning_mib}",
        f"critical_window_minutes = {settings.critical_window_minutes}", f"critical_mib = {settings.critical_mib}", "",
        "[[sources]]", 'id = "local-mihomo"', 'kind = "network.mihomo"', "enabled = true",
    ]
    for server in settings.remote_servers:
        if server["usage_alert_enabled"]:
            policy_id = f"{server['id']}-daily-usage"
            lines.extend(["", "[[policies]]", f"id = {json.dumps(policy_id)}",
                          'kind = "network.daily.usage"', f"source_id = {json.dumps(server['id'])}",
                          f"warning_gib = {int(server['usage_warning_gib'])}",
                          f"critical_gib = {int(server['usage_critical_gib'])}"])
        lines.extend(["", "[[sources]]", f"id = {json.dumps(server['id'])}", 'kind = "network.linux-xray"',
                      f"label = {json.dumps(server['label'], ensure_ascii=False)}",
                      f"enabled = {'true' if server['enabled'] else 'false'}",
                      f"ssh_host = {json.dumps(server['ssh_host'], ensure_ascii=False)}",
                      f"xray_stats_enabled = {'true' if server['xray_stats_enabled'] else 'false'}",
                      f"billing_mode = {json.dumps(server['billing_mode'])}"])
    return "\n".join(lines) + "\n"


def _legacy_settings(raw: dict[str, Any]) -> UserSettings:
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是表")
    _require_exact_keys(raw, LEGACY_TOP_LEVEL_KEYS, "旧配置")
    monitor = _require_table(raw, "monitor")
    remote = _require_table(raw, "remote")
    _require_exact_keys(monitor, MONITOR_KEYS, "[monitor]")
    if set(remote) == LEGACY_REMOTE_KEYS:
        host = remote.get("ssh_host") if isinstance(remote.get("ssh_host"), str) else ""
        legacy_sources: list[Any] = [{"id": "legacy", "label": host.strip() or "VPS", **remote}]
    else:
        _require_exact_keys(remote, REMOTE_KEYS, "[remote]")
        legacy_sources = remote.get("servers")
    if not isinstance(legacy_sources, list):
        raise ValueError("[remote].servers 必须是数组")
    sources: list[dict[str, Any]] = [{"id": "local-mihomo", "kind": "network.mihomo", "enabled": True}]
    for index, server in enumerate(legacy_sources):
        if not isinstance(server, dict):
            raise ValueError(f"远端服务器 #{index + 1} 必须是表")
        _require_exact_keys(server, LEGACY_SERVER_KEYS, f"远端服务器 #{index + 1}")
        sources.append({"kind": "network.linux-xray", **{key: value for key, value in server.items() if key != "billing_cycle_start_day"}})
    return _settings_from_contract({
        "schema": CONFIG_SCHEMA, "app": {"menu_bar_mode": "health"},
        "integrations": LocalIntegrationPaths().as_payload(),
        "policies": [{"id": "network-traffic-alerts", "kind": "traffic.threshold", "resource_id": "network", **monitor}],
        "sources": sources,
    }, "schema")


def _write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)


def _migrate_legacy_config(path: Path, raw: dict[str, Any]) -> UserSettings:
    settings = _legacy_settings(raw)
    backup = path.with_name(f"{path.stem}.pre-{CONFIG_SCHEMA}{path.suffix}")
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    _write_atomic(path, serialize_settings(settings))
    return settings


def _migrate_previous_config(path: Path, raw: dict[str, Any]) -> UserSettings:
    """One-time migration that adds optional local integration path overrides."""
    document = json.loads(json.dumps(raw))
    document["schema_version"] = CONFIG_SCHEMA
    document["integrations"] = LocalIntegrationPaths().as_payload()
    settings = _settings_from_contract(document, "schema_version")
    backup = path.with_name(f"{path.stem}.pre-{CONFIG_SCHEMA}{path.suffix}")
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    _write_atomic(path, serialize_settings(settings))
    return settings


def read_user_settings(path: Path) -> UserSettings:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    if raw.get("schema_version") == CONFIG_SCHEMA:
        return _settings_from_contract(raw, "schema_version")
    if raw.get("schema_version") == PREVIOUS_CONFIG_SCHEMA:
        return _migrate_previous_config(path, raw)
    return _migrate_legacy_config(path, raw)


def write_user_settings(path: Path, payload: dict[str, Any]) -> UserSettings:
    settings = parse_settings(payload)
    _write_atomic(path, serialize_settings(settings))
    return settings


def runtime_config(settings: UserSettings, state_dir: Path) -> Config:
    monitor = MonitorConfig(SAMPLE_SECONDS, settings.warning_window_minutes * 60, settings.warning_mib * 1024 * 1024,
                            settings.critical_window_minutes * 60, settings.critical_mib * 1024 * 1024)
    remotes = []
    for server in settings.remote_servers:
        ssh_executable = str(settings.integrations.ssh_executable) if settings.integrations.ssh_executable else None
        vps = VpsConfig(server_id=server["id"], label=server["label"], enabled=server["enabled"], ssh_host=server["ssh_host"],
                        interface=VPS_INTERFACE, poll_seconds=REMOTE_POLL_SECONDS, billing_mode=server["billing_mode"],
                        ssh_executable=ssh_executable)
        xray = XrayStatsConfig(server_id=server["id"], label=server["label"], enabled=server["enabled"] and server["xray_stats_enabled"],
                               ssh_host=server["ssh_host"], api_server=XRAY_API_SERVER, binary_path=XRAY_BINARY_PATH,
                               poll_seconds=REMOTE_POLL_SECONDS, expected_users=(), flagged_users=(),
                               ssh_executable=ssh_executable)
        remotes.append(RemoteServerConfig(server["id"], server["label"], vps, xray, TrafficEstimationConfig(server["billing_mode"])))
    billing_policies = tuple(
        BillingBudgetPolicy(
            id=f"{server['id']}-daily-usage",
            source_id=server["id"],
            label=server["label"],
            warning_bytes=int(server["usage_warning_gib"]) * GIB,
            critical_bytes=int(server["usage_critical_gib"]) * GIB,
        )
        for server in settings.remote_servers
        if server["usage_alert_enabled"]
    )
    return Config(monitor, StateConfig(), tuple(remotes), billing_policies, settings.integrations, state_dir)


def default_config_path() -> Path:
    if DEFAULT_CONFIG.exists(): return DEFAULT_CONFIG
    if SOURCE_EXAMPLE_CONFIG.exists(): return SOURCE_EXAMPLE_CONFIG
    return BUNDLED_EXAMPLE_CONFIG


def read_config(path: Path | None) -> Config:
    selected = path or default_config_path()
    state_dir = Path(os.environ[STATE_DIRECTORY_ENV]).expanduser() if os.environ.get(STATE_DIRECTORY_ENV) else selected.parent / "state"
    return runtime_config(read_user_settings(selected), state_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Infra Sentinel settings bridge")
    parser.add_argument("command", choices=("defaults", "export", "write"))
    parser.add_argument("config", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "defaults": settings = default_user_settings()
        elif args.config is None: raise ValueError("export 和 write 需要配置文件路径")
        elif args.command == "export": settings = read_user_settings(args.config)
        else:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict): raise ValueError("设置数据必须是 JSON 对象")
            settings = write_user_settings(args.config, payload)
        print(json.dumps(settings_payload(settings), ensure_ascii=False))
        return 0
    except (OSError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
