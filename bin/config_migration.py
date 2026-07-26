#!/usr/bin/env python3
"""Migrate installed Traffic Sentinel configuration without losing user rules."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tomllib


LEGACY_TABLES = {"reconciliation", "vps.diagnostics"}
TABLE_HEADER = re.compile(r"^\s*\[\[?([A-Za-z0-9_.-]+)\]\]?\s*(?:#.*)?$")


def _without_legacy_tables(text: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        match = TABLE_HEADER.match(line.rstrip("\r\n"))
        if match:
            skipping = match.group(1) in LEGACY_TABLES
        if not skipping:
            kept.append(line)
    return "".join(kept).rstrip() + "\n"


def migrate_config(path: Path) -> bool:
    """Replace obsolete comparison/diagnostic tables with empirical estimates."""
    original = path.read_text(encoding="utf-8")
    raw = tomllib.loads(original)
    cleaned = _without_legacy_tables(original)
    if isinstance(raw.get("estimation"), dict):
        migrated = cleaned
    else:
        legacy = raw.get("reconciliation", {})
        proxy_group = legacy.get("reference_group", "proxy") if isinstance(legacy, dict) else "proxy"
        migrated = cleaned + (
            "\n[estimation]\n"
            "# Conservative ceiling: 2 billable VPS legs × 20% measured link overhead.\n"
            f'proxy_group = "{str(proxy_group).replace(chr(34), chr(92) + chr(34))}"\n'
            "vps_billing_legs = 2.0\n"
            "link_overhead_ratio = 0.20\n"
        )
    if migrated == original:
        return False
    backup = path.with_suffix(path.suffix + ".pre-estimation")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(migrated, encoding="utf-8")
    temporary.replace(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    migrate_config(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
