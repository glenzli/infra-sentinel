from __future__ import annotations

from datetime import datetime, timezone
from contextlib import redirect_stdout
import gzip
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.resources.ai.codex_telemetry.protocol import MAX_BODY_BYTES, MAX_RECORDS, parse_logs
from infra_sentinel.resources.ai.codex_telemetry.receiver import CodexTelemetryService, SETTINGS_FILENAME, _Server
from infra_sentinel.resources.ai.codex_telemetry.setup import configure
from infra_sentinel.resources.ai.codex_telemetry.state import MAX_MODELS, MAX_SEEN, MAX_STREAMS, TelemetryState
from infra_sentinel.cli.codex_telemetry import telemetry_state_directory

EPOCH = 1793242590
TOKEN = "local-test-token-" + "x" * 40


def log(kind, seconds, *, model="gpt-6-luna", session="private-conversation", output=83, ttft=880, duration=0, **extra):
    values = {"event.name": "codex.websocket_request" if kind in {"start", "failed"} else "codex.sse_event",
              "event.timestamp": datetime.fromtimestamp(EPOCH + seconds, timezone.utc).isoformat(timespec="milliseconds"),
              "conversation.id": session, "model": model, "user.email": "private@example.invalid",
              "user.account_id": "private-account", "prompt": "private-prompt", "arguments": "private-command",
              "output": "private-response", "code.file.path": "/private-project/secret"}
    if kind in {"start", "failed"}:
        values.update(success="false" if kind == "failed" else "true", duration_ms=str(duration))
    else:
        values.update({"event.kind": "response.completed", "output_token_count": str(output),
                       "reasoning_token_count": "0"})
        if ttft is not None:
            values["ttft_ms"] = ttft
    values.update(extra)
    return {"attributes": [{"key": k, "value": {"boolValue" if type(v) is bool else "intValue" if type(v) is int else "stringValue": v}}
                           for k, v in values.items()]}


def body(*logs):
    return json.dumps({"resourceLogs": [{"resource": {"attributes": [{"key": "host.name", "value": {"stringValue": "private-host"}}]},
                                        "scopeLogs": [{"logRecords": list(logs)}]}]}).encode()


def ingest(state, *records):
    raw = body(*records)
    events, count = parse_logs(raw, TOKEN.encode(), EPOCH + 100)
    state.record_batch(events, count, len(raw))


def rates(state):
    return {group["sample"]["model"]: next(m["value"] for m in group["metrics"] if m["unit"] == "tokens/s")
            for group in state.details("receiving") if group["id"] == "request-performance"}


class CorrelationTests(unittest.TestCase):
    def state(self, checkpoint=None):
        return TelemetryState(checkpoint, clock=lambda: EPOCH + 100)

    def test_real_observed_websocket_shape_excludes_tool_gap_and_warmup(self):
        state = self.state()
        records = [log("start", 0), log("complete", 1.185, output=0, ttft=None),
                   log("start", 2.800, duration=2), log("complete", 5.208, output=56, ttft=2014),
                   # The real probe's 2.132-second tool gap is not part of either request.
                   log("start", 7.340), log("complete", 8.970)]
        ingest(state, *records)
        self.assertAlmostEqual(rates(state)["gpt-6-luna"], 83 / 1.630, places=3)
        self.assertEqual(state.snapshot()["matched_requests"], 2)
        group = next(g for g in state.details("receiving") if g["id"] == "request-performance")
        self.assertIn("WebSocket", group["note"]["en"])
        self.assertNotIn("DSH", group["note"]["en"])
        self.assertFalse(any(g["id"] == "client-stream-performance" for g in state.details("receiving")))
        estimate = next(g for g in state.details("receiving") if g["id"] == "estimated-stream-performance")
        self.assertTrue(estimate["estimated"])
        self.assertEqual(estimate["sample"], group["sample"])
        self.assertAlmostEqual(estimate["metrics"][0]["value"], 83 / (1.630 - .880), places=3)
        ingest(state, *records)
        self.assertEqual(state.snapshot()["matched_requests"], 2)

    def test_missing_failed_overlapping_and_reordered_events_never_guess(self):
        cases = [[log("complete", 3)],
                 [log("start", 1), log("failed", 2), log("complete", 3)],
                 [log("start", 1), log("start", 2), log("complete", 3)],
                 [log("start", 3), log("complete", 3)]]
        for records in cases:
            with self.subTest(records=len(records)):
                state = self.state(); ingest(state, *records)
                self.assertEqual(rates(state), {})
        state = self.state()
        ingest(state, log("start", 1), log("complete", 3))
        self.assertTrue(rates(state))
        ingest(state, log("start", 2))
        self.assertEqual(rates(state), {})
        ingest(state, log("complete", 4), log("start", 5), log("complete", 7))
        self.assertTrue(rates(state))

    def test_concurrent_sessions_and_models_remain_separate(self):
        state = self.state()
        ingest(state, log("start", 1, model="gpt-6-astra"), log("start", 2, session="other"),
               log("complete", 3, model="gpt-6-astra", output=200), log("complete", 4, session="other", output=80))
        self.assertEqual(rates(state), {"gpt-6-astra": 100, "gpt-6-luna": 40})
        ingest(state, log("start", 5, session="third"), log("complete", 6, session="third", output=60))
        self.assertEqual(rates(state)["gpt-6-luna"], 60)

    def test_invalid_ttft_does_not_produce_latency_or_estimated_stream(self):
        state = self.state()
        ingest(state, log("start", 1), log("complete", 2, ttft=5000))
        groups = state.details("receiving")
        group = next(g for g in groups if g["id"] == "request-performance")
        self.assertNotIn("first-token-latency", {m["id"] for m in group["metrics"]})
        self.assertFalse(any(g["id"] == "estimated-stream-performance" for g in groups))
        self.assertEqual(rates(state)["gpt-6-luna"], 83)

    def test_checkpoint_is_numeric_only_private_bounded_and_not_written_per_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.json"
            state = self.state(path)
            ingest(state, log("start", 1), log("complete", 3))
            self.assertFalse(path.exists())
            state.checkpoint()
            raw = path.read_text()
            self.assertEqual(set(json.loads(raw)["samples"][0]), {
                "recorded_epoch", "model", "output_tokens", "duration_ms", "first_token_ms", "streaming_ms",
            })
            for private in ["private-conversation", "private-account", "private@example", "private-prompt", "private-command", "private-response", "/private-project", TOKEN]:
                self.assertNotIn(private, raw)
                self.assertNotIn(private, json.dumps(state.details("receiving")))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            mtime = path.stat().st_mtime_ns
            ingest(state, log("start", 4))
            state.checkpoint()
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            restored = self.state(path)
            self.assertEqual(rates(restored), rates(state))
            path.write_text('{"schema":1,"samples":[{"model":"private"}]}')
            self.assertEqual(rates(self.state(path)), {})

    def test_state_cardinality_is_bounded(self):
        state = self.state()
        for n in range(MAX_SEEN + 3):
            events, count = parse_logs(body(log("start", n / 1000, session=f"session-{n}", model=f"gpt-test-{n}"),
                                            log("complete", n / 1000 + .01, session=f"session-{n}", model=f"gpt-test-{n}")),
                                       TOKEN.encode(), EPOCH + 100)
            state.record_batch(events, count, 1)
        snapshot = state.snapshot()
        self.assertLessEqual(snapshot["pending_streams"], MAX_STREAMS)
        self.assertLessEqual(snapshot["dedup_entries"], MAX_SEEN)
        self.assertLessEqual(snapshot["model_count"], MAX_MODELS)

    def test_invalidating_models_releases_origin_state(self):
        state = self.state()
        for index in range(MAX_MODELS + 5):
            model = f"gpt-test-{index}"
            ingest(state, log("start", 1, model=model), log("complete", 3, model=model))
            ingest(state, log("start", 2, model=model))
        self.assertEqual(rates(state), {})
        self.assertEqual(len(state._origins), 0)

    def test_expired_samples_are_removed_from_running_and_reloaded_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.json"
            state = self.state(path)
            ingest(state, log("start", 1), log("complete", 3))
            state.checkpoint()
            original = path.read_text()
            state._clock = lambda: EPOCH + 8 * 86400
            state.checkpoint()
            self.assertEqual(json.loads(path.read_text())["samples"], [])
            path.write_text(original)
            service = CodexTelemetryService(Path(directory), clock=lambda: EPOCH + 8 * 86400)
            service.state = TelemetryState(path, clock=service.clock)
            self.assertEqual(service.details(), [])  # Expiry also works with collection disabled.
            self.assertEqual(json.loads(path.read_text())["samples"], [])


class AdmissionTests(unittest.TestCase):
    def test_unknown_content_never_enters_normalized_events(self):
        events, records = parse_logs(body(log("start", 1), log("complete", 3)), TOKEN.encode(), EPOCH + 100)
        self.assertEqual(records, 2)
        self.assertEqual(len(events), 2)
        self.assertNotIn("private", repr(events))
        self.assertNotIn("private-conversation", repr(events))
        other, _ = parse_logs(body(log("start", 1)), b"different-key", EPOCH + 100)
        self.assertNotEqual(events[0].session, other[0].session)

    def test_invalid_counters_timestamps_models_and_duplicate_fields_are_ignored(self):
        for output in ["NaN", "-1", "1.5", "9007199254740992"]:
            raw = body(log("complete", 3, output=output))
            self.assertEqual(parse_logs(raw, TOKEN.encode(), EPOCH + 100)[0], [])
        for extra in [{"event.timestamp": "2026-10-29T00:00:00"}, {"model": "../private/path"},
                      {"event.timestamp": "2099-01-01T00:00:00Z"}]:
            self.assertEqual(parse_logs(body(log("start", 1, **extra)), TOKEN.encode(), EPOCH + 100)[0], [])
        record = log("start", 1)
        record["attributes"].append(record["attributes"][0])
        self.assertEqual(parse_logs(body(record), TOKEN.encode(), EPOCH + 100)[0], [])

    def test_body_and_envelope_limits(self):
        for raw in [b"x" * (MAX_BODY_BYTES + 1), b"null", b"{", body(*[{}] * (MAX_RECORDS + 1))]:
            with self.assertRaises(ValueError):
                parse_logs(raw, TOKEN.encode(), EPOCH + 100)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = CodexTelemetryService(Path(self.directory.name), clock=lambda: EPOCH + 100)
        self.service.token = TOKEN
        self.server = _Server(self.service, 0)
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.directory.cleanup()

    def post(self, payload, *, path="/v1/logs", **extra):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN, **extra}
        try:
            connection.request("POST", path, payload, headers)
            response = connection.getresponse(); status = response.status; response.read()
            return status
        finally:
            connection.close()

    def test_authenticated_http_produces_sample_without_retaining_payload(self):
        self.assertEqual(self.post(body(log("start", 1), log("complete", 3))), 200)
        self.assertAlmostEqual(rates(self.service.state)["gpt-6-luna"], 41.5)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_auth_origin_host_and_endpoint_admission(self):
        raw = body(log("start", 1))
        self.assertEqual(self.post(raw, Authorization="Bearer wrong"), 403)
        self.assertEqual(self.post(raw, Origin="https://example.invalid"), 403)
        self.assertEqual(self.post(raw, Host="example.invalid"), 403)
        self.assertEqual(self.post(raw, path="/v1/traces"), 404)
        self.assertEqual(self.service.state.snapshot()["received_events"], 0)

    def test_compression_and_bomb_limits(self):
        self.assertEqual(self.post(gzip.compress(body(log("start", 1), log("complete", 3))), **{"Content-Encoding": "gzip"}), 200)
        self.assertEqual(self.post(gzip.compress(b"x" * (MAX_BODY_BYTES + 1)), **{"Content-Encoding": "gzip"}), 400)
        self.assertEqual(self.post(b"", **{"Content-Length": str(MAX_BODY_BYTES + 1)}), 413)

    def test_disabled_and_occupied_listener_do_not_fail_service_lifecycle(self):
        service = CodexTelemetryService(Path(self.directory.name))
        service.start(); self.assertEqual(service.status, "disabled"); service.stop()
        (Path(self.directory.name) / SETTINGS_FILENAME).write_text(json.dumps({"schema": 1, "port": self.server.server_port, "token": TOKEN}))
        service.start(); self.assertEqual(service.status, "unavailable"); service.stop()


class SetupTests(unittest.TestCase):
    def test_setup_defaults_to_desktop_state_instead_of_source_or_bundle_directory(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("/user")):
            for platform, expected in [("darwin", "/user/Library/Application Support/Infra Sentinel/state"),
                                       ("win32", "/user/AppData/Local/Infra Sentinel/state"),
                                       ("linux", "/user/.local/share/Infra Sentinel/state")]:
                with self.subTest(platform=platform), patch("sys.platform", platform):
                    self.assertEqual(telemetry_state_directory(), Path(expected))
            self.assertEqual(telemetry_state_directory(Path("/private/profile/config.toml")), Path("/private/profile/state"))
        with patch.dict(os.environ, {"INFRA_SENTINEL_STATE_DIR": "/private/overridden/state"}):
            self.assertEqual(telemetry_state_directory(Path("/ignored/config.toml")), Path("/private/overridden/state"))
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/private/data"}, clear=True), patch("sys.platform", "linux"):
            self.assertEqual(telemetry_state_directory(), Path("/private/data/Infra Sentinel/state"))
        with patch.dict(os.environ, {"LOCALAPPDATA": "/private/local"}, clear=True), patch("sys.platform", "win32"):
            self.assertEqual(telemetry_state_directory(), Path("/private/local/Infra Sentinel/state"))

    def test_packaged_entry_point_uses_desktop_state_without_loading_bundle_example(self):
        from infra_sentinel.app import agent
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("/user")), \
                patch("sys.platform", "darwin"), patch("sys.argv", ["infra-agent", "--codex-telemetry", "status"]), \
                patch("infra_sentinel.cli.codex_telemetry.run_action", return_value={"configured": False}) as action, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(agent.main(), 0)
        action.assert_called_once_with("status", Path("/user/Library/Application Support/Infra Sentinel/state"))

    def test_opt_in_is_idempotent_preserves_user_settings_and_disables_only_owned_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / "config.toml"; state = root / "state"
            original = 'model = "gpt-6-astra"\n[features]\nhooks = true\n'
            config.write_text(original)
            configure("enable", state, config)
            enabled = config.read_text(); settings = json.loads((state / SETTINGS_FILENAME).read_text())
            self.assertIn(original.rstrip(), enabled)
            self.assertIn('trace_exporter = "none"', enabled)
            self.assertIn(settings["token"], enabled)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            configure("enable", state, config)
            self.assertEqual(config.read_text(), enabled)
            self.assertTrue(configure("status", state, config)["configured"])
            configure("disable", state, config)
            self.assertIn(original.rstrip(), config.read_text())
            self.assertFalse((state / SETTINGS_FILENAME).exists())
            configure("enable", state, config)
            self.assertTrue(configure("status", state, config)["configured"])

    def test_existing_or_modified_exporters_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / "config.toml"; state = root / "state"
            original = '[otel]\nexporter = "none"\n'; config.write_text(original)
            with self.assertRaises(ValueError): configure("enable", state, config)
            self.assertEqual(config.read_text(), original)
            config.write_text('model = "gpt-6-astra"\n')
            configure("enable", state, config)
            config.write_text(config.read_text().replace('protocol = "json"', 'protocol = "binary"'))
            modified = config.read_text()
            with self.assertRaises(ValueError): configure("disable", state, config)
            self.assertEqual(config.read_text(), modified)


if __name__ == "__main__":
    unittest.main()
