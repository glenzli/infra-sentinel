"""Privacy-minimal OpenCode session usage collector.

This owner supports OpenCode CLI and OpenCode Desktop.  Desktop collection
uses a read-only aggregate query over assistant-message usage metadata only;
it never selects transcript text, project paths, account rows, or credentials.
The CLI currently renders a terminal table rather than machine-readable data,
so that fallback parser intentionally fails closed when required rows change.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any

from infra_sentinel.resources.ai.contract import ai_usage_snapshot, daily_usage, detail_group, localized, model_usage, token_metric, usage_window
from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.core.model import MetricPoint


OPENCODE_POLL_SECONDS = 60
OPENCODE_TIMEOUT_SECONDS = 20
OPENCODE_CHECKPOINT_SCHEMA = "20260809.2"
OPENCODE_COUNTER_SCHEMA = "20260809.1"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MODEL_HEADER = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.:@-]+$")
_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)([KMB])?$")
_SUMMARY_LABELS = (
    "Total Cost", "Avg Cost/Day", "Avg Tokens/Session", "Median Tokens/Session",
    "Input Tokens", "Output Tokens", "Cache Write", "Cache Read", "Sessions", "Messages", "Days", "Cost", "Input", "Output",
)


@dataclass(frozen=True)
class OpenCodeStats:
    sessions: int
    messages: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    models: tuple[dict[str, Any], ...]
    output_includes_reasoning: bool

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class OpenCodeDailyUsage:
    date: str
    total_tokens: int
    models: tuple[dict[str, Any], ...]


def discover_opencode(preferred: Path | None = None) -> str | None:
    """Find only executable CLI locations; no OpenCode data directories are read."""
    discovered = shutil.which("opencode")
    candidates = [
        str(preferred) if preferred else None,
        discovered,
        str(Path.home() / ".local" / "bin" / "opencode"),
        str(Path.home() / ".opencode" / "bin" / "opencode"),
        "/opt/homebrew/bin/opencode",
        "/usr/local/bin/opencode",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os_access_executable(candidate):
            return candidate
    return None


def discover_opencode_desktop_database(preferred: Path | None = None) -> Path | None:
    """Locate OpenCode Desktop's session database, never its auth store."""
    candidates = [preferred, Path.home() / ".local" / "share" / "opencode" / "opencode.db"]
    for variable in ("LOCALAPPDATA", "APPDATA"):
        if root := os.environ.get(variable):
            candidates.append(Path(root) / "OpenCode" / "opencode.db")
    return next((candidate for candidate in candidates if candidate and candidate.is_file()), None)


def os_access_executable(path: str) -> bool:
    # Kept tiny and injectable-adjacent so the discovery contract stays
    # explicit in tests without importing any OpenCode-owned local files.
    return Path(path).exists() and os.access(path, os.X_OK)


def _table_text(line: str) -> str:
    text = _ANSI_ESCAPE.sub("", line).strip()
    if text.startswith("│") and text.endswith("│"):
        return text[1:-1].strip()
    return ""


def _number(value: str) -> int:
    compact = value.replace(",", "").strip()
    match = _NUMBER.fullmatch(compact)
    if not match:
        raise ValueError("unsupported OpenCode numeric value")
    amount = float(match.group(1))
    multiplier = {None: 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return max(0, round(amount * multiplier))


def _cost(value: str) -> float:
    if not value.startswith("$"):
        raise ValueError("unsupported OpenCode cost value")
    amount = float(value[1:].replace(",", ""))
    return max(0.0, amount)


def _value_after_label(text: str, label: str) -> str | None:
    if not text.startswith(label):
        return None
    value = text[len(label):].strip()
    return value or None


def parse_opencode_stats(output: str) -> OpenCodeStats:
    """Parse documented human-readable stats without retaining transcript text.

    ``--models`` combines reasoning tokens into each model's output field in
    OpenCode's current display.  We preserve that useful total and state the
    semantic fact instead of inventing a separate reasoning count.
    """
    rows = [_table_text(line) for line in output.splitlines()]
    summary: dict[str, str] = {}
    models: list[dict[str, Any]] = []
    current_model: dict[str, Any] | None = None
    for text in rows:
        if not text:
            continue
        if _MODEL_HEADER.fullmatch(text):
            current_model = {
                "id": text,
                "messages": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": 0.0,
            }
            models.append(current_model)
            continue
        for label in _SUMMARY_LABELS:
            value = _value_after_label(text, label)
            if value is None:
                continue
            if current_model is not None:
                model_fields = {
                    "Messages": ("messages", _number),
                    "Input Tokens": ("input_tokens", _number),
                    "Output Tokens": ("output_tokens", _number),
                    "Cache Read": ("cache_read_tokens", _number),
                    "Cache Write": ("cache_write_tokens", _number),
                    "Cost": ("cost_usd", _cost),
                }
                model = model_fields.get(label)
                if model:
                    current_model[model[0]] = model[1](value)
                    break
            summary[label] = value
            break
    required = {"Sessions", "Messages", "Input", "Output", "Cache Read", "Cache Write", "Total Cost"}
    if not required.issubset(summary):
        missing = ", ".join(sorted(required - set(summary)))
        raise ValueError(f"OpenCode stats output missing required rows: {missing}")
    if models:
        return OpenCodeStats(
            sessions=_number(summary["Sessions"]),
            messages=_number(summary["Messages"]),
            input_tokens=sum(int(item["input_tokens"]) for item in models),
            output_tokens=sum(int(item["output_tokens"]) for item in models),
            reasoning_tokens=0,
            cache_read_tokens=sum(int(item["cache_read_tokens"]) for item in models),
            cache_write_tokens=sum(int(item["cache_write_tokens"]) for item in models),
            cost_usd=sum(float(item["cost_usd"]) for item in models),
            models=tuple(models),
            output_includes_reasoning=True,
        )
    return OpenCodeStats(
        sessions=_number(summary["Sessions"]),
        messages=_number(summary["Messages"]),
        input_tokens=_number(summary["Input"]),
        output_tokens=_number(summary["Output"]),
        reasoning_tokens=0,
        cache_read_tokens=_number(summary["Cache Read"]),
        cache_write_tokens=_number(summary["Cache Write"]),
        cost_usd=_cost(summary["Total Cost"]),
        models=(),
        output_includes_reasoning=False,
    )


def _day_start_epoch(epoch: float) -> int:
    local = datetime.fromtimestamp(epoch).astimezone()
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _number_from_database(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cost_from_database(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def read_opencode_desktop_stats(path: Path, epoch: float, since_epoch: float | None = None) -> OpenCodeStats:
    """Aggregate assistant-message token metadata from OpenCode Desktop.

    JSON accessors extract only provider, model, role, token counters, and
    cost inside SQLite. Neither message data nor any text-bearing column is
    selected, returned, stored, logged, or placed in the Projection.
    """
    if not path.is_file():
        raise OSError("OpenCodeDesktopDatabaseMissing")
    cutoff = _day_start_epoch(epoch) if since_epoch is None else max(0, int(since_epoch * 1000))
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        model_rows = connection.execute("""
            SELECT
                COALESCE(NULLIF(json_extract(data, '$.providerID'), ''), 'unknown') AS provider,
                COALESCE(NULLIF(json_extract(data, '$.modelID'), ''), 'unknown') AS model,
                COUNT(*) AS messages,
                COALESCE(SUM(CAST(json_extract(data, '$.tokens.input') AS INTEGER)), 0) AS input_tokens,
                COALESCE(SUM(CAST(json_extract(data, '$.tokens.output') AS INTEGER)), 0) AS output_tokens,
                COALESCE(SUM(CAST(json_extract(data, '$.tokens.reasoning') AS INTEGER)), 0) AS reasoning_tokens,
                COALESCE(SUM(CAST(json_extract(data, '$.tokens.cache.read') AS INTEGER)), 0) AS cache_read_tokens,
                COALESCE(SUM(CAST(json_extract(data, '$.tokens.cache.write') AS INTEGER)), 0) AS cache_write_tokens,
                COALESCE(SUM(CAST(json_extract(data, '$.cost') AS REAL)), 0) AS cost_usd
            FROM message
            WHERE time_created >= ?
              AND json_extract(data, '$.role') = 'assistant'
            GROUP BY provider, model
            ORDER BY input_tokens + output_tokens + reasoning_tokens + cache_read_tokens + cache_write_tokens DESC, provider, model
        """, (cutoff,)).fetchall()
        session_count = _number_from_database(connection.execute("""
            SELECT COUNT(DISTINCT session_id)
            FROM message
            WHERE time_created >= ?
              AND json_extract(data, '$.role') = 'assistant'
        """, (cutoff,)).fetchone()[0])
    models = tuple({
        "id": f"{str(row[0])}/{str(row[1])}",
        "messages": _number_from_database(row[2]),
        "input_tokens": _number_from_database(row[3]),
        "output_tokens": _number_from_database(row[4]),
        "reasoning_tokens": _number_from_database(row[5]),
        "cache_read_tokens": _number_from_database(row[6]),
        "cache_write_tokens": _number_from_database(row[7]),
        "cost_usd": _cost_from_database(row[8]),
    } for row in model_rows)
    return OpenCodeStats(
        sessions=session_count,
        messages=sum(int(item["messages"]) for item in models),
        input_tokens=sum(int(item["input_tokens"]) for item in models),
        output_tokens=sum(int(item["output_tokens"]) for item in models),
        reasoning_tokens=sum(int(item["reasoning_tokens"]) for item in models),
        cache_read_tokens=sum(int(item["cache_read_tokens"]) for item in models),
        cache_write_tokens=sum(int(item["cache_write_tokens"]) for item in models),
        cost_usd=sum(float(item["cost_usd"]) for item in models),
        models=models,
        output_includes_reasoning=False,
    )


def read_opencode_desktop_daily_history(path: Path) -> tuple[OpenCodeDailyUsage, ...]:
    """Read exact calendar-day/model totals without selecting message content."""
    if not path.is_file():
        raise OSError("OpenCodeDesktopDatabaseMissing")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute("""
            SELECT
                date(time_created / 1000.0, 'unixepoch', 'localtime') AS local_day,
                COALESCE(NULLIF(json_extract(data, '$.providerID'), ''), 'unknown') AS provider,
                COALESCE(NULLIF(json_extract(data, '$.modelID'), ''), 'unknown') AS model,
                COALESCE(SUM(CAST(json_extract(data, '$.tokens.input') AS INTEGER)), 0)
                  + COALESCE(SUM(CAST(json_extract(data, '$.tokens.output') AS INTEGER)), 0)
                  + COALESCE(SUM(CAST(json_extract(data, '$.tokens.reasoning') AS INTEGER)), 0)
                  + COALESCE(SUM(CAST(json_extract(data, '$.tokens.cache.read') AS INTEGER)), 0)
                  + COALESCE(SUM(CAST(json_extract(data, '$.tokens.cache.write') AS INTEGER)), 0) AS total_tokens
            FROM message
            WHERE json_extract(data, '$.role') = 'assistant'
            GROUP BY local_day, provider, model
            ORDER BY local_day, total_tokens DESC, provider, model
        """).fetchall()
    days: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = str(row[0] or "")
        if not day:
            continue
        days.setdefault(day, []).append({
            "id": f"{str(row[1])}/{str(row[2])}",
            "tokens": _number_from_database(row[3]),
        })
    return tuple(
        OpenCodeDailyUsage(
            date=day,
            total_tokens=sum(int(model["tokens"]) for model in models),
            models=tuple(models),
        )
        for day, models in sorted(days.items())
    )


def _iso_now(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _metric_point(
    *, timestamp: str, epoch: float, metric: str, value: int | float, dimensions: dict[str, str], unit: str,
) -> MetricPoint:
    return MetricPoint(
        observed_at=timestamp,
        observed_epoch=epoch,
        metric=metric,
        instrument="counter",
        value=value,
        unit=unit,
        source_id="opencode",
        resource_id="ai_usage",
        dimensions=dimensions,
        attribution_method="exact",
        confidence="high",
    )


class OpenCodeUsageCollector:
    """Own OpenCode discovery, paced CLI polling, and counter checkpoints."""

    capability = CollectorCapability(
        id="ai.opencode.session-usage",
        source_id="opencode",
        source_kind="ai.opencode",
        resource_id="ai_usage",
        metrics=("ai.tokens.total", "ai.tokens.input", "ai.tokens.output", "ai.tokens.reasoning", "ai.tokens.cache_read", "ai.tokens.cache_write", "ai.cost.usd"),
    )

    def __init__(
        self,
        *,
        executable_finder: Callable[[], str | None] = discover_opencode,
        desktop_database_finder: Callable[[], Path | None] = discover_opencode_desktop_database,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.time,
        poll_seconds: int = OPENCODE_POLL_SECONDS,
        checkpoint_path: Path | None = None,
    ) -> None:
        self._executable_finder = executable_finder
        self._desktop_database_finder = desktop_database_finder
        self._runner = runner
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._next_poll_epoch = 0.0
        self._snapshot: dict[str, Any] = {"available": False, "status": "unavailable"}
        self._previous: dict[str, int | float] = {}
        self._checkpoint_path = checkpoint_path
        self._checkpoint_day: str | None = None

    def _load_checkpoint(self, epoch: float) -> None:
        day = datetime.fromtimestamp(epoch).astimezone().date().isoformat()
        if self._checkpoint_day == day:
            return
        payload: dict[str, Any] = {}
        if self._checkpoint_path is not None:
            try:
                payload = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        counters = payload.get("counters")
        counter_schema = payload.get("counter_schema", payload.get("schema"))
        self._previous = {
            str(key): value
            for key, value in counters.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        } if (
            counter_schema == OPENCODE_COUNTER_SCHEMA
            and payload.get("day") == day
            and isinstance(counters, dict)
        ) else {}
        self._checkpoint_day = day

    def _write_checkpoint(self) -> None:
        if self._checkpoint_path is None or self._checkpoint_day is None:
            return
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "schema": OPENCODE_CHECKPOINT_SCHEMA,
            "counter_schema": OPENCODE_COUNTER_SCHEMA,
            "day": self._checkpoint_day,
            "counters": self._previous,
        }, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._checkpoint_path)

    @staticmethod
    def _models_with_totals(stats: OpenCodeStats) -> list[dict[str, Any]]:
        return [dict(model, total_tokens=(
            int(model["input_tokens"]) + int(model["output_tokens"]) + int(model.get("reasoning_tokens", 0))
            + int(model["cache_read_tokens"]) + int(model["cache_write_tokens"])
        )) for model in stats.models]

    def _snapshot_for(
        self,
        stats: OpenCodeStats,
        timestamp: str,
        collection_method: str,
        lifetime_tokens: int | None,
        lifetime_models: list[dict[str, Any]] | None,
        daily_history: tuple[OpenCodeDailyUsage, ...] | None = None,
    ) -> dict[str, Any]:
        today_models = self._models_with_totals(stats)
        today_by_model = {str(model["id"]): int(model["total_tokens"]) for model in today_models}
        lifetime_by_model = {str(model["id"]): int(model["total_tokens"]) for model in lifetime_models or []}
        model_ids = list(dict.fromkeys([
            *(str(model["id"]) for model in lifetime_models or []),
            *(str(model["id"]) for model in today_models),
        ]))
        models = [model_usage(
            model_id,
            today_tokens=today_by_model.get(model_id, 0),
            cumulative_tokens=lifetime_by_model.get(model_id),
            today_method="provider-day",
            today_detail=localized("provider model total", "提供方模型累计"),
            cumulative_method="provider-history",
            cumulative_detail=localized("readable local history", "可读本地历史"),
        ) for model_id in model_ids]
        output_label = localized("Output + reasoning", "输出 + 推理") if stats.output_includes_reasoning else localized("Output", "输出")
        return ai_usage_snapshot(
            source_id="opencode",
            label="OpenCode",
            status="ok",
            observed_at=timestamp,
            collection_method=collection_method,
            today=usage_window(
                stats.total_tokens,
                method="provider-day",
                detail=localized("provider-reported session metadata", "供应商返回的会话元数据"),
            ),
            cumulative=usage_window(
                lifetime_tokens,
                method="provider-history" if lifetime_tokens is not None else "unavailable",
                detail=localized("all readable local history", "全部可读本地历史") if lifetime_tokens is not None else localized("not available from this collector", "当前采集器无法提供"),
            ),
            models=models,
            details=[
                detail_group("token-breakdown", localized("Token breakdown", "Token 分类明细"), [
                    token_metric("input", localized("Input", "输入"), stats.input_tokens, localized("provider-reported", "供应商返回")),
                    token_metric("output", output_label, stats.output_tokens, localized("reasoning included", "含推理 Token") if stats.output_includes_reasoning else localized("provider-reported", "供应商返回")),
                    token_metric("reasoning", localized("Reasoning", "推理"), stats.reasoning_tokens, localized("provider-reported", "供应商返回")),
                    token_metric("cache", localized("Cache", "缓存"), stats.cache_read_tokens + stats.cache_write_tokens, localized(f"Read {stats.cache_read_tokens} · Write {stats.cache_write_tokens}", f"读取 {stats.cache_read_tokens} · 写入 {stats.cache_write_tokens}")),
                ], badge=localized("today", "今日")),
                detail_group("activity", localized("Activity", "活动"), [
                    token_metric("sessions", localized("Sessions", "会话"), stats.sessions, localized("readable local session count", "可读本地会话数"), unit="count"),
                    token_metric("messages", localized("Messages", "消息"), stats.messages, localized("assistant message metadata", "助手消息元数据"), unit="count"),
                    token_metric("reported-cost", localized("Reported cost", "已报告成本"), stats.cost_usd, localized("provider-reported for this window", "供应商返回的当前窗口成本"), unit="usd"),
                ]),
            ],
            confidence="high",
            privacy="aggregate-session-stats-only",
            daily_history=[
                daily_usage(day.date, day.total_tokens, list(day.models))
                for day in daily_history
            ] if daily_history is not None else None,
        )

    def _interval_points(self, stats: OpenCodeStats, timestamp: str, epoch: float) -> tuple[MetricPoint, ...]:
        current: dict[str, int | float] = {"all:ai.messages": stats.messages}
        fields = {
            "total_tokens": ("ai.tokens.total", "tokens"),
            "input_tokens": ("ai.tokens.input", "tokens"),
            "output_tokens": ("ai.tokens.output", "tokens"),
            "reasoning_tokens": ("ai.tokens.reasoning", "tokens"),
            "cache_read_tokens": ("ai.tokens.cache_read", "tokens"),
            "cache_write_tokens": ("ai.tokens.cache_write", "tokens"),
            "cost_usd": ("ai.cost.usd", "usd"),
        }
        metric_units = {metric: unit for metric, unit in fields.values()} | {"ai.messages": "messages"}
        if stats.models:
            for model in stats.models:
                for field, (metric, _unit) in fields.items():
                    current[f"{model['id']}:{metric}"] = (
                        int(model["input_tokens"]) + int(model["output_tokens"]) + int(model.get("reasoning_tokens", 0))
                        + int(model["cache_read_tokens"]) + int(model["cache_write_tokens"])
                    ) if field == "total_tokens" else model.get(field, 0)
        else:
            current = {
                "all:ai.tokens.input": stats.input_tokens,
                "all:ai.tokens.total": stats.total_tokens,
                "all:ai.tokens.output": stats.output_tokens,
                "all:ai.tokens.reasoning": stats.reasoning_tokens,
                "all:ai.tokens.cache_read": stats.cache_read_tokens,
                "all:ai.tokens.cache_write": stats.cache_write_tokens,
                "all:ai.cost.usd": stats.cost_usd,
                "all:ai.messages": stats.messages,
            }
        points: list[MetricPoint] = []
        for key, total in current.items():
            previous = self._previous.get(key)
            delta = total if previous is None or total < previous else total - previous
            self._previous[key] = total
            if not delta:
                continue
            _scope, metric = key.rsplit(":", 1)
            model_id: str | None = None
            # Split only on the known metric suffix; provider/model identifiers
            # can themselves contain ':' in custom OpenCode configurations.
            for candidate in ("ai.tokens.total", "ai.tokens.input", "ai.tokens.output", "ai.tokens.reasoning", "ai.tokens.cache_read", "ai.tokens.cache_write", "ai.cost.usd", "ai.messages"):
                suffix = f":{candidate}"
                if key.endswith(suffix):
                    model_id = key[:-len(suffix)]
                    metric = candidate
                    break
            unit = metric_units[metric]
            dimensions = {"scope": "all"} if model_id == "all" else {"model": str(model_id)}
            points.append(_metric_point(timestamp=timestamp, epoch=epoch, metric=metric, value=delta, dimensions=dimensions, unit=unit))
        return tuple(points)

    def collect(self, context: CollectorContext) -> Collection:
        epoch = float(context.local_sample.get("epoch") or self._clock())
        if epoch < self._next_poll_epoch:
            return Collection(status=str(self._snapshot.get("status", "ok")), snapshot=self._snapshot)
        self._next_poll_epoch = epoch + self._poll_seconds
        desktop_database = self._desktop_database_finder()
        executable = None if desktop_database else self._executable_finder()
        if not executable and not desktop_database:
            self._snapshot = {"available": False, "status": "unavailable"}
            return Collection(status="unavailable", snapshot=self._snapshot)
        try:
            timestamp = _iso_now(epoch)
            self._load_checkpoint(epoch)
            if desktop_database:
                stats = read_opencode_desktop_stats(desktop_database, epoch)
                lifetime_stats = read_opencode_desktop_stats(desktop_database, epoch, since_epoch=0)
                daily_history = read_opencode_desktop_daily_history(desktop_database)
                lifetime_tokens = lifetime_stats.total_tokens
                lifetime_models = self._models_with_totals(lifetime_stats)
                collection_method = "desktop-session-metadata"
            else:
                completed = self._runner(
                    [executable, "stats", "--days", "0", "--models"],
                    capture_output=True,
                    text=True,
                    timeout=OPENCODE_TIMEOUT_SECONDS,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError("OpenCodeStatsCommandFailed")
                stats = parse_opencode_stats(completed.stdout)
                lifetime_tokens = None
                lifetime_models = None
                daily_history = None
                collection_method = "cli-session-summary"
            snapshot = self._snapshot_for(
                stats, timestamp, collection_method, lifetime_tokens, lifetime_models, daily_history,
            )
            points = self._interval_points(stats, timestamp, epoch)
            self._write_checkpoint()
        except (OSError, sqlite3.DatabaseError, subprocess.TimeoutExpired, ValueError, RuntimeError):
            # Keep the last complete aggregate visible, but make its stale
            # state unmistakable.  A parser or CLI failure must never turn a
            # known total into a misleading zero.
            self._snapshot = {**self._snapshot, "available": True, "status": "error", "label": "OpenCode"}
            return Collection(status="error", snapshot=self._snapshot)
        self._snapshot = snapshot
        return Collection(points=points, status="ok", snapshot=snapshot)
