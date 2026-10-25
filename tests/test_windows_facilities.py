from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from infra_sentinel.resources.facilities.discovery import (
    DiscoveryError, DiscoveryOffer, Registration, WINDOWS_PIPE_BINDING,
    discovery_paths, read_registration, resolve_windows_pipe, validate_runtime_paths,
)
from infra_sentinel.resources.facilities.observer import FacilityMonitor
from infra_sentinel.resources.facilities.protocols import (
    DEFAULT_ADAPTERS, PCP_ADAPTER, DEV_MESH_OBSERVER_ADAPTER,
    FacilityProtocolError, FacilityTransportError, _exchange_pipe,
)
from test_facility_protocols import snapshot
from windows_facility_fixtures import PipeServer, make_private


def registered(root, adapter, endpoint, generation="gen-1"):
    return Registration(root / "registrations" / "test--local.json", "test", "local", generation,
                        (DiscoveryOffer(adapter.protocol, (adapter.protocol_version,), WINDOWS_PIPE_BINDING, endpoint),))


def publish(value):
    document = {
        "schema": "infra.discovery.registration", "schema_version": "20260812.1",
        "service": {"kind": value.kind, "instance_id": value.instance_id, "generation": value.generation},
        "offers": [{"protocol": offer.protocol, "protocol_versions": list(offer.protocol_versions),
                    "binding": offer.binding, "endpoint": offer.endpoint} for offer in value.offers],
    }
    temporary = value.path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document), encoding="utf-8")
    make_private(temporary)
    temporary.replace(value.path)


def private_runtime(root):
    root.mkdir(exist_ok=True)
    make_private(root)
    (root / "registrations").mkdir(exist_ok=True)
    make_private(root / "registrations")


class WindowsBindingSelectionTests(unittest.TestCase):
    def test_only_canonical_local_namespace_is_resolved(self):
        for endpoint in (r"\\remote\pipe\infra-protocol\test", r"\\.\pipe\other\test",
                         r"\\.\pipe\infra-protocol\..\test", r"\\?\pipe\infra-protocol\test"):
            with self.subTest(endpoint=endpoint), self.assertRaises(DiscoveryError):
                resolve_windows_pipe(DiscoveryOffer("test", ("1",), WINDOWS_PIPE_BINDING, endpoint))

    def test_selection_uses_native_binding_and_exact_application_version(self):
        root = Path(".")
        value = registered(root, PCP_ADAPTER, r"\\.\pipe\infra-protocol\test")
        with patch("infra_sentinel.resources.facilities.protocols._local_bindings",
                   return_value=(WINDOWS_PIPE_BINDING,)):
            self.assertIsNotNone(PCP_ADAPTER.select(value))
            self.assertIsNone(PCP_ADAPTER.select(replace(value, offers=(replace(value.offers[0], protocol_versions=("future",)),))))
        with patch("infra_sentinel.resources.facilities.protocols._local_bindings",
                   return_value=("infra.local.unix-socket",)):
            self.assertIsNone(PCP_ADAPTER.select(value))


@unittest.skipUnless(os.name == "nt", "native Windows named pipes and ACLs")
class WindowsFacilityTests(unittest.TestCase):
    def test_all_three_provider_protocols_cross_real_byte_pipes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for adapter in DEFAULT_ADAPTERS:
                with self.subTest(protocol=adapter.protocol):
                    value = registered(root, adapter, "")
                    response = snapshot(adapter, value)
                    response["extensions"][adapter.extension_key]["private_test_marker"] = "PRIVATE_NOT_PROJECTED"
                    with PipeServer(json.dumps(response).encode() + b"\n", chunks=47) as server:
                        value = registered(root, adapter, server.endpoint)
                        observed = adapter.observe(discovery_paths(root), value, adapter.select(value))
                        self.assertEqual(json.loads(server.request), {"schema": adapter.request_schema,
                            "schema_version": adapter.protocol_version, "operation": "snapshot"})
                        self.assertEqual(observed.status, "healthy")
                        self.assertEqual(observed.sequence, 7)
                        self.assertNotIn("PRIVATE_NOT_PROJECTED", json.dumps(observed.snapshot))

    def test_lf_completion_and_eof_completion_remain_distinct(self):
        with PipeServer(b'{"ok":true}\n', hold=2) as server:
            start = time.monotonic()
            self.assertEqual(_exchange_pipe(server.endpoint, b"request\n", response_limit=100,
                                           timeout=0.4, require_eof=False), {"ok": True})
            self.assertLess(time.monotonic() - start, 0.4)
        with PipeServer(b'{"ok":true}\n', hold=2) as server:
            start = time.monotonic()
            with self.assertRaises(FacilityTransportError):
                _exchange_pipe(server.endpoint, b"request\n", response_limit=100, timeout=0.15, require_eof=True)
            self.assertLess(time.monotonic() - start, 0.7)

    def test_total_deadline_stops_a_slow_partial_response(self):
        with PipeServer(b'{"ok":true}\n', chunks=1, delay=0.06) as server:
            start = time.monotonic()
            with self.assertRaises(FacilityTransportError):
                _exchange_pipe(server.endpoint, b"request\n", response_limit=100, timeout=0.18, require_eof=False)
            self.assertLess(time.monotonic() - start, 0.7)

    def test_busy_and_missing_pipe_connections_are_bounded(self):
        from infra_sentinel.resources.facilities.windows_pipe import WindowsPipeConnection
        with PipeServer(b"{}\n", hold=2) as server:
            with WindowsPipeConnection(server.endpoint, 1) as first:
                first.sendall(b"request\n")
                self.assertEqual(first.recv(20), b"{}\n")
                start = time.monotonic()
                with self.assertRaises(FacilityTransportError):
                    _exchange_pipe(server.endpoint, b"request\n", response_limit=100, timeout=0.08, require_eof=False)
                self.assertLess(time.monotonic() - start, 0.5)
            missing = server.endpoint
        with self.assertRaises(FacilityTransportError):
            _exchange_pipe(missing, b"request\n", response_limit=100, timeout=0.1, require_eof=False)

    def test_bounded_frames_strict_json_and_single_response(self):
        cases = [(b"{}", 100), (b"x" * 101, 100), (b"{}\ntrailing", 100),
                 (b'{}\n{}\n', 100), (b'{"a":1,"a":2}\n', 100), (b"\xff\n", 100)]
        for response, limit in cases:
            with self.subTest(response=response), PipeServer(response) as server:
                with self.assertRaises(FacilityProtocolError):
                    _exchange_pipe(server.endpoint, b"request\n", response_limit=limit, timeout=1, require_eof=True)

    def test_message_pipe_and_different_server_user_are_rejected_before_request(self):
        from infra_sentinel.resources.facilities import windows_security as security
        from infra_sentinel.resources.facilities import windows_pipe as pipe
        with PipeServer(b"{}\n", message_mode=True) as server:
            with self.assertRaises(FacilityTransportError):
                _exchange_pipe(server.endpoint, b"request\n", response_limit=100, timeout=1, require_eof=False)
            self.assertEqual(server.request, b"")
        with PipeServer(b"{}\n") as server:
            with pipe.WindowsPipeConnection(server.endpoint, 1) as connection:
                self.assertTrue(server.admitted.wait(1))
                user = security.current_user_sid()
                with patch.object(security, "_process_user", return_value=b"different-user"), \
                     patch.object(security, "current_user_sid", return_value=user), self.assertRaises(PermissionError):
                    security.validate_pipe_server(connection._handle)
            self.assertEqual(server.request, b"")
        with PipeServer(b"{}\n") as server:
            with patch.object(pipe, "validate_pipe_server", side_effect=PermissionError("different user")), \
                 self.assertRaises(FacilityTransportError):
                _exchange_pipe(server.endpoint, b"request\n", response_limit=100, timeout=1, require_eof=False)
            self.assertEqual(server.request, b"")

    def test_private_acl_required_for_files_and_runtime_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_runtime(root)
            value = registered(root, PCP_ADAPTER, r"\\.\pipe\infra-protocol\test")
            publish(value)
            validate_runtime_paths(discovery_paths(root))
            self.assertEqual(read_registration(value.path).generation, "gen-1")
            make_private(value.path, broad=True)
            with self.assertRaises(DiscoveryError):
                read_registration(value.path)
            make_private(value.path)
            make_private(root / "registrations", broad=True)
            with self.assertRaises(DiscoveryError):
                validate_runtime_paths(discovery_paths(root))

    def test_junction_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, alias = root / "real", root / "alias"
            private_runtime(target)
            value = registered(target, PCP_ADAPTER, r"\\.\pipe\infra-protocol\test")
            publish(value)
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with self.assertRaises(DiscoveryError):
                    read_registration(alias / "registrations" / value.path.name)
            finally:
                alias.rmdir()  # Remove only the junction, never its target tree.

    def test_monitor_reconciles_generation_rejects_races_and_removes_missing_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_runtime(root)
            monitor = FacilityMonitor(logging.getLogger("windows-facility-test"), root)
            now = datetime.now(timezone.utc).timestamp()
            for index in (1, 2):
                value = registered(root, PCP_ADAPTER, "", generation=f"gen-{index}")
                with PipeServer(json.dumps(snapshot(PCP_ADAPTER, value)).encode() + b"\n") as server:
                    value = replace(value, offers=(replace(value.offers[0], endpoint=server.endpoint),))
                    publish(value)
                    monitor.refresh_once(now + index)
                    state = monitor.snapshot()
                    self.assertEqual(state["healthy"], 1)
                    self.assertEqual(state["items"][0]["generation"], f"gen-{index}")
            value = registered(root, PCP_ADAPTER, "", generation="gen-race")
            with PipeServer(json.dumps(snapshot(PCP_ADAPTER, value)).encode() + b"\n",
                            before_reply=lambda: publish(replace(value, generation="gen-new"))) as server:
                value = replace(value, offers=(replace(value.offers[0], endpoint=server.endpoint),))
                publish(value)
                monitor.refresh_once(now + 3)
                state = monitor.snapshot()
                self.assertEqual(state["items"][0]["status"], "unreachable")
                self.assertNotIn("snapshot", state["items"][0])
            value.path.unlink()
            monitor.refresh_once(now + 4)
            self.assertEqual(monitor.snapshot()["total"], 0)

    def test_timeouts_release_pipe_and_event_handles(self):
        import ctypes
        from ctypes import wintypes as W
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = W.HANDLE
        kernel.GetProcessHandleCount.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
        kernel.GetProcessHandleCount.restype = W.BOOL

        def handles():
            count = W.DWORD()
            self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)))
            return count.value

        before = handles()
        for _ in range(8):
            with PipeServer(b"", hold=2) as server:
                with self.assertRaises(FacilityTransportError):
                    _exchange_pipe(server.endpoint, b"request\n", response_limit=100, timeout=0.04, require_eof=True)
        self.assertLessEqual(handles(), before + 2)


if __name__ == "__main__":
    unittest.main()
