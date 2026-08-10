from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
import threading
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from facility_protocols import (  # noqa: E402
    FacilityProtocolError,
    INFER_RUNTIME_ADAPTER,
    PCP_ADAPTER,
    _read_response_frame,
    _request,
    select_adapter,
)
from infra_discovery import DiscoveryOffer, Registration  # noqa: E402


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def registration(protocol: str, kind: str, generation: str = "gen-1") -> Registration:
    return Registration(
        path=Path(f"/tmp/{kind}--local.json"),
        kind=kind,
        instance_id="local",
        generation=generation,
        renewed_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=45)).isoformat(),
        renewed=NOW,
        expires=NOW + timedelta(seconds=45),
        offers=(DiscoveryOffer(
            protocol,
            ("20260810.1",),
            "infra.local.unix-socket",
            f"sockets/{kind}-gen-1.sock",
        ),),
    )


def snapshot(adapter, registration_value: Registration) -> dict[str, object]:
    if adapter is PCP_ADAPTER:
        redactions = ["page_content", "query_text", "scope_names", "raw_audit", "storage_paths"]
        extensions = {"pcp": {"ignored": True}}
    else:
        redactions = [
            "credentials", "filesystem_paths", "job_identifiers", "job_metadata",
            "payloads", "raw_errors", "usage_ledger",
        ]
        extensions = {"infer-runtime": {"ignored": True}}
    return {
        "schema": adapter.snapshot_schema,
        "schema_version": "20260810.1",
        "service": {
            "kind": registration_value.kind,
            "instance_id": registration_value.instance_id,
            "generation": registration_value.generation,
        },
        "sequence": 7,
        "captured_at": NOW.isoformat(),
        "status": {"state": "healthy", "reason_codes": []},
        "headline_metrics": ["workload.active"],
        "metrics": [{"id": "workload.active", "kind": "gauge", "value": 2, "unit": "count"}],
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

    def test_selection_uses_exact_protocol_version_and_binding(self) -> None:
        pcp = registration("pcp.runtime.observer", "pcp")
        selected = select_adapter(pcp)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.adapter, PCP_ADAPTER)  # type: ignore[union-attr]
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
