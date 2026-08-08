"""Stable, content-free contracts shared by Infra Sentinel modules.

This module deliberately contains no collector I/O or presentation wording.  New
resource modules use these records to expose measured facts before they are
stored or rendered by a projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Instrument = Literal["counter", "gauge", "quota", "event"]
Confidence = Literal["high", "medium", "low"]
AttributionMethod = Literal["exact", "mapped", "inferred", "residual"]


@dataclass(frozen=True)
class MetricPoint:
    """A measured interval or point-in-time fact with privacy-safe dimensions."""

    observed_at: str
    metric: str
    instrument: Instrument
    value: int | float
    unit: str
    source_id: str
    resource_id: str
    dimensions: dict[str, str] = field(default_factory=dict)
    attribution_method: AttributionMethod = "exact"
    confidence: Confidence = "high"
    estimated: bool = False
    observed_epoch: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "observed_at": self.observed_at,
            "metric": self.metric,
            "instrument": self.instrument,
            "value": self.value,
            "unit": self.unit,
            "source_id": self.source_id,
            "resource_id": self.resource_id,
            "dimensions": dict(self.dimensions),
            "attribution_method": self.attribution_method,
            "confidence": self.confidence,
            "estimated": self.estimated,
        }
        if self.observed_epoch is not None:
            payload["observed_epoch"] = self.observed_epoch
        return payload


@dataclass(frozen=True)
class SourceStatus:
    """Health and capability of one configured data source."""

    id: str
    kind: str
    resource_id: str
    enabled: bool
    status: str
    label: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "resource_id": self.resource_id,
            "enabled": self.enabled,
            "status": self.status,
        }
        if self.label:
            payload["label"] = self.label
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        return payload
