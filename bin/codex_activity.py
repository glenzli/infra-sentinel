"""Codex lifecycle activity and model-aware traffic attribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any, Iterable


ACTIVITY_SCHEMA = 1
HOOK_EVENT_SCHEMA = 1
INBOX_NAME = "codex-hook-inbox"
MAX_FINGERPRINTS_PER_MODEL = 5_000
STALE_ACTOR_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class CodexActivityConfig:
    enabled: bool = True
    process_group: str = "codex"
    warning_active_subagents: int = 4
    warning_total_subagents: int = 10


def iso_now(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def model_label(model: str) -> str:
    normalized = model.casefold()
    if normalized.endswith("-sol"):
        return "Sol"
    if normalized.endswith("-terra"):
        return "Terra"
    if normalized.endswith("-luna"):
        return "Luna"
    return model or "Unknown"


def empty_model_stats() -> dict[str, int]:
    return {
        "traffic_bytes": 0,
        "exclusive_traffic_bytes": 0,
        "estimated_traffic_bytes": 0,
        "turns": 0,
        "subagents": 0,
        "tool_calls": 0,
        "read_like_calls": 0,
        "repeated_read_calls": 0,
        "tool_input_bytes": 0,
        "tool_response_bytes": 0,
        "compactions": 0,
    }


def drain_hook_inbox(state_dir: Path) -> list[dict[str, Any]]:
    """Atomically consume privacy-safe hook records written since the last sample."""
    inbox = state_dir / INBOX_NAME
    try:
        paths = sorted(path for path in inbox.glob("*.json") if path.is_file())
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        try:
            path.unlink()
        except OSError:
            continue
        if isinstance(payload, dict) and payload.get("schema") == HOOK_EVENT_SCHEMA:
            events.append(payload)
    return sorted(events, key=lambda event: float(event.get("epoch", 0)))


class CodexActivityMeter:
    """Persist hook activity and allocate exact process bytes to model estimates.

    Process traffic stays exact. Per-model traffic is high-confidence when only
    one model is active in the interval and explicitly estimated when models
    overlap.
    """

    def __init__(self, state_dir: Path, config: CodexActivityConfig) -> None:
        self.state_dir = state_dir
        self.config = config
        self.started_epoch: float | None = None
        self.last_event_epoch: float | None = None
        self.events_seen = 0
        self.active_actors: dict[str, dict[str, Any]] = {}
        self.models: dict[str, dict[str, int]] = {}
        self.seen_read_fingerprints: dict[str, set[str]] = {}
        self.total_subagents = 0
        self.peak_active_subagents = 0
        self.unassigned_traffic_bytes = 0
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "codex_activity.json"

    def _stats(self, model: str) -> dict[str, int]:
        return self.models.setdefault(model or "unknown", empty_model_stats())

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("schema") != ACTIVITY_SCHEMA:
            return
        try:
            started = payload.get("started_epoch")
            self.started_epoch = float(started) if started is not None else None
            last_event = payload.get("last_event_epoch")
            self.last_event_epoch = float(last_event) if last_event is not None else None
            self.events_seen = int(payload.get("events_seen", 0))
            self.total_subagents = int(payload.get("total_subagents", 0))
            self.peak_active_subagents = int(payload.get("peak_active_subagents", 0))
            self.unassigned_traffic_bytes = int(payload.get("unassigned_traffic_bytes", 0))
            actors = payload.get("active_actors", {})
            self.active_actors = {
                str(key): dict(value)
                for key, value in actors.items()
                if isinstance(value, dict) and isinstance(value.get("model"), str)
            } if isinstance(actors, dict) else {}
            saved_models = payload.get("models", {})
            if isinstance(saved_models, dict):
                for model, raw_stats in saved_models.items():
                    if not isinstance(raw_stats, dict):
                        continue
                    stats = empty_model_stats()
                    for key in stats:
                        stats[key] = int(raw_stats.get(key, 0))
                    self.models[str(model)] = stats
            fingerprints = payload.get("seen_read_fingerprints", {})
            if isinstance(fingerprints, dict):
                self.seen_read_fingerprints = {
                    str(model): {str(value) for value in values[-MAX_FINGERPRINTS_PER_MODEL:]}
                    for model, values in fingerprints.items()
                    if isinstance(values, list)
                }
        except (TypeError, ValueError):
            self._clear_statistics()

    def _save(self) -> None:
        payload = {
            "schema": ACTIVITY_SCHEMA,
            "started_epoch": self.started_epoch,
            "last_event_epoch": self.last_event_epoch,
            "events_seen": self.events_seen,
            "active_actors": self.active_actors,
            "models": self.models,
            "seen_read_fingerprints": {
                model: sorted(values)[-MAX_FINGERPRINTS_PER_MODEL:]
                for model, values in self.seen_read_fingerprints.items()
            },
            "total_subagents": self.total_subagents,
            "peak_active_subagents": self.peak_active_subagents,
            "unassigned_traffic_bytes": self.unassigned_traffic_bytes,
        }
        temporary = self.state_dir / ".codex_activity.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def _clear_statistics(self) -> None:
        self.models = {}
        self.seen_read_fingerprints = {}
        active_subagents = [actor for actor in self.active_actors.values() if actor.get("kind") == "subagent"]
        self.total_subagents = len(active_subagents)
        self.peak_active_subagents = len(active_subagents)
        for actor in active_subagents:
            self._stats(str(actor.get("model", "")) or "unknown")["subagents"] += 1
        self.unassigned_traffic_bytes = 0
        self.events_seen = 0
        self.last_event_epoch = None

    def reset(self, epoch: float) -> None:
        """Align activity totals while preserving actors already in flight."""
        self.started_epoch = float(epoch)
        self._clear_statistics()
        self._save()

    def align_started_epoch(self, epoch: float | None) -> None:
        if epoch is None:
            return
        if self.started_epoch is None or abs(self.started_epoch - float(epoch)) > 0.5:
            self.reset(float(epoch))

    def _prune_stale_actors(self, now: float) -> None:
        self.active_actors = {
            key: actor
            for key, actor in self.active_actors.items()
            if now - float(actor.get("last_seen_epoch", now)) <= STALE_ACTOR_SECONDS
        }

    def _actor_models(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for actor in self.active_actors.values():
            model = str(actor.get("model", "")) or "unknown"
            counts[model] = counts.get(model, 0) + 1
        return counts

    def _process_event(self, event: dict[str, Any]) -> tuple[str, int]:
        event_name = str(event.get("event", ""))
        model = str(event.get("model", "")) or "unknown"
        epoch = float(event.get("epoch", time.time()))
        session_id = str(event.get("session_id", ""))
        turn_id = str(event.get("turn_id", ""))
        self.events_seen += 1
        self.last_event_epoch = max(epoch, self.last_event_epoch or epoch)
        stats = self._stats(model)
        weight = 1

        if event_name == "UserPromptSubmit" and turn_id:
            actor_key = f"turn:{session_id}:{turn_id}"
            if actor_key not in self.active_actors:
                stats["turns"] += 1
            self.active_actors[actor_key] = {
                "kind": "turn",
                "model": model,
                "session_id": session_id,
                "last_seen_epoch": epoch,
            }
        elif event_name == "Stop" and turn_id:
            self.active_actors.pop(f"turn:{session_id}:{turn_id}", None)
        elif event_name == "SubagentStart":
            agent_id = str(event.get("agent_id", ""))
            if agent_id:
                actor_key = f"agent:{agent_id}"
                if actor_key not in self.active_actors:
                    self.total_subagents += 1
                    stats["subagents"] += 1
                self.active_actors[actor_key] = {
                    "kind": "subagent",
                    "model": model,
                    "session_id": session_id,
                    "last_seen_epoch": epoch,
                }
                active = sum(1 for actor in self.active_actors.values() if actor.get("kind") == "subagent")
                self.peak_active_subagents = max(self.peak_active_subagents, active)
        elif event_name == "SubagentStop":
            agent_id = str(event.get("agent_id", ""))
            if agent_id:
                self.active_actors.pop(f"agent:{agent_id}", None)
        elif event_name == "SessionEnd":
            self.active_actors = {
                key: actor
                for key, actor in self.active_actors.items()
                if actor.get("session_id") != session_id
            }
        elif event_name == "PostToolUse":
            stats["tool_calls"] += 1
            input_bytes = int(event.get("tool_input_bytes", 0))
            response_bytes = int(event.get("tool_response_bytes", 0))
            stats["tool_input_bytes"] += input_bytes
            stats["tool_response_bytes"] += response_bytes
            weight = max(1, input_bytes + response_bytes)
            if bool(event.get("read_like")):
                stats["read_like_calls"] += 1
                fingerprint = str(event.get("input_fingerprint", ""))
                if fingerprint:
                    seen = self.seen_read_fingerprints.setdefault(model, set())
                    if fingerprint in seen:
                        stats["repeated_read_calls"] += 1
                    elif len(seen) < MAX_FINGERPRINTS_PER_MODEL:
                        seen.add(fingerprint)
        elif event_name == "PostCompact":
            stats["compactions"] += 1

        exact_turn_key = f"turn:{session_id}:{turn_id}" if turn_id else ""
        for actor_key, actor in self.active_actors.items():
            if actor_key == exact_turn_key or actor.get("session_id") == session_id:
                actor["last_seen_epoch"] = epoch
        return model, weight

    def _allocate_traffic(self, total_bytes: int, model_weights: dict[str, int]) -> None:
        if total_bytes <= 0:
            return
        if not model_weights:
            self.unassigned_traffic_bytes += total_bytes
            return
        if len(model_weights) == 1:
            model = next(iter(model_weights))
            stats = self._stats(model)
            stats["traffic_bytes"] += total_bytes
            stats["exclusive_traffic_bytes"] += total_bytes
            return
        weight_total = sum(max(1, weight) for weight in model_weights.values())
        remaining = total_bytes
        ordered = sorted(model_weights.items(), key=lambda item: item[0])
        for index, (model, weight) in enumerate(ordered):
            allocation = remaining if index == len(ordered) - 1 else int(round(total_bytes * max(1, weight) / weight_total))
            allocation = max(0, min(remaining, allocation))
            remaining -= allocation
            stats = self._stats(model)
            stats["traffic_bytes"] += allocation
            stats["estimated_traffic_bytes"] += allocation

    def record(self, sample: dict[str, Any], events: Iterable[dict[str, Any]]) -> None:
        now = float(sample.get("epoch", time.time()))
        if self.started_epoch is None:
            self.started_epoch = now
        self._prune_stale_actors(now)
        before = self._actor_models()
        weights = dict(before)
        event_models: set[str] = set()
        for event in events:
            model, weight = self._process_event(event)
            event_models.add(model)
            weights[model] = weights.get(model, 0) + weight
        after = self._actor_models()
        for model, count in after.items():
            weights[model] = weights.get(model, 0) + count
        for model in event_models:
            weights.setdefault(model, 1)
        traffic = sample.get("groups", {}).get(self.config.process_group, {})
        total_bytes = int(traffic.get("up_bytes", 0)) + int(traffic.get("down_bytes", 0))
        model_set = set(before) | event_models | set(after)
        selected_weights = {model: max(1, weights.get(model, 1)) for model in model_set}
        self._allocate_traffic(total_bytes, selected_weights)
        self._save()

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        current_epoch = time.time() if now is None else now
        self._prune_stale_actors(current_epoch)
        active_models = self._actor_models()
        active_subagents = sum(1 for actor in self.active_actors.values() if actor.get("kind") == "subagent")
        models = []
        for model, stats in self.models.items():
            traffic = stats["traffic_bytes"]
            confidence = stats["exclusive_traffic_bytes"] / traffic if traffic > 0 else None
            models.append({
                "id": model,
                "label": model_label(model),
                **stats,
                "exclusive_ratio": confidence,
                "active_actors": active_models.get(model, 0),
                "traffic_quality": "mixed_estimate" if stats["estimated_traffic_bytes"] > 0 else ("exclusive" if traffic > 0 else "no_traffic"),
            })
        models.sort(key=lambda item: (item["traffic_bytes"], item["tool_calls"]), reverse=True)
        total_tool_calls = sum(model["tool_calls"] for model in models)
        total_read_like = sum(model["read_like_calls"] for model in models)
        total_repeated = sum(model["repeated_read_calls"] for model in models)
        total_input_bytes = sum(model["tool_input_bytes"] for model in models)
        total_response_bytes = sum(model["tool_response_bytes"] for model in models)
        risk = (
            "warning"
            if active_subagents > self.config.warning_active_subagents or self.total_subagents > self.config.warning_total_subagents
            else "normal"
        )
        return {
            "schema": ACTIVITY_SCHEMA,
            "enabled": self.config.enabled,
            "process_group": self.config.process_group,
            "started_at": iso_now(self.started_epoch),
            "started_epoch": self.started_epoch,
            "last_event_at": iso_now(self.last_event_epoch),
            "events_seen": self.events_seen,
            "integration_status": "active" if self.events_seen > 0 else "waiting",
            "active_models": [{"id": model, "label": model_label(model), "actors": count} for model, count in sorted(active_models.items())],
            "active_turns": sum(1 for actor in self.active_actors.values() if actor.get("kind") == "turn"),
            "active_subagents": active_subagents,
            "total_subagents": self.total_subagents,
            "peak_active_subagents": self.peak_active_subagents,
            "tool_calls": total_tool_calls,
            "read_like_calls": total_read_like,
            "repeated_read_calls": total_repeated,
            "tool_input_bytes": total_input_bytes,
            "tool_response_bytes": total_response_bytes,
            "unassigned_traffic_bytes": self.unassigned_traffic_bytes,
            "models": models,
            "risk": risk,
            "limits": {
                "warning_active_subagents": self.config.warning_active_subagents,
                "warning_total_subagents": self.config.warning_total_subagents,
            },
            "privacy": {
                "prompts_stored": False,
                "commands_stored": False,
                "paths_stored": False,
                "tool_contents_stored": False,
            },
        }
