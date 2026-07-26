"""Persist interface-scoped proxy traffic without mixing relay socket legs."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any, Iterable


# Version 3 replaces fragile per-socket snapshots with `nettop -t` process
# summaries. Closed short-lived sockets can otherwise disappear between reads.
PROXY_SEGMENT_SCHEMA = 3
PROXY_BASELINE_SCHEMA = 3
PROXY_CYCLE_SCHEMA = 3
CATEGORIES = ("external", "loopback", "other")


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def _empty_totals() -> dict[str, dict[str, int]]:
    return {category: {"up_bytes": 0, "down_bytes": 0} for category in CATEGORIES}


def _counter_key(item: dict[str, Any]) -> tuple[str, int | None, str]:
    return str(item.get("category", "other")), item.get("pid"), str(item.get("name", ""))


class ProxySegmentTracker:
    """Turn filtered per-process proxy counters into interface-class deltas."""

    def __init__(self, previous: dict[tuple[str, int | None, str], dict[str, int]] | None = None) -> None:
        self.previous = previous or {}

    def apply(self, timestamp: str, epoch: float, counters: Iterable[dict[str, Any]]) -> dict[str, Any]:
        next_previous: dict[tuple[str, int | None, str], dict[str, int]] = {}
        totals = _empty_totals()
        for counter in counters:
            category = str(counter.get("category", "other"))
            if category not in CATEGORIES:
                continue
            key = _counter_key(counter)
            current_up = int(counter["up_bytes"])
            current_down = int(counter["down_bytes"])
            prior = self.previous.get(key)
            up_delta = current_up - prior["up_bytes"] if prior and current_up >= prior["up_bytes"] else 0
            down_delta = current_down - prior["down_bytes"] if prior and current_down >= prior["down_bytes"] else 0
            totals[category]["up_bytes"] += up_delta
            totals[category]["down_bytes"] += down_delta
            next_previous[key] = {"up_bytes": current_up, "down_bytes": current_down}
        self.previous = next_previous
        return {
            "schema": PROXY_SEGMENT_SCHEMA,
            "timestamp": timestamp,
            "epoch": epoch,
            "categories": totals,
        }

    def serialize(self) -> dict[str, Any]:
        counters = []
        for (category, pid, name), values in self.previous.items():
            counters.append({
                "category": category,
                "pid": pid,
                "name": name,
                "up_bytes": values["up_bytes"],
                "down_bytes": values["down_bytes"],
            })
        return {"schema": PROXY_BASELINE_SCHEMA, "counters": counters}


def load_tracker(state_dir: Path) -> ProxySegmentTracker:
    try:
        payload = json.loads((state_dir / "proxy_segment_baseline.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ProxySegmentTracker()
    if payload.get("schema") != PROXY_BASELINE_SCHEMA:
        return ProxySegmentTracker()
    previous: dict[tuple[str, int | None, str], dict[str, int]] = {}
    for item in payload.get("counters", []):
        if not isinstance(item, dict) or not item.get("name") or item.get("category") not in CATEGORIES:
            continue
        try:
            previous[_counter_key(item)] = {"up_bytes": int(item["up_bytes"]), "down_bytes": int(item["down_bytes"])}
        except (KeyError, TypeError, ValueError):
            continue
    return ProxySegmentTracker(previous)


def save_tracker(state_dir: Path, tracker: ProxySegmentTracker) -> None:
    temporary = state_dir / ".proxy_segment_baseline.json.tmp"
    temporary.write_text(json.dumps(tracker.serialize(), ensure_ascii=False), encoding="utf-8")
    temporary.replace(state_dir / "proxy_segment_baseline.json")


class ProxyCycleMeter:
    """Own the independent external/loopback proxy billing-cycle totals."""

    def __init__(self, state_dir: Path, state: Any, cycle_start: float) -> None:
        self.state_dir = state_dir
        self.log_state = state
        self.cycle_start = cycle_start
        self.coverage_started: float | None = None
        self.totals = _empty_totals()
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "proxy_segment_cycle.json"

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("schema") != PROXY_CYCLE_SCHEMA or int(payload.get("cycle_start", -1)) != int(self.cycle_start):
            return
        try:
            coverage = payload.get("coverage_started")
            self.coverage_started = float(coverage) if coverage is not None else None
            for category in CATEGORIES:
                item = payload.get("categories", {}).get(category, {})
                self.totals[category] = {"up_bytes": int(item.get("up_bytes", 0)), "down_bytes": int(item.get("down_bytes", 0))}
        except (AttributeError, KeyError, TypeError, ValueError):
            self.coverage_started = None
            self.totals = _empty_totals()

    def _save(self) -> None:
        payload = {
            "schema": PROXY_CYCLE_SCHEMA,
            "cycle_start": self.cycle_start,
            "coverage_started": self.coverage_started,
            "categories": self.totals,
        }
        temporary = self.state_dir / ".proxy_segment_cycle.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def _append_sample(self, sample: dict[str, Any]) -> None:
        path = self.state_dir / "proxy_segments.jsonl"
        if path.exists() and path.stat().st_size >= self.log_state.max_log_bytes:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"proxy_segments-{timestamp}.jsonl"))
            archives = sorted(self.state_dir.glob("proxy_segments-*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
            for stale in archives[self.log_state.backups:]:
                stale.unlink(missing_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")

    def record(self, sample: dict[str, Any], cycle_start: float) -> None:
        if int(cycle_start) != int(self.cycle_start):
            self.cycle_start = cycle_start
            self.coverage_started = None
            self.totals = _empty_totals()
        self._append_sample(sample)
        if self.coverage_started is None:
            self.coverage_started = float(sample["epoch"])
        for category in CATEGORIES:
            current = sample["categories"][category]
            self.totals[category]["up_bytes"] += int(current["up_bytes"])
            self.totals[category]["down_bytes"] += int(current["down_bytes"])
        self._save()

    def snapshot(self) -> dict[str, Any]:
        categories = []
        for category in CATEGORIES:
            traffic = self.totals[category]
            categories.append({"id": category, **traffic, "total_bytes": traffic["up_bytes"] + traffic["down_bytes"]})
        return {
            "coverage_started_at": iso_now(self.coverage_started) if self.coverage_started is not None else None,
            "categories": categories,
        }
