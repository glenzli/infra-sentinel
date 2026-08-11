from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.facilities.observer import FacilityMonitor  # noqa: E402
from infra_sentinel.resources.facilities.protocols import (  # noqa: E402
    DEV_MESH_OBSERVER_ADAPTER,
    DEV_MESH_OBSERVER_PROTOCOL_VERSION,
    FacilityObservation,
    PCP_ADAPTER,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def timestamp(offset: int) -> str:
    return (NOW + timedelta(seconds=offset)).isoformat()


def registration(
    *,
    expires: int = 45,
    protocol: str = "pcp.runtime.observer",
    protocol_version: str = "20260810.1",
    kind: str = "pcp",
    endpoint_token: str = "test-gen-1",
) -> dict[str, object]:
    return {
        "schema": "infra.discovery.registration",
        "schema_version": "20260810.1",
        "service": {"kind": kind, "instance_id": "local", "generation": "gen-1"},
        "lease": {"renewed_at": timestamp(min(expires - 45, 0)), "expires_at": timestamp(expires)},
        "offers": [{
            "protocol": protocol,
            "protocol_versions": [protocol_version],
            "binding": "infra.local.unix-socket",
            "endpoint": f"sockets/{endpoint_token}.sock",
        }],
    }


def observation(
    *,
    label: str = "PCP",
    metric_id: str = "pcp.pages.current",
) -> FacilityObservation:
    captured = timestamp(0)
    return FacilityObservation(
        label=label,
        status="healthy",
        observed_at=captured,
        sequence=1,
        snapshot={
            "schema": "infra-sentinel.facility-observation",
            "schema_version": "20260810.1",
            "captured_at": captured,
            "sequence": 1,
            "status": {"state": "healthy", "reason_codes": []},
            "headline_metrics": [metric_id],
            "metrics": [{"id": metric_id, "kind": "gauge", "value": 2}],
            "issues": [],
        },
        console_url="http://127.0.0.1:4318/",
    )


def private_runtime(temporary: str) -> Path:
    root = Path(temporary)
    root.chmod(0o700)
    (root / "registrations").mkdir(mode=0o700)
    (root / "sockets").mkdir(mode=0o700)
    return root


def write_registration(root: Path, value: dict[str, object]) -> Path:
    service = value["service"]
    if not isinstance(service, dict):
        raise AssertionError("test registration service must be an object")
    path = root / "registrations" / f"{service['kind']}--{service['instance_id']}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


class FacilityMonitorTests(unittest.TestCase):
    def test_monitor_discovers_offer_and_projects_adapter_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            write_registration(root, registration())
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])

            with patch.object(type(PCP_ADAPTER), "observe", return_value=observation()):
                monitor.refresh_once(NOW.timestamp())
            state = monitor.snapshot()

            self.assertEqual(state["status"], "healthy")
            self.assertEqual(state["healthy"], 1)
            facility = state["items"][0]
            self.assertEqual(facility["protocol"], "pcp.runtime.observer")
            self.assertEqual(facility["generation"], "gen-1")
            self.assertEqual(facility["console_url"], "http://127.0.0.1:4318/")
            self.assertEqual(facility["snapshot"]["metrics"][0]["value"], 2)

    def test_monitor_discovers_and_projects_dev_mesh_observer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            write_registration(root, registration(
                protocol="dev-mesh.observer.status",
                protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
                kind="dev-mesh-observer",
                endpoint_token="dm-gen-1",
            ))
            monitor = FacilityMonitor(
                logging.getLogger("facility-test"),
                root,
                [DEV_MESH_OBSERVER_ADAPTER],
            )
            observed = observation(
                label="Dev Mesh Observer",
                metric_id="dev_mesh.workspaces.available",
            )

            with patch.object(
                type(DEV_MESH_OBSERVER_ADAPTER),
                "observe",
                return_value=observed,
            ):
                monitor.refresh_once(NOW.timestamp())
            state = monitor.snapshot()

            self.assertEqual(state["status"], "healthy")
            facility = state["items"][0]
            self.assertEqual(facility["label"], "Dev Mesh Observer")
            self.assertEqual(facility["protocol"], "dev-mesh.observer.status")
            self.assertEqual(facility["protocol_version"], "20260812.1")
            self.assertEqual(facility["console_url"], "http://127.0.0.1:4318/")
            self.assertEqual(
                facility["snapshot"]["headline_metrics"],
                ["dev_mesh.workspaces.available"],
            )

    def test_sequence_must_advance_within_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            write_registration(root, registration())
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])
            first = observation()
            with patch.object(type(PCP_ADAPTER), "observe", return_value=first):
                monitor.refresh_once(NOW.timestamp())
            monitor._records["pcp:local"].next_poll_epoch = 0
            with patch.object(type(PCP_ADAPTER), "observe", return_value=first):
                monitor.refresh_once(NOW.timestamp() + 1)
            facility = monitor.snapshot()["items"][0]
            self.assertEqual(facility["status"], "unreachable")
            self.assertEqual(facility["snapshot"]["sequence"], 1)

    def test_sequence_still_advances_when_endpoint_changes_in_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            path = write_registration(root, registration())
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])
            first = observation()
            with patch.object(type(PCP_ADAPTER), "observe", return_value=first):
                monitor.refresh_once(NOW.timestamp())

            changed = registration()
            changed["offers"][0]["endpoint"] = "sockets/pcp-rebound.sock"  # type: ignore[index]
            path.write_text(json.dumps(changed), encoding="utf-8")
            path.chmod(0o600)
            with patch.object(type(PCP_ADAPTER), "observe", return_value=first):
                monitor.refresh_once(NOW.timestamp() + 1)

            facility = monitor.snapshot()["items"][0]
            self.assertEqual(facility["status"], "unreachable")
            self.assertEqual(facility["error_kind"], "FacilityProtocolError")

    def test_unknown_application_protocol_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            write_registration(root, registration(protocol="future.runtime.status"))
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])

            monitor.refresh_once(NOW.timestamp())

            self.assertEqual(monitor.snapshot()["status"], "disabled")
            self.assertEqual(monitor.snapshot()["items"], [])

    def test_expired_registration_retains_only_a_previously_observed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            path = write_registration(root, registration())
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])
            with patch.object(type(PCP_ADAPTER), "observe", return_value=observation()):
                monitor.refresh_once(NOW.timestamp())

            expired = registration(expires=-1)
            expired["lease"] = {"renewed_at": timestamp(-46), "expires_at": timestamp(-1)}
            path.write_text(json.dumps(expired), encoding="utf-8")
            path.chmod(0o600)
            monitor.refresh_once(NOW.timestamp())

            facility = monitor.snapshot()["items"][0]
            self.assertEqual(facility["status"], "stale")
            self.assertEqual(facility["error_kind"], "LeaseExpired")

    def test_missing_runtime_root_means_no_discovered_facilities_not_global_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "not-created"
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])

            monitor.refresh_once(NOW.timestamp())

            state = monitor.snapshot()
            self.assertEqual(state["status"], "disabled")
            self.assertNotIn("error_kind", state)

    def test_invalid_runtime_root_is_isolated_as_discovery_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = private_runtime(temporary)
            root.chmod(0o755)
            monitor = FacilityMonitor(logging.getLogger("facility-test"), root, [PCP_ADAPTER])

            monitor.refresh_once(NOW.timestamp())

            state = monitor.snapshot()
            self.assertEqual(state["status"], "degraded")
            self.assertEqual(state["attention"], 1)
            self.assertEqual(state["error_kind"], "DiscoveryError")


if __name__ == "__main__":
    unittest.main()
