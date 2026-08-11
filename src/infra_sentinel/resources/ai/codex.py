"""Privacy-minimal Codex local workload collector.

Codex currently persists a per-thread ``tokens_used`` counter in its local
state database.  Child-agent rollouts retain their own cumulative context, so
they must not be added to the user-facing token counter.  This collector uses
only ``thread_source = 'user'`` for user token and model totals, while keeping
all-thread topology as a separate workload signal.  It never selects titles,
prompts, previews, working directories, Git metadata, agent names, or agent
paths.  Its daily window starts from a Sentinel baseline on the local calendar
day, which may not share the reporting boundary used by Codex's own panel.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from infra_sentinel.resources.ai.contract import ai_usage_snapshot, detail_group, localized, model_usage, token_metric, usage_window
from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.core.model import MetricPoint


CODEX_POLL_SECONDS = 20
ACTIVE_WINDOW_SECONDS = 10 * 60
DAILY_BASELINE_SCHEMA = "20260809.4"


@dataclass(frozen=True)
class CodexThreadCounter:
    identifier: str
    model: str
    tokens: int


@dataclass(frozen=True)
class CodexStats:
    total_tokens: int
    models: tuple[dict[str, Any], ...]
    user_counters: tuple[CodexThreadCounter, ...]
    threads: int
    user_threads: int
    subagents: int
    subagent_tokens: int
    recent_threads: int
    recent_subagents: int
    maximum_depth: int


def discover_codex_state_database(preferred: Path | None = None) -> Path | None:
    """Find the active Codex state store, preferring the current location."""
    candidates = (
        preferred,
        Path.home() / ".codex" / "state_5.sqlite",
        Path.home() / ".codex" / "sqlite" / "state_5.sqlite",
    )
    return next((candidate for candidate in candidates if candidate and candidate.is_file()), None)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _iso_now(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def read_codex_state_stats(path: Path, epoch: float) -> CodexStats:
    """Read only aggregate counters and topology facts from Codex state.

    The SQL expressions classify a source inside SQLite and return only
    counts.  The JSON source payload itself, which can contain task-specific
    metadata, is never selected or returned.
    """
    if not path.is_file():
        raise OSError("CodexStateDatabaseMissing")
    cutoff = int((epoch - ACTIVE_WINDOW_SECONDS) * 1000)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        counter_rows = connection.execute("""
            SELECT id, COALESCE(NULLIF(model, ''), 'unknown'), tokens_used
            FROM threads
            WHERE thread_source = 'user'
            ORDER BY id
        """).fetchall()
        totals = connection.execute("""
            SELECT
                COUNT(*) AS threads,
                COALESCE(SUM(CASE WHEN thread_source = 'user' THEN tokens_used ELSE 0 END), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN thread_source = 'user' THEN 1 ELSE 0 END), 0) AS user_threads,
                COALESCE(SUM(CASE
                    WHEN thread_source = 'subagent'
                      OR (json_valid(source) AND json_type(source, '$.subagent') IS NOT NULL)
                    THEN 1 ELSE 0 END), 0) AS subagents,
                COALESCE(SUM(CASE
                    WHEN thread_source = 'subagent'
                      OR (json_valid(source) AND json_type(source, '$.subagent') IS NOT NULL)
                    THEN tokens_used ELSE 0 END), 0) AS subagent_tokens,
                COALESCE(SUM(CASE WHEN updated_at_ms >= ? THEN 1 ELSE 0 END), 0) AS recent_threads,
                COALESCE(SUM(CASE WHEN updated_at_ms >= ? AND (
                    thread_source = 'subagent'
                    OR (json_valid(source) AND json_type(source, '$.subagent') IS NOT NULL)
                ) THEN 1 ELSE 0 END), 0) AS recent_subagents,
                COALESCE(MAX(CASE WHEN json_valid(source)
                    THEN CAST(json_extract(source, '$.subagent.thread_spawn.depth') AS INTEGER)
                    ELSE 0 END), 0) AS maximum_depth
            FROM threads
        """, (cutoff, cutoff)).fetchone()
    counters = tuple(CodexThreadCounter(str(row[0]), str(row[1]), _integer(row[2])) for row in counter_rows)
    model_totals: dict[str, dict[str, int]] = {}
    for counter in counters:
        model = model_totals.setdefault(counter.model, {"threads": 0, "total_tokens": 0})
        model["threads"] += 1
        model["total_tokens"] += counter.tokens
    models = tuple({"id": model, **values} for model, values in sorted(
        model_totals.items(), key=lambda item: (-item[1]["total_tokens"], item[0])
    ))
    return CodexStats(
        total_tokens=_integer(totals[1]),
        models=models,
        user_counters=counters,
        threads=_integer(totals[0]),
        user_threads=_integer(totals[2]),
        subagents=_integer(totals[3]),
        subagent_tokens=_integer(totals[4]),
        recent_threads=_integer(totals[5]),
        recent_subagents=_integer(totals[6]),
        maximum_depth=_integer(totals[7]),
    )


class CodexUsageCollector:
    """Own Codex local-state discovery, aggregate reads, and delta emission."""

    capability = CollectorCapability(
        id="ai.codex.local-workload",
        source_id="codex",
        source_kind="ai.codex",
        resource_id="ai_usage",
        metrics=("ai.tokens.total", "ai.threads", "ai.subagents"),
    )

    def __init__(
        self,
        *,
        database_finder: Callable[[], Path | None] = discover_codex_state_database,
        clock: Callable[[], float] = time.time,
        poll_seconds: int = CODEX_POLL_SECONDS,
        checkpoint_path: Path | None = None,
    ) -> None:
        self._database_finder = database_finder
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._next_poll_epoch = 0.0
        self._snapshot: dict[str, Any] = {"available": False, "status": "unavailable"}
        self._previous_thread_tokens: dict[str, int] = {}
        self._checkpoint_path = checkpoint_path
        self._daily_baseline: int | None = None
        self._daily_thread_baselines: dict[str, int] = {}
        self._daily_key: str | None = None
        self._daily_started_at: str | None = None

    def _write_daily_checkpoint(self) -> None:
        if self._checkpoint_path is None or self._daily_key is None or self._daily_baseline is None:
            return
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "schema": DAILY_BASELINE_SCHEMA, "day": self._daily_key,
            "baseline_tokens": self._daily_baseline,
            "baseline_threads": self._daily_thread_baselines,
            "last_threads": self._previous_thread_tokens,
            "started_at": self._daily_started_at,
        }, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._checkpoint_path)

    def _daily_window(self, stats: CodexStats, epoch: float, timestamp: str) -> tuple[int, str, dict[str, int]]:
        day = datetime.fromtimestamp(epoch).astimezone().date().isoformat()
        if self._daily_key != day:
            checkpoint: dict[str, Any] = {}
            if self._checkpoint_path is not None:
                try:
                    checkpoint = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    checkpoint = {}
            stored_baseline = _integer(checkpoint.get("baseline_tokens"))
            checkpoint_is_current = (
                checkpoint.get("schema") == DAILY_BASELINE_SCHEMA
                and checkpoint.get("day") == day
                # A counter cannot legitimately be below a daily baseline.
                # Treat it as a changed accounting scope and re-baseline rather
                # than indefinitely displaying a protected zero.
                and stored_baseline <= stats.total_tokens
            )
            if checkpoint_is_current:
                self._daily_baseline = stored_baseline
                stored_threads = checkpoint.get("baseline_threads")
                self._daily_thread_baselines = {
                    str(identifier): _integer(tokens)
                    for identifier, tokens in stored_threads.items()
                } if isinstance(stored_threads, dict) else {}
                last_threads = checkpoint.get("last_threads")
                self._previous_thread_tokens = {
                    str(identifier): _integer(tokens)
                    for identifier, tokens in last_threads.items()
                } if isinstance(last_threads, dict) else {
                    counter.identifier: counter.tokens for counter in stats.user_counters
                }
                self._daily_started_at = str(checkpoint.get("started_at") or timestamp)
            else:
                self._daily_baseline = stats.total_tokens
                self._daily_thread_baselines = {counter.identifier: counter.tokens for counter in stats.user_counters}
                self._previous_thread_tokens = dict(self._daily_thread_baselines)
                self._daily_started_at = timestamp
            self._daily_key = day
            self._write_daily_checkpoint()
        model_today: dict[str, int] = {}
        for counter in stats.user_counters:
            baseline = self._daily_thread_baselines.get(counter.identifier, 0)
            delta = max(0, counter.tokens - baseline)
            model_today[counter.model] = model_today.get(counter.model, 0) + delta
        return sum(model_today.values()), self._daily_started_at or timestamp, model_today

    def _snapshot_for(self, stats: CodexStats, timestamp: str, epoch: float) -> dict[str, Any]:
        today_tokens, started_at, model_today = self._daily_window(stats, epoch, timestamp)
        today_detail = localized(
            "local calendar-day baseline; may differ from the Codex Usage panel reporting day",
            "本机自然日基线；可能与 Codex 用量面板的统计日界线不同",
        )
        cumulative_detail = localized(
            "sum of readable local root-thread counters; approximate and not billing",
            "可读本地主任务计数器之和；近似值，非账单口径",
        )
        return ai_usage_snapshot(
            source_id="codex",
            label="Codex",
            status="ok",
            observed_at=timestamp,
            collection_method="local-state-metadata",
            today=usage_window(
                today_tokens,
                method="sentinel-day-baseline",
                detail=today_detail,
                started_at=started_at,
            ),
            cumulative=usage_window(
                stats.total_tokens,
                method="local-root-thread",
                detail=cumulative_detail,
            ),
            models=[model_usage(
                str(model["id"]),
                today_tokens=model_today.get(str(model["id"]), 0),
                cumulative_tokens=int(model["total_tokens"]),
                today_method="sentinel-day-baseline",
                today_detail=today_detail,
                cumulative_method="local-root-thread",
                cumulative_detail=cumulative_detail,
            ) for model in stats.models],
            details=[detail_group("task-topology", localized("Task topology", "任务拓扑"), [
                token_metric("root-threads", localized("Root threads", "主任务"), stats.user_threads, localized("source classified as user", "来源标为 user"), unit="count"),
                token_metric("all-local-threads", localized("All local threads", "本地全部任务"), stats.threads, localized("content never read", "不读取任务内容"), unit="count"),
                token_metric("subagents", localized("Subagents", "子 Agent"), stats.subagents, localized("workload count", "工作负载数量"), unit="count"),
                token_metric("recent-subagents", localized("Recent subagents", "近期子 Agent"), stats.recent_subagents, localized(f"{ACTIVE_WINDOW_SECONDS // 60} minute activity window", f"{ACTIVE_WINDOW_SECONDS // 60} 分钟活动窗口"), unit="count"),
                token_metric("deepest-spawn", localized("Deepest spawn", "最大层级"), stats.maximum_depth, localized("observed spawn depth", "已观测派生层级"), unit="count"),
                token_metric("derived-rollout-raw-count", localized("Derived rollout raw count", "派生 rollout 原始计数"), stats.subagent_tokens, localized("not additive to root or account total", "不可与主任务或账户总量相加")),
            ], note=localized(
                "Derived rollout counters can include child work, replayed parent context, and cached input. They are diagnostic only, not a billing or account-activity total.",
                "派生 rollout 计数可能包含子任务工作、重放的父上下文与缓存输入；仅用于诊断，不是账单或账户活动总量。",
            ), badge=localized("diagnostic only", "仅诊断"))],
            confidence="medium",
            privacy="aggregate-thread-state-only",
        )

    @staticmethod
    def _point(timestamp: str, epoch: float, metric: str, value: int, dimensions: dict[str, str]) -> MetricPoint:
        return MetricPoint(
            observed_at=timestamp,
            observed_epoch=epoch,
            metric=metric,
            instrument="counter",
            value=value,
            unit="tokens" if metric == "ai.tokens.total" else "threads",
            source_id="codex",
            resource_id="ai_usage",
            dimensions=dimensions,
            attribution_method="local-reported",
            confidence="medium",
        )

    def collect(self, context: CollectorContext) -> Collection:
        epoch = float(context.local_sample.get("epoch") or self._clock())
        if epoch < self._next_poll_epoch:
            return Collection(status=str(self._snapshot.get("status", "ok")), snapshot=self._snapshot)
        self._next_poll_epoch = epoch + self._poll_seconds
        database = self._database_finder()
        if database is None:
            self._snapshot = {"available": False, "status": "unavailable"}
            return Collection(status="unavailable", snapshot=self._snapshot)
        try:
            timestamp = _iso_now(epoch)
            stats = read_codex_state_stats(database, epoch)
            snapshot = self._snapshot_for(stats, timestamp, epoch)
        except (OSError, sqlite3.DatabaseError, ValueError):
            self._snapshot = {**self._snapshot, "available": True, "status": "error", "label": "Codex"}
            return Collection(status="error", snapshot=self._snapshot)
        points_list: list[MetricPoint] = []
        model_deltas: dict[str, int] = {}
        for counter in stats.user_counters:
            previous = self._previous_thread_tokens.get(counter.identifier, 0)
            delta = max(0, counter.tokens - previous)
            if delta:
                model_deltas[counter.model] = model_deltas.get(counter.model, 0) + delta
        total_delta = sum(model_deltas.values())
        if total_delta:
            points_list.append(self._point(timestamp, epoch, "ai.tokens.total", total_delta, {"scope": "local-state"}))
            points_list.extend(
                self._point(timestamp, epoch, "ai.tokens.total", value, {"model": model})
                for model, value in sorted(model_deltas.items())
            )
        self._previous_thread_tokens = {counter.identifier: counter.tokens for counter in stats.user_counters}
        self._write_daily_checkpoint()
        self._snapshot = snapshot
        return Collection(points=tuple(points_list), status="ok", snapshot=snapshot)
