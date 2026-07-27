#!/usr/bin/env python3
"""Migrate installed Traffic Sentinel configuration to Mihomo-native analysis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib
from typing import Any


LEGACY_TABLES = {
    "codex_activity",
    "process_groups",
    "reconciliation",
    "vps.diagnostics",
}
TABLE_HEADER = re.compile(r"^\s*\[\[?([A-Za-z0-9_.-]+)\]\]?\s*(?:#.*)?$")
CONFIG_KEY = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")
LEGACY_HOOK_MARKER = "--traffic-sentinel-capture"


def _without_obsolete_config(text: str) -> str:
    kept: list[str] = []
    skipping = False
    current_table = ""
    for line in text.splitlines(keepends=True):
        match = TABLE_HEADER.match(line.rstrip("\r\n"))
        if match:
            current_table = match.group(1)
            skipping = current_table in LEGACY_TABLES
        if skipping:
            continue
        key = CONFIG_KEY.match(line)
        obsolete_key = (
            (current_table == "monitor" and key and key.group(1) == "alert_group")
            or (
                current_table == "estimation"
                and key
                and key.group(1) in {"link_overhead_ratio", "proxy_group"}
            )
        )
        if obsolete_key:
            continue
        kept.append(line)
    return "".join(kept).rstrip() + "\n"


def migrate_config(path: Path) -> bool:
    """Remove process attribution while retaining remote observer settings."""
    original = path.read_text(encoding="utf-8")
    cleaned = _without_obsolete_config(original)
    cleaned_raw = tomllib.loads(cleaned)
    migrated = cleaned
    if not isinstance(cleaned_raw.get("estimation"), dict):
        migrated += (
            "\n[estimation]\n"
            "# Provider billing counts the configured number of VPS traffic legs.\n"
            "vps_billing_legs = 2.0\n"
        )
    if migrated == original:
        return False
    backup = path.with_suffix(path.suffix + ".pre-mihomo")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(migrated, encoding="utf-8")
    temporary.replace(path)
    return True


def _is_legacy_hook(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and LEGACY_HOOK_MARKER in str(handler.get("command", ""))
    )


def remove_legacy_codex_hooks(hooks_path: Path | None = None) -> bool:
    """Remove only this App's retired hook while preserving every other hook."""
    selected = hooks_path or (Path.home() / ".codex" / "hooks.json")
    try:
        original = selected.read_text(encoding="utf-8")
        root = json.loads(original)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(root, dict) or not isinstance(root.get("hooks"), dict):
        return False
    changed = False
    hooks = root["hooks"]
    for event, raw_groups in list(hooks.items()):
        if not isinstance(raw_groups, list):
            continue
        cleaned_groups: list[Any] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict) or not isinstance(raw_group.get("hooks"), list):
                cleaned_groups.append(raw_group)
                continue
            handlers = [handler for handler in raw_group["hooks"] if not _is_legacy_hook(handler)]
            if len(handlers) != len(raw_group["hooks"]):
                changed = True
            if handlers:
                group = dict(raw_group)
                group["hooks"] = handlers
                cleaned_groups.append(group)
        hooks[event] = cleaned_groups
    if not changed:
        return False
    backup = selected.with_suffix(selected.suffix + ".pre-domain-attribution")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    temporary = selected.with_name(f".{selected.name}.traffic-sentinel.tmp")
    temporary.write_text(json.dumps(root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(selected)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    migrate_config(args.config)
    remove_legacy_codex_hooks()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
