"""Read-only projection of accepted isolated CLI usage receipts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.resources.ai.antigravity_pricing import (
    estimate_antigravity_text_api_cost, resolve_antigravity_reference,
)
from infra_sentinel.resources.ai.codex_pricing import estimate_standard_api_cost
from infra_sentinel.resources.ai.contract import (
    ai_usage_snapshot, daily_usage, detail_group, hourly_usage, localized,
    model_usage, pricing_day, token_metric, usage_window,
)
from infra_sentinel.resources.ai.pricing_catalog import PriceCatalogLookup, bundled_pricing_catalog


class CliUsageReceiptCollector:
    capability = CollectorCapability(
        id="ai.cli-usage-receipts", source_id="cli-receipts",
        source_kind="ai.cli-receipts", resource_id="ai_usage", metrics=(),
    )

    def __init__(self, summary: Callable[[], dict[str, Any]], *, pricing_catalog: PriceCatalogLookup | None = None) -> None:
        self._summary = summary
        self._pricing_catalog = pricing_catalog or bundled_pricing_catalog()

    def _pricing(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, object]]]:
        days: dict[str, dict[str, dict[str, Any]]] = {}
        references: dict[str, dict[str, object]] = {}
        for row in rows:
            day, client, model = row["date"], row["client"], row["model"]
            if client == "agy":
                resolved = resolve_antigravity_reference(model, catalog=self._pricing_catalog, usage_date=day)
                reference, price = resolved.projection(), resolved.price
            else:
                price = self._pricing_catalog.price_for("codex", model, day)
                reference = {"model": model, "match": "exact" if price else "unpriced", "available": price is not None}
            references[model] = reference
            aggregate = days.setdefault(day, {}).setdefault(model, {
                "id": model, "cost_usd": 0.0, "priced_tokens": 0, "unpriced_tokens": 0,
                "price_reference": reference,
            })
            cached = row["cached_input_tokens"]
            cache_write = row["cache_write_input_tokens"]
            if (price is None or cached is None
                or (client == "codex" and price.cache_write_per_million is not None and cache_write is None)
                or (client == "agy" and cache_write)):
                aggregate["unpriced_tokens"] += row["total_tokens"]
                continue
            if client == "agy":
                # Receipt input already includes cache and output includes
                # reasoning; the AGY history estimator expects cache separate.
                estimate = estimate_antigravity_text_api_cost({model: {
                    "input_tokens": row["input_tokens"] - cached,
                    "cache_read_tokens": cached,
                    "output_tokens": row["output_tokens"],
                }}, catalog=self._pricing_catalog, usage_date=day)
            else:
                estimate = estimate_standard_api_cost({model: {
                    "input_tokens": row["input_tokens"],
                    "cached_input_tokens": cached,
                    "cache_write_input_tokens": cache_write or 0,
                    "output_tokens": row["output_tokens"],
                    "total_tokens": row["total_tokens"],
                }}, catalog=self._pricing_catalog, usage_date=day)
            aggregate["cost_usd"] += estimate.total_cost_usd
            aggregate["priced_tokens"] += estimate.priced_tokens
            aggregate["unpriced_tokens"] += estimate.unpriced_tokens
        return [pricing_day(
            day, kind="catalog-text-api-reference",
            cost_usd=sum(row["cost_usd"] for row in models.values()),
            priced_tokens=sum(row["priced_tokens"] for row in models.values()),
            unpriced_tokens=sum(row["unpriced_tokens"] for row in models.values()),
            models=list(models.values()),
        ) for day, models in sorted(days.items())], references

    def collect(self, context: CollectorContext) -> Collection:
        data = self._summary()
        rows = data["daily"]
        if not rows:
            return Collection(status="unavailable")
        epoch = float(context.local_sample["epoch"])
        observed = datetime.fromtimestamp(epoch).astimezone()
        today = observed.date().isoformat()
        days: dict[str, dict[str, int]] = {}
        models: dict[str, int] = {}
        for row in rows:
            model = row["model"]
            tokens = row["total_tokens"]
            day = days.setdefault(row["date"], {})
            day[model] = day.get(model, 0) + tokens
            models[model] = models.get(model, 0) + tokens
        today_rows = [row for row in rows if row["date"] == today]
        today_models = days.get(today, {})
        pricing, references = self._pricing(data.get("pricing", []))
        detail = localized(
            "Counts reported by an outer caller for isolated CLI attempts. Regular persisted sessions must not also be submitted. Reasoning is included in output. No speed is inferred.",
            "外层调用器提交的隔离 CLI 用量；已有常规会话记录的调用不应再次提交。推理计入输出，不推算速度。",
        )
        provenance = localized(
            "Model names retain the caller's reported/requested provenance. Requested names do not verify the backend model. Cache and reasoning breakdowns may be absent.",
            "模型名称保留调用器的报告或请求来源；请求的模型名称不能验证后端身份。缓存和推理细分可能缺失。",
        )
        hours: dict[float, dict[str, int]] = {}
        for row in data["hourly"]:
            hour = hours.setdefault(row["epoch"], {})
            hour[row["model"]] = hour.get(row["model"], 0) + row["total_tokens"]
        snapshot = ai_usage_snapshot(
            source_id=self.capability.source_id, label="CLI receipts", status="ok",
            observed_at=observed.isoformat(timespec="seconds"), collection_method="isolated-cli-usage-receipts",
            today=usage_window(sum(today_models.values()), method="caller-reported-isolated-usage", detail=detail),
            cumulative=usage_window(sum(models.values()), method="idempotent-receipt-ledger", detail=detail),
            models=[model_usage(
                model, today_tokens=today_models.get(model, 0), cumulative_tokens=tokens,
                today_method="caller-reported-isolated-usage", today_detail=detail,
                cumulative_method="idempotent-receipt-ledger", cumulative_detail=detail,
                price_reference=references.get(model),
            ) for model, tokens in sorted(models.items())],
            details=[detail_group("cli-receipts", localized("CLI usage receipts", "CLI 用量回执"), [
                token_metric("input-tokens", localized("Input", "输入"), sum(row["input_tokens"] for row in today_rows), detail),
                token_metric("output-tokens", localized("Output", "输出"), sum(row["output_tokens"] for row in today_rows), detail),
                token_metric("receipt-count", localized("Receipts today", "当日回执"), sum(row["receipts"] for row in today_rows), detail, unit="count"),
                token_metric("requested-model-count", localized("Requested model names", "使用请求模型名的回执"), sum(row["receipts"] for row in today_rows if row["model_basis"] == "requested"), provenance, unit="count"),
            ], note=provenance), detail_group("cli-receipt-pricing", localized("API price reference", "API 参考估算"), [], note=localized(
                "Uses the dated catalog and known effort suffixes while preserving original model IDs. Missing cache breakdowns remain unpriced. Output already includes reasoning. This is a text API reference, not the CLI provider's bill.",
                "按适用日期的目录及已知推理强度后缀匹配，保留原始模型名。缺少必要缓存细分的用量不估价，输出已含推理。这是文本 API 参考估算，不是 CLI 提供方账单。",
            ))],
            confidence="medium", privacy="numeric-usage-and-hashed-event-identities-only",
            daily_history=[daily_usage(day, sum(values.values()), [{"id": model, "tokens": tokens} for model, tokens in sorted(values.items())]) for day, values in sorted(days.items())],
            hourly_history=[hourly_usage(hour, sum(values.values()), [{"id": model, "tokens": tokens} for model, tokens in sorted(values.items())]) for hour, values in sorted(hours.items())],
            hourly_method="receipt-completion-time",
            pricing_history=pricing,
        )
        return Collection(snapshot=snapshot, checked_at=snapshot["observed_at"])
