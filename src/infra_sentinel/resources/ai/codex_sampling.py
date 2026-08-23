"""Aggregate-only reconstruction of Codex rollout token metadata.

The durable ledger and read-only audit reconstruct local usage from
``total_token_usage`` deltas, suppressing unchanged snapshots and treating a
counter decrease as a new generation. They are intentionally account-agnostic:
rollouts removed before observation and usage from another machine are absent.

No rollout content, task identifiers, paths, or raw JSON records are retained.
The checkpoint contains only daily aggregate counters plus irreversible file
markers and byte offsets, so later sampling does not replay the same events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, tzinfo
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


LEDGER_SCHEMA = "20260824.1"
MAX_LEDGER_SCAN_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024
MAX_SAMPLED_MODELS = 128


@dataclass
class TokenComposition:
    events: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    models: dict[str, int] = field(default_factory=dict)
    model_compositions: dict[str, "TokenComposition"] = field(default_factory=dict)

    def add(self, model: str, usage: dict[str, int]) -> None:
        self.events += 1
        self.input_tokens += usage["input_tokens"]
        self.cached_input_tokens += usage["cached_input_tokens"]
        self.cache_write_input_tokens += usage["cache_write_input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.reasoning_output_tokens += usage["reasoning_output_tokens"]
        self.total_tokens += usage["total_tokens"]
        identifier = model if model in self.models or len(self.models) < MAX_SAMPLED_MODELS else "other"
        self.models[identifier] = self.models.get(identifier, 0) + usage["total_tokens"]
        composition = self.model_compositions.setdefault(identifier, TokenComposition())
        composition.add_aggregate(usage)

    def add_aggregate(self, usage: dict[str, int]) -> None:
        """Accumulate one model's usage without creating nested model maps."""
        self.events += 1
        self.input_tokens += usage["input_tokens"]
        self.cached_input_tokens += usage["cached_input_tokens"]
        self.cache_write_input_tokens += usage["cache_write_input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.reasoning_output_tokens += usage["reasoning_output_tokens"]
        self.total_tokens += usage["total_tokens"]

    def merge(self, other: "TokenComposition") -> None:
        """Merge another aggregate without inventing token events."""
        self.events += other.events
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_write_input_tokens += other.cache_write_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        self.total_tokens += other.total_tokens
        for identifier, tokens in other.models.items():
            accepted = identifier if identifier in self.models or len(self.models) < MAX_SAMPLED_MODELS else "other"
            self.models[accepted] = self.models.get(accepted, 0) + tokens
        for identifier, composition in other.model_compositions.items():
            accepted = identifier if identifier in self.model_compositions or len(self.model_compositions) < MAX_SAMPLED_MODELS else "other"
            self.model_compositions.setdefault(accepted, TokenComposition()).merge(composition)

    def as_payload(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "models": dict(sorted(self.models.items())),
            "model_compositions": {
                identifier: composition.as_model_payload()
                for identifier, composition in sorted(self.model_compositions.items())
            },
        }

    def as_model_payload(self) -> dict[str, int]:
        return {
            "events": self.events,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "TokenComposition":
        payload = raw if isinstance(raw, dict) else {}
        models_raw = payload.get("models")
        models = {
            str(identifier): _integer(tokens)
            for identifier, tokens in models_raw.items()
            if _valid_model(identifier)
        } if isinstance(models_raw, dict) else {}
        model_compositions_raw = payload.get("model_compositions")
        model_compositions = {
            str(identifier): cls.from_model_payload(composition)
            for identifier, composition in model_compositions_raw.items()
            if _valid_model(identifier)
        } if isinstance(model_compositions_raw, dict) else {}
        return cls(
            events=_integer(payload.get("events")),
            input_tokens=_integer(payload.get("input_tokens")),
            cached_input_tokens=_integer(payload.get("cached_input_tokens")),
            cache_write_input_tokens=_integer(payload.get("cache_write_input_tokens")),
            output_tokens=_integer(payload.get("output_tokens")),
            reasoning_output_tokens=_integer(payload.get("reasoning_output_tokens")),
            total_tokens=_integer(payload.get("total_tokens")),
            models=models,
            model_compositions=model_compositions,
        )

    @classmethod
    def from_model_payload(cls, raw: object) -> "TokenComposition":
        payload = raw if isinstance(raw, dict) else {}
        return cls(
            events=_integer(payload.get("events")),
            input_tokens=_integer(payload.get("input_tokens")),
            cached_input_tokens=_integer(payload.get("cached_input_tokens")),
            cache_write_input_tokens=_integer(payload.get("cache_write_input_tokens")),
            output_tokens=_integer(payload.get("output_tokens")),
            reasoning_output_tokens=_integer(payload.get("reasoning_output_tokens")),
            total_tokens=_integer(payload.get("total_tokens")),
        )


@dataclass
class RolloutAuditDay:
    """One local calendar day's reconstructed, aggregate-only usage."""

    composition: TokenComposition = field(default_factory=TokenComposition)
    source_tokens: dict[str, int] = field(default_factory=dict)
    token_records: int = 0
    duplicate_snapshots: int = 0
    counter_resets: int = 0
    delta_last_mismatches: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            **self.composition.as_payload(),
            "source_tokens": dict(sorted(self.source_tokens.items())),
            "token_records": self.token_records,
            "duplicate_snapshots": self.duplicate_snapshots,
            "counter_resets": self.counter_resets,
            "delta_last_mismatches": self.delta_last_mismatches,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "RolloutAuditDay":
        payload = raw if isinstance(raw, dict) else {}
        return cls(
            composition=TokenComposition.from_payload(payload),
            source_tokens={
                str(source)[:64]: _integer(tokens)
                for source, tokens in payload.get("source_tokens", {}).items()
                if isinstance(source, str) and source
            } if isinstance(payload.get("source_tokens"), dict) else {},
            token_records=_integer(payload.get("token_records")),
            duplicate_snapshots=_integer(payload.get("duplicate_snapshots")),
            counter_resets=_integer(payload.get("counter_resets")),
            delta_last_mismatches=_integer(payload.get("delta_last_mismatches")),
        )


@dataclass
class RolloutAudit:
    """Coverage and daily totals from one read-only rollout reconstruction."""

    start_day: str
    end_day: str
    days: dict[str, RolloutAuditDay] = field(default_factory=dict)
    roots: int = 0
    candidate_files: int = 0
    scanned_files: int = 0
    duplicate_files: int = 0
    files_with_usage: int = 0
    bytes_scanned: int = 0
    read_errors: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "scope": "local-visible-codex-jsonl",
            "start_day": self.start_day,
            "end_day": self.end_day,
            "coverage": {
                "roots": self.roots,
                "candidate_files": self.candidate_files,
                "scanned_files": self.scanned_files,
                "duplicate_files": self.duplicate_files,
                "files_with_usage": self.files_with_usage,
                "bytes_scanned": self.bytes_scanned,
                "read_errors": self.read_errors,
            },
            "days": {day: usage.as_payload() for day, usage in sorted(self.days.items())},
        }


@dataclass(frozen=True)
class LedgerIncrement:
    timestamp: str
    epoch: float
    day: str
    model: str
    source: str
    usage: dict[str, int]
    duplicate: bool = False
    reset: bool = False
    delta_last_mismatch: bool = False


@dataclass(frozen=True)
class LedgerUpdate:
    increments: tuple[LedgerIncrement, ...]
    scanned_bytes: int
    partial: bool


@dataclass
class CodexRolloutLedger:
    """Durable aggregate ledger that survives rollout deletion after capture."""

    days: dict[str, RolloutAuditDay] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at: str | None = None
    rebuilt_at: str | None = None
    partial: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "days": {day: usage.as_payload() for day, usage in sorted(self.days.items())},
            "files": self.files,
            "updated_at": self.updated_at,
            "rebuilt_at": self.rebuilt_at,
            "partial": self.partial,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "CodexRolloutLedger":
        payload = raw if isinstance(raw, dict) else {}
        if payload.get("schema") != LEDGER_SCHEMA:
            return cls()
        days_raw = payload.get("days")
        files_raw = payload.get("files")
        return cls(
            days={
                str(day): RolloutAuditDay.from_payload(usage)
                for day, usage in days_raw.items()
                if _valid_day(day)
            } if isinstance(days_raw, dict) else {},
            files={
                str(marker): _ledger_file_from_payload(entry)
                for marker, entry in files_raw.items()
                if _valid_marker(marker) and isinstance(entry, dict)
            } if isinstance(files_raw, dict) else {},
            updated_at=str(payload.get("updated_at")) if payload.get("updated_at") else None,
            rebuilt_at=str(payload.get("rebuilt_at")) if payload.get("rebuilt_at") else None,
            partial=bool(payload.get("partial")),
        )

    def cumulative(self) -> TokenComposition:
        aggregate = TokenComposition()
        for usage in self.days.values():
            aggregate.merge(usage.composition)
        return aggregate


def discover_codex_rollout_roots(preferred: Iterable[Path] | None = None) -> tuple[Path, ...]:
    """Return readable live and archived rollout roots without combining copies."""
    candidates = tuple(preferred) if preferred is not None else (
        Path.home() / ".codex" / "sessions",
        Path.home() / ".codex" / "archived_sessions",
    )
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            roots.append(resolved)
            seen.add(resolved)
    return tuple(roots)


def audit_codex_rollouts(
    roots: Iterable[Path],
    *,
    start_day: date,
    end_day: date,
    timezone: tzinfo,
) -> RolloutAudit:
    """Reconstruct local daily usage from visible rollout cumulative counters.

    Only session classification, ``turn_context.model``, event timestamps, and
    token-count metadata are parsed. A first observation or counter reset uses
    ``last_token_usage``; later increases use the cumulative delta. This keeps
    inherited fork baselines out while recovering gaps between visible events.
    Unchanged cumulative snapshots contribute zero.
    """
    if end_day < start_day:
        raise ValueError("end_day must not precede start_day")
    readable_roots = discover_codex_rollout_roots(roots)
    result = RolloutAudit(start_day.isoformat(), end_day.isoformat(), roots=len(readable_roots))
    start_epoch = datetime.combine(start_day, datetime_time.min, tzinfo=timezone).timestamp()
    candidates: list[tuple[int, Path]] = []
    for root in readable_roots:
        try:
            for path in root.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    result.read_errors += 1
                    continue
                rollout_day = _rollout_day_or_none(root, path)
                if stat.st_mtime < start_epoch or (rollout_day is not None and rollout_day > end_day):
                    continue
                candidates.append((stat.st_size, path))
        except OSError:
            result.read_errors += 1
    result.candidate_files = len(candidates)
    size_counts: dict[int, int] = {}
    for size, _ in candidates:
        size_counts[size] = size_counts.get(size, 0) + 1
    seen_content: set[tuple[int, str]] = set()
    for size, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        if size_counts[size] > 1:
            try:
                identity = (size, _file_digest(path))
            except OSError:
                result.read_errors += 1
                continue
            if identity in seen_content:
                result.duplicate_files += 1
                continue
            seen_content.add(identity)
        try:
            scanned_bytes, has_usage = _audit_rollout_file(
                path, result.days, start_day=start_day, end_day=end_day, timezone=timezone,
            )
        except OSError:
            result.read_errors += 1
            continue
        result.scanned_files += 1
        result.bytes_scanned += scanned_bytes
        if has_usage:
            result.files_with_usage += 1
    return result


def load_codex_rollout_ledger(path: Path | None) -> CodexRolloutLedger:
    if path is None:
        return CodexRolloutLedger()
    try:
        return CodexRolloutLedger.from_payload(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return CodexRolloutLedger()


def save_codex_rollout_ledger(path: Path | None, ledger: CodexRolloutLedger) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger.as_payload(), separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def rebuild_codex_rollout_ledger(
    roots: Iterable[Path],
    *,
    timezone: tzinfo,
    now: datetime,
) -> CodexRolloutLedger:
    """Rebuild all still-visible local history without emitting live increments."""
    ledger = CodexRolloutLedger()
    update_codex_rollout_ledger(roots, ledger, timezone=timezone, now=now, max_scan_bytes=None)
    ledger.rebuilt_at = now.astimezone().isoformat(timespec="seconds")
    ledger.partial = False
    return ledger


def update_codex_rollout_ledger(
    roots: Iterable[Path],
    ledger: CodexRolloutLedger,
    *,
    timezone: tzinfo,
    now: datetime,
    max_scan_bytes: int | None = MAX_LEDGER_SCAN_BYTES,
) -> LedgerUpdate:
    """Ingest append-only rollout tails into a durable aggregate ledger."""
    readable_roots = discover_codex_rollout_roots(roots)
    candidates: dict[str, tuple[int, int, Path]] = {}
    for root in readable_roots:
        try:
            for path in root.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                marker = _ledger_file_marker(path)
                current = candidates.get(marker)
                candidate = (stat.st_size, stat.st_mtime_ns, path)
                if current is None or candidate[:2] > current[:2]:
                    candidates[marker] = candidate
        except OSError:
            continue
    remaining = max_scan_bytes
    increments: list[LedgerIncrement] = []
    scanned_bytes = 0
    partial = False
    for marker, (size, modified, path) in sorted(
        candidates.items(), key=lambda item: (item[1][1], item[1][0], item[0]), reverse=True,
    ):
        entry = ledger.files.get(marker, {})
        offset = _integer(entry.get("offset"))
        if size < offset:
            # A moved archived copy can be shorter than the already observed
            # live file. Never replay it or roll the durable offset backwards.
            continue
        if size == offset:
            continue
        if remaining is not None and remaining <= 0:
            partial = True
            break
        budget = size - offset if remaining is None else remaining
        consumed, next_entry, file_increments, exhausted = _ingest_ledger_file_tail(
            path, offset, budget, entry, timezone=timezone,
        )
        ledger.files[marker] = {**next_entry, "offset": consumed, "modified": modified, "incomplete": exhausted}
        used = max(0, consumed - offset)
        scanned_bytes += used
        if remaining is not None:
            remaining -= used
        for increment in file_increments:
            _add_ledger_increment(ledger, increment)
            if increment.usage["total_tokens"] > 0:
                increments.append(increment)
        partial = partial or exhausted
    if scanned_bytes:
        ledger.updated_at = now.astimezone().isoformat(timespec="seconds")
    ledger.partial = partial or any(bool(entry.get("incomplete")) for entry in ledger.files.values())
    return LedgerUpdate(tuple(increments), scanned_bytes, ledger.partial)


def _ingest_ledger_file_tail(
    path: Path,
    offset: int,
    remaining: int,
    stored: dict[str, Any],
    *,
    timezone: tzinfo,
) -> tuple[int, dict[str, Any], list[LedgerIncrement], bool]:
    model = _model_or_unknown(stored.get("model"))
    source = _source_or_unknown(stored.get("source"))
    previous = _usage_from_payload(stored.get("previous"))
    observations: list[LedgerIncrement] = []
    exhausted = False
    position = offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while remaining > 0:
                line_start = position
                line = handle.readline(min(MAX_LINE_BYTES + 1, remaining + 1))
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) > MAX_LINE_BYTES:
                        position += len(line)
                        remaining -= len(line)
                        while remaining > 0:
                            suffix = handle.readline(min(MAX_LINE_BYTES + 1, remaining + 1))
                            if not suffix:
                                break
                            position += len(suffix)
                            remaining -= len(suffix)
                            if suffix.endswith(b"\n"):
                                break
                        exhausted = True
                        continue
                    position = line_start
                    exhausted = True
                    break
                position += len(line)
                remaining -= len(line)
                if len(line) > MAX_LINE_BYTES:
                    exhausted = True
                    continue
                record = _record(line)
                if record is None:
                    continue
                payload = record.get("payload")
                record_type = record.get("type")
                if record_type == "session_meta" and isinstance(payload, dict):
                    source = _rollout_source(payload)
                    continue
                if record_type == "turn_context" and isinstance(payload, dict):
                    model = _model_or_unknown(payload.get("model"))
                    continue
                if record_type != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                total = _usage_field(payload, "total_token_usage")
                if total is None:
                    continue
                last = _usage_field(payload, "last_token_usage")
                current_total = total["total_tokens"]
                previous_total = previous["total_tokens"] if previous is not None else None
                duplicate = previous_total is not None and current_total == previous_total
                reset = previous_total is not None and current_total < previous_total
                if previous is None or reset:
                    delta = last or total
                elif duplicate:
                    delta = _zero_usage()
                else:
                    delta = {field: max(0, total[field] - previous[field]) for field in _usage_fields()}
                timestamp = _event_timestamp(record.get("timestamp"), timezone)
                if timestamp is not None:
                    observations.append(LedgerIncrement(
                        timestamp=timestamp.isoformat(timespec="seconds"),
                        epoch=timestamp.timestamp(),
                        day=timestamp.date().isoformat(),
                        model=model,
                        source=source,
                        usage=delta,
                        duplicate=duplicate,
                        reset=reset,
                        delta_last_mismatch=(
                            last is not None and not duplicate and delta["total_tokens"] != last["total_tokens"]
                        ),
                    ))
                previous = total
    except OSError:
        return offset, _ledger_file_from_payload(stored), [], True
    entry = {
        "model": model,
        "source": source,
        "previous": previous or _zero_usage(),
    }
    try:
        more_bytes = path.stat().st_size > position
    except OSError:
        more_bytes = True
    return position, entry, observations, exhausted or (remaining <= 0 and more_bytes)


def _add_ledger_increment(ledger: CodexRolloutLedger, increment: LedgerIncrement) -> None:
    day = ledger.days.setdefault(increment.day, RolloutAuditDay())
    day.token_records += 1
    day.duplicate_snapshots += int(increment.duplicate)
    day.counter_resets += int(increment.reset)
    day.delta_last_mismatches += int(increment.delta_last_mismatch)
    if increment.usage["total_tokens"] <= 0:
        return
    day.composition.add(increment.model, increment.usage)
    day.source_tokens[increment.source] = day.source_tokens.get(increment.source, 0) + increment.usage["total_tokens"]


def _audit_rollout_file(
    path: Path,
    days: dict[str, RolloutAuditDay],
    *,
    start_day: date,
    end_day: date,
    timezone: tzinfo,
) -> tuple[int, bool]:
    previous: dict[str, int] | None = None
    model = "unknown"
    source = "unknown"
    scanned_bytes = 0
    has_usage = False
    with path.open("rb") as handle:
        for line in handle:
            scanned_bytes += len(line)
            if len(line) > MAX_LINE_BYTES:
                continue
            record = _record(line)
            if record is None:
                continue
            payload = record.get("payload")
            record_type = record.get("type")
            if record_type == "session_meta" and isinstance(payload, dict):
                source = _rollout_source(payload)
                continue
            if record_type == "turn_context" and isinstance(payload, dict):
                model = _model_or_unknown(payload.get("model"))
                continue
            if record_type != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            total = _usage_field(payload, "total_token_usage")
            if total is None:
                continue
            last = _usage_field(payload, "last_token_usage")
            current_total = total["total_tokens"]
            previous_total = previous["total_tokens"] if previous is not None else None
            duplicate = previous_total is not None and current_total == previous_total
            reset = previous_total is not None and current_total < previous_total
            if previous is None or reset:
                delta = last or total
            elif duplicate:
                delta = _zero_usage()
            else:
                delta = {field: max(0, total[field] - previous[field]) for field in _usage_fields()}
            event_day = _event_date(record.get("timestamp"), timezone)
            if event_day is not None and start_day <= event_day <= end_day:
                day = days.setdefault(event_day.isoformat(), RolloutAuditDay())
                day.token_records += 1
                day.duplicate_snapshots += int(duplicate)
                day.counter_resets += int(reset)
                if last is not None and not duplicate and delta["total_tokens"] != last["total_tokens"]:
                    day.delta_last_mismatches += 1
                if delta["total_tokens"] > 0:
                    day.composition.add(model, delta)
                    day.source_tokens[source] = day.source_tokens.get(source, 0) + delta["total_tokens"]
                    has_usage = True
            previous = total
    return scanned_bytes, has_usage


def _record(line: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _usage(payload: dict[str, Any]) -> dict[str, int] | None:
    return _usage_field(payload, "last_token_usage")


def _usage_field(payload: dict[str, Any], key: str) -> dict[str, int] | None:
    info = payload.get("info")
    raw = info.get(key) if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        return None
    fields = _usage_fields()
    if any(field not in raw for field in fields):
        return None
    values = {field: _integer(raw.get(field)) for field in fields}
    if values["total_tokens"] <= 0:
        return None
    return values


def _usage_fields() -> tuple[str, ...]:
    return (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens",
    )


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in _usage_fields()}


def _event_date(value: object, timezone: tzinfo) -> date | None:
    timestamp = _event_timestamp(value, timezone)
    return timestamp.date() if timestamp is not None else None


def _event_timestamp(value: object, timezone: tzinfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)
    except ValueError:
        return None


def _rollout_source(payload: dict[str, Any]) -> str:
    source = payload.get("thread_source")
    if isinstance(source, str) and source.strip():
        return source.strip()[:64]
    if payload.get("parent_thread_id") or payload.get("forked_from_id"):
        return "subagent"
    return "user"


def _source_or_unknown(value: object) -> str:
    return str(value).strip()[:64] if isinstance(value, str) and value.strip() else "unknown"


def _usage_from_payload(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict) or any(field not in raw for field in _usage_fields()):
        return None
    values = {field: _integer(raw.get(field)) for field in _usage_fields()}
    return values if values["total_tokens"] > 0 else None


def _ledger_file_from_payload(raw: object) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    return {
        "offset": _integer(payload.get("offset")),
        "model": _model_or_unknown(payload.get("model")),
        "source": _source_or_unknown(payload.get("source")),
        "previous": _usage_from_payload(payload.get("previous")) or _zero_usage(),
        "modified": _integer(payload.get("modified")),
        "incomplete": bool(payload.get("incomplete")),
    }


def _ledger_file_marker(path: Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _rollout_day_or_none(root: Path, path: Path) -> date | None:
    try:
        year, month, day, *_ = path.relative_to(root).parts
        return date(int(year), int(month), int(day))
    except (ValueError, TypeError):
        name = path.name
        if name.startswith("rollout-") and len(name) >= 18:
            try:
                return date.fromisoformat(name[8:18])
            except ValueError:
                pass
        return None


def _integer(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def _model_or_unknown(value: object) -> str:
    return str(value).strip()[:128] if _valid_model(value) else "unknown"


def _valid_model(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def _valid_day(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_marker(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
