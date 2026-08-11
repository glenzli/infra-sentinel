"""Read-only facility monitoring composed from discovery and protocol adapters.

The monitor owns reconciliation and polling lifecycle only.  Infra Discovery
owns service selection; each provider adapter owns its application wire; the
projection emitted here is private to Infra Sentinel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time
from typing import Any, Iterable

from infra_sentinel.resources.facilities.protocols import (
    AdapterSelection,
    DEFAULT_ADAPTERS,
    FacilityObservation,
    FacilityProtocolAdapter,
    FacilityProtocolError,
    select_adapter,
)
from infra_sentinel.resources.facilities.discovery import (
    DISCOVERY_VERSION,
    DiscoveryError,
    DiscoveryPaths,
    LeaseExpired,
    Registration,
    discovery_paths,
    read_registration,
    validate_runtime_paths,
)


POLL_SECONDS = 15.0
RECONCILE_SECONDS = 5.0
RETAIN_STALE_SECONDS = 300.0


@dataclass
class _ObservedFacility:
    registration: Registration
    selection: AdapterSelection
    status: str
    observation: FacilityObservation | None
    last_success_epoch: float | None
    next_poll_epoch: float
    error_kind: str | None = None

    @property
    def id(self) -> str:
        return self.registration.id

    def projection(self) -> dict[str, Any]:
        observation = self.observation
        payload: dict[str, Any] = {
            "id": self.registration.id,
            "kind": self.registration.kind,
            "instance_id": self.registration.instance_id,
            "generation": self.registration.generation,
            "label": observation.label if observation else self.selection.adapter.label,
            "status": self.status,
            "observed_at": observation.observed_at if observation else None,
            "lease_expires_at": self.registration.expires_at,
            "protocol": self.selection.adapter.protocol,
            "protocol_version": self.selection.protocol_version,
            "binding": self.selection.offer.binding,
        }
        if observation is not None:
            payload["snapshot"] = observation.snapshot
            if observation.console_url:
                payload["console_url"] = observation.console_url
        if self.error_kind:
            payload["error_kind"] = self.error_kind
        return payload


class FacilityMonitor:
    """Reconcile discovery leases and poll selected protocols off the hot path."""

    def __init__(
        self,
        logger: logging.Logger,
        runtime_directory: Path | None = None,
        adapters: Iterable[FacilityProtocolAdapter] = DEFAULT_ADAPTERS,
    ) -> None:
        self.paths: DiscoveryPaths = discovery_paths(runtime_directory)
        self.adapters = tuple(adapters)
        self.logger = logger
        self._records: dict[str, _ObservedFacility] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._discovery_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="infra-facility-observer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                self.logger.exception("facility reconciliation failed")
            self._stop.wait(RECONCILE_SECONDS)

    def _registrations(self, now: datetime) -> dict[str, Registration]:
        if not self.paths.root.exists():
            return {}
        validate_runtime_paths(self.paths)
        registrations: dict[str, Registration] = {}
        for path in sorted(self.paths.registrations.glob("*.json")):
            try:
                registration = read_registration(path, now=now, require_live=False)
            except DiscoveryError as error:
                self.logger.warning(
                    "ignored Infra Discovery registration path=%s error=%s",
                    path.name,
                    error,
                )
                continue
            registrations[registration.id] = registration
        return registrations

    @staticmethod
    def _same_selection(record: _ObservedFacility, registration: Registration, selection: AdapterSelection) -> bool:
        return (
            record.registration.generation == registration.generation
            and record.selection.adapter.protocol == selection.adapter.protocol
            and record.selection.protocol_version == selection.protocol_version
            and record.selection.offer == selection.offer
        )

    @staticmethod
    def _same_protocol_generation(
        record: _ObservedFacility,
        registration: Registration,
        selection: AdapterSelection,
    ) -> bool:
        return (
            record.registration.generation == registration.generation
            and record.selection.adapter.protocol == selection.adapter.protocol
        )

    def _confirm_current(
        self,
        registration: Registration,
        selection: AdapterSelection,
        now: datetime,
    ) -> Registration:
        current = read_registration(registration.path, now=now, require_live=True)
        if current.generation != registration.generation:
            raise FacilityProtocolError("provider generation changed during observation")
        current_selection = select_adapter(current, self.adapters)
        if current_selection != selection:
            raise FacilityProtocolError("selected protocol offer changed during observation")
        return current

    def refresh_once(self, now: float | None = None) -> None:
        current_epoch = time.time() if now is None else now
        current_time = datetime.fromtimestamp(current_epoch, timezone.utc)
        try:
            registrations = self._registrations(current_time)
            discovery_error = None
        except (OSError, DiscoveryError) as error:
            registrations = {}
            discovery_error = type(error).__name__
            self.logger.warning("Infra Discovery unavailable error=%s", error)

        with self._lock:
            previous = dict(self._records)
        updated: dict[str, _ObservedFacility] = {}
        for facility_id, registration in registrations.items():
            prior = previous.get(facility_id)
            selection = select_adapter(registration, self.adapters)
            if selection is None:
                continue
            if registration.expires.timestamp() <= current_epoch:
                if prior and self._same_selection(prior, registration, selection):
                    prior.registration = registration
                    prior.status = "stale"
                    prior.error_kind = LeaseExpired.__name__
                    updated[facility_id] = prior
                continue
            same_selection = prior is not None and self._same_selection(prior, registration, selection)
            if same_selection and current_epoch < prior.next_poll_epoch:
                prior.registration = registration
                updated[facility_id] = prior
                continue
            try:
                observation = selection.adapter.observe(self.paths, registration, selection)
                registration = self._confirm_current(registration, selection, current_time)
                if (
                    prior is not None
                    and self._same_protocol_generation(prior, registration, selection)
                    and prior.observation is not None
                    and observation.sequence <= prior.observation.sequence
                ):
                    raise FacilityProtocolError(
                        "provider snapshot sequence did not advance within its generation"
                    )
                record = _ObservedFacility(
                    registration=registration,
                    selection=selection,
                    status=observation.status,
                    observation=observation,
                    last_success_epoch=current_epoch,
                    next_poll_epoch=current_epoch + POLL_SECONDS,
                )
            except (DiscoveryError, FacilityProtocolError) as error:
                self.logger.warning(
                    "facility observation failed id=%s protocol=%s error=%s",
                    facility_id,
                    selection.adapter.protocol,
                    error,
                )
                record = _ObservedFacility(
                    registration=registration,
                    selection=selection,
                    status="unreachable",
                    observation=prior.observation if same_selection else None,
                    last_success_epoch=prior.last_success_epoch if same_selection else None,
                    next_poll_epoch=current_epoch + POLL_SECONDS,
                    error_kind=type(error).__name__,
                )
            updated[facility_id] = record

        for facility_id, prior in previous.items():
            if facility_id in updated:
                continue
            if current_epoch <= prior.registration.expires.timestamp() + RETAIN_STALE_SECONDS:
                prior.status = "stale"
                prior.error_kind = "RegistrationMissing"
                updated[facility_id] = prior
        with self._lock:
            self._records = updated
            self._discovery_error = discovery_error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = [record.projection() for record in self._records.values()]
            discovery_error = self._discovery_error
        items.sort(key=lambda item: (str(item.get("label") or "").lower(), str(item.get("id") or "")))
        healthy = sum(1 for item in items if item["status"] == "healthy")
        attention = sum(1 for item in items if item["status"] not in {"healthy", "starting"})
        if any(item["status"] in {"unavailable", "unreachable"} for item in items):
            status = "critical"
        elif attention:
            status = "degraded"
        elif items and healthy == len(items):
            status = "healthy"
        elif items:
            status = "starting"
        elif discovery_error:
            status = "degraded"
            attention = 1
        else:
            status = "disabled"
        projection: dict[str, Any] = {
            "schema": f"infra.discovery.registration@{DISCOVERY_VERSION}",
            "status": status,
            "total": len(items),
            "healthy": healthy,
            "attention": attention,
            "items": items,
        }
        if discovery_error:
            projection["error_kind"] = discovery_error
        return projection
