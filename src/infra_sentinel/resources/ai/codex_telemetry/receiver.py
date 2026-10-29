"""Small authenticated loopback listener; no body logging or unbounded queue."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import hmac
import json
from pathlib import Path
import socket
import threading
import time
import zlib

from .protocol import MAX_BODY_BYTES, parse_logs
from .state import TelemetryState

SETTINGS_FILENAME = "codex-telemetry-listener.json"
CHECKPOINT_FILENAME = "codex-telemetry-samples.json"
DEFAULT_PORT = 43182
MAX_WORKERS = 2
MAX_BYTES_PER_SECOND = 4 * MAX_BODY_BYTES


def read_settings(state_dir: Path) -> dict | None:
    path = state_dir / SETTINGS_FILENAME
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("settings-size")
    settings = json.loads(raw)
    if (not isinstance(settings, dict) or set(settings) != {"schema", "port", "token"}
            or type(settings["schema"]) is not int or settings["schema"] != 1 or type(settings["port"]) is not int
            or not 1024 <= settings["port"] <= 65535
            or not isinstance(settings["token"], str) or not 40 <= len(settings["token"]) <= 128
            or not all(c.isascii() and (c.isalnum() or c in "-_") for c in settings["token"])):
        raise ValueError("invalid-settings")
    return settings


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4
    allow_reuse_address = True

    def __init__(self, service, port):
        self.service = service
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)
        self.budget_lock = threading.Lock()
        self.budget_epoch = time.monotonic()
        self.budget_bytes = 0
        super().__init__(("127.0.0.1", port), _Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.service.state.reject()
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # HTTPServer's default error path prints traceback/request information.
        self.service.state.reject()

    def admit(self, size):
        with self.budget_lock:
            now = time.monotonic()
            if now - self.budget_epoch >= 1:
                self.budget_epoch, self.budget_bytes = now, 0
            if self.budget_bytes + size > MAX_BYTES_PER_SECOND:
                return False
            self.budget_bytes += size
            return True


class _Handler(BaseHTTPRequestHandler):
    # Close each batch connection: slow senders cannot occupy a persistent slot.
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(1)
        super().setup()

    def log_message(self, *args):
        pass

    def _reply(self, status):
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
        except (OSError, ValueError):
            pass

    def do_POST(self):
        service = self.server.service
        try:
            if self.path != "/v1/logs":
                self._reply(404)
                return
            if (self.headers.get("Origin") is not None or self.headers.get("Transfer-Encoding") is not None
                    or len(self.headers.get_all("Host", [])) != 1
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                    or len(self.headers.get_all("Authorization", [])) != 1
                    or not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + service.token).encode())):
                service.state.reject()
                self._reply(403)
                return
            lengths = self.headers.get_all("Content-Length", [])
            length = lengths[0] if len(lengths) == 1 else ""
            if not length.isascii() or not length.isdigit() or len(length) > 8:
                service.state.reject()
                self._reply(400)
                return
            size = int(length)
            if not 0 < size <= MAX_BODY_BYTES:
                service.state.reject()
                self._reply(413)
                return
            if not self.server.admit(size):
                service.state.reject()
                self._reply(429)
                return
            if self.headers.get_content_type() != "application/json":
                service.state.reject()
                self._reply(415)
                return
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("short-body")
            encoding = self.headers.get("Content-Encoding", "identity")
            if encoding == "gzip":
                inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = inflater.decompress(raw, MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES or not inflater.eof or inflater.unused_data:
                    raise ValueError("compressed-body-size")
            elif encoding != "identity":
                raise ValueError("content-encoding")
            events, records = parse_logs(raw, service.token.encode(), service.clock())
            # Only already-reduced numerical events enter shared receiver state.
            service.state.record_batch(events, records, size)
            self._reply(200)
        except (OSError, ValueError, UnicodeError, RecursionError, zlib.error):
            service.state.reject()
            self._reply(400)


class CodexTelemetryService:
    def __init__(self, state_dir: Path, *, clock=time.time):
        self.state_dir = state_dir
        self.clock = clock
        self.state = TelemetryState(state_dir / CHECKPOINT_FILENAME, clock=clock)
        self.server = None
        self.thread = None
        self.token = ""
        self.status = "disabled"

    def start(self):
        try:
            settings = read_settings(self.state_dir)
            if settings is None:
                return
            self.token = settings["token"]
            self.server = _Server(self, settings["port"])
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .25},
                                           name="codex-local-telemetry", daemon=True)
            self.thread.start()
            self.status = "waiting"
        except (OSError, ValueError, TypeError):
            self.status = "unavailable"

    def details(self):
        status = "receiving" if self.status == "waiting" and self.state.snapshot()["received_events"] else self.status
        try:
            self.state.checkpoint()
        except OSError:
            # A diagnostics checkpoint failure must never fail usage collection.
            status = "unavailable"
        if self.status == "disabled":
            return []
        return self.state.details(status)

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=1)
        try:
            self.state.checkpoint()
        except OSError:
            pass
