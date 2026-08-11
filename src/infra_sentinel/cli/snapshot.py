#!/usr/bin/env python3
"""Create privacy-bounded Mihomo attribution snapshots for traffic alerts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid
from typing import Any

from infra_sentinel.app.configuration import Config, read_config
from infra_sentinel.app.agent import ALERT_WINDOW_FILENAME, ensure_state_dir, iso_now, latest_jsonl


def create_snapshot(config: Config, event: dict[str, Any]) -> Path:
    """Persist aggregate domains and counters without packet or content capture."""
    ensure_state_dir(config)
    sample = event.get("sample", {})
    snapshot = {
        "schema": 2,
        "created_at": iso_now(),
        "event": {
            "id": event.get("id"),
            "type": event.get("type"),
            "level": event.get("level"),
            "timestamp": event.get("timestamp"),
        },
        "privacy": {
            "packet_capture": False,
            "request_contents_read": False,
            "prompts_recorded": False,
            "paths_recorded": False,
            "active_domains_only": True,
        },
        "mihomo": {
            "kernel": sample.get("kernel", {}),
            "services": sample.get("services", []),
            "routes": sample.get("routes", {}),
            "attribution": sample.get("attribution", {}),
            "active_connections": sample.get("active_connections", 0),
        },
    }
    target = config.state_dir / "snapshots" / f"{event.get('id', uuid.uuid4().hex)}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def manual_event(config: Config) -> dict[str, Any]:
    sample = None
    try:
        checkpoint = json.loads((config.state_dir / ALERT_WINDOW_FILENAME).read_text(encoding="utf-8"))
        samples = checkpoint.get("samples", []) if isinstance(checkpoint, dict) else []
        if isinstance(samples, list) and samples:
            sample = samples[-1]
    except (OSError, json.JSONDecodeError):
        pass
    if not isinstance(sample, dict):
        sample = latest_jsonl(config.state_dir / "samples.jsonl")
    if sample is None:
        raise RuntimeError("尚无本地采样；请先打开 Infra Sentinel App")
    return {
        "id": f"manual-{uuid.uuid4().hex}",
        "type": "manual",
        "level": "manual",
        "timestamp": iso_now(),
        "sample": sample,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成不含请求内容的 Mihomo 流量证据快照")
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
