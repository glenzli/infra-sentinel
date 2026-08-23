"""Aggregate-only reconstruction of Codex rollout token metadata.

The durable ledger and read-only audit reconstruct local usage from
``last_token_usage`` increments, using cumulative totals only to suppress
unchanged snapshots, inherited lineage prefixes, and stale regressions. They
are intentionally account-agnostic: rollouts removed before observation and
usage from another machine are absent.

No rollout content, task identifiers, paths, or raw JSON records are retained.
The checkpoint contains only daily aggregate counters plus irreversible file,
lineage, parser state, and scoped dedup markers with byte offsets, so later sampling does not
replay the same events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


LEDGER_SCHEMA = "20260824.3"
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
    inherited_snapshots: int = 0
    counter_resets: int = 0
    delta_last_mismatches: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            **self.composition.as_payload(),
            "source_tokens": dict(sorted(self.source_tokens.items())),
            "token_records": self.token_records,
            "duplicate_snapshots": self.duplicate_snapshots,
            "inherited_snapshots": self.inherited_snapshots,
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
            inherited_snapshots=_integer(payload.get("inherited_snapshots")),
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
    inherited: bool = False
    reset: bool = False
    delta_last_mismatch: bool = False


@dataclass(frozen=True)
class LedgerUpdate:
    increments: tuple[LedgerIncrement, ...]
    scanned_bytes: int
    partial: bool


@dataclass(frozen=True)
class RolloutIdentity:
    """Irreversible lineage metadata needed to suppress inherited history."""

    session: str
    parent: str | None


@dataclass
class RolloutParseState:
    """Privacy-minimal state for one Codex rollout JSONL stream."""

    model: str = "unknown"
    source: str = "unknown"
    previous: dict[str, int] | None = None
    session: str | None = None
    parent: str | None = None
    fork_waiting: bool = False
    child_session: str | None = None
    child_session_ms: int | None = None
    replay_session_seen: bool = False
    inherited_baseline: dict[str, int] | None = None
    inherited_reported_total: int | None = None
    task_started_turns: set[str] = field(default_factory=set)
    child_is_user_fork: bool = False
    last_accepted_epoch: float | None = None


@dataclass(frozen=True)
class ParsedTokenEvent:
    epoch: float
    model: str
    source: str
    usage: dict[str, int]
    duplicate: bool = False
    inherited: bool = False
    reset: bool = False
    delta_last_mismatch: bool = False


@dataclass
class CodexRolloutLedger:
    """Durable aggregate ledger that survives rollout deletion after capture."""

    days: dict[str, RolloutAuditDay] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    dedup_keys: set[str] = field(default_factory=set)
    updated_at: str | None = None
    rebuilt_at: str | None = None
    partial: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "days": {day: usage.as_payload() for day, usage in sorted(self.days.items())},
            "files": self.files,
            "dedup_keys": sorted(self.dedup_keys),
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
        dedup_keys_raw = payload.get("dedup_keys")
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
            dedup_keys={
                str(key)
                for key in dedup_keys_raw
                if _valid_fingerprint(key)
            } if isinstance(dedup_keys_raw, list) else set(),
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

    Only session classification, turn boundaries, event timestamps, and
    token-count metadata are parsed. ``last_token_usage`` is the request
    increment; cumulative totals are used only for replay gates, duplicate and
    stale-regression checks, and degraded records without a last-usage value.
    """
    if end_day < start_day:
        raise ValueError("end_day must not precede start_day")
    readable_roots = discover_codex_rollout_roots(roots)
    result = RolloutAudit(start_day.isoformat(), end_day.isoformat(), roots=len(readable_roots))
    candidates: list[tuple[int, Path, RolloutIdentity]] = []
    for root in readable_roots:
        try:
            for path in root.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    result.read_errors += 1
                    continue
                rollout_day = _rollout_day_or_none(root, path)
                if rollout_day is not None and rollout_day > end_day:
                    continue
                candidates.append((stat.st_size, path, _rollout_identity(path)))
        except OSError:
            result.read_errors += 1
    result.candidate_files = len(candidates)
    size_counts: dict[int, int] = {}
    for size, _, _ in candidates:
        size_counts[size] = size_counts.get(size, 0) + 1
    seen_content: set[tuple[int, str]] = set()
    seen_dedup_keys: set[str] = set()
    for size, path, identity in sorted(candidates, key=lambda item: item[1].name):
        if size_counts[size] > 1:
            try:
                content_identity = (size, _file_digest(path))
            except OSError:
                result.read_errors += 1
                continue
            if content_identity in seen_content:
                result.duplicate_files += 1
                continue
            seen_content.add(content_identity)
        try:
            scanned_bytes, has_usage = _audit_rollout_file(
                path, result.days, start_day=start_day, end_day=end_day, timezone=timezone,
                identity=identity,
                seen_dedup_keys=seen_dedup_keys,
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
    candidates: dict[str, tuple[int, int, Path, RolloutIdentity]] = {}
    for root in readable_roots:
        try:
            for path in root.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                marker = _ledger_file_marker(path)
                current = candidates.get(marker)
                candidate = (stat.st_size, stat.st_mtime_ns, path, _rollout_identity(path))
                if current is None or candidate[:2] > current[:2]:
                    candidates[marker] = candidate
        except OSError:
            continue
    remaining = max_scan_bytes
    increments: list[LedgerIncrement] = []
    scanned_bytes = 0
    partial = False
    for marker, (size, modified, path, identity) in sorted(
        candidates.items(), key=lambda item: (item[1][1], item[0]),
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
            identity=identity,
            seen_dedup_keys=ledger.dedup_keys,
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
    identity: RolloutIdentity,
    seen_dedup_keys: set[str],
) -> tuple[int, dict[str, Any], list[LedgerIncrement], bool]:
    state = _parse_state_from_payload(stored, identity)
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
                event = _consume_rollout_record(
                    record, state, seen_dedup_keys=seen_dedup_keys,
                    file_scope=identity.session, timezone=timezone,
                )
                if event is not None:
                    timestamp = datetime.fromtimestamp(event.epoch, tz=timezone)
                    observations.append(LedgerIncrement(
                        timestamp=timestamp.isoformat(timespec="seconds"),
                        epoch=timestamp.timestamp(),
                        day=timestamp.date().isoformat(),
                        model=event.model,
                        source=event.source,
                        usage=event.usage,
                        duplicate=event.duplicate,
                        inherited=event.inherited,
                        reset=event.reset,
                        delta_last_mismatch=event.delta_last_mismatch,
                    ))
    except OSError:
        return offset, _ledger_file_from_payload(stored), [], True
    entry = _parse_state_as_payload(state)
    try:
        more_bytes = path.stat().st_size > position
    except OSError:
        more_bytes = True
    return position, entry, observations, exhausted or (remaining <= 0 and more_bytes)


def _add_ledger_increment(ledger: CodexRolloutLedger, increment: LedgerIncrement) -> None:
    day = ledger.days.setdefault(increment.day, RolloutAuditDay())
    day.token_records += 1
    day.duplicate_snapshots += int(increment.duplicate)
    day.inherited_snapshots += int(increment.inherited)
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
    identity: RolloutIdentity,
    seen_dedup_keys: set[str],
) -> tuple[int, bool]:
    state = RolloutParseState(session=identity.session, parent=identity.parent)
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
            event = _consume_rollout_record(
                record, state, seen_dedup_keys=seen_dedup_keys,
                file_scope=identity.session, timezone=timezone,
            )
            if event is not None:
                event_day = datetime.fromtimestamp(event.epoch, tz=timezone).date()
            else:
                event_day = None
            if event is not None and event_day is not None and start_day <= event_day <= end_day:
                day = days.setdefault(event_day.isoformat(), RolloutAuditDay())
                day.token_records += 1
                day.duplicate_snapshots += int(event.duplicate)
                day.inherited_snapshots += int(event.inherited)
                day.counter_resets += int(event.reset)
                day.delta_last_mismatches += int(event.delta_last_mismatch)
                if event.usage["total_tokens"] > 0:
                    day.composition.add(event.model, event.usage)
                    day.source_tokens[event.source] = day.source_tokens.get(event.source, 0) + event.usage["total_tokens"]
                    has_usage = True
    return scanned_bytes, has_usage


def _consume_rollout_record(
    record: dict[str, Any],
    state: RolloutParseState,
    *,
    seen_dedup_keys: set[str],
    file_scope: str,
    timezone: tzinfo,
) -> ParsedTokenEvent | None:
    """Apply Tokscale-compatible Codex fork and token-count semantics."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    record_type = record.get("type")
    payload_type = payload.get("type")
    is_token_count = record_type == "event_msg" and payload_type == "token_count"

    if state.fork_waiting:
        if record_type == "turn_context" and _fork_turn_starts_child(state, payload.get("turn_id")):
            state.fork_waiting = False
            state.replay_session_seen = False
            state.task_started_turns.clear()
            state.child_is_user_fork = False
            if state.child_session:
                state.session = state.child_session
            state.model = _model_or_unknown(payload.get("model"))
            return None
        if record_type == "event_msg" and payload_type == "task_started":
            turn_id = payload.get("turn_id")
            if _task_starts_child(state, turn_id, payload.get("started_at")):
                state.task_started_turns.add(_opaque_marker("turn", turn_id))
        elif record_type == "session_meta":
            identifier = payload.get("id") or payload.get("session_id")
            if identifier and _opaque_marker("session", identifier) != state.child_session:
                state.replay_session_seen = True
        elif is_token_count:
            total = _usage_field(payload, "total_token_usage")
            if total is not None:
                state.previous = total
                state.inherited_baseline = total
                state.inherited_reported_total = total.get("total_tokens")
                inherited_timestamp = _event_timestamp(record.get("timestamp"), timezone)
                if inherited_timestamp is not None:
                    return ParsedTokenEvent(
                        inherited_timestamp.timestamp(), state.model, state.source,
                        _zero_usage(), inherited=True,
                    )
        return None

    if record_type == "session_meta":
        identifier = payload.get("id") or payload.get("session_id")
        identifier_marker = _opaque_marker("session", identifier) if identifier else None
        parent_identifier = _forked_from_identifier(payload)
        repeated_active_child = (
            identifier_marker is not None
            and identifier_marker == state.child_session
            and not state.fork_waiting
        )
        if identifier_marker:
            state.session = identifier_marker
        if parent_identifier:
            state.parent = _opaque_marker("session", parent_identifier)
            state.child_session = identifier_marker
            state.child_session_ms = _uuid_v7_epoch_ms(identifier)
            if not repeated_active_child:
                state.fork_waiting = True
                state.replay_session_seen = False
                state.inherited_baseline = None
                state.inherited_reported_total = None
                state.task_started_turns.clear()
                state.child_is_user_fork = payload.get("thread_source") == "user"
        if state.parent is None:
            state.source = _rollout_source(payload)
        else:
            state.source = "subagent"
        return None

    event_timestamp = _event_timestamp(record.get("timestamp"), timezone)
    event_epoch = event_timestamp.timestamp() if event_timestamp is not None else None
    if record_type == "turn_context":
        state.model = _model_or_unknown(payload.get("model"))
        state.last_accepted_epoch = event_epoch
        return None
    if record_type == "event_msg" and payload_type == "user_message":
        message = payload.get("message")
        if isinstance(message, str) and message.strip() and not message.lstrip().startswith("<"):
            state.last_accepted_epoch = event_epoch
        return None
    if not is_token_count or event_epoch is None:
        return None

    total = _usage_field(payload, "total_token_usage")
    last = _usage_field(payload, "last_token_usage")
    if total is None and last is None:
        return None

    if _is_inherited_baseline(state, total):
        return ParsedTokenEvent(
            event_epoch, state.model, state.source, _zero_usage(), inherited=True,
        )
    state.inherited_baseline = None
    state.inherited_reported_total = None

    previous = state.previous
    duplicate = False
    reset = False
    next_previous = previous
    if total is not None and last is not None and previous is not None:
        if _same_counter(total, previous):
            usage = _zero_usage()
            duplicate = True
        elif _counter_regressed(total, previous) and _looks_like_stale_regression(total, previous, last):
            usage = _zero_usage()
            duplicate = True
        else:
            usage = last
            next_previous = total
            reset = _counter_regressed(total, previous)
    elif total is not None and last is not None:
        usage = last
        next_previous = total
    elif total is not None and previous is not None:
        if _same_counter(total, previous):
            usage = _zero_usage()
            duplicate = True
        elif not _counter_regressed(total, previous):
            usage = _usage_delta(total, previous)
            next_previous = total
        else:
            state.previous = total
            return ParsedTokenEvent(event_epoch, state.model, state.source, _zero_usage(), reset=True)
    elif total is not None:
        usage = total
        next_previous = total
    elif last is not None and previous is not None:
        usage = last
        next_previous = _usage_add(previous, last)
    else:
        usage = last or _zero_usage()

    if usage["total_tokens"] <= 0:
        return ParsedTokenEvent(
            event_epoch, state.model, state.source, _zero_usage(),
            duplicate=duplicate, reset=reset,
        )
    state.previous = next_previous
    effective_epoch = state.last_accepted_epoch if state.last_accepted_epoch is not None else event_epoch
    if state.last_accepted_epoch is None or event_epoch > state.last_accepted_epoch:
        state.last_accepted_epoch = event_epoch

    scope = state.parent or state.session or file_scope
    dedup_key = _scoped_token_dedup_key(scope, state.model, total, usage, event_epoch)
    inherited = dedup_key in seen_dedup_keys
    seen_dedup_keys.add(dedup_key)
    if inherited:
        usage = _zero_usage()
    return ParsedTokenEvent(
        effective_epoch, state.model, state.source, usage,
        duplicate=duplicate, inherited=inherited, reset=reset,
    )


def _forked_from_identifier(payload: dict[str, Any]) -> object | None:
    direct = payload.get("forked_from_id") or payload.get("parent_thread_id")
    if direct:
        return direct
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return spawn.get("parent_thread_id") if isinstance(spawn, dict) else None


def _fork_turn_starts_child(state: RolloutParseState, turn_id: object) -> bool:
    if not state.replay_session_seen or not state.child_session:
        return True
    turn_ms = _uuid_v7_epoch_ms(turn_id)
    if turn_ms is None or state.child_session_ms is None:
        return state.child_is_user_fork or _opaque_marker("turn", turn_id) in state.task_started_turns
    if turn_ms != state.child_session_ms:
        return turn_ms > state.child_session_ms
    return state.child_is_user_fork or _opaque_marker("turn", turn_id) in state.task_started_turns


def _task_starts_child(state: RolloutParseState, turn_id: object, started_at: object) -> bool:
    if not turn_id:
        return False
    if state.child_session_ms is None:
        return True
    turn_ms = _uuid_v7_epoch_ms(turn_id)
    if turn_ms is not None:
        return turn_ms >= state.child_session_ms
    try:
        started_at_seconds = int(started_at)
    except (TypeError, ValueError):
        return False
    return started_at_seconds >= state.child_session_ms // 1000


def _uuid_v7_epoch_ms(identifier: object) -> int | None:
    if not isinstance(identifier, str):
        return None
    parts = identifier.split("-")
    if (
        len(parts) != 5 or [len(part) for part in parts] != [8, 4, 4, 4, 12]
        or not parts[2].lower().startswith("7")
        or any(not all(character in "0123456789abcdefABCDEF" for character in part) for part in parts)
    ):
        return None
    try:
        return int((parts[0] + parts[1]).lower(), 16)
    except ValueError:
        return None


def _is_inherited_baseline(state: RolloutParseState, total: dict[str, int] | None) -> bool:
    if total is None:
        return False
    if (
        state.inherited_reported_total is not None
        and total["total_tokens"] <= state.inherited_reported_total
    ):
        return True
    baseline = state.inherited_baseline
    return baseline is not None and all(total[field] <= baseline[field] for field in _counter_fields())


def _counter_fields() -> tuple[str, ...]:
    return "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"


def _same_counter(left: dict[str, int], right: dict[str, int]) -> bool:
    return all(left[field] == right[field] for field in _counter_fields())


def _counter_regressed(current: dict[str, int], previous: dict[str, int]) -> bool:
    return any(current[field] < previous[field] for field in _counter_fields())


def _counter_magnitude(usage: dict[str, int]) -> int:
    return sum(usage[field] for field in _counter_fields())


def _looks_like_stale_regression(
    current: dict[str, int], previous: dict[str, int], last: dict[str, int],
) -> bool:
    current_total = _counter_magnitude(current)
    previous_total = _counter_magnitude(previous)
    last_total = _counter_magnitude(last)
    if min(current_total, previous_total, last_total) <= 0:
        return False
    return current_total * 100 >= previous_total * 98 or current_total + last_total * 2 >= previous_total


def _usage_delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    result = {field: max(0, current[field] - previous[field]) for field in _usage_fields()}
    result["total_tokens"] = max(0, current["total_tokens"] - previous["total_tokens"])
    return result


def _usage_add(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {field: left[field] + right[field] for field in _usage_fields()}


def _scoped_token_dedup_key(
    scope: str,
    model: str,
    total: dict[str, int] | None,
    usage: dict[str, int],
    event_epoch: float,
) -> str:
    canonical = [scope, model]
    if total is not None:
        canonical.extend(total[field] for field in _counter_fields())
    else:
        canonical.extend([event_epoch, *(usage[field] for field in _usage_fields())])
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode("utf-8")).hexdigest()


def _record(line: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _usage_field(payload: dict[str, Any], key: str) -> dict[str, int] | None:
    info = payload.get("info")
    raw = info.get(key) if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        return None
    input_tokens = _integer(raw.get("input_tokens"))
    output_tokens = _integer(raw.get("output_tokens"))
    values = {
        "input_tokens": input_tokens,
        "cached_input_tokens": max(
            _integer(raw.get("cached_input_tokens")),
            _integer(raw.get("cache_read_input_tokens")),
        ),
        "cache_write_input_tokens": _integer(raw.get("cache_write_input_tokens")),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": _integer(raw.get("reasoning_output_tokens")),
        "total_tokens": _integer(raw.get("total_tokens")) or input_tokens + output_tokens,
    }
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


def _event_timestamp(value: object, timezone: tzinfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)
    except ValueError:
        return None


def _rollout_identity(path: Path) -> RolloutIdentity:
    """Read only opaque lineage identifiers from the rollout header."""
    fallback = _opaque_marker("file", path.name)
    try:
        with path.open("rb") as handle:
            for _ in range(32):
                line = handle.readline(MAX_LINE_BYTES + 1)
                if not line or len(line) > MAX_LINE_BYTES:
                    break
                record = _record(line)
                payload = record.get("payload") if record is not None else None
                if record is None or record.get("type") != "session_meta" or not isinstance(payload, dict):
                    continue
                identifier = payload.get("id") or payload.get("session_id")
                parent = _forked_from_identifier(payload)
                return RolloutIdentity(
                    _opaque_marker("session", identifier) if identifier else fallback,
                    _opaque_marker("session", parent) if parent else None,
                )
    except OSError:
        pass
    return RolloutIdentity(fallback, None)


def _opaque_marker(namespace: str, value: object) -> str:
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(value).encode("utf-8"))
    return digest.hexdigest()


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
    if not isinstance(raw, dict):
        return None
    values = {field: _integer(raw.get(field)) for field in _usage_fields()}
    return values if values["total_tokens"] > 0 else None


def _parse_state_from_payload(raw: object, identity: RolloutIdentity) -> RolloutParseState:
    payload = raw if isinstance(raw, dict) else {}
    task_turns = payload.get("task_started_turns")
    return RolloutParseState(
        model=_model_or_unknown(payload.get("model")),
        source=_source_or_unknown(payload.get("source")),
        previous=_usage_from_payload(payload.get("previous")),
        session=str(payload.get("session")) if _valid_marker(payload.get("session")) else identity.session,
        parent=str(payload.get("parent")) if _valid_marker(payload.get("parent")) else identity.parent,
        fork_waiting=bool(payload.get("fork_waiting")),
        child_session=str(payload.get("child_session")) if _valid_marker(payload.get("child_session")) else None,
        child_session_ms=_positive_integer_or_none(payload.get("child_session_ms")),
        replay_session_seen=bool(payload.get("replay_session_seen")),
        inherited_baseline=_usage_from_payload(payload.get("inherited_baseline")),
        inherited_reported_total=_positive_integer_or_none(payload.get("inherited_reported_total")),
        task_started_turns={
            str(marker) for marker in task_turns if _valid_marker(marker)
        } if isinstance(task_turns, list) else set(),
        child_is_user_fork=bool(payload.get("child_is_user_fork")),
        last_accepted_epoch=_positive_float_or_none(payload.get("last_accepted_epoch")),
    )


def _parse_state_as_payload(state: RolloutParseState) -> dict[str, Any]:
    return {
        "model": state.model,
        "source": state.source,
        "previous": state.previous,
        "session": state.session,
        "parent": state.parent,
        "fork_waiting": state.fork_waiting,
        "child_session": state.child_session,
        "child_session_ms": state.child_session_ms,
        "replay_session_seen": state.replay_session_seen,
        "inherited_baseline": state.inherited_baseline,
        "inherited_reported_total": state.inherited_reported_total,
        "task_started_turns": sorted(state.task_started_turns),
        "child_is_user_fork": state.child_is_user_fork,
        "last_accepted_epoch": state.last_accepted_epoch,
    }


def _ledger_file_from_payload(raw: object) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    state = _parse_state_as_payload(_parse_state_from_payload(payload, RolloutIdentity(
        str(payload.get("session")) if _valid_marker(payload.get("session")) else _opaque_marker("missing", "session"),
        str(payload.get("parent")) if _valid_marker(payload.get("parent")) else None,
    )))
    return {
        "offset": _integer(payload.get("offset")),
        **state,
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


def _positive_integer_or_none(value: object) -> int | None:
    number = _integer(value)
    return number if number > 0 else None


def _positive_float_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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


def _valid_fingerprint(value: object) -> bool:
    return _valid_marker(value)
