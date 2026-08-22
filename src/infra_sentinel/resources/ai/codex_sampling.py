"""Bounded, aggregate-only sampling of visible Codex rollout token metadata.

Codex's SQLite state is the durable source for Sentinel's main local workload
counter.  Rollout JSONL files are intentionally *not* a replacement: users can
remove them and their shape is an implementation detail.  This module reads a
small, validated subset only to describe the observed input/output/cache mix.

No rollout content, task identifiers, paths, or raw JSON records are retained.
The checkpoint contains only daily aggregate counters plus irreversible file
markers and byte offsets, so later sampling does not replay the same events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


SAMPLE_SCHEMA = "20260821.2"
MAX_SESSION_FILES = 256
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024
RETAIN_DAYS = 8
MAX_SAMPLED_MODELS = 32


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
class JsonlSampleState:
    days: dict[str, TokenComposition] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    partial: bool = False
    updated_at: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": SAMPLE_SCHEMA,
            "days": {day: sample.as_payload() for day, sample in sorted(self.days.items())},
            "files": self.files,
            "partial": self.partial,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "JsonlSampleState":
        payload = raw if isinstance(raw, dict) else {}
        if payload.get("schema") != SAMPLE_SCHEMA:
            return cls()
        days_raw = payload.get("days")
        days = {
            str(day): TokenComposition.from_payload(sample)
            for day, sample in days_raw.items()
            if _valid_day(day)
        } if isinstance(days_raw, dict) else {}
        files_raw = payload.get("files")
        files = {
            str(marker): {
                "offset": _integer(entry.get("offset")),
                "model": _model_or_unknown(entry.get("model")),
                "day": str(entry.get("day")) if _valid_day(entry.get("day")) else None,
                "incomplete": bool(entry.get("incomplete")),
            }
            for marker, entry in files_raw.items()
            if _valid_marker(marker) and isinstance(entry, dict)
        } if isinstance(files_raw, dict) else {}
        return cls(
            days=days,
            files=files,
            partial=bool(payload.get("partial")),
            updated_at=str(payload.get("updated_at")) if payload.get("updated_at") else None,
        )


def discover_codex_session_root(preferred: Path | None = None) -> Path | None:
    """Find Codex's visible session directory without inspecting its contents."""
    candidates = (preferred, Path.home() / ".codex" / "sessions")
    return next((candidate for candidate in candidates if candidate and candidate.is_dir()), None)


def load_jsonl_sample_state(path: Path | None) -> JsonlSampleState:
    if path is None:
        return JsonlSampleState()
    try:
        return JsonlSampleState.from_payload(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return JsonlSampleState()


def save_jsonl_sample_state(path: Path | None, state: JsonlSampleState) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state.as_payload(), separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def sample_visible_rollouts(
    root: Path | None,
    state: JsonlSampleState,
    *,
    now: datetime,
) -> JsonlSampleState:
    """Ingest bounded new token-count records from visible rollout files.

    The parser intentionally accepts only ``turn_context.model`` and
    ``event_msg/token_count/info/last_token_usage``.  Any schema drift,
    malformed line, oversized record, or truncated file is ignored rather than
    guessed at.  Stored markers are SHA-256 digests of relative file names;
    original paths do not enter the checkpoint or Projection.
    """
    if root is None or not root.is_dir():
        return state
    cutoff_day = now.date().toordinal() - RETAIN_DAYS
    files = sorted(
        (
            item for item in root.rglob("*.jsonl")
            if item.is_file() and _rollout_day(root, item).toordinal() >= cutoff_day
        ),
        key=lambda item: (_rollout_day(root, item), item.stat().st_mtime_ns, item.name),
        reverse=True,
    )[:MAX_SESSION_FILES]
    remaining = MAX_SCAN_BYTES
    touched = False
    partial = len(files) >= MAX_SESSION_FILES
    for path in files:
        if remaining <= 0:
            partial = True
            break
        marker = _file_marker(root, path)
        entry = state.files.get(marker, {})
        offset = _integer(entry.get("offset"))
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < offset:
            # A rewritten rollout cannot safely be replayed without a durable
            # event identifier.  Keep prior aggregates and wait for future
            # append-only records rather than risking duplicate attribution.
            state.files[marker] = {**entry, "offset": size, "incomplete": True}
            partial = True
            touched = True
            continue
        if size == offset:
            continue
        consumed, model, day, did_ingest, exhausted = _ingest_file_tail(
            path, offset, remaining, state, _model_or_unknown(entry.get("model")), entry.get("day"), now,
        )
        state.files[marker] = {"offset": consumed, "model": model, "day": day, "incomplete": exhausted}
        remaining -= max(0, consumed - offset)
        partial = partial or exhausted
        touched = touched or did_ingest or consumed != offset
    state.days = {day: sample for day, sample in state.days.items() if datetime.fromisoformat(day).date().toordinal() >= cutoff_day}
    state.files = {
        marker: entry for marker, entry in state.files.items()
        if _valid_day(entry.get("day")) and datetime.fromisoformat(str(entry["day"])).date().toordinal() >= cutoff_day
    }
    if touched:
        state.updated_at = now.astimezone().isoformat(timespec="seconds")
    state.partial = partial or any(bool(entry.get("incomplete")) for entry in state.files.values())
    return state


def current_day_sample(state: JsonlSampleState, epoch: float) -> TokenComposition | None:
    day = datetime.fromtimestamp(epoch).astimezone().date().isoformat()
    sample = state.days.get(day)
    return sample if sample and sample.events else None


def composition_note(sample: TokenComposition, *, partial: bool) -> dict[str, str]:
    input_share = _ratio(sample.input_tokens, sample.total_tokens)
    output_share = _ratio(sample.output_tokens, sample.total_tokens)
    reasoning_share = _ratio(sample.reasoning_output_tokens, sample.output_tokens)
    cached_input_rate = _ratio(sample.cached_input_tokens, sample.input_tokens)
    coverage = "Partial scan; " if partial else ""
    coverage_zh = "扫描受限；" if partial else ""
    return {
        "en": (
            f"{coverage}visible rollout sample only: input {input_share:.1%}, "
            f"output {output_share:.1%}, reasoning {reasoning_share:.1%} of output, "
            f"cached input {cached_input_rate:.1%} of input. "
            "It never changes the SQLite workload total or represents billing."
        ),
        "zh": (
            f"{coverage_zh}仅为当前可见 rollout 的抽样：输入 {input_share:.1%}，"
            f"输出 {output_share:.1%}，推理占输出 {reasoning_share:.1%}，缓存输入占输入 {cached_input_rate:.1%}。"
            "不会改写 SQLite 工作负载总量，也不是账单。"
        ),
    }


def _ingest_file_tail(
    path: Path,
    offset: int,
    remaining: int,
    state: JsonlSampleState,
    model: str,
    stored_day: object,
    now: datetime,
) -> tuple[int, str, str, bool, bool]:
    day = str(stored_day) if _valid_day(stored_day) else now.astimezone().date().isoformat()
    ingested = False
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
                        # Deliberately skip an oversized raw JSON record while
                        # consuming its remainder; otherwise it would block
                        # every later incremental pass at the same offset.
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
                    # Appends can be observed while the writer has not yet
                    # completed a JSONL record.  Leave that suffix untouched
                    # so a later low-frequency pass can parse it atomically.
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
                record_type = record.get("type")
                payload = record.get("payload")
                if record_type == "turn_context" and isinstance(payload, dict):
                    model = _model_or_unknown(payload.get("model"))
                    continue
                if record_type != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                usage = _usage(payload)
                if usage is None:
                    continue
                event_day = _event_day(record.get("timestamp"), now)
                state.days.setdefault(event_day, TokenComposition()).add(model, usage)
                day = event_day
                ingested = True
    except OSError:
        return offset, model, day, False, True
    return position, model, day, ingested, exhausted or remaining <= 0


def _record(line: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _usage(payload: dict[str, Any]) -> dict[str, int] | None:
    info = payload.get("info")
    raw = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        return None
    fields = (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens",
    )
    if any(field not in raw for field in fields):
        return None
    values = {field: _integer(raw.get(field)) for field in fields}
    if values["total_tokens"] <= 0:
        return None
    return values


def _event_day(value: object, fallback: datetime) -> str:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().date().isoformat()
        except ValueError:
            pass
    return fallback.astimezone().date().isoformat()


def _file_marker(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()


def _rollout_day(root: Path, path: Path):
    try:
        year, month, day, *_ = path.relative_to(root).parts
        return datetime(int(year), int(month), int(day)).date()
    except (ValueError, TypeError):
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().date()


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


def _ratio(numerator: int, denominator: int) -> float:
    return max(0.0, numerator / denominator) if denominator > 0 else 0.0
