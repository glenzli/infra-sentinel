"""Infer Runtime daily settlement adapter for the unified AI usage view.

Infer publishes one absolute current-local-day snapshot through its already
discovered facility status socket.  This owner keeps a compact local history
and replaces a day's model identities on each newer snapshot; it never turns a
poll into an additive counter.  Each model row retains its immutable Runtime
``execution_origin`` fact.  Runtime attempts made through Codex App Server are
ephemeral and are not necessarily represented by the local Codex task
collector, so both ``codex`` and ``other`` origins remain visible here.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any, Callable

from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.resources.ai.contract import (
    ai_usage_snapshot,
    daily_usage,
    detail_group,
    localized,
    model_usage,
    pricing_day,
    token_metric,
    usage_window,
)
from infra_sentinel.resources.ai.codex_pricing import estimate_standard_api_cost
from infra_sentinel.resources.facilities.protocols import (
    INFER_RUNTIME_USAGE_DAILY_SCHEMA,
    INFER_RUNTIME_USAGE_DAILY_VERSION,
)


INFER_RUNTIME_DAILY_HISTORY_SCHEMA = "20260813.1"
INFER_RUNTIME_FACILITY_ID = "infer-runtime:local"


def _iso_now(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _local_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().date().isoformat()


class InferRuntimeUsageCollector:
    """Project origin-safe Infer settlement aggregates without a new facility card."""

    capability = CollectorCapability(
        id="ai.infer-runtime.daily-settlement",
        source_id="infer-runtime",
        source_kind="ai.infer-runtime",
        resource_id="ai_usage",
        metrics=(),
    )

    def __init__(
        self,
        *,
        checkpoint_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._checkpoint_path = checkpoint_path
        self._clock = clock
        self._history = self._load_history()

    def _load_history(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            raw = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != INFER_RUNTIME_DAILY_HISTORY_SCHEMA:
            return {}
        raw_days = raw.get("days")
        if not isinstance(raw_days, dict):
            return {}
        days: dict[str, dict[str, dict[str, Any]]] = {}
        for day, raw_models in raw_days.items():
            if not isinstance(day, str) or not isinstance(raw_models, dict):
                continue
            models: dict[str, dict[str, Any]] = {}
            for identity, raw_model in raw_models.items():
                if not isinstance(identity, str) or not isinstance(raw_model, dict):
                    continue
                origin = raw_model.get("execution_origin")
                if origin not in {"codex", "other"}:
                    continue
                identifier = raw_model.get("id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                try:
                    tokens = int(raw_model.get("total_tokens") or 0)
                    input_tokens = int(raw_model.get("input_tokens") or 0)
                    output_tokens = int(raw_model.get("output_tokens") or 0)
                    cost = float(raw_model.get("cost_usd") or 0)
                except (TypeError, ValueError):
                    continue
                if min(tokens, input_tokens, output_tokens) < 0 or cost < 0:
                    continue
                models[identity] = {
                    "id": identifier,
                    "execution_origin": origin,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": tokens,
                    "cost_usd": cost,
                }
            days[day] = models
        return days

    def _write_history(self) -> None:
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": INFER_RUNTIME_DAILY_HISTORY_SCHEMA,
            "days": {
                day: {identity: models[identity] for identity in sorted(models)}
                for day, models in sorted(self._history.items())
            },
        }
        temporary = self._checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._checkpoint_path)

    @staticmethod
    def _usage_daily(facilities: dict[str, Any]) -> dict[str, Any] | None:
        items = facilities.get("items")
        if not isinstance(items, list):
            return None
        facility = next((
            item for item in items
            if isinstance(item, dict)
            and item.get("id") == INFER_RUNTIME_FACILITY_ID
            and item.get("kind") == "infer-runtime"
            and isinstance(item.get("snapshot"), dict)
        ), None)
        if not isinstance(facility, dict):
            return None
        snapshot = facility.get("snapshot")
        extensions = snapshot.get("extensions") if isinstance(snapshot, dict) else None
        infer = extensions.get("infer-runtime") if isinstance(extensions, dict) else None
        usage = infer.get("usage_daily") if isinstance(infer, dict) else None
        if not isinstance(usage, dict):
            return None
        if (
            usage.get("schema") != INFER_RUNTIME_USAGE_DAILY_SCHEMA
            or usage.get("schema_version") != INFER_RUNTIME_USAGE_DAILY_VERSION
            or usage.get("calendar") != "host_local"
            or not isinstance(usage.get("days"), list)
        ):
            return None
        return usage

    @staticmethod
    def _runtime_models(usage: dict[str, Any], fallback_day: str) -> tuple[str, dict[str, dict[str, Any]]]:
        raw_days = usage.get("days")
        if not isinstance(raw_days, list) or len(raw_days) > 1:
            raise ValueError("Infer daily usage has an unsupported day shape")
        if not raw_days:
            return fallback_day, {}
        raw_day = raw_days[0]
        if not isinstance(raw_day, dict) or not isinstance(raw_day.get("date"), str):
            raise ValueError("Infer daily usage day is invalid")
        if raw_day["date"] != fallback_day:
            raise ValueError("Infer daily usage is not for the current host-local day")
        raw_models = raw_day.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("Infer daily usage models are invalid")
        models: dict[str, dict[str, Any]] = {}
        for row in raw_models:
            if not isinstance(row, dict):
                raise ValueError("Infer daily usage model is invalid")
            # Protocol normalization has already rejected malformed facts.  The
            # second exact check keeps this persistence boundary fail-closed if
            # a caller supplies an unnormalized facility snapshot in tests.
            origin = row.get("execution_origin")
            if origin not in {"codex", "other"}:
                continue
            identifier = row.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Infer model identity is invalid")
            identity = f"{origin}:{identifier}"
            if identity in models:
                raise ValueError("Infer daily usage duplicates a model identity")
            models[identity] = {
                "id": identifier,
                "execution_origin": origin,
                "input_tokens": max(0, int(row.get("input_tokens") or 0)),
                "output_tokens": max(0, int(row.get("output_tokens") or 0)),
                "total_tokens": max(0, int(row.get("total_tokens") or 0)),
                "cost_usd": max(0.0, float(row.get("cost_usd") or 0)),
            }
        return str(raw_day["date"]), models

    @staticmethod
    def _summaries(history: dict[str, dict[str, dict[str, Any]]]) -> tuple[
        list[dict[str, Any]], dict[str, int], dict[str, float], dict[str, dict[str, int]],
    ]:
        daily: list[dict[str, Any]] = []
        totals: dict[str, int] = {}
        costs: dict[str, float] = {}
        token_details: dict[str, dict[str, int]] = {}
        for day, rows in sorted(history.items()):
            day_totals: dict[str, int] = {}
            for row in rows.values():
                identifier = str(row["id"])
                tokens = max(0, int(row["total_tokens"]))
                day_totals[identifier] = day_totals.get(identifier, 0) + tokens
                totals[identifier] = totals.get(identifier, 0) + tokens
                costs[identifier] = costs.get(identifier, 0.0) + max(0.0, float(row["cost_usd"]))
                detail = token_details.setdefault(identifier, {"input": 0, "output": 0})
                detail["input"] += max(0, int(row["input_tokens"]))
                detail["output"] += max(0, int(row["output_tokens"]))
            day_models = [
                {"id": identifier, "tokens": tokens}
                for identifier, tokens in sorted(day_totals.items())
            ]
            daily.append(daily_usage(day, sum(day_totals.values()), day_models))
        return daily, totals, costs, token_details

    @staticmethod
    def _pricing_history(history: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]] | None:
        """Keep Runtime execution origins honest at the price boundary.

        Runtime's own cost value is authoritative only for ``other`` attempts.
        A ``codex`` attempt is explicitly a Codex subscription execution, so
        Runtime correctly leaves it unpriced; Sentinel may instead provide the
        same clearly-labelled standard API text reference it uses for local
        Codex samples. A zero ``cost_usd`` cannot distinguish a free request
        from an unavailable price, so it remains unpriced until Runtime
        publishes an explicit availability fact.
        """
        pricing: list[dict[str, Any]] = []
        for day, rows in sorted(history.items()):
            codex_compositions: dict[str, dict[str, int]] = {}
            direct_models: dict[str, dict[str, float | int]] = {}
            other_unpriced_tokens = 0
            for row in rows.values():
                identifier = str(row["id"])
                tokens = max(0, int(row["total_tokens"]))
                if row["execution_origin"] == "codex":
                    composition = codex_compositions.setdefault(identifier, {
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    })
                    composition["input_tokens"] += max(0, int(row["input_tokens"]))
                    composition["output_tokens"] += max(0, int(row["output_tokens"]))
                    composition["total_tokens"] += tokens
                    continue

                cost = max(0.0, float(row["cost_usd"]))
                if cost <= 0:
                    other_unpriced_tokens += tokens
                    continue
                aggregate = direct_models.setdefault(identifier, {
                    "cost_usd": 0.0,
                    "priced_tokens": 0,
                    "unpriced_tokens": 0,
                })
                aggregate["cost_usd"] = float(aggregate["cost_usd"]) + cost
                aggregate["priced_tokens"] = int(aggregate["priced_tokens"]) + tokens

            codex_reference = estimate_standard_api_cost(codex_compositions)
            model_rows: dict[str, dict[str, float | int]] = {
                identifier: dict(row) for identifier, row in direct_models.items()
            }
            for estimate in codex_reference.models:
                aggregate = model_rows.setdefault(estimate.model, {
                    "cost_usd": 0.0,
                    "priced_tokens": 0,
                    "unpriced_tokens": 0,
                })
                aggregate["cost_usd"] = float(aggregate["cost_usd"]) + estimate.cost_usd
                aggregate["priced_tokens"] = int(aggregate["priced_tokens"]) + estimate.tokens

            priced_tokens = codex_reference.priced_tokens + sum(
                int(row["priced_tokens"]) for row in direct_models.values()
            )
            if priced_tokens <= 0:
                continue
            cost_usd = codex_reference.total_cost_usd + sum(
                float(row["cost_usd"]) for row in direct_models.values()
            )
            pricing.append(pricing_day(
                day,
                kind="runtime-origin-aware-price-reference",
                cost_usd=cost_usd,
                priced_tokens=priced_tokens,
                unpriced_tokens=codex_reference.unpriced_tokens + other_unpriced_tokens,
                models=[
                    {"id": identifier, **row}
                    for identifier, row in sorted(model_rows.items())
                ],
            ))
        return pricing or None

    def _snapshot_for(self, observed_at: str, current_day: str) -> dict[str, Any]:
        daily, cumulative_tokens, cumulative_costs, _ = self._summaries(self._history)
        current = self._history.get(current_day, {})
        current_by_model: dict[str, dict[str, Any]] = {}
        for row in current.values():
            identifier = str(row["id"])
            aggregate = current_by_model.setdefault(identifier, {
                "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            })
            aggregate["total_tokens"] += int(row["total_tokens"])
            aggregate["input_tokens"] += int(row["input_tokens"])
            aggregate["output_tokens"] += int(row["output_tokens"])
            aggregate["cost_usd"] += float(row["cost_usd"])
        today_tokens = sum(int(row["total_tokens"]) for row in current.values())
        all_models = sorted(
            set(cumulative_tokens) | set(current_by_model),
            key=lambda identifier: (-cumulative_tokens.get(identifier, 0), identifier),
        )
        today_detail = localized(
            "Infer Runtime's current host-local-day settled aggregate, including Codex App Server attempts that are not durable local Codex tasks.",
            "Infer Runtime 当前主机自然日的已结算聚合，包含不会形成持久 Codex 本地任务的 Codex App Server 尝试。",
        )
        cumulative_detail = localized(
            "Sentinel's local daily history, replaced by each exact Runtime snapshot rather than re-added per poll.",
            "Sentinel 本地每日历史；每次由 Runtime 精确快照覆盖，不会随轮询重复累加。",
        )
        day_input = sum(int(row["input_tokens"]) for row in current.values())
        day_output = sum(int(row["output_tokens"]) for row in current.values())
        day_cost = sum(float(row["cost_usd"]) for row in current.values())
        model_costs = [
            token_metric(
                f"settled-cost:{identifier}",
                localized(identifier, identifier),
                float(row["cost_usd"]),
                localized(
                    f"{int(row['total_tokens']):,} settled tokens; input {int(row['input_tokens']):,}, output {int(row['output_tokens']):,}",
                    f"已结算 {int(row['total_tokens']):,} Token；输入 {int(row['input_tokens']):,}，输出 {int(row['output_tokens']):,}",
                ),
                unit="usd",
            )
            for identifier, row in sorted(current_by_model.items())
        ]
        return ai_usage_snapshot(
            source_id="infer-runtime",
            label="Infer Runtime",
            status="ok",
            observed_at=observed_at,
            collection_method="facility-status-daily-upsert",
            today=usage_window(today_tokens, method="runtime-settled-host-day", detail=today_detail),
            cumulative=usage_window(sum(cumulative_tokens.values()), method="sentinel-daily-upsert-history", detail=cumulative_detail),
            models=[model_usage(
                identifier,
                today_tokens=int(current_by_model.get(identifier, {}).get("total_tokens") or 0),
                cumulative_tokens=cumulative_tokens.get(identifier, 0),
                today_method="runtime-settled-host-day",
                today_detail=today_detail,
                cumulative_method="sentinel-daily-upsert-history",
                cumulative_detail=cumulative_detail,
            ) for identifier in all_models],
            details=[
                detail_group("daily-settlement", localized("Daily settlement", "当日结算"), [
                    token_metric("input-tokens", localized("Input", "输入"), day_input, today_detail),
                    token_metric("output-tokens", localized("Output", "输出"), day_output, today_detail),
                    token_metric("settled-cost", localized("Cost", "费用"), day_cost, today_detail, unit="usd"),
                ], note=localized(
                    "Both Runtime execution origins are retained. Codex App Server attempts use ephemeral threads and are not assumed to overlap the local Codex task collector.",
                    "保留 Runtime 的两种执行来源。Codex App Server 尝试使用 ephemeral 线程，不假定会与 Codex 本地任务采集重叠。",
                )),
                detail_group("model-settlement", localized("Model settlement", "模型结算"), model_costs,
                    note=localized("Per-model settled cost for the current local day.", "当前本机自然日按模型的已结算费用。"),
                ),
            ],
            confidence="high",
            privacy="settled-aggregate-model-usage-only",
            daily_history=daily,
            pricing_history=self._pricing_history(self._history),
        )

    def collect(self, context: CollectorContext) -> Collection:
        epoch = float(context.local_sample.get("epoch") or self._clock())
        usage = self._usage_daily(context.facilities)
        if usage is None:
            return Collection(status="unavailable", snapshot={"available": False, "status": "unavailable"})
        current_day, models = self._runtime_models(usage, _local_day(epoch))
        if self._history.get(current_day) != models:
            self._history[current_day] = models
            self._write_history()
        return Collection(status="ok", snapshot=self._snapshot_for(_iso_now(epoch), current_day))
