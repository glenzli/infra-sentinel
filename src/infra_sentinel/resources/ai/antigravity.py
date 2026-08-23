"""Privacy-bounded Antigravity token usage collector.

Antigravity, Antigravity IDE, and Antigravity CLI keep local conversation
databases below ~/.gemini/<store>/conversations. Their generation metadata is an
opaque protobuf blob. This adapter decodes only bounded token counters, model
identifiers, response de-duplication identifiers, and generation timestamps. It
never reads steps or text-bearing conversation payloads, and never persists a
copied database row or protobuf blob.

This is an internal client format, not an account-quota API. The normalized
values are local generation metadata, useful for trends but not a provider bill
or remaining subscription quota.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.core.model import MetricPoint
from infra_sentinel.resources.ai.contract import (
    ai_usage_snapshot,
    daily_usage,
    hourly_usage,
    pricing_day,
    detail_group,
    localized,
    model_usage,
    token_metric,
    usage_window,
)
from infra_sentinel.resources.ai.antigravity_pricing import (
    ANTIGRAVITY_TEXT_PRICE_REFERENCES_EFFECTIVE_DATE,
    estimate_antigravity_text_api_cost,
)


ANTIGRAVITY_POLL_SECONDS = 300
# Compatibility alias for callers that used the early CLI-only collector.
ANTIGRAVITY_CLI_POLL_SECONDS = ANTIGRAVITY_POLL_SECONDS
MAX_DATABASE_FILES = 512
MAX_GENERATIONS_PER_DATABASE = 20_000
MAX_METADATA_BYTES = 256 * 1024


@dataclass
class _TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    generations: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens + self.cache_read_tokens

    def add(self, other: "_TokenTotals") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.generations += other.generations


@dataclass(frozen=True)
class AntigravityHistory:
    days: dict[str, dict[str, _TokenTotals]]
    hours: dict[int, dict[str, _TokenTotals]]
    sessions: int
    stores: int = 0
    skipped_metadata_rows: int = 0

    @property
    def cumulative(self) -> _TokenTotals:
        total = _TokenTotals()
        for models in self.days.values():
            for values in models.values():
                total.add(values)
        return total

    def models(self) -> dict[str, _TokenTotals]:
        totals: dict[str, _TokenTotals] = {}
        for models in self.days.values():
            for identifier, values in models.items():
                totals.setdefault(identifier, _TokenTotals()).add(values)
        return totals


class _ProtoReader:
    """Small protobuf wire reader for Antigravity generation metadata only."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._position = 0

    def _varint(self) -> int | None:
        value = 0
        shift = 0
        while self._position < len(self._payload) and shift < 64:
            byte = self._payload[self._position]
            self._position += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
        return None

    def fields(self) -> Iterator[tuple[int, int, int | bytes | None]]:
        while self._position < len(self._payload):
            tag = self._varint()
            if tag is None or tag >> 3 == 0:
                return
            field, wire = tag >> 3, tag & 0x7
            if wire == 0:
                yield field, wire, self._varint()
                continue
            if wire == 1:
                end = self._position + 8
                if end > len(self._payload):
                    return
                self._position = end
                yield field, wire, None
                continue
            if wire == 2:
                size = self._varint()
                if size is None:
                    return
                end = self._position + size
                if end > len(self._payload):
                    return
                value = self._payload[self._position:end]
                self._position = end
                yield field, wire, value
                continue
            if wire == 5:
                end = self._position + 4
                if end > len(self._payload):
                    return
                self._position = end
                yield field, wire, None
                continue
            return


def _message_field(payload: bytes, wanted: int) -> bytes | None:
    for field, wire, value in _ProtoReader(payload).fields():
        if field == wanted and wire == 2 and isinstance(value, bytes):
            return value
    return None


def _varint_field(payload: bytes, wanted: int) -> int | None:
    for field, wire, value in _ProtoReader(payload).fields():
        if field == wanted and wire == 0 and isinstance(value, int):
            return value
    return None


def _string_field(payload: bytes, wanted: int) -> str | None:
    value = _message_field(payload, wanted)
    if value is None:
        return None
    try:
        decoded = value.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return decoded or None


def _timestamp_millis(payload: bytes) -> int | None:
    seconds = _varint_field(payload, 1)
    nanos = _varint_field(payload, 2) or 0
    if seconds is None or not 0 <= nanos <= 999_999_999:
        return None
    milliseconds = seconds * 1_000 + nanos // 1_000_000
    return milliseconds if 0 < milliseconds < 32_503_680_000_000 else None


@dataclass(frozen=True)
class _Generation:
    model: str | None
    display_model: str | None
    response_id: str | None
    timestamp_millis: int | None
    totals: _TokenTotals


def _parse_generation(blob: bytes) -> _Generation | None:
    if len(blob) > MAX_METADATA_BYTES:
        raise ValueError("AntigravityGenerationMetadataTooLarge")
    chat_model = _message_field(blob, 1)
    if chat_model is None:
        return None
    usage = _message_field(chat_model, 4)
    if usage is None:
        return None
    # Observed schema: system prompt (#1), fresh input (#2), cache read (#5),
    # text output (#9), and reasoning (#10).
    totals = _TokenTotals(
        input_tokens=min(2**63 - 1, (_varint_field(usage, 1) or 0) + (_varint_field(usage, 2) or 0)),
        output_tokens=min(2**63 - 1, _varint_field(usage, 9) or 0),
        reasoning_tokens=min(2**63 - 1, _varint_field(usage, 10) or 0),
        cache_read_tokens=min(2**63 - 1, _varint_field(usage, 5) or 0),
        generations=1,
    )
    if totals.total_tokens == 0:
        return None
    generation_timestamp = _message_field(chat_model, 9)
    return _Generation(
        model=_string_field(chat_model, 19),
        display_model=_string_field(chat_model, 21),
        response_id=_string_field(usage, 11),
        timestamp_millis=_timestamp_millis(_message_field(generation_timestamp, 4) or b"") if generation_timestamp else None,
        totals=totals,
    )


def discover_antigravity_conversations(preferred: Path | None = None) -> tuple[Path, ...]:
    """Locate each local Antigravity conversation store, never auth/config."""
    gemini_home = Path.home() / ".gemini"
    candidates = [
        preferred,
        gemini_home / "antigravity" / "conversations",
        gemini_home / "antigravity-ide" / "conversations",
    ]
    if root := os.environ.get("GEMINI_CLI_HOME"):
        candidates.append(Path(root) / "antigravity-cli" / "conversations")
    candidates.append(gemini_home / "antigravity-cli" / "conversations")
    discovered: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path is None or not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            discovered.append(resolved)
    return tuple(discovered)


def discover_antigravity_cli_conversations(preferred: Path | None = None) -> Path | None:
    """Compatibility lookup for the standalone CLI store only."""
    if preferred is not None and preferred.is_dir():
        return preferred
    roots = []
    if root := os.environ.get("GEMINI_CLI_HOME"):
        roots.append(Path(root) / "antigravity-cli" / "conversations")
    roots.append(Path.home() / ".gemini" / "antigravity-cli" / "conversations")
    return next((path for path in roots if path.is_dir()), None)


def _local_day(epoch_millis: int) -> str:
    return datetime.fromtimestamp(epoch_millis / 1_000).astimezone().date().isoformat()


def _file_mtime_millis(path: Path) -> int:
    return int(path.stat().st_mtime * 1_000)


def _session_rows(path: Path) -> tuple[list[_Generation], int]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    parsed: list[_Generation] = []
    skipped = 0
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        cursor = connection.execute(
            "SELECT CASE WHEN length(data) <= ? THEN data ELSE NULL END, length(data) "
            "FROM gen_metadata ORDER BY idx LIMIT ?",
            (MAX_METADATA_BYTES, MAX_GENERATIONS_PER_DATABASE + 1),
        )
        for index, (value, size) in enumerate(cursor, start=1):
            if index > MAX_GENERATIONS_PER_DATABASE:
                raise ValueError("AntigravityGenerationLimitExceeded")
            if not isinstance(size, int) or size < 0:
                raise ValueError("AntigravityGenerationMetadataInvalid")
            if size > MAX_METADATA_BYTES:
                skipped += 1
                continue
            if not isinstance(value, bytes):
                raise ValueError("AntigravityGenerationMetadataInvalid")
            generation = _parse_generation(value)
            if generation is not None:
                parsed.append(generation)
    return parsed, skipped


def _conversation_databases(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    databases = tuple(sorted(path for path in directory.glob("*.db") if path.is_file()))
    if len(databases) > MAX_DATABASE_FILES:
        raise ValueError("AntigravityConversationLimitExceeded")
    return databases


def read_antigravity_history(directories: tuple[Path, ...]) -> AntigravityHistory:
    """Read bounded generation metadata from all discovered local stores.

    A response ID is de-duplicated across stores in memory. Oversized metadata
    rows are deliberately skipped before their blob is returned to Python.
    """
    directories = tuple(path for path in directories if path.is_dir())
    if not directories:
        raise OSError("AntigravityConversationsMissing")
    days: dict[str, dict[str, _TokenTotals]] = defaultdict(dict)
    hours: dict[int, dict[str, _TokenTotals]] = defaultdict(dict)
    sessions = 0
    skipped_metadata_rows = 0
    seen_response_ids: set[str] = set()
    for directory in directories:
        for database in _conversation_databases(directory):
            rows, skipped = _session_rows(database)
            skipped_metadata_rows += skipped
            if not rows:
                continue
            sessions += 1
            # Continuation rows sometimes omit the machine ID. Recover only
            # where this single database has one unambiguous ID for the same
            # display label; never infer a model from a display name.
            display_models: dict[str, set[str]] = defaultdict(set)
            for row in rows:
                if row.model and row.display_model:
                    display_models[row.display_model].add(row.model)
            recovered = {
                label: next(iter(models))
                for label, models in display_models.items()
                if len(models) == 1
            }
            fallback_timestamp = _file_mtime_millis(database)
            for row in rows:
                if row.response_id:
                    if row.response_id in seen_response_ids:
                        continue
                    seen_response_ids.add(row.response_id)
                model = row.model or (recovered.get(row.display_model) if row.display_model else None) or "unknown"
                day = _local_day(row.timestamp_millis or fallback_timestamp)
                days[day].setdefault(model, _TokenTotals()).add(row.totals)
                if row.timestamp_millis is not None:
                    hour = int(row.timestamp_millis / 1_000 // 3_600) * 3_600
                    hours[hour].setdefault(model, _TokenTotals()).add(row.totals)
    return AntigravityHistory(
        dict(days),
        dict(hours),
        sessions,
        stores=len(directories),
        skipped_metadata_rows=skipped_metadata_rows,
    )


def read_antigravity_cli_history(directory: Path) -> AntigravityHistory:
    """Compatibility entrypoint for the former one-store collector."""
    return read_antigravity_history((directory,))


def _iso_now(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _metric_point(*, timestamp: str, epoch: float, metric: str, value: int, model: str) -> MetricPoint:
    return MetricPoint(
        observed_at=timestamp,
        observed_epoch=epoch,
        metric=metric,
        instrument="counter",
        value=value,
        unit="tokens",
        source_id="antigravity",
        resource_id="ai_usage",
        dimensions={"model": model},
        attribution_method="sampled-delta",
        confidence="medium",
        estimated=True,
    )


class AntigravityUsageCollector:
    """Read local Antigravity generation counters through the common AI contract."""

    capability = CollectorCapability(
        id="ai.antigravity.generation-usage",
        source_id="antigravity",
        source_kind="ai.antigravity",
        resource_id="ai_usage",
        metrics=("ai.tokens.total", "ai.tokens.input", "ai.tokens.output", "ai.tokens.reasoning", "ai.tokens.cache_read"),
    )

    def __init__(
        self,
        *,
        conversations_finder: Callable[[], Path | tuple[Path, ...] | None] = discover_antigravity_conversations,
        clock: Callable[[], float] = time.time,
        poll_seconds: int = ANTIGRAVITY_POLL_SECONDS,
    ) -> None:
        self._conversations_finder = conversations_finder
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._next_poll_epoch = 0.0
        self._snapshot: dict[str, Any] = {"available": False, "status": "unavailable"}
        self._previous: dict[str, _TokenTotals] = {}
        self._started_day: str | None = None
        self._started_at_epoch: float | None = None

    @staticmethod
    def _snapshot_for(
        history: AntigravityHistory,
        timestamp: str,
        epoch: float,
        started_at_epoch: float | None = None,
    ) -> dict[str, Any]:
        day = _local_day(int(epoch * 1_000))
        today_models = history.days.get(day, {})
        cumulative_models = history.models()
        today = _TokenTotals()
        for values in today_models.values():
            today.add(values)
        cumulative = history.cumulative
        hourly_rows: list[dict[str, Any]] = []
        hourly_tokens = 0
        for hour, model_values in sorted(history.hours.items()):
            if _local_day(hour * 1_000) != day:
                continue
            total = sum(values.total_tokens for values in model_values.values())
            hourly_tokens += total
            hourly_rows.append(hourly_usage(
                hour,
                total,
                [
                    {"id": identifier, "tokens": values.total_tokens}
                    for identifier, values in sorted(model_values.items())
                ],
            ))
        identifiers = sorted(cumulative_models, key=lambda identifier: (-cumulative_models[identifier].total_tokens, identifier))
        models = [
            model_usage(
                identifier,
                today_tokens=today_models.get(identifier, _TokenTotals()).total_tokens,
                cumulative_tokens=cumulative_models[identifier].total_tokens,
                today_method="local-calendar-day",
                today_detail=localized("locally recorded generation metadata", "本地记录的生成元数据"),
                cumulative_method="local-conversation-history",
                cumulative_detail=localized("all readable local generation metadata", "全部可读取的本地生成元数据"),
            )
            for identifier in identifiers
        ]
        daily = []
        pricing = []
        for date, model_values in sorted(history.days.items()):
            estimate = estimate_antigravity_text_api_cost(model_values)
            daily.append(daily_usage(
                date,
                sum(values.total_tokens for values in model_values.values()),
                [{"id": identifier, "tokens": values.total_tokens} for identifier, values in sorted(model_values.items())],
            ))
            pricing.append(pricing_day(
                date,
                kind="catalog-text-api-reference",
                cost_usd=estimate.total_cost_usd,
                priced_tokens=estimate.priced_tokens,
                unpriced_tokens=estimate.unpriced_tokens,
                models=[{"id": identifier, "cost_usd": cost, "priced_tokens": tokens}
                        for identifier, cost, tokens in estimate.model_costs],
            ))
        today_estimate = estimate_antigravity_text_api_cost(today_models)
        cumulative_estimate = estimate_antigravity_text_api_cost(cumulative_models)
        price_metrics = [
            token_metric(
                "antigravity-api-today", localized("Today reference value", "今日参考价"), today_estimate.total_cost_usd,
                localized("explicit local model mappings only", "仅明确的本地模型映射"), unit="usd",
            ),
            token_metric(
                "antigravity-api-cumulative", localized("Local-history reference value", "本地历史参考价"), cumulative_estimate.total_cost_usd,
                localized("explicit local model mappings only", "仅明确的本地模型映射"), unit="usd",
            ),
            token_metric(
                "antigravity-api-priced-tokens", localized("Price-matched local Tokens", "可计价本地 Token"), cumulative_estimate.priced_tokens,
                localized("models with an explicit documented price mapping", "有明确官方价格映射的模型"),
            ),
        ]
        if cumulative_estimate.unpriced_tokens:
            price_metrics.append(token_metric(
                "antigravity-api-unpriced-tokens", localized("Unpriced local Tokens", "未计价本地 Token"), cumulative_estimate.unpriced_tokens,
                localized("no explicit official price mapping", "没有明确的官方价格映射"),
            ))
        return ai_usage_snapshot(
            source_id="antigravity",
            label="Antigravity",
            status="ok",
            observed_at=timestamp,
            collection_method="local-generation-metadata",
            today=usage_window(
                today.total_tokens,
                method="local-calendar-day",
                detail=localized("local generation metadata; not remaining account quota", "本地生成元数据；不是账户剩余额度"),
                started_at=_iso_now(started_at_epoch or epoch),
            ),
            cumulative=usage_window(
                cumulative.total_tokens,
                method="local-conversation-history",
                detail=localized("all readable local generation metadata", "全部可读取的本地生成元数据"),
            ),
            models=models,
            details=[
                detail_group("token-breakdown", localized("Token breakdown", "Token 分类明细"), [
                    token_metric("input", localized("Input", "输入"), today.input_tokens, localized("locally recorded", "本地记录")),
                    token_metric("output", localized("Output", "输出"), today.output_tokens, localized("locally recorded", "本地记录")),
                    token_metric("reasoning", localized("Reasoning", "推理"), today.reasoning_tokens, localized("locally recorded", "本地记录")),
                    token_metric("cache", localized("Cache read", "缓存读取"), today.cache_read_tokens, localized("locally recorded", "本地记录")),
                ], badge=localized("today", "今日")),
                detail_group("activity", localized("Activity", "活动"), [
                    token_metric("stores", localized("Stores", "数据目录"), history.stores, localized("discovered Antigravity conversation stores", "已发现的 Antigravity 会话目录"), unit="count"),
                    token_metric("sessions", localized("Sessions", "会话"), history.sessions, localized("readable local conversation databases", "可读取的本地会话数据库"), unit="count"),
                    token_metric("generations", localized("Generations", "生成"), today.generations, localized("generation metadata rows", "生成元数据行"), unit="count"),
                    token_metric("skipped_metadata", localized("Skipped oversized metadata", "跳过超大元数据"), history.skipped_metadata_rows, localized("metadata rows larger than the bounded reader limit", "超过受限读取器大小上限的元数据行"), unit="count"),
                ]),
                detail_group(
                    "antigravity-api-reference", localized("Antigravity API references", "Antigravity API 参考"), price_metrics,
                    note=localized(
                        "Maps decoded text input, cache-read, output, and reasoning counters through explicit local model mappings to Gemini Developer API and Google Cloud Claude reference prices checked on " + ANTIGRAVITY_TEXT_PRICE_REFERENCES_EFFECTIVE_DATE + ". Gemini 3.1 Pro uses its <=200k-context tier. It excludes storage, grounding, tools, multimodal legs, opaque aliases, and all Antigravity plan terms. Not a bill or quota balance.",
                        "仅将已解码文本输入、缓存读取、输出与推理计数，经明确的本地模型映射，换算为 " + ANTIGRAVITY_TEXT_PRICE_REFERENCES_EFFECTIVE_DATE + " 核对的 Gemini Developer API 与 Google Cloud Claude 参考价。Gemini 3.1 Pro 使用 <=200k 上下文档位；缓存存储、检索、工具、多模态、内部别名和全部 Antigravity 套餐条款均不计入；不是账单或额度余额。",
                    ),
                    badge=localized("reference · not billing", "估算 · 非账单"),
                ),
            ],
            confidence="medium",
            privacy="decoded-generation-token-metadata-only",
            daily_history=daily,
            hourly_history=hourly_rows,
            hourly_unattributed_tokens=max(0, today.total_tokens - hourly_tokens),
            hourly_method="generation-event-hour-with-sampled-baseline",
            pricing_history=pricing,
        )

    def _interval_points(self, history: AntigravityHistory, timestamp: str, epoch: float) -> tuple[MetricPoint, ...]:
        day = _local_day(int(epoch * 1_000))
        current = history.days.get(day, {})
        if not self._previous:
            self._previous = {identifier: _TokenTotals(**values.__dict__) for identifier, values in current.items()}
            return ()
        points: list[MetricPoint] = []
        fields = {
            "ai.tokens.total": "total_tokens",
            "ai.tokens.input": "input_tokens",
            "ai.tokens.output": "output_tokens",
            "ai.tokens.reasoning": "reasoning_tokens",
            "ai.tokens.cache_read": "cache_read_tokens",
        }
        for identifier, totals in current.items():
            previous = self._previous.get(identifier, _TokenTotals())
            for metric, field in fields.items():
                value = getattr(totals, field)
                delta = value if identifier not in self._previous or value < getattr(previous, field) else value - getattr(previous, field)
                if delta:
                    points.append(_metric_point(timestamp=timestamp, epoch=epoch, metric=metric, value=delta, model=identifier))
        self._previous = {identifier: _TokenTotals(**values.__dict__) for identifier, values in current.items()}
        return tuple(points)

    def collect(self, context: CollectorContext) -> Collection:
        epoch = float(context.local_sample.get("epoch") or self._clock())
        day = _local_day(int(epoch * 1_000))
        if self._started_day != day:
            self._started_day = day
            self._started_at_epoch = epoch
        if epoch < self._next_poll_epoch:
            return Collection(status=str(self._snapshot.get("status", "ok")), snapshot=self._snapshot)
        self._next_poll_epoch = epoch + self._poll_seconds
        discovered = self._conversations_finder()
        if discovered is None:
            self._snapshot = {"available": False, "status": "unavailable"}
            return Collection(status="unavailable", snapshot=self._snapshot)
        directories = (discovered,) if isinstance(discovered, Path) else tuple(discovered)
        if not directories:
            self._snapshot = {"available": False, "status": "unavailable"}
            return Collection(status="unavailable", snapshot=self._snapshot)
        try:
            timestamp = _iso_now(epoch)
            history = read_antigravity_history(directories)
            snapshot = self._snapshot_for(history, timestamp, epoch, self._started_at_epoch or epoch)
            points = self._interval_points(history, timestamp, epoch)
        except (OSError, sqlite3.DatabaseError, ValueError):
            self._snapshot = {**self._snapshot, "available": True, "status": "error", "label": "Antigravity"}
            return Collection(status="error", snapshot=self._snapshot)
        self._snapshot = snapshot
        return Collection(points=points, status="ok", snapshot=snapshot)
