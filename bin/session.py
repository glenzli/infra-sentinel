"""Resettable Mihomo-domain and remote-billing observation sessions."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from sample_timing import DEFAULT_EXPECTED_INTERVAL_SECONDS
from traffic_estimation import TrafficEstimationConfig, estimate_traffic, minute_rate_trend
from vps import VPS_SAMPLE_SCHEMA


SESSION_SCHEMA = 5
RESET_REQUEST_SCHEMA = 1
HISTORY_LIMIT = 2_000
HISTORY_WINDOW_SECONDS = 15 * 60
RESET_REQUEST_NAME = "session-reset.request.json"
ROUTES = ("proxy", "direct", "blocked", "unknown", "unattributed")


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def consume_reset_request(state_dir: Path) -> dict[str, Any] | None:
    """Consume a dashboard request exactly once; malformed requests are ignored."""
    path = state_dir / RESET_REQUEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        path.unlink()
    except OSError:
        return None
    if payload.get("schema") != RESET_REQUEST_SCHEMA or not isinstance(payload.get("id"), str):
        return None
    return payload


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

    def _clear(self) -> None:
        self.kernel = self._empty_traffic()
        self.services = {}
        self.routes = self._empty_routes()
        self.attribution_observed_bytes = 0
        self.attribution_unattributed_bytes = 0
        self.vps = self._empty_vps()
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

    def set_vps_baseline(self, vps_state: dict[str, Any]) -> None:
        last = vps_state.get("last_sample", {})
        if vps_state.get("status") == "error" or last.get("schema") != VPS_SAMPLE_SCHEMA:
            self._save()
            return
        try:
            self.last_vps_sample_epoch = float(last["epoch"])
            self.vps_baselined_at = self.last_vps_sample_epoch
        except (KeyError, TypeError, ValueError):
            pass
        self._save()

    @staticmethod
    def _add(target: dict[str, int], source: Any) -> None:
        source = source if isinstance(source, dict) else {}
        target["up_bytes"] += max(0, int(source.get("up_bytes", 0)))
        target["down_bytes"] += max(0, int(source.get("down_bytes", 0)))

    def record(self, sample: dict[str, Any], vps_state: dict[str, Any]) -> None:
        if self.started_epoch is None:
            self.reset(float(sample["epoch"]), "automatic")
            self.set_vps_baseline(vps_state)
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

        last = vps_state.get("last_sample", {})
        try:
            vps_epoch = float(last["epoch"])
            interval_start = float(last["interval_started_epoch"])
        except (KeyError, TypeError, ValueError):
            vps_epoch = None
            interval_start = None
        if vps_epoch is not None and vps_epoch != self.last_vps_sample_epoch:
            self.last_vps_sample_epoch = vps_epoch
            if self.vps_baselined_at is None or (
                self.started_epoch is not None
                and interval_start is not None
                and interval_start >= self.started_epoch
            ):
                interval_in = max(0, int(last.get("in_bytes", 0)))
                interval_out = max(0, int(last.get("out_bytes", 0)))
                self.vps["in_bytes"] += interval_in
                self.vps["out_bytes"] += interval_out
                self.vps_intervals += 1
                if last.get("packet_counters_ready"):
                    self.vps["in_packets"] += max(0, int(last.get("in_packets", 0)))
                    self.vps["out_packets"] += max(0, int(last.get("out_packets", 0)))
                    self.vps["packet_covered_in_bytes"] += interval_in
                    self.vps["packet_covered_out_bytes"] += interval_out
                    self.vps_packet_intervals += 1
            self.vps_baselined_at = vps_epoch

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
        vps_enabled: bool,
        estimation_config: TrafficEstimationConfig,
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
        vps_interface_total = self.vps["in_bytes"] + self.vps["out_bytes"]
        vps_billable = estimation_config.billable_bytes(
            self.vps["in_bytes"],
            self.vps["out_bytes"],
        )
        vps_ready = bool(vps_enabled and self.vps_intervals > 0)
        xray_state = xray_stats or {}
        xray_logical_total = int(xray_state.get("total_bytes", 0))
        xray_ready = bool(xray_state.get("ready") and int(xray_state.get("intervals", 0)) > 0)
        vps_packet_count = estimation_config.billable_packets(
            self.vps["in_packets"],
            self.vps["out_packets"],
        )
        packet_covered_bytes = estimation_config.billable_bytes(
            self.vps["packet_covered_in_bytes"],
            self.vps["packet_covered_out_bytes"],
        )
        estimates = estimate_traffic(
            vps_billable,
            vps_packet_count,
            packet_covered_bytes,
            xray_logical_total,
            vps_ready,
            xray_ready,
            estimation_config,
        )
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
                **self.vps,
                "total_bytes": vps_billable,
                "interface_total_bytes": vps_interface_total,
                "total_packets": vps_packet_count,
                "billing_mode": estimation_config.billing_mode,
                "packet_intervals": self.vps_packet_intervals,
                "baselined_at": iso_now(self.vps_baselined_at) if self.vps_baselined_at is not None else None,
                "intervals": self.vps_intervals,
            },
            "vps_ready": vps_ready,
            "duration_seconds": duration_seconds,
            "breakdown": estimates,
            "trend": minute_rate_trend(
                self.history,
                (service["id"] for service in attributed_services[:3]),
                expected_interval_seconds=self.expected_interval_seconds,
            ),
        }
