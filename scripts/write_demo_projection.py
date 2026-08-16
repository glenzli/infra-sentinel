#!/usr/bin/env python3
"""Write a fixed, anonymous Projection for README screenshots.

This tool does not contact the local Agent, Mihomo, a VPS, a facility, or an
upstream provider. It exists solely to give the desktop shell a privacy-safe
document fixture through ``INFRA_SENTINEL_STATIC_PROJECTION``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SCHEMA = "20260812.1"
OBSERVED_AT = "2026-08-16T10:30:00+08:00"


def usage_window(tokens: int, *, cumulative: bool = False) -> dict[str, object]:
    return {
        "available": True,
        "tokens": tokens,
        "method": "demo-history" if cumulative else "demo-day",
        "detail": {"en": "anonymous demo", "zh": "匿名演示"},
        "started_at": "2026-08-16T00:00:00+08:00" if not cumulative else None,
    }


def source(identifier: str, kind: str, resource: str, label: str) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": kind,
        "resource_id": resource,
        "label": label,
        "enabled": True,
        "status": "ok",
        "updated_at": OBSERVED_AT,
    }


def facility(
    identifier: str,
    kind: str,
    label: str,
    metrics: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": kind,
        "instance_id": "local",
        "generation": "demo-20260816",
        "label": label,
        "status": "healthy",
        "observed_at": OBSERVED_AT,
        "console_url": "http://127.0.0.1:8765",
        "protocol": f"{kind}.status",
        "protocol_version": "20260816.1",
        "binding": "infra.local.unix-socket",
        "snapshot": {
            "schema": f"{kind}.status.snapshot",
            "schema_version": "20260816.1",
            "captured_at": OBSERVED_AT,
            "sequence": 12,
            "status": {"state": "healthy", "reason_codes": []},
            "headline_metrics": [metric["id"] for metric in metrics[:3]],
            "metrics": metrics,
            "issues": [],
        },
    }


def projection() -> dict[str, object]:
    ai_sources = [
        {
            "source_id": "ai.opencode",
            "label": "OpenCode",
            "status": "ok",
            "collection_method": "demo-desktop-metadata",
            "usage": {"today": usage_window(18_400_000), "cumulative": usage_window(246_000_000, cumulative=True)},
            "models": [{"id": "deepseek/deepseek-v4-flash", "tokens": 18_400_000}],
            "details": [],
        },
        {
            "source_id": "ai.codex",
            "label": "Codex",
            "status": "ok",
            "collection_method": "demo-local-state",
            "usage": {"today": usage_window(24_600_000), "cumulative": usage_window(612_000_000, cumulative=True)},
            "models": [
                {"id": "gpt-5.6-sol", "tokens": 16_200_000},
                {"id": "gpt-5.6-terra", "tokens": 8_400_000},
            ],
            "details": [],
        },
        {
            "source_id": "ai.infer-runtime",
            "label": "Infer Runtime",
            "status": "ok",
            "collection_method": "demo-host-local-day",
            "usage": {"today": usage_window(3_700_000), "cumulative": usage_window(58_000_000, cumulative=True)},
            "models": [{"id": "qwen3.5:4b-mlx", "tokens": 3_700_000}],
            "details": [],
        },
    ]
    facilities = [
        facility("pcp:local", "pcp", "PCP", [
            {"id": "requests.total", "kind": "counter", "value": 18_420, "unit": "count"},
            {"id": "requests.latency.p95_ms", "kind": "gauge", "value": 42, "unit": "ms"},
            {"id": "pcp.pages.current", "kind": "gauge", "value": 1240, "unit": "count"},
        ]),
        facility("infer-runtime:local", "infer-runtime", "Infer Runtime", [
            {"id": "infer.workload.active_attempts", "kind": "gauge", "value": 2, "unit": "count"},
            {"id": "infer.workload.queued_jobs", "kind": "gauge", "value": 1, "unit": "count"},
            {"id": "infer.resources.pressure", "kind": "state", "value": "normal"},
        ]),
        facility("dev-mesh-observer:local", "dev-mesh-observer", "Dev Mesh Observer", [
            {"id": "dev_mesh.workspaces.available", "kind": "gauge", "value": 9, "unit": "count"},
            {"id": "dev_mesh.collection.pending_events", "kind": "gauge", "value": 0, "unit": "count"},
            {"id": "dev_mesh.contentions.stalled", "kind": "gauge", "value": 0, "unit": "count"},
        ]),
    ]
    upstream = {
        "schema": "upstream.status@20260812.1",
        "status": "healthy",
        "total": 3,
        "healthy": 3,
        "attention": 0,
        "unknown": 0,
        "updated_at": OBSERVED_AT,
        "items": [
            {"id": "openai", "label": "OpenAI", "status": "healthy", "available": True, "description": "All API components operational", "observed_at": OBSERVED_AT, "status_url": "https://status.openai.com/", "components": [{"id": "api", "name": "API", "status": "operational", "level": "healthy"}], "incidents": []},
            {"id": "claude", "label": "Claude", "status": "healthy", "available": True, "description": "All API components operational", "observed_at": OBSERVED_AT, "status_url": "https://status.claude.com/", "components": [{"id": "api", "name": "Claude API", "status": "operational", "level": "healthy"}], "incidents": []},
            {"id": "deepseek", "label": "DeepSeek", "status": "healthy", "available": True, "description": "All API components operational", "observed_at": OBSERVED_AT, "status_url": "https://status.deepseek.com/", "components": [{"id": "api", "name": "API service", "status": "operational", "level": "healthy"}], "incidents": []},
        ],
    }
    system = {
        "schema": "system.resources@20260812.1",
        "available": True,
        "platform": "macos",
        "capabilities": ["cpu.utilization", "memory.pressure", "memory.capacity", "memory.swap", "disk.capacity", "disk.throughput", "disk.health", "disk.process-attribution", "thermal.pressure"],
        "status": "healthy",
        "quality": "exact",
        "reasons": [],
        "observed_at": OBSERVED_AT,
        "cpu": {"percent": 27.4},
        "memory": {"pressure": "normal", "pressure_exact": True, "total_bytes": 34_359_738_368, "available_bytes": 12_884_901_888, "compressed_bytes": 2_147_483_648, "swap_used_bytes": 0, "swapin_bytes_per_second": 0, "swapout_bytes_per_second": 0},
        "disk": {
            "total_bytes": 1_000_204_886_016,
            "free_bytes": 418_759_311_360,
            "used_percent": 58.1,
            "read_bytes_per_second": 5_767_168,
            "write_bytes_per_second": 2_621_440,
            "read_iops": 42,
            "write_iops": 18,
            "physical_io_available": True,
            "attribution": {"available": True, "ready": True, "coverage_ratio": 0.82, "attributed_read_bytes_per_second": 4_718_592, "attributed_write_bytes_per_second": 2_097_152, "unattributed_read_bytes_per_second": 1_048_576, "unattributed_write_bytes_per_second": 524_288, "observed_processes": 32, "skipped_processes": 4, "apps": [{"id": "infra-sentinel", "label": "Infra Sentinel", "read_bytes_per_second": 524_288, "write_bytes_per_second": 262_144, "process_count": 2}, {"id": "developer-tools", "label": "Developer Tools", "read_bytes_per_second": 3_145_728, "write_bytes_per_second": 1_572_864, "process_count": 4}]},
            "health": {"state": "healthy", "observed_at": OBSERVED_AT, "reason_codes": [], "read_errors": 0, "write_errors": 0, "read_retries": 0, "write_retries": 0, "interval_seconds": 21_600},
        },
        "thermal": {"state": "normal"},
        "persistence": {"interval_seconds": 900},
        "privacy": "anonymous demo",
    }
    resources = [
        {"id": "network", "category": "usage", "status": "healthy", "enabled": True, "primary_metric": "network.bytes", "primary_value": 41_265_381_888, "primary_unit": "bytes", "primary_source_id": "local-mihomo", "source_count": 3, "online_source_count": 3},
        {"id": "ai_usage", "category": "usage", "status": "healthy", "enabled": True, "primary_metric": "ai.tokens.total", "primary_value": 46_700_000, "primary_unit": "tokens", "primary_source_id": "ai.codex", "source_count": 3, "online_source_count": 3},
        {"id": "system", "category": "runtime", "status": "healthy", "enabled": True, "primary_metric": "system.cpu.percent", "primary_value": 27.4, "primary_unit": "percent", "primary_source_id": "system.local", "source_count": 1, "online_source_count": 1},
        {"id": "upstream_status", "category": "dependency", "status": "healthy", "enabled": True, "primary_metric": "upstream.providers.healthy", "primary_value": 3, "primary_unit": "count", "primary_source_id": "upstream.status", "source_count": 3, "online_source_count": 3},
    ]
    return {
        "schema": SCHEMA,
        "updated_at": OBSERVED_AT,
        "protocol": {"schema": SCHEMA, "transport": "local-files"},
        "infra": {
            "overall": {"status": "healthy", "active_alerts": 0},
            "resources": resources,
            "sources": [
                source("local-mihomo", "network.mihomo", "network", "Mihomo"),
                source("vps-demo-a", "network.linux-xray", "network", "VPS Atlas"),
                source("vps-demo-b", "network.linux-xray", "network", "VPS Nova"),
                source("ai.opencode", "ai.opencode", "ai_usage", "OpenCode"),
                source("ai.codex", "ai.codex", "ai_usage", "Codex"),
                source("ai.infer-runtime", "ai.infer-runtime", "ai_usage", "Infer Runtime"),
                source("system.local", "system.resources", "system", "This Mac"),
                source("upstream.status", "upstream.status", "upstream_status", "Official status"),
            ],
            "collectors": [],
            "network_diagnostics": {},
            "ai_usage": {
                "schema": "ai.usage@20260812.1",
                "aggregate": {"today": usage_window(46_700_000), "cumulative": usage_window(916_000_000, cumulative=True)},
                "sources": ai_sources,
            },
            "facilities": {"schema": "infra.facilities@20260812.1", "status": "healthy", "total": 3, "healthy": 3, "attention": 0, "items": facilities},
            "upstream_status": upstream,
            "system": system,
        },
        "session": {"duration_seconds": 98_460, "remote_servers": [{"id": "vps-demo-a"}, {"id": "vps-demo-b"}]},
        "vps": {"daily_usage_guards": [
            {"label": "VPS Atlas", "level": "none", "usage_bytes": 18_253_611_008, "warning_bytes": 53_687_091_200, "critical_bytes": 85_899_345_920},
            {"label": "VPS Nova", "level": "none", "usage_bytes": 14_495_514_624, "warning_bytes": 53_687_091_200, "critical_bytes": 85_899_345_920},
        ]},
        "xray_stats": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="write anonymous Infra Sentinel screenshot Projection")
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    (args.state_dir / "projection.json").write_text(
        json.dumps(projection(), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
