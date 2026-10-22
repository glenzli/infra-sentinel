"""Read-only, provider-reported DeepSeek Harness session usage.

Only usage, route, lifecycle and event-time metadata leave the parser. Streaming
and final reports replace one another within an attempt; retries are separate
attempts. Persistent state contains only irreversible request markers.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from infra_sentinel.core.collectors import Collection, CollectorCapability, CollectorContext
from infra_sentinel.core.model import MetricPoint
from infra_sentinel.resources.ai.performance import PerformanceSample, model_performance_details
from infra_sentinel.resources.ai.contract import (
    ai_usage_snapshot, daily_usage, detail_group, hourly_usage, localized, model_usage,
    token_metric, usage_window,
)



POLL_SECONDS = 60
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_SESSION_BYTES = 64 * 1024 * 1024
CHECKPOINT_SCHEMA = "dsh-usage-seen@20261015.1"
FIELDS = {"inputTokens": "input", "outputTokens": "output", "cacheReadTokens": "cache_read", "cacheWriteTokens": "cache_write"}


def discover_dsh_sessions() -> Path | None:
    root = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser() / "sessions"
    return root if root.is_dir() else None


def _counter(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("Invalid DSH usage metadata")
    return value


def _marker(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _route(value: object, fallback: str = "unknown") -> str:
    if not isinstance(value, dict):
        return fallback
    provider, model = value.get("provider"), value.get("model")
    if all(isinstance(part, str) and 0 < len(part) <= 128 for part in (provider, model)):
        return f"{provider}/{model}"
    return fallback


@contextmanager
def _open_log(path: Path):
    if path.name.endswith(".zstd"):
        try:
            from compression import zstd
            opener, errors = zstd.open, (zstd.ZstdError,)
        except ImportError:
            try:
                import zstandard
            except ImportError as error:
                raise ValueError("DSH Zstd support requires Python 3.14 or the dsh extra") from error
            opener, errors = zstandard.open, (zstandard.ZstdError,)
        try:
            with opener(path, "rb") as stream:
                yield stream
        except errors as error:
            raise ValueError("Invalid DSH Zstd session") from error
    else:
        with path.open("rb") as stream:
            yield stream


@dataclass(frozen=True)
class DshRequest:
    marker: str
    epoch: float
    model: str
    usage: dict[str, int]
    settled: bool
    performance: PerformanceSample | None = None


@dataclass(frozen=True)
class DshSession:
    marker: str
    sequence: int
    requests: tuple[DshRequest, ...]


def _stream_performance(data: dict[str, Any], epoch: float, model: str, output: int,
                        started: int | None) -> PerformanceSample | None:
    stream = data.get("stream")
    if not isinstance(stream, list) or not stream or data.get("interrupted") is True:
        return None
    first = None
    previous = -1
    finished = None
    for index, record in enumerate(stream):
        if not isinstance(record, dict):
            return None
        kind = record.get("type")
        if kind == "chunk":
            stamp = record.get("time")
            chunk = record.get("chunk")
            if type(stamp) is not int or not 0 <= stamp <= 2**53 - 1 or stamp < previous or not isinstance(chunk, dict):
                return None
            previous = stamp
            if chunk.get("type") in {"text-delta", "reasoning-delta", "tool-call-delta"}:
                first = stamp if first is None else first
            if chunk.get("type") == "finish":
                reason = chunk.get("reason")
                reason_kind = reason.get("kind", reason.get("type")) if isinstance(reason, dict) else reason
                if index != len(stream) - 1 or reason_kind not in ("stop", "tool-calls", "max-tokens", "length"):
                    return None
                finished = stamp
        elif kind in {"text-chunks", "reasoning-chunks", "tool-call-chunks"}:
            stamp, deltas = record.get("time0"), record.get("dt")
            members = record.get("args" if kind == "tool-call-chunks" else "texts")
            if (type(stamp) is not int or stamp < previous or not isinstance(deltas, list)
                    or not isinstance(members, list) or not members or len(deltas) != len(members) - 1
                    or any(type(dt) is not int or dt < 0 for dt in deltas)):
                return None
            previous = stamp + sum(deltas)
            first = stamp if first is None else first
        else:
            return None
    if first is None or finished is None or finished <= first or finished > round(epoch * 1000):
        return None
    reliable_start = type(started) is int and 0 <= started <= first
    return PerformanceSample(epoch, model, output,
                             finished - started if reliable_start else None,
                             first - started if reliable_start else None, finished - first)


def read_dsh_session(path: Path) -> DshSession:
    """Fold the supported v4 durable encoding without retaining transcript data."""
    samples: dict[str, DshRequest] = {}
    pending: DshRequest | None = None
    placement: tuple[int, int] | None = None
    attempt = 0
    route = "unknown"
    sequence = -1
    consumed = 0
    starts: dict[tuple[int, int], int | None] = {}
    with _open_log(path) as stream:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            raise ValueError("Incomplete or oversized DSH header")
        header = json.loads(line)
        if not isinstance(header, dict) or header.get("type") != "session" or header.get("version") != 4:
            raise ValueError("Unsupported DSH session format")
        identity = header.get("id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Missing DSH session identity")
        inherited = header.get("seedLength", header.get("inheritedEventCount"))
        if inherited is None:
            if header.get("isSeeded") is True or header.get("parentSession"):
                raise ValueError("Unknown DSH inherited prefix")
            inherited = 0
        inherited = _counter(inherited)
        session_marker = _marker(identity)
        while line := stream.readline(MAX_LINE_BYTES + 1):
            consumed += len(line)
            if len(line) > MAX_LINE_BYTES or consumed > MAX_SESSION_BYTES or not line.endswith(b"\n"):
                raise ValueError("Incomplete or oversized DSH session")
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("Invalid DSH session record")
            kind, data = record.get("type"), record.get("data")
            # Packed rows contain text deltas only; usage is never packed.
            if kind in {"text-chunks", "reasoning-chunks", "tool-call-chunks"}:
                continue
            seq = _counter(record.get("seq"))
            if seq <= sequence:
                raise ValueError("Unordered DSH session")
            sequence = seq
            if seq < inherited or not isinstance(data, dict):
                continue
            timing_key = (data.get("turn"), data.get("step"))
            if kind == "step/start" and all(type(v) is int and v >= 0 for v in timing_key):
                stamp = record.get("time")
                starts[timing_key] = stamp if type(stamp) is int and stamp >= 0 else None
            elif kind in {"assistant/attempt", "llm/retry-started"} and all(type(v) is int and v >= 0 for v in timing_key):
                # A retry has no explicit model-dispatch timestamp. Keep its
                # observed stream, but never borrow the original attempt start.
                starts[timing_key] = None
            if kind == "request/context":
                route = _route(data, route)
            elif kind == "request/header":
                request_header = data.get("header", {})
                if isinstance(request_header, dict):
                    route = _route(request_header.get("config"), route)
            if kind == "llm/retry-started":
                if pending is not None:
                    samples[pending.marker] = replace(pending, settled=True)
                    pending = None
                attempt += 1
                continue
            raw_usage = None
            if kind == "assistant/message":
                raw_usage = data.get("usage")
            elif kind == "assistant/chunk":
                chunk = data.get("chunk")
                if isinstance(chunk, dict) and chunk.get("type") == "usage":
                    raw_usage = chunk.get("usage")
            if raw_usage is not None:
                if not isinstance(raw_usage, dict):
                    raise ValueError("Invalid DSH usage report")
                key = (_counter(data.get("turn")), _counter(data.get("step")))
                if key != placement:
                    if pending is not None:
                        samples[pending.marker] = replace(pending, settled=True)
                    placement, attempt = key, 0
                counters = {target: _counter(raw_usage.get(source, 0)) for source, target in FIELDS.items()}
                if "inputTokens" not in raw_usage or "outputTokens" not in raw_usage:
                    raise ValueError("Missing DSH token counters")
                if "totalTokens" in raw_usage and _counter(raw_usage["totalTokens"]) != sum(counters.values()):
                    raise ValueError("Conflicting DSH token total")
                epoch = _counter(record.get("time")) / 1_000
                datetime.fromtimestamp(epoch)
                message = data.get("message", {})
                model = _route(message.get("source"), route) if isinstance(message, dict) else route
                pending = DshRequest(
                    _marker(f"{session_marker}:{key[0]}:{key[1]}:{attempt}"),
                    epoch, model, counters, kind == "assistant/message",
                    _stream_performance(data, epoch, model, counters["output"], starts.get(key))
                    if kind == "assistant/message" else None,
                )
            elif kind in {"step/end", "turn/end"} and pending is not None:
                samples[pending.marker] = replace(pending, settled=True)
                pending = None
        if pending is not None:
            samples[pending.marker] = pending
    return DshSession(session_marker, sequence, tuple(samples.values()))


class DshUsageCollector:
    capability = CollectorCapability("ai.dsh.local-sessions", "dsh", "ai.dsh", "ai_usage", ("ai.tokens.total",))

    def __init__(
        self, *, sessions_finder: Callable[[], Path | None] = discover_dsh_sessions,
        checkpoint_path: Path | None = None, poll_seconds: int = POLL_SECONDS,
    ) -> None:
        self._finder = sessions_finder
        self._checkpoint = checkpoint_path
        self._poll_seconds = max(1, poll_seconds)
        self._next_poll = 0.0
        self._checked_at: str | None = None
        self._cache: dict[Path, tuple[tuple[int, int], DshSession]] = {}
        self._snapshot: dict[str, Any] = {"available": False, "status": "unavailable"}
        self._seen: set[str] | None = None
        if checkpoint_path is not None:
            try:
                saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if (
                    isinstance(saved, dict) and saved.get("schema") == CHECKPOINT_SCHEMA
                    and isinstance(saved.get("seen"), list)
                    and all(isinstance(key, str) and len(key) == 64 and all(c in "0123456789abcdef" for c in key) for key in saved["seen"])
                ):
                    self._seen = set(saved["seen"])
            except (OSError, ValueError):
                pass

    def collect(self, context: CollectorContext) -> Collection:
        epoch = float(context.local_sample["epoch"])
        if epoch < self._next_poll:
            return Collection(status=self._snapshot["status"], snapshot=self._snapshot, checked_at=self._checked_at)
        self._next_poll = epoch + self._poll_seconds
        self._checked_at = datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
        root = self._finder()
        if root is None:
            self._snapshot = {"available": False, "status": "unavailable", "label": "DSH"}
            return Collection(status="unavailable", snapshot=self._snapshot, checked_at=self._checked_at)
        sessions: dict[str, DshSession] = {}
        errors = 0
        paths = sorted({*root.rglob("*.jsonl"), *root.rglob("*.jsonl.zstd")})
        for path in paths:
            previous = self._cache.get(path)
            try:
                stat = path.stat()
                fingerprint = (stat.st_size, stat.st_mtime_ns)
                session = previous[1] if previous is not None and previous[0] == fingerprint else read_dsh_session(path)
                self._cache[path] = (fingerprint, session)
            except (OSError, ValueError, EOFError, UnicodeError, OverflowError):
                errors += 1
                if previous is None:
                    continue
                session = previous[1]
            if session.marker not in sessions or session.sequence > sessions[session.marker].sequence:
                sessions[session.marker] = session
        self._cache = {path: value for path, value in self._cache.items() if path in paths}
        if errors and not sessions:
            self._snapshot = {"available": False, "status": "error", "label": "DSH"}
            return Collection(status="error", snapshot=self._snapshot, checked_at=self._checked_at)
        requests = [request for session in sessions.values() for request in session.requests]
        points = []
        bootstrap = self._seen is None
        seen = set(self._seen or ())
        for request in requests:
            if not request.settled or request.marker in seen:
                continue
            seen.add(request.marker)
            if not bootstrap and sum(request.usage.values()) > 0:
                points.append(MetricPoint(
                    observed_at=datetime.fromtimestamp(request.epoch).astimezone().isoformat(timespec="milliseconds"),
                    observed_epoch=request.epoch, metric="ai.tokens.total", instrument="counter",
                    value=sum(request.usage.values()), unit="tokens", source_id="dsh", resource_id="ai_usage",
                    dimensions={"model": request.model}, attribution_method="local-dsh-request", confidence="high",
                ))
        if self._checkpoint is not None and (bootstrap or seen != self._seen):
            self._checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._checkpoint.with_suffix(".tmp")
            temporary.write_text(json.dumps({"schema": CHECKPOINT_SCHEMA, "seen": sorted(seen)}), encoding="utf-8")
            temporary.replace(self._checkpoint)
        self._seen = seen
        self._snapshot = self._make_snapshot(requests, epoch, len(sessions), errors)
        timed = [r.performance for r in requests if r.settled and r.performance is not None]
        self._snapshot["details"].extend(model_performance_details(timed))
        return Collection(points=tuple(points), status=self._snapshot["status"], snapshot=self._snapshot, checked_at=self._checked_at)

    @staticmethod
    def _make_snapshot(requests: list[DshRequest], epoch: float, sessions: int, errors: int) -> dict[str, Any]:
        now = datetime.fromtimestamp(epoch).astimezone()
        day = now.date().isoformat()
        days: dict[str, dict[str, int]] = {}
        hours: dict[int, dict[str, int]] = {}
        models: dict[str, int] = {}
        today_models: dict[str, int] = {}
        composition = dict.fromkeys(FIELDS.values(), 0)
        for request in requests:
            date = datetime.fromtimestamp(request.epoch).astimezone()
            total = sum(request.usage.values())
            models[request.model] = models.get(request.model, 0) + total
            by_day = days.setdefault(date.date().isoformat(), {})
            by_day[request.model] = by_day.get(request.model, 0) + total
            if date.date().isoformat() == day:
                today_models[request.model] = today_models.get(request.model, 0) + total
                by_hour = hours.setdefault(int(request.epoch // 3600) * 3600, {})
                by_hour[request.model] = by_hour.get(request.model, 0) + total
                for field, value in request.usage.items():
                    composition[field] += value
        return ai_usage_snapshot(
            source_id="dsh", label="DSH", status="degraded" if errors else "ok", observed_at=now.isoformat(timespec="seconds"),
            collection_method="DSH local session usage metadata",
            today=usage_window(sum(today_models.values()), method="local-event-day", detail=localized("provider usage by event date", "按事件日期统计供应商返回用量")),
            cumulative=usage_window(sum(models.values()), method="readable-local-sessions", detail=localized("all readable local DSH sessions", "全部可读的本机 DSH 会话")),
            models=[model_usage(model, today_tokens=today_models.get(model, 0), cumulative_tokens=total) for model, total in sorted(models.items())],
            details=[detail_group("dsh-tokens", localized("Token breakdown", "Token 分类明细"), [
                token_metric(field, label, composition[field], localized("today · provider-reported", "今日 · 供应商返回"))
                for field, label in (
                    ("input", localized("Uncached input", "未缓存输入")), ("output", localized("Output", "输出")),
                    ("cache_read", localized("Cache read", "缓存读取")), ("cache_write", localized("Cache write", "缓存写入")),
                )
            ] + [
                token_metric("sessions", localized("Readable sessions", "可读会话"), sessions, localized("local sessions", "本机会话"), unit="count"),
                token_metric("unreadable-sessions", localized("Unreadable session files", "未能读取的会话文件"), errors, localized("unsupported or incomplete records", "格式不支持或记录尚未完整写入"), unit="count"),
            ],
                note=localized("Streaming samples are replaced by final usage for the same attempt; retries count separately. Explicit inherited prefixes are excluded. No model-price aliases are inferred.", "同次调用的最终用量替换流式样本；重试单独计数。排除明确的继承前缀，不推测模型价格别名。"))],
            confidence="high", privacy="usage metadata and irreversible request markers only",
            daily_history=[daily_usage(date, sum(rows.values()), [{"id": model, "tokens": total} for model, total in sorted(rows.items())]) for date, rows in sorted(days.items())],
            hourly_history=[hourly_usage(hour, sum(rows.values()), [{"id": model, "tokens": total} for model, total in sorted(rows.items())]) for hour, rows in sorted(hours.items())],
            hourly_method="dsh-event-hour",
        )
