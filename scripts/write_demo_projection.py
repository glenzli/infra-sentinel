#!/usr/bin/env python3
"""Write an anonymous Projection for reproducible README screenshots.

The fixture uses fixed values anchored to the local capture day. It does not
contact the Agent, Mihomo, a VPS, a facility, or an upstream provider.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from infra_sentinel.resources.ai.contract import (
    ai_usage_snapshot,
    daily_usage,
    hourly_usage,
    localized,
    model_usage,
    pricing_day,
    usage_window,
)


SCHEMA = "20260812.1"


def source(
    identifier: str,
    kind: str,
    resource: str,
    label: str,
    observed_at: str,
) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": kind,
        "resource_id": resource,
        "label": label,
        "enabled": True,
        "status": "ok",
        "updated_at": observed_at,
    }


def facility(
    identifier: str,
    kind: str,
    label: str,
    metrics: list[dict[str, object]],
    observed_at: str,
) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": kind,
        "instance_id": "local",
        "generation": "anonymous-demo",
        "label": label,
        "status": "healthy",
        "observed_at": observed_at,
        "console_url": "http://127.0.0.1:8765",
        "protocol": f"{kind}.status",
        "protocol_version": "20260816.1",
        "binding": "infra.local.unix-socket",
        "snapshot": {
            "schema": f"{kind}.status.snapshot",
            "schema_version": "20260816.1",
            "captured_at": observed_at,
            "sequence": 12,
            "status": {"state": "healthy", "reason_codes": []},
            "headline_metrics": [metric["id"] for metric in metrics[:3]],
            "metrics": metrics,
            "issues": [],
        },
    }


def _hourly_rows(
    observed: datetime,
    rows: list[tuple[int, list[tuple[str, int]], bool]],
) -> list[dict[str, Any]]:
    available = [
        (hour, models, estimated)
        for hour, models, estimated in rows
        if hour <= observed.hour
    ]
    if not available:
        available = [(observed.hour, rows[0][1], True)]
    return [
        hourly_usage(
            observed.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp(),
            sum(tokens for _, tokens in models),
            [{"id": model, "tokens": tokens} for model, tokens in models],
            estimated=estimated,
        )
        for hour, models, estimated in available
    ]


def _demo_ai_source(
    *,
    source_id: str,
    label: str,
    observed: datetime,
    collection_method: str,
    hourly_rows: list[tuple[int, list[tuple[str, int]], bool]],
    cumulative_models: dict[str, int],
    pricing_kind: str | None = None,
    pricing_models: dict[str, float] | None = None,
) -> dict[str, Any]:
    hourly = _hourly_rows(observed, hourly_rows)
    today_models: dict[str, int] = {}
    for row in hourly:
        for model in row["models"]:
            identifier = str(model["id"])
            today_models[identifier] = today_models.get(identifier, 0) + int(model["tokens"])
    today_total = sum(today_models.values())
    cumulative_total = sum(cumulative_models.values())
    started_at = datetime.fromtimestamp(
        min(int(row["epoch"]) for row in hourly),
        tz=observed.tzinfo,
    ).isoformat(timespec="seconds")
    models = [
        model_usage(
            identifier,
            today_tokens=today_models.get(identifier, 0),
            cumulative_tokens=cumulative_models.get(identifier, 0),
            today_method="anonymous-demo-day",
            today_detail=localized("anonymous demo", "匿名演示"),
            cumulative_method="anonymous-demo-history",
            cumulative_detail=localized("anonymous demo", "匿名演示"),
        )
        for identifier in dict.fromkeys((*cumulative_models, *today_models))
    ]
    pricing = None
    if pricing_kind and pricing_models:
        price_rows = [
            {
                "id": identifier,
                "cost_usd": pricing_models.get(identifier, 0.0),
                "priced_tokens": today_models.get(identifier, 0),
            }
            for identifier in today_models
            if pricing_models.get(identifier, 0.0) > 0
        ]
        priced_tokens = sum(int(row["priced_tokens"]) for row in price_rows)
        pricing = [
            pricing_day(
                observed.date().isoformat(),
                kind=pricing_kind,
                cost_usd=sum(float(row["cost_usd"]) for row in price_rows),
                priced_tokens=priced_tokens,
                unpriced_tokens=max(0, today_total - priced_tokens),
                models=price_rows,
            )
        ]
    return ai_usage_snapshot(
        source_id=source_id,
        label=label,
        status="ok",
        observed_at=observed.isoformat(timespec="seconds"),
        collection_method=collection_method,
        today=usage_window(
            today_total,
            method="anonymous-demo-day",
            detail=localized("anonymous demo", "匿名演示"),
            started_at=started_at,
        ),
        cumulative=usage_window(
            cumulative_total,
            method="anonymous-demo-history",
            detail=localized("anonymous demo", "匿名演示"),
        ),
        models=models,
        details=[],
        confidence="high",
        privacy="anonymous-demo",
        daily_history=[
            daily_usage(
                observed.date().isoformat(),
                today_total,
                [{"id": identifier, "tokens": tokens} for identifier, tokens in today_models.items()],
            )
        ],
        hourly_history=hourly,
        hourly_method="anonymous-demo-event-hour",
        pricing_history=pricing,
    )


def projection(observed: datetime | None = None) -> dict[str, object]:
    observed = observed or datetime.now().astimezone()
    observed_at = observed.isoformat(timespec="seconds")
    ai_sources = [
        _demo_ai_source(
            source_id="opencode",
            label="OpenCode",
            observed=observed,
            collection_method="desktop-message-metadata",
            hourly_rows=[
                (5, [("deepseek/deepseek-v4-flash", 6_200_000)], False),
                (11, [("deepseek/deepseek-v4-flash", 7_400_000)], False),
                (17, [("deepseek/deepseek-v4-flash", 4_800_000)], False),
            ],
            cumulative_models={"deepseek/deepseek-v4-flash": 246_000_000},
            pricing_kind="provider-reported-cost",
            pricing_models={"deepseek/deepseek-v4-flash": 26.40},
        ),
        _demo_ai_source(
            source_id="codex",
            label="Codex",
            observed=observed,
            collection_method="local-rollout-jsonl",
            hourly_rows=[
                (2, [("gpt-5.6-sol", 2_400_000)], False),
                (8, [("gpt-5.6-sol", 4_800_000)], False),
                (11, [("gpt-5.6-terra", 3_600_000)], False),
                (14, [("gpt-5.6-sol", 5_400_000)], False),
                (17, [("gpt-5.6-sol", 3_600_000), ("gpt-5.6-terra", 2_400_000)], False),
                (20, [("gpt-5.6-terra", 2_400_000)], False),
            ],
            cumulative_models={"gpt-5.6-sol": 420_000_000, "gpt-5.6-terra": 192_000_000},
            pricing_kind="local-rollout-standard-api-projection",
            pricing_models={"gpt-5.6-sol": 312.00, "gpt-5.6-terra": 72.00},
        ),
        _demo_ai_source(
            source_id="antigravity",
            label="Antigravity",
            observed=observed,
            collection_method="local-generation-metadata",
            hourly_rows=[
                (8, [("gemini-3.1-pro", 2_100_000)], False),
                (14, [("gemini-3.1-pro", 3_200_000)], False),
                (20, [("claude-sonnet-4-6", 2_500_000)], True),
            ],
            cumulative_models={"gemini-3.1-pro": 84_000_000, "claude-sonnet-4-6": 34_000_000},
            pricing_kind="catalog-text-api-reference",
            pricing_models={"gemini-3.1-pro": 18.90, "claude-sonnet-4-6": 25.00},
        ),
        _demo_ai_source(
            source_id="infer-runtime",
            label="Infer Runtime",
            observed=observed,
            collection_method="host-local-sampling",
            hourly_rows=[
                (11, [("qwen3.5:4b-mlx", 1_500_000)], True),
                (17, [("qwen3.5:4b-mlx", 2_200_000)], True),
            ],
            cumulative_models={"qwen3.5:4b-mlx": 58_000_000},
        ),
    ]
    today_tokens = sum(int(item["usage"]["today"]["tokens"]) for item in ai_sources)
    cumulative_tokens = sum(int(item["usage"]["cumulative"]["tokens"]) for item in ai_sources)
    facilities = [
        facility("pcp:local", "pcp", "PCP", [
            {"id": "requests.total", "kind": "counter", "value": 18_420, "unit": "count"},
            {"id": "requests.latency.p95_ms", "kind": "gauge", "value": 42, "unit": "ms"},
            {"id": "pcp.pages.current", "kind": "gauge", "value": 1240, "unit": "count"},
        ], observed_at),
        facility("infer-runtime:local", "infer-runtime", "Infer Runtime", [
            {"id": "infer.workload.active_attempts", "kind": "gauge", "value": 2, "unit": "count"},
            {"id": "infer.workload.queued_jobs", "kind": "gauge", "value": 1, "unit": "count"},
            {"id": "infer.resources.pressure", "kind": "state", "value": "normal"},
        ], observed_at),
        facility("dev-mesh-observer:local", "dev-mesh-observer", "Dev Mesh Observer", [
            {"id": "dev_mesh.workspaces.available", "kind": "gauge", "value": 9, "unit": "count"},
            {"id": "dev_mesh.collection.pending_events", "kind": "gauge", "value": 0, "unit": "count"},
            {"id": "dev_mesh.contentions.stalled", "kind": "gauge", "value": 0, "unit": "count"},
        ], observed_at),
    ]
    upstream = {
        "schema": "upstream.status@20260812.1",
        "status": "healthy",
        "total": 3,
        "healthy": 3,
        "attention": 0,
        "unknown": 0,
        "updated_at": observed_at,
        "items": [
            {"id": "openai", "label": "OpenAI", "status": "healthy", "available": True, "description": "All API components operational", "observed_at": observed_at, "status_url": "https://status.openai.com/", "components": [{"id": "api", "name": "API", "status": "operational", "level": "healthy"}], "incidents": []},
            {"id": "claude", "label": "Claude", "status": "healthy", "available": True, "description": "All API components operational", "observed_at": observed_at, "status_url": "https://status.claude.com/", "components": [{"id": "api", "name": "Claude API", "status": "operational", "level": "healthy"}], "incidents": []},
            {"id": "deepseek", "label": "DeepSeek", "status": "healthy", "available": True, "description": "All API components operational", "observed_at": observed_at, "status_url": "https://status.deepseek.com/", "components": [{"id": "api", "name": "API service", "status": "operational", "level": "healthy"}], "incidents": []},
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
        "observed_at": observed_at,
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
            "health": {"state": "healthy", "observed_at": observed_at, "reason_codes": [], "read_errors": 0, "write_errors": 0, "read_retries": 0, "write_retries": 0, "interval_seconds": 21_600},
        },
        "thermal": {"state": "normal"},
        "persistence": {"interval_seconds": 900},
        "privacy": "anonymous demo",
    }
    resources = [
        {"id": "network", "category": "usage", "status": "healthy", "enabled": True, "primary_metric": "network.bytes", "primary_value": 41_265_381_888, "primary_unit": "bytes", "primary_source_id": "local-mihomo", "source_count": 3, "online_source_count": 3},
        {"id": "ai_usage", "category": "usage", "status": "healthy", "enabled": True, "primary_metric": "ai.tokens.total", "primary_value": today_tokens, "primary_unit": "tokens", "primary_source_id": "codex", "source_count": 4, "online_source_count": 4},
        {"id": "system", "category": "runtime", "status": "healthy", "enabled": True, "primary_metric": "system.cpu.percent", "primary_value": 27.4, "primary_unit": "percent", "primary_source_id": "system.local", "source_count": 1, "online_source_count": 1},
        {"id": "upstream_status", "category": "dependency", "status": "healthy", "enabled": True, "primary_metric": "upstream.providers.healthy", "primary_value": 3, "primary_unit": "count", "primary_source_id": "upstream.status", "source_count": 3, "online_source_count": 3},
    ]
    all_sources = [
        source("local-mihomo", "network.mihomo", "network", "Mihomo", observed_at),
        source("vps-demo-a", "network.linux-xray", "network", "VPS Atlas", observed_at),
        source("vps-demo-b", "network.linux-xray", "network", "VPS Nova", observed_at),
        source("opencode", "ai.opencode", "ai_usage", "OpenCode", observed_at),
        source("codex", "ai.codex", "ai_usage", "Codex", observed_at),
        source("antigravity", "ai.antigravity", "ai_usage", "Antigravity", observed_at),
        source("infer-runtime", "ai.infer-runtime", "ai_usage", "Infer Runtime", observed_at),
        source("system.local", "system.resources", "system", "This Mac", observed_at),
        source("upstream.status", "upstream.status", "upstream_status", "Official status", observed_at),
    ]
    return {
        "schema": SCHEMA,
        "updated_at": observed_at,
        "protocol": {"schema": SCHEMA, "transport": "local-files"},
        "infra": {
            "overall": {"status": "healthy", "active_alerts": 0},
            "resources": resources,
            "sources": all_sources,
            "collectors": [],
            "network_diagnostics": {},
            "ai_usage": {
                "schema": "ai.usage@20260824.1",
                "aggregate": {
                    "today": usage_window(today_tokens, method="anonymous-demo-day", detail=localized("anonymous demo", "匿名演示")),
                    "cumulative": usage_window(cumulative_tokens, method="anonymous-demo-history", detail=localized("anonymous demo", "匿名演示")),
                },
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
