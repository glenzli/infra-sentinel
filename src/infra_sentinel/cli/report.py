#!/usr/bin/env python3
"""Print a time-bounded local attribution and VPS traffic summary."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time
from typing import Any, Iterable

from infra_sentinel.app.configuration import Config, read_config
from infra_sentinel.app.agent import SAMPLE_SCHEMA, format_bytes, iter_jsonl
from infra_sentinel.resources.network.vps import SUPPORTED_VPS_SAMPLE_SCHEMAS, iter_vps_samples


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def records(config: Config, prefix: str) -> Iterable[dict[str, Any]]:
    for path in sorted(config.state_dir.glob(f"{prefix}*.jsonl"), key=lambda item: item.stat().st_mtime):
        yield from iter_jsonl(path)


def sum_local_traffic(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "up_bytes": 0,
        "down_bytes": 0,
        "sample_count": 0,
        "services": {},
    }
    for sample in samples:
        traffic = sample.get("kernel", {})
        result["up_bytes"] += int(traffic.get("up_bytes", 0))
        result["down_bytes"] += int(traffic.get("down_bytes", 0))
        result["sample_count"] += 1
        for service in sample.get("services", []):
            if not isinstance(service, dict):
                continue
            service_id = str(service.get("id", "unknown_host"))
            current = result["services"].setdefault(
                service_id,
                {"label": str(service.get("label", service_id)), "bytes": 0},
            )
            current["bytes"] += int(service.get("total_bytes", 0))
    return result


def sum_vps_traffic(samples: Iterable[dict[str, Any]]) -> dict[str, int]:
    result = {"in_bytes": 0, "out_bytes": 0, "sample_count": 0}
    for sample in samples:
        result["in_bytes"] += int(sample.get("in_bytes", 0))
        result["out_bytes"] += int(sample.get("out_bytes", 0))
        result["sample_count"] += 1
    result["total_bytes"] = result["in_bytes"] + result["out_bytes"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 Traffic Sentinel 的 Mihomo 与 VPS 记录")
    parser.add_argument("--config", type=Path, help="TOML 配置文件")
    parser.add_argument("--hours", type=float, default=24, help="最近多少小时；默认 24")
    parser.add_argument("--since", help="开始时间，ISO 8601，例如 2026-07-26T09:00:00+08:00")
    parser.add_argument("--until", help="结束时间，ISO 8601；默认现在")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        until = parse_timestamp(args.until) if args.until else time.time()
        since = parse_timestamp(args.since) if args.since else until - args.hours * 3600
        if since > until:
            raise ValueError("--since 不能晚于 --until")
        local_samples = [sample for sample in records(config, "samples") if sample.get("schema") == SAMPLE_SCHEMA and since <= float(sample.get("epoch", -1)) <= until]
        vps_by_server: list[tuple[Any, list[dict[str, Any]]]] = []
        for server in config.remote_servers:
            server_dir = config.state_dir / "remote" / server.id
            samples = [sample for sample in iter_vps_samples(server_dir)
                       if sample.get("schema") in SUPPORTED_VPS_SAMPLE_SCHEMAS
                       and since <= float(sample.get("epoch", -1)) <= until]
            vps_by_server.append((server, samples))
        matching_events = [event for event in records(config, "events") if isinstance(event.get("timestamp"), str) and event.get("sample", {}).get("schema") == SAMPLE_SCHEMA and since <= parse_timestamp(event["timestamp"]) <= until]
        print(f"时间段：{datetime.fromtimestamp(since).astimezone().isoformat(timespec='seconds')} 至 {datetime.fromtimestamp(until).astimezone().isoformat(timespec='seconds')}")
        print(f"本机采样数：{len(local_samples)}")
        traffic = sum_local_traffic(local_samples)
        print(f"Mihomo 本机总量：↑ {format_bytes(traffic['up_bytes'])}  ↓ {format_bytes(traffic['down_bytes'])}  合计 {format_bytes(traffic['up_bytes'] + traffic['down_bytes'])}")
        for service in sorted(traffic["services"].values(), key=lambda item: item["bytes"], reverse=True)[:10]:
            print(f"  {service['label']}：{format_bytes(service['bytes'])}")
        enabled_servers = [(server, sum_vps_traffic(samples)) for server, samples in vps_by_server if server.vps.enabled]
        if enabled_servers:
            total_in = sum(item["in_bytes"] for _, item in enabled_servers)
            total_out = sum(item["out_bytes"] for _, item in enabled_servers)
            print(f"VPS 网卡合计（入 + 出）：入 {format_bytes(total_in)}  出 {format_bytes(total_out)}  T {format_bytes(total_in + total_out)}")
            for server, item in enabled_servers:
                billable = server.estimation.billable_bytes(item["in_bytes"], item["out_bytes"])
                print(f"  {server.label}：入 {format_bytes(item['in_bytes'])}  出 {format_bytes(item['out_bytes'])}  T {format_bytes(billable)}")
        else:
            print("VPS 网卡：配置中未启用")
        if matching_events:
            by_type: dict[str, int] = {}
            for event in matching_events:
                by_type[event.get("type", "unknown")] = by_type.get(event.get("type", "unknown"), 0) + 1
            print("事件：" + "，".join(f"{kind} {count}" for kind, count in sorted(by_type.items())))
        else:
            print("事件：无")
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
