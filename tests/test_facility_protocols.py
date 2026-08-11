from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.resources.facilities.protocols import (  # noqa: E402
    DEV_MESH_OBSERVER_ADAPTER,
    DEV_MESH_OBSERVER_PROTOCOL_VERSION,
    FacilityProtocolError,
    INFER_RUNTIME_ADAPTER,
    PCP_ADAPTER,
    _read_response_frame,
    _request,
    select_adapter,
)
from infra_sentinel.resources.facilities.discovery import (  # noqa: E402
    DiscoveryOffer,
    DiscoveryPaths,
    Registration,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def registration(
    protocol: str,
    kind: str,
    generation: str = "gen-1",
    *,
    protocol_version: str = "20260810.1",
    binding: str = "infra.local.unix-socket",
    endpoint_token: str = "test-gen-1",
) -> Registration:
    return Registration(
        path=Path(f"/tmp/{kind}--local.json"),
        kind=kind,
        instance_id="local",
        generation=generation,
        offers=(DiscoveryOffer(
            protocol,
            (protocol_version,),
            binding,
            f"sockets/{endpoint_token}.sock",
        ),),
    )


def snapshot(adapter, registration_value: Registration) -> dict[str, object]:
    if adapter is PCP_ADAPTER:
        redactions = ["page_content", "query_text", "scope_names", "raw_audit", "storage_paths"]
        extensions = {"pcp": {"ignored": True}}
    elif adapter is INFER_RUNTIME_ADAPTER:
        redactions = [
            "credentials", "filesystem_paths", "job_identifiers", "job_metadata",
            "payloads", "raw_errors", "usage_ledger",
        ]
        extensions = {"infer-runtime": {"ignored": True}}
    else:
        if adapter is not DEV_MESH_OBSERVER_ADAPTER:
            raise AssertionError("unsupported test adapter")
        redactions = [
            "coordination_owner_ids", "workspace_paths", "git_revisions",
            "branch_names", "event_payloads", "raw_errors", "database_paths",
            "claim_scopes",
        ]
        extensions = {"dev-mesh-observer": {"collector_enabled": True}}
    if adapter is DEV_MESH_OBSERVER_ADAPTER:
        headline = [
            "dev_mesh.workspaces.available",
            "dev_mesh.collection.pending_events",
            "dev_mesh.contentions.stalled",
        ]
        metrics = [
            {"id": metric_id, "kind": "gauge", "value": index, "unit": "count"}
            for index, metric_id in enumerate(headline)
        ]
    else:
        headline = ["workload.active"]
        metrics = [{"id": "workload.active", "kind": "gauge", "value": 2, "unit": "count"}]
    return {
        "schema": adapter.snapshot_schema,
        "schema_version": adapter.protocol_version,
        "service": {
            "kind": registration_value.kind,
            "instance_id": registration_value.instance_id,
            "generation": registration_value.generation,
        },
        "sequence": 7,
        "captured_at": NOW.isoformat(),
        "status": {"state": "healthy", "reason_codes": []},
        "headline_metrics": headline,
        "metrics": metrics,
        "issues": [],
        "redaction": {"excluded": redactions},
        "links": {"console_url": "http://127.0.0.1:4318/"},
        "extensions": extensions,
    }


class FacilityProtocolTests(unittest.TestCase):
    def test_provider_requests_are_independent(self) -> None:
        self.assertEqual(json.loads(_request(PCP_ADAPTER.request_schema)), {
            "schema": "pcp.runtime.observer.request",
            "schema_version": "20260810.1",
            "operation": "snapshot",
        })
        self.assertEqual(json.loads(_request(INFER_RUNTIME_ADAPTER.request_schema)), {
            "schema": "infer-runtime.status.request",
            "schema_version": "20260810.1",
            "operation": "snapshot",
        })
        self.assertEqual(json.loads(_request(
            DEV_MESH_OBSERVER_ADAPTER.request_schema,
            DEV_MESH_OBSERVER_ADAPTER.protocol_version,
        )), {
            "schema": "dev-mesh.observer.status.request",
            "schema_version": "20260812.1",
            "operation": "snapshot",
        })

    def test_selection_uses_exact_protocol_version_and_binding(self) -> None:
        pcp = registration("pcp.runtime.observer", "pcp")
        selected = select_adapter(pcp)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.adapter, PCP_ADAPTER)  # type: ignore[union-attr]
        dev_mesh = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            endpoint_token="dm-gen-1",
        )
        selected = select_adapter(dev_mesh)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.adapter, DEV_MESH_OBSERVER_ADAPTER)  # type: ignore[union-attr]
        wrong_version = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version="20260812.0",
            endpoint_token="dm-gen-1",
        )
        self.assertIsNone(select_adapter(wrong_version))
        wrong_binding = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            binding="infra.local.tcp",
            endpoint_token="dm-gen-1",
        )
        self.assertIsNone(select_adapter(wrong_binding))
        unknown = registration("future.runtime.status", "future")
        self.assertIsNone(select_adapter(unknown))

    def test_provider_error_envelopes_remain_protocol_specific(self) -> None:
        self.assertEqual(PCP_ADAPTER._error_code({
            "schema": "pcp.runtime.observer.error",
            "schema_version": "20260810.1",
            "code": "invalid_request",
            "message": "bad request",
        }, "20260810.1"), "invalid_request")
        self.assertEqual(INFER_RUNTIME_ADAPTER._error_code({
            "schema": "infer-runtime.status.error",
            "schema_version": "20260810.1",
            "error": {"code": "snapshot_unavailable"},
        }, "20260810.1"), "snapshot_unavailable")
        self.assertEqual(DEV_MESH_OBSERVER_ADAPTER._error_code({
            "schema": "dev-mesh.observer.status.error",
            "schema_version": "20260812.1",
            "error": {"code": "invalid_request"},
        }, "20260812.1"), "invalid_request")

    def test_provider_error_requires_matching_version_and_complete_shape(self) -> None:
        with self.assertRaises(FacilityProtocolError):
            PCP_ADAPTER._error_code({
                "schema_version": "wrong", "code": "invalid_request", "message": "bad",
            }, "20260810.1")
        with self.assertRaises(FacilityProtocolError):
            INFER_RUNTIME_ADAPTER._error_code({
                "schema_version": "20260810.1", "error": {},
            }, "20260810.1")

    def test_pcp_snapshot_normalizes_without_leaking_extensions(self) -> None:
        pcp = registration("pcp.runtime.observer", "pcp")
        observed = PCP_ADAPTER._normalize(snapshot(PCP_ADAPTER, pcp), pcp, "20260810.1")
        self.assertEqual(observed.status, "healthy")
        self.assertEqual(observed.console_url, "http://127.0.0.1:4318/")
        self.assertNotIn("extensions", observed.snapshot)

    def test_infer_snapshot_requires_discovery_generation_identity(self) -> None:
        infer = registration("infer-runtime.status", "infer-runtime")
        value = snapshot(INFER_RUNTIME_ADAPTER, infer)
        value["service"]["generation"] = "wrong"  # type: ignore[index]
        with self.assertRaises(FacilityProtocolError):
            INFER_RUNTIME_ADAPTER._normalize(value, infer, "20260810.1")

    def test_headline_must_reference_a_published_metric(self) -> None:
        pcp = registration("pcp.runtime.observer", "pcp")
        value = snapshot(PCP_ADAPTER, pcp)
        value["headline_metrics"] = ["missing"]
        with self.assertRaises(FacilityProtocolError):
            PCP_ADAPTER._normalize(value, pcp, "20260810.1")

    def test_provider_owned_metric_shapes_are_safely_projected(self) -> None:
        pcp = registration("pcp.runtime.observer", "pcp")
        value = snapshot(PCP_ADAPTER, pcp)
        value["metrics"][0]["dimensions"] = {"path": "x" * 200}  # type: ignore[index]
        value["metrics"].append({  # type: ignore[union-attr]
            "id": "provider.structured",
            "kind": "state",
            "value": {"provider": "owned"},
            "window_seconds": 0,
        })
        observed = PCP_ADAPTER._normalize(value, pcp, "20260810.1")
        self.assertNotIn("dimensions", observed.snapshot["metrics"][0])
        self.assertNotIn(
            "provider.structured",
            [metric["id"] for metric in observed.snapshot["metrics"]],
        )

    def test_required_provider_extension_and_redactions_are_enforced(self) -> None:
        infer = registration("infer-runtime.status", "infer-runtime")
        value = snapshot(INFER_RUNTIME_ADAPTER, infer)
        value["extensions"] = {}
        with self.assertRaises(FacilityProtocolError):
            INFER_RUNTIME_ADAPTER._normalize(value, infer, "20260810.1")

        dev_mesh = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            endpoint_token="dm-gen-1",
        )
        value = snapshot(DEV_MESH_OBSERVER_ADAPTER, dev_mesh)
        value["redaction"]["excluded"].remove("claim_scopes")  # type: ignore[index,union-attr]
        with self.assertRaises(FacilityProtocolError):
            DEV_MESH_OBSERVER_ADAPTER._normalize(
                value,
                dev_mesh,
                DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            )

    def test_dev_mesh_snapshot_projects_only_bounded_aggregates_and_loopback_console(self) -> None:
        dev_mesh = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            endpoint_token="dm-gen-1",
        )
        value = snapshot(DEV_MESH_OBSERVER_ADAPTER, dev_mesh)
        value["extensions"]["dev-mesh-observer"]["workspace_paths"] = [  # type: ignore[index]
            "/private/workspace"
        ]
        value["metrics"].append({  # type: ignore[union-attr]
            "id": "dev_mesh.future.aggregate",
            "kind": "gauge",
            "value": {"workspace": "/private/workspace"},
        })
        value["issues"].extend([  # type: ignore[union-attr]
            {
                "code": "dev_mesh.collection.failed",
                "severity": "warning",
                "observed_at": NOW.isoformat(),
                "subject_id": "observer",
            },
            {
                "code": "dev_mesh.collection.stale",
                "severity": "warning",
                "observed_at": NOW.isoformat(),
                "subject_id": "observer",
            },
            {
                "code": "dev_mesh.workspace.none_registered",
                "severity": "info",
                "observed_at": NOW.isoformat(),
                "subject_id": "observer",
            },
            {
                "code": "dev_mesh.future.issue",
                "severity": "warning",
                "observed_at": NOW.isoformat(),
                "subject_id": "/private/workspace",
            },
        ])

        observed = DEV_MESH_OBSERVER_ADAPTER._normalize(
            value,
            dev_mesh,
            DEV_MESH_OBSERVER_PROTOCOL_VERSION,
        )

        self.assertEqual(observed.label, "Dev Mesh Observer")
        self.assertEqual(observed.console_url, "http://127.0.0.1:4318/")
        self.assertEqual(
            [metric["id"] for metric in observed.snapshot["metrics"]],
            [
                "dev_mesh.workspaces.available",
                "dev_mesh.collection.pending_events",
                "dev_mesh.contentions.stalled",
            ],
        )
        self.assertEqual(
            observed.snapshot["issues"],
            [
                {
                    "code": "dev_mesh.collection.failed",
                    "severity": "warning",
                    "observed_at": NOW.isoformat(),
                    "subject_id": "observer",
                },
                {
                    "code": "dev_mesh.collection.stale",
                    "severity": "warning",
                    "observed_at": NOW.isoformat(),
                    "subject_id": "observer",
                },
                {
                    "code": "dev_mesh.workspace.none_registered",
                    "severity": "info",
                    "observed_at": NOW.isoformat(),
                    "subject_id": "observer",
                },
            ],
        )
        self.assertNotIn("extensions", observed.snapshot)
        self.assertNotIn("/private/workspace", json.dumps(observed.snapshot))

    def test_dev_mesh_issue_projection_stops_at_first_64_raw_issues(self) -> None:
        dev_mesh = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            endpoint_token="dm-gen-1",
        )
        value = snapshot(DEV_MESH_OBSERVER_ADAPTER, dev_mesh)
        value["issues"] = [
            {
                "code": f"dev_mesh.future.issue.{index}",
                "severity": "warning",
                "observed_at": NOW.isoformat(),
                "subject_id": "observer",
            }
            for index in range(64)
        ] + [{
            "code": "dev_mesh.collection.failed",
            "severity": "warning",
            "observed_at": NOW.isoformat(),
            "subject_id": "observer",
        }]

        observed = DEV_MESH_OBSERVER_ADAPTER._normalize(
            value,
            dev_mesh,
            DEV_MESH_OBSERVER_PROTOCOL_VERSION,
        )

        self.assertEqual(observed.snapshot["issues"], [])

    def test_dev_mesh_snapshot_requires_exact_headlines_and_aggregate_issue_subject(self) -> None:
        dev_mesh = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            endpoint_token="dm-gen-1",
        )
        value = snapshot(DEV_MESH_OBSERVER_ADAPTER, dev_mesh)
        value["headline_metrics"] = list(reversed(value["headline_metrics"]))  # type: ignore[arg-type]
        with self.assertRaises(FacilityProtocolError):
            DEV_MESH_OBSERVER_ADAPTER._normalize(
                value,
                dev_mesh,
                DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            )

        value = snapshot(DEV_MESH_OBSERVER_ADAPTER, dev_mesh)
        value["issues"] = [{
            "code": "dev_mesh.collection.failed",
            "severity": "warning",
            "observed_at": NOW.isoformat(),
            "subject_id": "workspace-secret",
        }]
        with self.assertRaises(FacilityProtocolError):
            DEV_MESH_OBSERVER_ADAPTER._normalize(
                value,
                dev_mesh,
                DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            )

    def test_dev_mesh_adapter_exchanges_one_exact_request_and_eof_completed_snapshot(self) -> None:
        root = Path("/tmp/infra-protocol")
        dev_mesh = registration(
            "dev-mesh.observer.status",
            "dev-mesh-observer",
            protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
            endpoint_token="dm-gen-1",
        )
        selected = DEV_MESH_OBSERVER_ADAPTER.select(dev_mesh)
        self.assertIsNotNone(selected)
        with patch(
            "infra_sentinel.resources.facilities.protocols._exchange_line",
            return_value=snapshot(DEV_MESH_OBSERVER_ADAPTER, dev_mesh),
        ) as exchange:
            observed = DEV_MESH_OBSERVER_ADAPTER.observe(
                DiscoveryPaths(root, root / "registrations", root / "sockets"),
                dev_mesh,
                selected,  # type: ignore[arg-type]
            )

        exchange.assert_called_once_with(
            root / "sockets" / "dm-gen-1.sock",
            _request("dev-mesh.observer.status.request", "20260812.1"),
            response_limit=256 * 1024,
            timeout=2.0,
            require_eof=True,
        )
        request = exchange.call_args.args[1]
        self.assertLessEqual(len(request), 512)
        self.assertTrue(request.endswith(b"\n"))
        self.assertEqual(observed.label, "Dev Mesh Observer")
        self.assertEqual(observed.sequence, 7)

    def test_pcp_frame_completes_at_lf_without_waiting_for_eof(self) -> None:
        reader, writer = socket.socketpair()
        try:
            writer.sendall(b'{"ok":true}\n')
            self.assertEqual(
                _read_response_frame(reader, response_limit=64, require_eof=False),
                b'{"ok":true}',
            )
        finally:
            reader.close()
            writer.close()

    def test_infer_frame_requires_eof_and_rejects_trailing_frame(self) -> None:
        reader, writer = socket.socketpair()
        sender = threading.Thread(target=lambda: (writer.sendall(b'{"ok":true}\n'), writer.close()))
        sender.start()
        try:
            self.assertEqual(
                _read_response_frame(reader, response_limit=64, require_eof=True),
                b'{"ok":true}',
            )
        finally:
            sender.join(timeout=1)
            reader.close()

        reader, writer = socket.socketpair()
        try:
            writer.sendall(b'{"ok":true}\n{}\n')
            with self.assertRaises(FacilityProtocolError):
                _read_response_frame(reader, response_limit=64, require_eof=True)
        finally:
            reader.close()
            writer.close()


if __name__ == "__main__":
    unittest.main()
