"""Collector contracts and isolated execution for Infra Sentinel resources.

Collectors convert already-observed, privacy-safe facts into canonical metrics.
They do not own storage, rendering, or user notifications.  This keeps a
collector failure local: other sources still produce usable observations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from infra_model import MetricPoint


@dataclass(frozen=True)
class CollectorCapability:
    """Stable declaration of one independently runnable collector."""

    id: str
    source_id: str
    source_kind: str
    resource_id: str
    metrics: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "resource_id": self.resource_id,
            "metrics": list(self.metrics),
        }


@dataclass(frozen=True)
class CollectorContext:
    """The current, already-collected facts available to metric adapters."""

    local_sample: dict[str, Any]
    remote_state: dict[str, Any]


@dataclass(frozen=True)
class Collection:
    """One collector result, with an optional privacy-safe current snapshot.

    Interval metric points remain the canonical historical facts.  A snapshot
    exists for sources such as a local AI client which also expose a useful
    current-day summary without forcing that summary to be re-summed as an
    interval counter.
    """

    points: tuple[MetricPoint, ...] = ()
    status: str = "ok"
    snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class CollectorRun:
    """One collector outcome, separate from source business metrics."""

    capability: CollectorCapability
    status: str
    points: tuple[MetricPoint, ...] = ()
    snapshot: dict[str, Any] | None = None
    error_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "capability": self.capability.as_dict(),
            "status": self.status,
            "point_count": len(self.points),
        }
        if self.error_kind:
            payload["error_kind"] = self.error_kind
        if self.snapshot is not None:
            payload["snapshot"] = self.snapshot
        return payload


class Collector(Protocol):
    capability: CollectorCapability

    def collect(self, context: CollectorContext) -> Iterable[MetricPoint] | Collection:
        """Return canonical points without mutating runtime state."""


class CallableCollector:
    """Small adapter for collectors that are pure functions over one context."""

    def __init__(
        self,
        capability: CollectorCapability,
        collect: Callable[[CollectorContext], Iterable[MetricPoint]],
    ) -> None:
        self.capability = capability
        self._collect = collect

    def collect(self, context: CollectorContext) -> Iterable[MetricPoint]:
        return self._collect(context)


class CollectorRegistry:
    """Own collector registration, capability discovery, and failure isolation."""

    def __init__(self, collectors: Iterable[Collector] = ()) -> None:
        self._collectors: dict[str, Collector] = {}
        for collector in collectors:
            self.register(collector)

    def register(self, collector: Collector) -> None:
        capability = collector.capability
        if not capability.id or not capability.source_id or not capability.resource_id:
            raise ValueError("collector capability requires id, source_id, and resource_id")
        if capability.id in self._collectors:
            raise ValueError(f"duplicate collector id: {capability.id}")
        self._collectors[capability.id] = collector

    def capabilities(self) -> list[dict[str, Any]]:
        return [collector.capability.as_dict() for collector in self._collectors.values()]

    def collect(self, context: CollectorContext) -> tuple[CollectorRun, ...]:
        runs: list[CollectorRun] = []
        for collector in self._collectors.values():
            try:
                result = collector.collect(context)
            except Exception as exc:
                runs.append(CollectorRun(
                    capability=collector.capability,
                    status="error",
                    error_kind=type(exc).__name__,
                ))
                continue
            if isinstance(result, Collection):
                points = result.points
                status = result.status
                snapshot = result.snapshot
            else:
                points = tuple(result)
                status = "ok"
                snapshot = None
            runs.append(CollectorRun(
                capability=collector.capability,
                status=status,
                points=points,
                snapshot=snapshot,
            ))
        return tuple(runs)


def collected_points(runs: Iterable[CollectorRun]) -> Iterable[MetricPoint]:
    """Expose successful metric facts while preserving failed-run diagnostics."""
    for run in runs:
        yield from run.points
