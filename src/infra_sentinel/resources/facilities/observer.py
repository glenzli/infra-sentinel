"""Read-only facility monitoring composed from discovery and protocol adapters.

The monitor owns reconciliation and polling lifecycle only.  Infra Discovery
owns service selection; each provider adapter owns its application wire; the
projection emitted here is private to Infra Sentinel.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    FacilityTransportError,
    select_adapter,
)
from infra_sentinel.core.status_stability import StatusDecision, StatusStabilizer
from infra_sentinel.resources.facilities.discovery import (
    DISCOVERY_VERSION,
    DiscoveryError,
    DiscoveryPaths,
    Registration,
    discovery_paths,
    read_registration,
    validate_runtime_paths,
)


POLL_SECONDS = 15.0
RECONCILE_SECONDS = 5.0
FACILITY_STATUS_RANKS = {
    "starting": 0,
    "stopping": 0,
    "healthy": 0,
    "degraded": 1,
    "unavailable": 2,
    "unreachable": 2,
}
DISCOVERY_STATUS_RANKS = {"healthy": 0, "error": 1}


@dataclass
class _ObservedFacility:
    registration: Registration
    selection: AdapterSelection
    status: str
    observation: FacilityObservation | None
    last_success_epoch: float | None
    next_poll_epoch: float
    error_kind: str | None = None
    stability: StatusStabilizer | None = None
    confirmation: StatusDecision | None = None

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
        if self.confirmation and self.confirmation.pending_status:
            payload["confirmation"] = {
                "candidate_status": self.confirmation.pending_status,
                "consecutive": self.confirmation.pending_count,
                "required": self.confirmation.required_count,
            }
        return payload


class FacilityMonitor:
    """Reconcile candidate registrations and poll selected protocols off the hot path."""

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
        self._discovery_stability = StatusStabilizer(
            "healthy",
            DISCOVERY_STATUS_RANKS,
            worsen_after=3,
            recover_after=2,
        )
        self._discovery_confirmation: StatusDecision | None = None

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

    def _registrations(self) -> dict[str, Registration]:
        if not self.paths.root.exists():
            return {}
        validate_runtime_paths(self.paths)
        registrations: dict[str, Registration] = {}
        for path in sorted(self.paths.registrations.glob("*.json")):
            try:
                registration = read_registration(path)
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
    ) -> Registration:
        current = read_registration(registration.path)
        if current.generation != registration.generation:
            raise FacilityProtocolError("provider generation changed during observation")
        current_selection = select_adapter(current, self.adapters)
        if current_selection != selection:
            raise FacilityProtocolError("selected protocol offer changed during observation")
        return current

    @staticmethod
    def _stability(prior: _ObservedFacility | None) -> StatusStabilizer:
        if prior is not None and prior.stability is not None:
            return prior.stability
        initial = prior.status if prior is not None else "starting"
        return StatusStabilizer(initial, FACILITY_STATUS_RANKS, worsen_after=3, recover_after=2)

    @staticmethod
    def _confirmed_record(
        registration: Registration,
        selection: AdapterSelection,
        prior: _ObservedFacility | None,
        observation: FacilityObservation,
        current_epoch: float,
    ) -> _ObservedFacility:
        stability = FacilityMonitor._stability(prior)
        initial_observation = prior is None or prior.observation is None
        prior_observation = prior.observation if prior else None
        decision = stability.observe(
            observation.status,
            immediate=(initial_observation and observation.status in {"healthy", "starting", "stopping"}),
        )
        accepted = decision.status == observation.status
        retained_snapshot = dict(prior_observation.snapshot) if prior_observation is not None else None
        if retained_snapshot is not None:
            retained_snapshot["captured_at"] = observation.observed_at
            retained_snapshot["sequence"] = observation.sequence
            retained_snapshot["status"] = {
                "state": decision.status,
                "reason_codes": retained_snapshot.get("status", {}).get("reason_codes", []),
            }
        return _ObservedFacility(
            registration=registration,
            selection=selection,
            status=decision.status,
            observation=(
                observation
                if accepted or prior_observation is None
                else FacilityObservation(
                    label=prior_observation.label,
                    status=decision.status,
                    observed_at=observation.observed_at,
                    sequence=observation.sequence,
                    snapshot=retained_snapshot or prior_observation.snapshot,
                    console_url=prior_observation.console_url,
                )
            ),
            last_success_epoch=current_epoch,
            next_poll_epoch=current_epoch + POLL_SECONDS,
            error_kind=None if accepted else (prior.error_kind if prior else None),
            stability=stability,
            confirmation=decision,
        )

    @staticmethod
    def _failed_record(
        registration: Registration,
        selection: AdapterSelection,
        prior: _ObservedFacility | None,
        same_selection: bool,
        current_epoch: float,
        error: Exception,
    ) -> _ObservedFacility:
        stable_prior = prior if same_selection else None
        stability = FacilityMonitor._stability(stable_prior)
        transient = isinstance(error, FacilityTransportError)
        decision = stability.observe("unreachable", immediate=not transient)
        accepted = decision.status == "unreachable"
        return _ObservedFacility(
            registration=registration,
            selection=selection,
            status=decision.status,
            observation=stable_prior.observation if stable_prior else None,
            last_success_epoch=stable_prior.last_success_epoch if stable_prior else None,
            next_poll_epoch=current_epoch + POLL_SECONDS,
            error_kind=type(error).__name__ if accepted else None,
            stability=stability,
            confirmation=decision,
        )

    def refresh_once(self, now: float | None = None) -> None:
        current_epoch = time.time() if now is None else now
        with self._lock:
            previous = dict(self._records)
            confirmed_discovery_error = self._discovery_error
        try:
            registrations = self._registrations()
            discovery_candidate = None
        except (OSError, DiscoveryError) as error:
            registrations = None
            discovery_candidate = type(error).__name__
            self.logger.warning("Infra Discovery unavailable error=%s", error)

        discovery_decision = self._discovery_stability.observe(
            "error" if discovery_candidate else "healthy"
        )
        if discovery_decision.status == "error":
            discovery_error = discovery_candidate or confirmed_discovery_error
        else:
            discovery_error = None

        # A failed directory read says nothing about whether the last observed
        # facilities stopped.  Keep the last-good set until discovery can be
        # read again; a successful empty scan still removes registrations
        # immediately, as required by the lease-free Discovery contract.
        updated: dict[str, _ObservedFacility] = dict(previous) if registrations is None else {}
        for facility_id, registration in (registrations or {}).items():
            prior = previous.get(facility_id)
            selection = select_adapter(registration, self.adapters)
            if selection is None:
                continue
            same_selection = prior is not None and self._same_selection(prior, registration, selection)
            if same_selection and current_epoch < prior.next_poll_epoch:
                prior.registration = registration
                updated[facility_id] = prior
                continue
            try:
                observation = selection.adapter.observe(self.paths, registration, selection)
                registration = self._confirm_current(registration, selection)
                if (
                    prior is not None
                    and self._same_protocol_generation(prior, registration, selection)
                    and prior.observation is not None
                    and observation.sequence <= prior.observation.sequence
                ):
                    raise FacilityProtocolError(
                        "provider snapshot sequence did not advance within its generation"
                    )
                record = self._confirmed_record(
                    registration,
                    selection,
                    prior if same_selection else None,
                    observation,
                    current_epoch,
                )
            except (DiscoveryError, FacilityProtocolError) as error:
                self.logger.warning(
                    "facility observation failed id=%s protocol=%s error=%s",
                    facility_id,
                    selection.adapter.protocol,
                    error,
                )
                record = self._failed_record(
                    registration,
                    selection,
                    prior,
                    same_selection,
                    current_epoch,
                    error,
                )
            updated[facility_id] = record

        with self._lock:
            self._records = updated
            self._discovery_error = discovery_error
            self._discovery_confirmation = discovery_decision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = [record.projection() for record in self._records.values()]
            discovery_error = self._discovery_error
            discovery_confirmation = self._discovery_confirmation
        items.sort(key=lambda item: (str(item.get("label") or "").lower(), str(item.get("id") or "")))
        healthy = sum(1 for item in items if item["status"] == "healthy")
        attention = sum(1 for item in items if item["status"] not in {"healthy", "starting"})
        if any(item["status"] in {"unavailable", "unreachable"} for item in items):
            status = "critical"
        elif attention or discovery_error:
            status = "degraded"
        elif items and healthy == len(items):
            status = "healthy"
        elif items:
            status = "starting"
        else:
            status = "disabled"
        if discovery_error:
            attention += 1
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
        if discovery_confirmation and discovery_confirmation.pending_status:
            projection["confirmation"] = {
                "candidate_status": discovery_confirmation.pending_status,
                "consecutive": discovery_confirmation.pending_count,
                "required": discovery_confirmation.required_count,
            }
        return projection
