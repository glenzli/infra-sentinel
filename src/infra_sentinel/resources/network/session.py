"""Resettable Mihomo-domain and remote-billing observation sessions."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from infra_sentinel.core.timing import DEFAULT_EXPECTED_INTERVAL_SECONDS
from infra_sentinel.resources.network.traffic_estimation import (
    TREND_WINDOW_MINUTES,
    TrafficEstimationConfig,
    estimate_fleet_traffic,
    estimate_traffic,
    minute_rate_trend,
)
from infra_sentinel.resources.network.vps import VPS_SAMPLE_SCHEMA


SESSION_SCHEMA = 6
HISTORY_LIMIT = 2_000
HISTORY_WINDOW_SECONDS = TREND_WINDOW_MINUTES * 60
ROUTES = ("proxy", "direct", "blocked", "unknown", "unattributed")


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


class SessionMeter:
    """Persist one user-started comparison across Mihomo, Xray, and VPS."""

    def __init__(
        self,
        state_dir: Path,
        expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
    ) -> None:
        self.state_dir = state_dir
        self.expected_interval_seconds = float(expected_interval_seconds)
        self.started_epoch: float | None = None
        self.started_reason = ""
        self.kernel = self._empty_traffic()
        self.services: dict[str, dict[str, Any]] = {}
        self.routes = self._empty_routes()
        self.attribution_observed_bytes = 0
        self.attribution_unattributed_bytes = 0
        self.vps = self._empty_vps()
        self.vps_servers: dict[str, dict[str, Any]] = {}
        self.vps_baselined_at: float | None = None
        self.last_vps_sample_epoch: float | None = None
        self.vps_intervals = 0
        self.vps_packet_intervals = 0
        self.history: list[dict[str, Any]] = []
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "session.json"

    @staticmethod
    def _empty_traffic() -> dict[str, int]:
        return {"up_bytes": 0, "down_bytes": 0}

    @classmethod
    def _empty_routes(cls) -> dict[str, dict[str, int]]:
        return {route: cls._empty_traffic() for route in ROUTES}

    @staticmethod
    def _empty_vps() -> dict[str, int]:
        return {
            "in_bytes": 0,
            "out_bytes": 0,
            "in_packets": 0,
            "out_packets": 0,
            "packet_covered_in_bytes": 0,
            "packet_covered_out_bytes": 0,
        }

    @staticmethod
    def _empty_vps_server(label: str = "VPS", billing_mode: str = "both") -> dict[str, Any]:
        return {
            "id": "default",
            "label": label,
            "billing_mode": billing_mode,
            "in_bytes": 0,
            "out_bytes": 0,
            "in_packets": 0,
            "out_packets": 0,
            "packet_covered_in_bytes": 0,
            "packet_covered_out_bytes": 0,
            "vps_baselined_at": None,
            "last_vps_sample_epoch": None,
            "vps_intervals": 0,
            "vps_packet_intervals": 0,
        }

    def _clear(self) -> None:
        self.kernel = self._empty_traffic()
        self.services = {}
        self.routes = self._empty_routes()
        self.attribution_observed_bytes = 0
        self.attribution_unattributed_bytes = 0
        self.vps = self._empty_vps()
        self.vps_servers = {}
        self.vps_baselined_at = None
        self.last_vps_sample_epoch = None
        self.vps_intervals = 0
        self.vps_packet_intervals = 0
        self.history = []

    @staticmethod
    def _traffic(raw: Any) -> dict[str, int]:
        raw = raw if isinstance(raw, dict) else {}
        return {
            "up_bytes": max(0, int(raw.get("up_bytes", 0))),
            "down_bytes": max(0, int(raw.get("down_bytes", 0))),
        }

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("schema") != SESSION_SCHEMA:
            return
        try:
            started = payload.get("started_epoch")
            self.started_epoch = float(started) if started is not None else None
            self.started_reason = str(payload.get("started_reason", ""))
            self.kernel = self._traffic(payload.get("kernel"))
            self.services = {}
            for service_id, raw_service in payload.get("services", {}).items():
                if not isinstance(raw_service, dict):
                    continue
                traffic = self._traffic(raw_service)
                self.services[str(service_id)] = {
                    "id": str(service_id),
                    "label": str(raw_service.get("label", service_id)),
                    **traffic,
                }
            self.routes = self._empty_routes()
            for route in ROUTES:
                self.routes[route] = self._traffic(payload.get("routes", {}).get(route))
            attribution = payload.get("attribution", {})
            self.attribution_observed_bytes = max(0, int(attribution.get("observed_bytes", 0)))
            self.attribution_unattributed_bytes = max(0, int(attribution.get("unattributed_bytes", 0)))
            saved_vps = payload.get("vps", {})
            self.vps = {
                "in_bytes": max(0, int(saved_vps.get("in_bytes", 0))),
                "out_bytes": max(0, int(saved_vps.get("out_bytes", 0))),
                "in_packets": max(0, int(saved_vps.get("in_packets", 0))),
                "out_packets": max(0, int(saved_vps.get("out_packets", 0))),
                "packet_covered_in_bytes": max(0, int(saved_vps.get("packet_covered_in_bytes", 0))),
                "packet_covered_out_bytes": max(0, int(saved_vps.get("packet_covered_out_bytes", 0))),
            }
            self.vps_servers = {}
            for server_id, raw_server in payload.get("vps_servers", {}).items():
                if not isinstance(raw_server, dict):
                    continue
                server = self._empty_vps_server(
                    str(raw_server.get("label", server_id)),
                    str(raw_server.get("billing_mode", "both")),
                )
                server["id"] = str(server_id)
                for key in (
                    "in_bytes", "out_bytes", "in_packets", "out_packets",
                    "packet_covered_in_bytes", "packet_covered_out_bytes",
                    "vps_intervals", "vps_packet_intervals",
                ):
                    server[key] = max(0, int(raw_server.get(key, 0)))
                for key in ("vps_baselined_at", "last_vps_sample_epoch"):
                    value = raw_server.get(key)
                    server[key] = float(value) if value is not None else None
                self.vps_servers[server["id"]] = server
            self.vps_intervals = max(0, int(payload.get("vps_intervals", 0)))
            self.vps_packet_intervals = max(0, int(payload.get("vps_packet_intervals", 0)))
            baseline = payload.get("vps_baselined_at")
            self.vps_baselined_at = float(baseline) if baseline is not None else None
            latest = payload.get("last_vps_sample_epoch")
            self.last_vps_sample_epoch = float(latest) if latest is not None else None
            raw_history = payload.get("history", [])
            self.history = [item for item in raw_history if isinstance(item, dict)][-HISTORY_LIMIT:]
        except (AttributeError, TypeError, ValueError):
            self.started_epoch = None
            self.started_reason = ""
            self._clear()

    def _save(self) -> None:
        payload = {
            "schema": SESSION_SCHEMA,
            "started_epoch": self.started_epoch,
            "started_reason": self.started_reason,
            "kernel": self.kernel,
            "services": self.services,
            "routes": self.routes,
            "attribution": {
                "observed_bytes": self.attribution_observed_bytes,
                "unattributed_bytes": self.attribution_unattributed_bytes,
            },
            "vps": self.vps,
            "vps_servers": self.vps_servers,
            "vps_intervals": self.vps_intervals,
            "vps_packet_intervals": self.vps_packet_intervals,
            "vps_baselined_at": self.vps_baselined_at,
            "last_vps_sample_epoch": self.last_vps_sample_epoch,
            "history": self.history,
        }
        temporary = self.state_dir / ".session.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def reset(self, epoch: float, reason: str) -> None:
        self.started_epoch = float(epoch)
        self.started_reason = reason
        self._clear()
        self._save()

    @staticmethod
    def _remote_rows(remote_state: Any) -> list[dict[str, Any]]:
        if isinstance(remote_state, dict) and isinstance(remote_state.get("servers"), list):
            return [row for row in remote_state["servers"] if isinstance(row, dict)]
        # Keep the in-memory API useful for unit callers that supply one VPS state.
        if isinstance(remote_state, dict):
            return [{
                "id": "default",
                "label": "VPS",
                "billing_mode": "both",
                "vps": remote_state,
                "xray_stats": {},
            }]
        return []

    def set_vps_baseline(self, remote_state: dict[str, Any]) -> None:
        for row in self._remote_rows(remote_state):
            vps_state = row.get("vps", {})
            last = vps_state.get("last_sample", {}) if isinstance(vps_state, dict) else {}
            if vps_state.get("status") == "error" or last.get("schema") != VPS_SAMPLE_SCHEMA:
                continue
            server_id = str(row.get("id", "default"))
            server = self.vps_servers.setdefault(
                server_id,
                self._empty_vps_server(str(row.get("label", server_id)), str(row.get("billing_mode", "both"))),
            )
            server["id"] = server_id
            server["label"] = str(row.get("label", server.get("label", server_id)))
            server["billing_mode"] = str(row.get("billing_mode", server.get("billing_mode", "both")))
            try:
                server["last_vps_sample_epoch"] = float(last["epoch"])
                server["vps_baselined_at"] = server["last_vps_sample_epoch"]
            except (KeyError, TypeError, ValueError):
                continue
        self._save()

    @staticmethod
    def _add(target: dict[str, int], source: Any) -> None:
        source = source if isinstance(source, dict) else {}
        target["up_bytes"] += max(0, int(source.get("up_bytes", 0)))
        target["down_bytes"] += max(0, int(source.get("down_bytes", 0)))

    def record(self, sample: dict[str, Any], remote_state: dict[str, Any]) -> None:
        if self.started_epoch is None:
            self.reset(float(sample["epoch"]), "automatic")
            self.set_vps_baseline(remote_state)
            return

        self._add(self.kernel, sample.get("kernel"))
        interval_services: dict[str, int] = {}
        for raw_service in sample.get("services", []):
            if not isinstance(raw_service, dict):
                continue
            service_id = str(raw_service.get("id", "unknown_host"))
            service = self.services.setdefault(service_id, {
                "id": service_id,
                "label": str(raw_service.get("label", service_id)),
                "up_bytes": 0,
                "down_bytes": 0,
            })
            self._add(service, raw_service)
            interval_services[service_id] = (
                max(0, int(raw_service.get("up_bytes", 0)))
                + max(0, int(raw_service.get("down_bytes", 0)))
            )
        for route in ROUTES:
            self._add(self.routes[route], sample.get("routes", {}).get(route))
        attribution = sample.get("attribution", {})
        self.attribution_observed_bytes += max(0, int(attribution.get("observed_bytes", 0)))
        self.attribution_unattributed_bytes += max(0, int(attribution.get("unattributed_bytes", 0)))

        for row in self._remote_rows(remote_state):
            vps_state = row.get("vps", {})
            last = vps_state.get("last_sample", {}) if isinstance(vps_state, dict) else {}
            try:
                vps_epoch = float(last["epoch"])
                interval_start = float(last["interval_started_epoch"])
            except (KeyError, TypeError, ValueError):
                continue
            server_id = str(row.get("id", "default"))
            server = self.vps_servers.setdefault(
                server_id,
                self._empty_vps_server(str(row.get("label", server_id)), str(row.get("billing_mode", "both"))),
            )
            server["label"] = str(row.get("label", server.get("label", server_id)))
            server["billing_mode"] = str(row.get("billing_mode", server.get("billing_mode", "both")))
            if vps_epoch == server.get("last_vps_sample_epoch"):
                continue
            server["last_vps_sample_epoch"] = vps_epoch
            if server.get("vps_baselined_at") is None or (
                self.started_epoch is not None and interval_start >= self.started_epoch
            ):
                interval_in = max(0, int(last.get("in_bytes", 0)))
                interval_out = max(0, int(last.get("out_bytes", 0)))
                server["in_bytes"] += interval_in
                server["out_bytes"] += interval_out
                server["vps_intervals"] += 1
                self.vps["in_bytes"] += interval_in
                self.vps["out_bytes"] += interval_out
                self.vps_intervals += 1
                if last.get("packet_counters_ready"):
                    in_packets = max(0, int(last.get("in_packets", 0)))
                    out_packets = max(0, int(last.get("out_packets", 0)))
                    server["in_packets"] += in_packets
                    server["out_packets"] += out_packets
                    server["packet_covered_in_bytes"] += interval_in
                    server["packet_covered_out_bytes"] += interval_out
                    server["vps_packet_intervals"] += 1
                    self.vps["in_packets"] += in_packets
                    self.vps["out_packets"] += out_packets
                    self.vps["packet_covered_in_bytes"] += interval_in
                    self.vps["packet_covered_out_bytes"] += interval_out
                    self.vps_packet_intervals += 1
            server["vps_baselined_at"] = vps_epoch

        kernel = sample.get("kernel", {})
        routes = sample.get("routes", {})
        self.history.append({
            "epoch": float(sample["epoch"]),
            "observed_seconds": float(sample.get("observed_seconds", 0.0)),
            "interval_kind": sample.get("interval_kind"),
            "expected_interval_seconds": sample.get(
                "expected_interval_seconds",
                self.expected_interval_seconds,
            ),
            "services": interval_services,
            "mihomo_total": max(0, int(kernel.get("total_bytes", 0))),
            "proxy_observed": max(0, int(routes.get("proxy", {}).get("total_bytes", 0))),
            "unattributed": max(0, int(attribution.get("unattributed_bytes", 0))),
        })
        cutoff = float(sample["epoch"]) - HISTORY_WINDOW_SECONDS
        self.history = [
            point
            for point in self.history[-HISTORY_LIMIT:]
            if float(point.get("epoch", 0)) >= cutoff
        ]
        self._save()

    def snapshot(
        self,
        remote_state: dict[str, Any] | bool,
        estimation_config: TrafficEstimationConfig | None = None,
        xray_stats: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        services = []
        for service in self.services.values():
            up_bytes = int(service["up_bytes"])
            down_bytes = int(service["down_bytes"])
            services.append({
                "id": service["id"],
                "label": service["label"],
                "up_bytes": up_bytes,
                "down_bytes": down_bytes,
                "total_bytes": up_bytes + down_bytes,
            })
        services.sort(key=lambda service: int(service["total_bytes"]), reverse=True)
        attributed_services = [service for service in services if service["id"] != "unattributed"]
        visible_services = attributed_services[:3]
        if len(attributed_services) > 3:
            visible_services.append({
                "id": "other_domains",
                "label": "Other domains",
                "up_bytes": 0,
                "down_bytes": 0,
                "total_bytes": sum(int(service["total_bytes"]) for service in attributed_services[3:]),
            })

        route_rows = []
        for route in ROUTES:
            traffic = self.routes[route]
            route_rows.append({
                "id": route,
                **traffic,
                "total_bytes": traffic["up_bytes"] + traffic["down_bytes"],
            })
        route_by_id = {row["id"]: row for row in route_rows}
        kernel_total = self.kernel["up_bytes"] + self.kernel["down_bytes"]
        proxy_observed = int(route_by_id["proxy"]["total_bytes"])
        unattributed = int(route_by_id["unattributed"]["total_bytes"])
        domain_attributed = max(0, kernel_total - unattributed)
        if isinstance(remote_state, bool):
            # Retain a small in-memory compatibility path for callers embedding
            # SessionMeter directly; persisted settings are intentionally owned by
            # the date-versioned source/policy configuration contract.
            mode = estimation_config.billing_mode if estimation_config is not None else "both"
            remote_state = {
                "servers": [{
                    "id": "default",
                    "label": "VPS",
                    "billing_mode": mode,
                    "vps": {"enabled": remote_state},
                    "xray_stats": xray_stats or {},
                }],
            }
        remote_rows = self._remote_rows(remote_state)
        server_snapshots: list[dict[str, Any]] = []
        fleet_rows: list[dict[str, Any]] = []
        aggregate_vps = self._empty_vps()
        vps_modes: set[str] = set()
        vps_ready_count = 0
        xray_ready_count = 0
        enabled_count = 0
        for row in remote_rows:
            server_id = str(row.get("id", "default"))
            mode = str(row.get("billing_mode", "both"))
            if mode not in ("both", "outbound"):
                mode = "both"
            server = self.vps_servers.get(server_id, self._empty_vps_server(str(row.get("label", server_id)), mode))
            server = {**server, "id": server_id, "label": str(row.get("label", server.get("label", server_id))), "billing_mode": mode}
            xray_state = row.get("xray_stats", {}) if isinstance(row.get("xray_stats"), dict) else {}
            config = TrafficEstimationConfig(mode)
            interface_total = server["in_bytes"] + server["out_bytes"]
            billable = config.billable_bytes(server["in_bytes"], server["out_bytes"])
            packet_count = config.billable_packets(server["in_packets"], server["out_packets"])
            packet_bytes = config.billable_bytes(server["packet_covered_in_bytes"], server["packet_covered_out_bytes"])
            xray_logical = max(0, int(xray_state.get("total_bytes", 0)))
            vps_is_enabled = bool(row.get("vps", {}).get("enabled", True))
            xray_is_ready = bool(xray_state.get("ready") and int(xray_state.get("intervals", 0)) > 0)
            vps_is_ready = bool(vps_is_enabled and server["vps_intervals"] > 0)
            enabled_count += 1 if vps_is_enabled else 0
            vps_ready_count += 1 if vps_is_ready else 0
            xray_ready_count += 1 if xray_is_ready else 0
            vps_modes.add(mode)
            for key in aggregate_vps:
                if key.endswith("bytes") or key.endswith("packets"):
                    aggregate_vps[key] += int(server[key])
            fleet_rows.append({
                "id": server_id,
                "billing_mode": mode,
                "xray_logical_bytes": xray_logical,
                "vps_billable_bytes": billable,
                "vps_packet_count": packet_count,
                "packet_covered_bytes": packet_bytes,
                "vps_ready": vps_is_ready,
                "xray_ready": xray_is_ready,
            })
            server_estimate = estimate_traffic(
                billable,
                packet_count,
                packet_bytes,
                xray_logical,
                vps_is_ready,
                xray_is_ready,
                config,
            )
            server_snapshots.append({
                "id": server_id,
                "label": server["label"],
                "billing_mode": mode,
                "vps": {
                    **server,
                    "total_bytes": billable,
                    "interface_total_bytes": interface_total,
                    "total_packets": packet_count,
                    "packet_intervals": server["vps_packet_intervals"],
                    "baselined_at": iso_now(server["vps_baselined_at"]) if server["vps_baselined_at"] is not None else None,
                    "intervals": server["vps_intervals"],
                    "ready": vps_is_ready,
                },
                "xray_stats": xray_state,
                "total_bytes": billable,
                "interface_total_bytes": interface_total,
                "xray_logical_bytes": xray_logical,
                "vps_ready": vps_is_ready,
                "xray_ready": xray_is_ready,
                "breakdown": server_estimate,
            })
        vps_interface_total = aggregate_vps["in_bytes"] + aggregate_vps["out_bytes"]
        vps_modes_label = next(iter(vps_modes)) if len(vps_modes) == 1 else ("mixed" if vps_modes else "both")
        vps_packet_count = sum(int(row["vps_packet_count"]) for row in fleet_rows)
        packet_covered_bytes = sum(int(row["packet_covered_bytes"]) for row in fleet_rows)
        vps_billable = sum(int(server["total_bytes"]) for server in server_snapshots)
        xray_logical_total = sum(int(row["xray_logical_bytes"]) for row in server_snapshots)
        vps_ready = bool(enabled_count and vps_ready_count == enabled_count)
        xray_ready = bool(enabled_count and xray_ready_count == enabled_count)
        estimates = estimate_fleet_traffic(fleet_rows)
        duration_seconds = (
            max(0, int((now if now is not None else time.time()) - self.started_epoch))
            if self.started_epoch is not None
            else 0
        )
        return {
            "started_at": iso_now(self.started_epoch) if self.started_epoch is not None else None,
            "started_epoch": self.started_epoch,
            "started_reason": self.started_reason,
            "kernel": {
                **self.kernel,
                "total_bytes": kernel_total,
            },
            "services": services,
            "visible_services": visible_services,
            "routes": route_rows,
            "proxy_observed_total_bytes": proxy_observed,
            "proxy_upper_bound_bytes": proxy_observed + unattributed,
            "domain_attributed_bytes": domain_attributed,
            "attribution": {
                "observed_bytes": self.attribution_observed_bytes,
                "unattributed_bytes": self.attribution_unattributed_bytes,
                "coverage": (
                    self.attribution_observed_bytes / kernel_total
                    if kernel_total > 0
                    else 1.0
                ),
            },
            "vps": {
                **aggregate_vps,
                "total_bytes": vps_billable,
                "interface_total_bytes": vps_interface_total,
                "total_packets": vps_packet_count,
                "billing_mode": vps_modes_label,
                "packet_intervals": self.vps_packet_intervals,
                "baselined_at": iso_now(self.vps_baselined_at) if self.vps_baselined_at is not None else None,
                "intervals": self.vps_intervals,
                "ready": vps_ready,
            },
            "vps_ready": vps_ready,
            "xray_ready": xray_ready,
            "remote_servers": server_snapshots,
            "duration_seconds": duration_seconds,
            "breakdown": estimates,
            "trend": minute_rate_trend(
                self.history,
                (service["id"] for service in attributed_services[:3]),
                expected_interval_seconds=self.expected_interval_seconds,
            ),
        }
