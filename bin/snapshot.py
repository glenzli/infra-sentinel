#!/usr/bin/env python3
"""Create privacy-bounded evidence snapshots for traffic alert events."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Any

from sentinel import Config, ensure_state_dir, iso_now, latest_jsonl, read_config


MAX_CONNECTIONS = 100


def connection_summary(pids: list[int]) -> list[dict[str, Any]]:
    """Record only endpoint summaries for alert-correlated Codex PIDs."""
    summaries: list[dict[str, Any]] = []
    for pid in sorted(set(pids)):
        try:
            completed = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:ESTABLISHED", "-Fpcn"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        current: dict[str, Any] = {"pid": pid, "process": None, "connections": []}
        for line in completed.stdout.splitlines():
            marker, value = line[0], line[1:]
            if marker == "p":
                current["pid"] = int(value) if value.isdigit() else pid
            elif marker == "c":
                current["process"] = value
            elif marker == "n" and len(current["connections"]) < MAX_CONNECTIONS:
                current["connections"].append(value)
        if current["connections"]:
            summaries.append(current)
    return summaries


def create_snapshot(config: Config, event: dict[str, Any]) -> Path:
    """Persist only process and network metadata. No workspace traversal occurs."""
    ensure_state_dir(config)
    alert_group = str(event.get("alert_group", config.monitor.alert_group))
    group_processes = event.get("sample", {}).get("processes", {}).get(alert_group, [])
    pids = [item["pid"] for item in group_processes if isinstance(item.get("pid"), int)]
    snapshot = {
        "schema": 1,
        "created_at": iso_now(),
        "event": {
            "id": event.get("id"),
            "type": event.get("type"),
            "level": event.get("level"),
            "timestamp": event.get("timestamp"),
        },
        "privacy": {
            "packet_capture": False,
            "file_contents_read": False,
            "prompts_recorded": False,
            "workspace_traversal": False,
        },
        "alert_group": alert_group,
        "process_deltas": group_processes,
        "connections": connection_summary(pids),
        "active_processes": [
            {"pid": item.get("pid"), "name": item.get("name")}
            for item in group_processes
        ],
    }
    target = config.state_dir / "snapshots" / f"{event.get('id', uuid.uuid4().hex)}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def manual_event(config: Config) -> dict[str, Any]:
    sample = latest_jsonl(config.state_dir / "samples.jsonl")
    if sample is None:
        raise RuntimeError("尚无 samples.jsonl；请先打开 Codex Traffic Sentinel App")
    return {
        "id": f"manual-{uuid.uuid4().hex}",
        "type": "manual",
        "level": "manual",
        "timestamp": iso_now(),
        "sample": sample,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成不含内容与提示词的 Codex 流量证据快照")
    parser.add_argument("--config", type=Path, help="TOML 配置文件")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        path = create_snapshot(config, manual_event(config))
        print(path)
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
