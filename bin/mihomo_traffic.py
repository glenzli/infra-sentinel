"""Exact Mihomo totals and privacy-bounded domain attribution over a local socket."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import time
from typing import Any


MIHOMO_SAMPLE_SCHEMA = 1
MIHOMO_TRACKER_SCHEMA = 1
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_SOCKET_CANDIDATES = (
    Path("/tmp/verge/verge-mihomo.sock"),
    Path("/tmp/clash-verge/verge-mihomo.sock"),
)

# Product labels are intentionally broad. For example, Google API traffic is
# called Google rather than Antigravity because a hostname alone cannot prove
# which Google client originated a request.
KNOWN_SERVICES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "chatgpt",
        "ChatGPT",
        (
            "chatgpt.com",
            "openai.com",
            "oaistatic.com",
            "oaiusercontent.com",
        ),
    ),
    (
        "google",
        "Google",
        (
            "google.com",
            "googleapis.com",
            "googleusercontent.com",
            "gstatic.com",
        ),
    ),
    (
        "github",
        "GitHub",
        (
            "github.com",
            "githubusercontent.com",
            "githubassets.com",
        ),
    ),
)

# This is not a public-suffix implementation. It only prevents the common
# fallback display from collapsing domains such as example.com.cn to com.cn.
COMMON_TWO_LEVEL_SUFFIXES = {
    "co.jp",
    "co.kr",
    "co.nz",
    "co.uk",
    "com.au",
    "com.br",
    "com.cn",
    "com.hk",
    "com.sg",
    "com.tw",
    "net.cn",
    "org.cn",
}


def iso_now(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _is_socket(path: Path) -> bool:
    try:
        metadata = path.stat()
        return stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid()
    except OSError:
        return False


def discover_mihomo_socket() -> Path:
    """Find a loopback-only Mihomo controller without user configuration."""
    for candidate in DEFAULT_SOCKET_CANDIDATES:
        if _is_socket(candidate):
            return candidate
    dynamic: list[Path] = []
    for pattern in ("/tmp/*/*mihomo*.sock", "/tmp/*mihomo*.sock"):
        dynamic.extend(Path("/").glob(pattern.removeprefix("/")))
    for candidate in sorted(set(dynamic), key=str):
        if _is_socket(candidate):
            return candidate
    raise RuntimeError("未找到 Mihomo 本机控制接口；请先启动 Clash Verge / Mihomo")


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    offset = 0
    while True:
        line_end = body.find(b"\r\n", offset)
        if line_end < 0:
            raise RuntimeError("Mihomo API 返回了无效的分块响应")
        try:
            size = int(body[offset:line_end].split(b";", 1)[0], 16)
        except ValueError as exc:
            raise RuntimeError("Mihomo API 返回了无效的分块长度") from exc
        offset = line_end + 2
        if size == 0:
            return bytes(decoded)
        chunk_end = offset + size
        if chunk_end > len(body):
            raise RuntimeError("Mihomo API 分块响应不完整")
        decoded.extend(body[offset:chunk_end])
        offset = chunk_end + 2


class MihomoApiClient:
    """Read-only client for Clash Verge's local Unix-domain controller."""

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("Mihomo API 响应上限必须大于 0")
        self.socket_path = socket_path
        self.max_response_bytes = int(max_response_bytes)

    def _request(self, path: str) -> dict[str, Any]:
        socket_path = self.socket_path if self.socket_path is not None and _is_socket(self.socket_path) else discover_mihomo_socket()
        self.socket_path = socket_path
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(3.0)
        try:
            connection.connect(str(socket_path))
            request = (
                f"GET {path} HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Accept: application/json\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            connection.sendall(request)
            parts: list[bytes] = []
            received_bytes = 0
            while True:
                part = connection.recv(262_144)
                if not part:
                    break
                received_bytes += len(part)
                if received_bytes > self.max_response_bytes:
                    self.socket_path = None
                    raise RuntimeError(
                        "Mihomo API 响应超过安全上限"
                        f"（{self.max_response_bytes} 字节）"
                    )
                parts.append(part)
        except OSError as exc:
            self.socket_path = None
            raise RuntimeError(f"无法读取 Mihomo 本机接口：{exc}") from exc
        finally:
            connection.close()

        raw = b"".join(parts)
        try:
            header, body = raw.split(b"\r\n\r\n", 1)
        except ValueError as exc:
            raise RuntimeError("Mihomo API 响应不完整") from exc
        status = header.splitlines()[0].decode("ascii", errors="replace") if header else ""
        if " 200 " not in status:
            raise RuntimeError(f"Mihomo API 返回 {status or '未知状态'}")
        if b"transfer-encoding: chunked" in header.lower():
            body = _decode_chunked(body)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Mihomo API 返回的不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Mihomo API 返回结构异常")
        return payload

    def connections(self) -> dict[str, Any]:
        return self._request("/connections")


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _fallback_site(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    tail = ".".join(labels[-2:])
    if tail in COMMON_TWO_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return tail


def classify_host(raw_host: Any) -> tuple[str, str, str]:
    """Return a stable service id, display label, and normalized hostname."""
    host = str(raw_host or "").strip().lower().rstrip(".")
    if not host:
        return "unknown_host", "Unknown host", ""
    for service_id, label, suffixes in KNOWN_SERVICES:
        if any(_host_matches(host, suffix) for suffix in suffixes):
            return service_id, label, host
    site = _fallback_site(host)
    digest = hashlib.sha256(site.encode("utf-8")).hexdigest()[:12]
    return f"domain_{digest}", site, host


def classify_route(raw_chains: Any) -> str:
    chains = [str(item).strip().upper() for item in raw_chains] if isinstance(raw_chains, list) else []
    if any(item.startswith("REJECT") for item in chains):
        return "blocked"
    if "DIRECT" in chains:
        return "direct"
    if chains:
        return "proxy"
    return "unknown"


def _safe_delta(current: int, previous: int | None) -> int:
    return current - previous if previous is not None and current >= previous else 0


def _scaled(values: list[int], target: int) -> list[int]:
    """Scale non-negative observations down so they never exceed a global total."""
    total = sum(values)
    if total <= target:
        return values
    if target <= 0 or total <= 0:
        return [0 for _ in values]
    scaled = [(value * target) // total for value in values]
    remaining = target - sum(scaled)
    order = sorted(range(len(values)), key=lambda index: values[index], reverse=True)
    for index in order[:remaining]:
        scaled[index] += 1
    return scaled


class MihomoTrafficTracker:
    """Reconcile exact core totals with best-effort per-connection attribution."""

    def __init__(
        self,
        previous_up: int | None = None,
        previous_down: int | None = None,
        previous_epoch: float | None = None,
        previous_connections: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.previous_up = previous_up
        self.previous_down = previous_down
        self.previous_epoch = previous_epoch
        self.previous_connections = previous_connections or {}

    def apply(self, payload: dict[str, Any], epoch: float) -> dict[str, Any]:
        current_up = max(0, int(payload.get("uploadTotal", 0)))
        current_down = max(0, int(payload.get("downloadTotal", 0)))
        counters_ready = (
            self.previous_up is not None
            and self.previous_down is not None
            and current_up >= self.previous_up
            and current_down >= self.previous_down
        )
        global_up = current_up - self.previous_up if counters_ready else 0
        global_down = current_down - self.previous_down if counters_ready else 0

        current_connections: dict[str, dict[str, int]] = {}
        observed: list[dict[str, Any]] = []
        raw_connections = payload.get("connections", [])
        if not isinstance(raw_connections, list):
            raw_connections = []
        for raw_connection in raw_connections:
            if not isinstance(raw_connection, dict):
                continue
            connection_id = str(raw_connection.get("id", "")).strip()
            if not connection_id:
                continue
            upload = max(0, int(raw_connection.get("upload", 0)))
            download = max(0, int(raw_connection.get("download", 0)))
            current_connections[connection_id] = {"up_bytes": upload, "down_bytes": download}
            prior = self.previous_connections.get(connection_id)
            if not counters_ready:
                up_delta = down_delta = 0
            elif prior is None:
                # A new connection was created after the previous snapshot. Its
                # current counters belong to the elapsed interval.
                up_delta, down_delta = upload, download
            else:
                up_delta = _safe_delta(upload, prior.get("up_bytes"))
                down_delta = _safe_delta(download, prior.get("down_bytes"))
            if up_delta <= 0 and down_delta <= 0:
                continue
            metadata = raw_connection.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            host = metadata.get("host") or metadata.get("destinationIP") or ""
            service_id, label, normalized_host = classify_host(host)
            observed.append({
                "service_id": service_id,
                "label": label,
                "host": normalized_host,
                "route": classify_route(raw_connection.get("chains")),
                "up_bytes": up_delta,
                "down_bytes": down_delta,
            })

        scaled_up = _scaled([int(item["up_bytes"]) for item in observed], global_up)
        scaled_down = _scaled([int(item["down_bytes"]) for item in observed], global_down)
        services: dict[str, dict[str, Any]] = {}
        routes: dict[str, dict[str, int]] = {
            route: {"up_bytes": 0, "down_bytes": 0}
            for route in ("proxy", "direct", "blocked", "unknown", "unattributed")
        }
        for index, item in enumerate(observed):
            up_bytes = scaled_up[index]
            down_bytes = scaled_down[index]
            service = services.setdefault(item["service_id"], {
                "id": item["service_id"],
                "label": item["label"],
                "up_bytes": 0,
                "down_bytes": 0,
                "hosts": set(),
            })
            service["up_bytes"] += up_bytes
            service["down_bytes"] += down_bytes
            if item["host"] and len(service["hosts"]) < 8:
                service["hosts"].add(item["host"])
            route = item["route"] if item["route"] in routes else "unknown"
            routes[route]["up_bytes"] += up_bytes
            routes[route]["down_bytes"] += down_bytes

        observed_up = sum(scaled_up)
        observed_down = sum(scaled_down)
        residual_up = max(0, global_up - observed_up)
        residual_down = max(0, global_down - observed_down)
        if residual_up or residual_down:
            services["unattributed"] = {
                "id": "unattributed",
                "label": "Unattributed",
                "up_bytes": residual_up,
                "down_bytes": residual_down,
                "hosts": set(),
            }
            routes["unattributed"]["up_bytes"] = residual_up
            routes["unattributed"]["down_bytes"] = residual_down

        service_rows = []
        for service in services.values():
            up_bytes = int(service["up_bytes"])
            down_bytes = int(service["down_bytes"])
            service_rows.append({
                "id": service["id"],
                "label": service["label"],
                "up_bytes": up_bytes,
                "down_bytes": down_bytes,
                "total_bytes": up_bytes + down_bytes,
                "hosts": sorted(service["hosts"]),
            })
        service_rows.sort(key=lambda item: int(item["total_bytes"]), reverse=True)
        route_rows = {
            route: {
                **traffic,
                "total_bytes": traffic["up_bytes"] + traffic["down_bytes"],
            }
            for route, traffic in routes.items()
        }
        total_bytes = global_up + global_down
        observed_bytes = observed_up + observed_down
        observed_seconds = max(
            0.001,
            epoch - self.previous_epoch if self.previous_epoch is not None else DEFAULT_POLL_SECONDS,
        )

        self.previous_up = current_up
        self.previous_down = current_down
        self.previous_epoch = epoch
        self.previous_connections = current_connections
        return {
            "schema": MIHOMO_SAMPLE_SCHEMA,
            "timestamp": iso_now(epoch),
            "epoch": epoch,
            "observed_seconds": observed_seconds,
            "kernel": {
                "up_bytes": global_up,
                "down_bytes": global_down,
                "total_bytes": total_bytes,
                "cumulative_up_bytes": current_up,
                "cumulative_down_bytes": current_down,
                "cumulative_total_bytes": current_up + current_down,
            },
            "services": service_rows,
            "routes": route_rows,
            "attribution": {
                "observed_bytes": observed_bytes,
                "unattributed_bytes": residual_up + residual_down,
                "coverage": observed_bytes / total_bytes if total_bytes > 0 else 1.0,
            },
            "active_connections": len(current_connections),
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "schema": MIHOMO_TRACKER_SCHEMA,
            "up_bytes": self.previous_up,
            "down_bytes": self.previous_down,
            "epoch": self.previous_epoch,
            "connections": self.previous_connections,
        }


def load_tracker(path: Path) -> MihomoTrafficTracker:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MihomoTrafficTracker()
    if payload.get("schema") != MIHOMO_TRACKER_SCHEMA:
        return MihomoTrafficTracker()
    connections: dict[str, dict[str, int]] = {}
    raw_connections = payload.get("connections", {})
    if isinstance(raw_connections, dict):
        for connection_id, counters in raw_connections.items():
            if not isinstance(counters, dict):
                continue
            try:
                connections[str(connection_id)] = {
                    "up_bytes": max(0, int(counters.get("up_bytes", 0))),
                    "down_bytes": max(0, int(counters.get("down_bytes", 0))),
                }
            except (TypeError, ValueError):
                continue
    try:
        up_bytes = int(payload["up_bytes"]) if payload.get("up_bytes") is not None else None
        down_bytes = int(payload["down_bytes"]) if payload.get("down_bytes") is not None else None
        epoch = float(payload["epoch"]) if payload.get("epoch") is not None else None
    except (TypeError, ValueError):
        return MihomoTrafficTracker()
    return MihomoTrafficTracker(up_bytes, down_bytes, epoch, connections)


def save_tracker(path: Path, tracker: MihomoTrafficTracker) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(tracker.serialize(), ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def combine_samples(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(samples)
    if not rows:
        raise RuntimeError("Mihomo 采样区间没有可用数据")
    services: dict[str, dict[str, Any]] = {}
    routes: dict[str, dict[str, int]] = {}
    kernel_up = kernel_down = 0
    observed_seconds = 0.0
    observed_bytes = unattributed_bytes = 0
    for row in rows:
        kernel = row.get("kernel", {})
        kernel_up += int(kernel.get("up_bytes", 0))
        kernel_down += int(kernel.get("down_bytes", 0))
        observed_seconds += float(row.get("observed_seconds", 0.0))
        attribution = row.get("attribution", {})
        observed_bytes += int(attribution.get("observed_bytes", 0))
        unattributed_bytes += int(attribution.get("unattributed_bytes", 0))
        for raw_service in row.get("services", []):
            if not isinstance(raw_service, dict):
                continue
            service_id = str(raw_service.get("id", "unknown_host"))
            service = services.setdefault(service_id, {
                "id": service_id,
                "label": str(raw_service.get("label", service_id)),
                "up_bytes": 0,
                "down_bytes": 0,
                "hosts": set(),
            })
            service["up_bytes"] += int(raw_service.get("up_bytes", 0))
            service["down_bytes"] += int(raw_service.get("down_bytes", 0))
            for host in raw_service.get("hosts", []):
                if isinstance(host, str) and len(service["hosts"]) < 8:
                    service["hosts"].add(host)
        for route, raw_traffic in row.get("routes", {}).items():
            if not isinstance(raw_traffic, dict):
                continue
            traffic = routes.setdefault(str(route), {"up_bytes": 0, "down_bytes": 0})
            traffic["up_bytes"] += int(raw_traffic.get("up_bytes", 0))
            traffic["down_bytes"] += int(raw_traffic.get("down_bytes", 0))

    service_rows = [
        {
            "id": service["id"],
            "label": service["label"],
            "up_bytes": service["up_bytes"],
            "down_bytes": service["down_bytes"],
            "total_bytes": service["up_bytes"] + service["down_bytes"],
            "hosts": sorted(service["hosts"]),
        }
        for service in services.values()
    ]
    service_rows.sort(key=lambda item: int(item["total_bytes"]), reverse=True)
    route_rows = {
        route: {
            **traffic,
            "total_bytes": traffic["up_bytes"] + traffic["down_bytes"],
        }
        for route, traffic in routes.items()
    }
    total_bytes = kernel_up + kernel_down
    latest = rows[-1]
    latest_kernel = latest.get("kernel", {})
    return {
        "schema": MIHOMO_SAMPLE_SCHEMA,
        "timestamp": latest["timestamp"],
        "epoch": latest["epoch"],
        "observed_seconds": max(observed_seconds, 0.001),
        "kernel": {
            "up_bytes": kernel_up,
            "down_bytes": kernel_down,
            "total_bytes": total_bytes,
            "cumulative_up_bytes": int(latest_kernel.get("cumulative_up_bytes", 0)),
            "cumulative_down_bytes": int(latest_kernel.get("cumulative_down_bytes", 0)),
            "cumulative_total_bytes": int(latest_kernel.get("cumulative_total_bytes", 0)),
        },
        "services": service_rows,
        "routes": route_rows,
        "attribution": {
            "observed_bytes": observed_bytes,
            "unattributed_bytes": unattributed_bytes,
            "coverage": observed_bytes / total_bytes if total_bytes > 0 else 1.0,
        },
        "active_connections": int(latest.get("active_connections", 0)),
    }


def collect_interval(
    client: MihomoApiClient,
    tracker: MihomoTrafficTracker,
    duration_seconds: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    *,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll locally during one persisted interval so short connections are seen."""
    duration_seconds = max(0.0, float(duration_seconds))
    poll_seconds = max(0.1, float(poll_seconds))
    deadline = monotonic() + duration_seconds
    samples: list[dict[str, Any]] = []
    last_error: Exception | None = None
    while True:
        try:
            samples.append(tracker.apply(client.connections(), clock()))
            last_error = None
        except Exception as exc:  # the next local poll may recover after a core reload
            last_error = exc
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleeper(min(poll_seconds, remaining))
    if not samples:
        if last_error is not None:
            raise last_error
        raise RuntimeError("Mihomo 采样区间没有可用数据")
    return combine_samples(samples)
