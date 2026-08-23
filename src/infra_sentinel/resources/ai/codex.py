"""Privacy-minimal Codex local rollout usage collector.

Codex usage is reconstructed from local rollout ``total_token_usage`` deltas
and retained in Sentinel's aggregate-only ledger. SQLite thread counters are
not an accounting input. Prompts, responses, project paths, task identifiers,
and raw rollout records never enter the checkpoint or Projection.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.core.model import MetricPoint
from infra_sentinel.resources.ai.codex_pricing import (
    OPENAI_STANDARD_TEXT_PRICES_EFFECTIVE_DATE,
    estimate_standard_api_cost,
)
from infra_sentinel.resources.ai.codex_sampling import (
    CodexRolloutLedger,
    LedgerIncrement,
    RolloutAuditDay,
    TokenComposition,
    discover_codex_rollout_roots,
    load_codex_rollout_ledger,
    rebuild_codex_rollout_ledger,
    save_codex_rollout_ledger,
    update_codex_rollout_ledger,
)
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


CODEX_POLL_SECONDS = 20
CODEX_EMPIRICAL_CACHE_WEIGHT = 0.3
_COMPONENT_METRICS = {
    "ai.tokens.total": "total_tokens",
    "ai.tokens.input": "input_tokens",
    "ai.tokens.output": "output_tokens",
    "ai.tokens.reasoning": "reasoning_output_tokens",
    "ai.tokens.cache_read": "cached_input_tokens",
    "ai.tokens.cache_write": "cache_write_input_tokens",
}


def _iso_now(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _composition_models(composition: TokenComposition) -> dict[str, dict[str, int]]:
    return {
        identifier: values.as_model_payload()
        for identifier, values in composition.model_compositions.items()
    }


def _empty_day() -> RolloutAuditDay:
    return RolloutAuditDay()


def _empirical_weighted_tokens(composition: TokenComposition) -> int:
    """Return a local compatibility indicator, not an official Codex rule."""
    uncached_input = max(0, composition.input_tokens - composition.cached_input_tokens)
    return round(
        uncached_input
        + composition.output_tokens
        + CODEX_EMPIRICAL_CACHE_WEIGHT * composition.cached_input_tokens
    )


class CodexUsageCollector:
    """Own Codex rollout discovery, durable aggregate ledger, and metric deltas."""

    capability = CollectorCapability(
        id="ai.codex.local-workload",
        source_id="codex",
        source_kind="ai.codex",
        resource_id="ai_usage",
        metrics=tuple(_COMPONENT_METRICS),
    )

    def __init__(
        self,
        *,
        rollout_roots_finder: Callable[[], tuple[Path, ...]] = discover_codex_rollout_roots,
        ledger_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        poll_seconds: int = CODEX_POLL_SECONDS,
    ) -> None:
        self._rollout_roots_finder = rollout_roots_finder
        self._ledger_path = ledger_path
        self._ledger = load_codex_rollout_ledger(ledger_path)
        self._clock = clock
        self._poll_seconds = max(1, int(poll_seconds))
        self._next_poll_epoch = 0.0
        self._snapshot: dict[str, Any] = {"available": False, "status": "unavailable"}

    @staticmethod
    def _daily_history(ledger: CodexRolloutLedger) -> list[dict[str, Any]]:
        return [
            daily_usage(
                day,
                usage.composition.total_tokens,
                [
                    {"id": identifier, "tokens": tokens}
                    for identifier, tokens in sorted(usage.composition.models.items())
                ],
            )
            for day, usage in sorted(ledger.days.items())
            if usage.composition.total_tokens > 0
        ]

    @staticmethod
    def _pricing_history(ledger: CodexRolloutLedger) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for day, usage in sorted(ledger.days.items()):
            if usage.composition.total_tokens <= 0:
                continue
            estimate = estimate_standard_api_cost(_composition_models(usage.composition))
            rows.append(pricing_day(
                day,
                kind="local-rollout-standard-api-projection",
                cost_usd=estimate.total_cost_usd,
                priced_tokens=estimate.priced_tokens,
                unpriced_tokens=estimate.unpriced_tokens,
                models=[
                    {"id": item.model, "cost_usd": item.cost_usd, "priced_tokens": item.tokens}
                    for item in estimate.models
                ],
            ))
        return rows

    @staticmethod
    def _details(ledger: CodexRolloutLedger, today: RolloutAuditDay, cumulative: TokenComposition) -> list[dict[str, Any]]:
        root_files = sum(1 for entry in ledger.files.values() if entry.get("source") == "user")
        subagent_files = sum(1 for entry in ledger.files.values() if entry.get("source") == "subagent")
        duplicate_snapshots = sum(day.duplicate_snapshots for day in ledger.days.values())
        counter_resets = sum(day.counter_resets for day in ledger.days.values())
        today_estimate = estimate_standard_api_cost(_composition_models(today.composition))
        cumulative_estimate = estimate_standard_api_cost(_composition_models(cumulative))
        price_metrics = [
            token_metric(
                "standard-api-today", localized("Today reference value", "今日参考价"),
                today_estimate.total_cost_usd,
                localized("current standard text-token reference", "当前标准文本 Token 参考价"), unit="usd",
            ),
            token_metric(
                "standard-api-cumulative", localized("Local-history reference value", "本机历史参考价"),
                cumulative_estimate.total_cost_usd,
                localized("all captured local rollout metadata", "全部已捕获本机 rollout 元数据"), unit="usd",
            ),
            token_metric(
                "standard-api-priced-tokens", localized("Price-matched local Tokens", "可计价本机 Token"),
                cumulative_estimate.priced_tokens,
                localized("models with an exact standard-price match", "有精确标准价格匹配的模型"),
            ),
        ]
        if cumulative_estimate.unpriced_tokens:
            price_metrics.append(token_metric(
                "standard-api-unpriced-tokens", localized("Unpriced local Tokens", "未计价本机 Token"),
                cumulative_estimate.unpriced_tokens,
                localized("no exact official model-price match", "没有精确官方模型价格匹配"),
            ))
        return [
            detail_group("token-breakdown", localized("Token breakdown", "Token 分类明细"), [
                token_metric("raw-total", localized("Raw total", "原始总量"), today.composition.total_tokens, localized("today · input plus output", "今日 · 输入加输出")),
                token_metric("input", localized("Input", "输入"), today.composition.input_tokens, localized("today", "今日")),
                token_metric("cache-read", localized("Cache read", "缓存读取"), today.composition.cached_input_tokens, localized("subset of input", "输入的一部分")),
                token_metric("cache-write", localized("Cache write", "缓存写入"), today.composition.cache_write_input_tokens, localized("today", "今日")),
                token_metric("output", localized("Output", "输出"), today.composition.output_tokens, localized("today", "今日")),
                token_metric("reasoning", localized("Reasoning", "推理"), today.composition.reasoning_output_tokens, localized("subset of output", "输出的一部分")),
            ], badge=localized("today · local JSONL", "今日 · 本机 JSONL")),
            detail_group("cached-weight-comparison", localized("Cached-weight comparison", "缓存折算对照"), [
                token_metric(
                    "weighted-today", localized("Today comparison", "今日对照量"),
                    _empirical_weighted_tokens(today.composition),
                    localized("uncached input + output + 30% cached input", "非缓存输入 + 输出 + 30% 缓存输入"),
                ),
                token_metric(
                    "weighted-cumulative", localized("Local-history comparison", "本机历史对照量"),
                    _empirical_weighted_tokens(cumulative),
                    localized("derived from all captured local JSONL", "由全部已捕获本机 JSONL 推导"),
                ),
            ], note=localized(
                "The 30% cache coefficient is an empirical compatibility indicator inferred from this machine's historical JSONL and legacy counters. OpenAI's public documentation does not define it as the Codex allowance formula. Raw Tokens and the API reference remain authoritative within this dashboard.",
                "30% 缓存系数只是依据本机历史 JSONL 与旧计数器反推的经验兼容指标；OpenAI 公开文档没有将其定义为 Codex 额度公式。本面板仍以原始 Token 与 API 参考估值为准。",
            ), badge=localized("derived · not official", "推导 · 非官方")),
            detail_group("rollout-ledger", localized("Rollout ledger", "Rollout 账本"), [
                token_metric("rollout-files", localized("Observed rollout files", "已观测 rollout 文件"), len(ledger.files), localized("irreversible file markers only", "仅保存不可逆文件标记"), unit="count"),
                token_metric("root-rollouts", localized("Root rollouts", "主任务 rollout"), root_files, localized("thread source user", "任务来源为 user"), unit="count"),
                token_metric("subagent-rollouts", localized("Subagent rollouts", "子 Agent rollout"), subagent_files, localized("included as real local requests", "作为真实本机请求计入"), unit="count"),
                token_metric("duplicate-snapshots", localized("Suppressed duplicate snapshots", "已排除重复快照"), duplicate_snapshots, localized("unchanged cumulative counter", "累计计数不变"), unit="count"),
                token_metric("counter-resets", localized("Counter generations", "计数器换代"), counter_resets, localized("decrease starts a new generation", "累计下降后开始新一代"), unit="count"),
            ], note=localized(
                "Aggregate ledger survives rollout deletion after capture. Usage from another machine and rollouts deleted before capture remain absent.",
                "聚合账本在捕获后不受 rollout 删除影响；其他机器的用量以及捕获前已删除的 rollout 仍然缺失。",
            ), badge=localized("local ledger", "本机账本")),
            detail_group(
                "standard-api-reference", localized("Standard API reference", "标准 API 参考"), price_metrics,
                note=localized(
                    "Applies the OpenAI standard text-token price snapshot checked on " + OPENAI_STANDARD_TEXT_PRICES_EFFECTIVE_DATE + " to captured input, cached input, cache writes, and output by observed model. Excludes unmatched aliases, long-context uplift, tools, multimodal, priority, regional processing, and subscription terms. Not a bill or quota balance.",
                    "将 " + OPENAI_STANDARD_TEXT_PRICES_EFFECTIVE_DATE + " 核对的 OpenAI 标准文本 Token 价格，按已观测模型代入捕获的输入、缓存输入、缓存写入和输出；未匹配别名、长上下文加价、工具、多模态、优先级、区域处理与订阅条款均不计入。不是账单或额度余额。",
                ),
                badge=localized("reference · not billing", "估算 · 非账单"),
            ),
        ]

    @classmethod
    def _snapshot_for(cls, ledger: CodexRolloutLedger, timestamp: str, epoch: float) -> dict[str, Any]:
        day_key = datetime.fromtimestamp(epoch).astimezone().date().isoformat()
        today = ledger.days.get(day_key, _empty_day())
        cumulative = ledger.cumulative()
        identifiers = sorted(cumulative.models, key=lambda identifier: (-cumulative.models[identifier], identifier))
        today_detail = localized("local calendar day from captured rollout events", "按已捕获 rollout 事件统计的本机自然日")
        cumulative_detail = localized(
            "captured local rollout ledger; excludes other machines and pre-capture deletions",
            "已捕获本机 rollout 账本；不含其他机器及捕获前删除的数据",
        )
        local_midnight = datetime.fromtimestamp(epoch).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return ai_usage_snapshot(
            source_id="codex",
            label="Codex",
            status="ok",
            observed_at=timestamp,
            collection_method="Codex local rollout JSONL ledger",
            today=usage_window(
                today.composition.total_tokens,
                method="local-rollout-calendar-day",
                detail=today_detail,
                started_at=local_midnight.isoformat(timespec="seconds"),
            ),
            cumulative=usage_window(
                cumulative.total_tokens,
                method="local-rollout-ledger",
                detail=cumulative_detail,
                started_at=ledger.rebuilt_at,
            ),
            models=[
                model_usage(
                    identifier,
                    today_tokens=today.composition.models.get(identifier, 0),
                    cumulative_tokens=cumulative.models[identifier],
                    today_method="local-rollout-calendar-day",
                    today_detail=today_detail,
                    cumulative_method="local-rollout-ledger",
                    cumulative_detail=cumulative_detail,
                )
                for identifier in identifiers
            ],
            details=cls._details(ledger, today, cumulative),
            confidence="medium",
            privacy="aggregate-rollout-token-metadata-only",
            daily_history=cls._daily_history(ledger),
            pricing_history=cls._pricing_history(ledger),
        )

    @staticmethod
    def _point(increment: LedgerIncrement, metric: str, value: int, dimensions: dict[str, str]) -> MetricPoint:
        return MetricPoint(
            observed_at=increment.timestamp,
            observed_epoch=increment.epoch,
            metric=metric,
            instrument="counter",
            value=value,
            unit="tokens",
            source_id="codex",
            resource_id="ai_usage",
            dimensions=dimensions,
            attribution_method="local-rollout-delta",
            confidence="medium",
        )

    @classmethod
    def _points(cls, increments: tuple[LedgerIncrement, ...]) -> tuple[MetricPoint, ...]:
        points: list[MetricPoint] = []
        for increment in increments:
            for metric, field in _COMPONENT_METRICS.items():
                value = int(increment.usage.get(field, 0))
                if not value:
                    continue
                if metric == "ai.tokens.total":
                    points.append(cls._point(increment, metric, value, {"scope": "local-jsonl"}))
                points.append(cls._point(increment, metric, value, {"model": increment.model}))
        return tuple(points)

    def collect(self, context: CollectorContext) -> Collection:
        epoch = float(context.local_sample.get("epoch") or self._clock())
        if epoch < self._next_poll_epoch:
            return Collection(status=str(self._snapshot.get("status", "ok")), snapshot=self._snapshot)
        self._next_poll_epoch = epoch + self._poll_seconds
        roots = self._rollout_roots_finder()
        if not roots:
            self._snapshot = {"available": False, "status": "unavailable"}
            return Collection(status="unavailable", snapshot=self._snapshot)
        now = datetime.fromtimestamp(epoch).astimezone()
        try:
            if not self._ledger.rebuilt_at:
                self._ledger = rebuild_codex_rollout_ledger(roots, timezone=now.tzinfo, now=now)
                increments: tuple[LedgerIncrement, ...] = ()
                save_codex_rollout_ledger(self._ledger_path, self._ledger)
            else:
                update = update_codex_rollout_ledger(roots, self._ledger, timezone=now.tzinfo, now=now)
                increments = update.increments
                if update.scanned_bytes:
                    save_codex_rollout_ledger(self._ledger_path, self._ledger)
            timestamp = _iso_now(epoch)
            snapshot = self._snapshot_for(self._ledger, timestamp, epoch)
            points = self._points(increments)
        except (OSError, ValueError, json.JSONDecodeError):
            self._snapshot = {**self._snapshot, "available": True, "status": "error", "label": "Codex"}
            return Collection(status="error", snapshot=self._snapshot)
        self._snapshot = snapshot
        return Collection(points=points, status="ok", snapshot=snapshot)
