"""Low-frequency observation of official upstream service status pages.

This owner performs public, read-only Statuspage requests outside the five-second
sampling loop.  It normalizes provider-specific component names into one small
snapshot and emits state transitions without treating transport failures as
provider outages.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.request import Request, urlopen


UPSTREAM_STATUS_SCHEMA = "20260811.1"
POLL_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class StatusProvider:
    id: str
    label: str
    summary_url: str
    status_url: str
    component_patterns: tuple[str, ...]
    payload_format: str = "statuspage"
    request_host: str | None = None


DEFAULT_PROVIDERS = (
    StatusProvider(
        "openai",
        "OpenAI",
        "https://status.openai.com/api/v2/summary.json",
        "https://status.openai.com/",
        ("api", "responses", "realtime", "embeddings", "fine-tuning", "batch", "moderations"),
    ),
    StatusProvider(
        "claude",
        "Claude",
        "https://status.claude.com/api/v2/summary.json",
        "https://status.claude.com/",
        ("*",),
    ),
    StatusProvider(
        "deepseek",
        "DeepSeek",
        # DeepSeek migrated its official public page from Atlassian Statuspage
        # to FlashDuty.  Use the page's public CNAME origin with the official
        # Host header because some TLS clients are reset on the custom domain.
        "https://statuspage.flashcat.cloud/",
        "https://status.deepseek.com/",
        ("*",),
        payload_format="flashduty",
        request_host="status.deepseek.com",
    ),
    StatusProvider(
        "moonshot",
        "Kimi / Moonshot",
        "https://status.moonshot.cn/api/v2/summary.json",
        "https://status.moonshot.cn/",
        ("api", "kimi", "open api"),
    ),
    StatusProvider(
        "cursor",
        "Cursor",
        "https://status.cursor.com/api/v2/summary.json",
        "https://status.cursor.com/",
        ("cli", "cloud agents", "ide", "review agents", "automations"),
    ),
)


TRACKABLE_STATES = {"healthy", "warning", "critical"}
COMPONENT_STATUS = {
    "operational": "healthy",
    "under_maintenance": "warning",
    "degraded_performance": "warning",
    "degraded": "warning",
    "partial_outage": "warning",
    "major_outage": "critical",
    "full_outage": "critical",
}
IMPACT_STATUS = {
    "none": "healthy",
    "maintenance": "warning",
    "minor": "warning",
    "major": "warning",
    "critical": "critical",
}
STATUS_RANK = {"healthy": 0, "warning": 1, "critical": 2}


def iso_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="seconds")


def _fetch_bytes(url: str, *, accept: str, host: str | None = None) -> bytes:
    curl = shutil.which("curl")
    if not curl and Path("/usr/bin/curl").is_file():
        curl = "/usr/bin/curl"
    if curl:
        command = [
                curl,
                "--silent",
                "--show-error",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-time",
                str(REQUEST_TIMEOUT_SECONDS),
                "--max-filesize",
                str(MAX_RESPONSE_BYTES),
                "--retry",
                "1",
                "--retry-delay",
                "1",
                "--retry-all-errors",
                "--header",
                f"Accept: {accept}",
                "--user-agent",
                "Infra-Sentinel/1",
        ]
        if host:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError("status request host is invalid")
            command.extend(("--header", f"Host: {host}"))
        command.append(url)
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=(REQUEST_TIMEOUT_SECONDS * 2) + 4,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"status request failed with curl exit {result.returncode}")
        encoded = result.stdout
    else:
        encoded = _fetch_with_urllib(url, accept=accept, host=host)
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("status response exceeds safety limit")
    return encoded


def fetch_statuspage_json(url: str) -> dict[str, Any]:
    return _decode_payload(_fetch_bytes(url, accept="application/json"))


def _fetch_with_urllib(url: str, *, accept: str, host: str | None = None) -> bytes:
    headers = {"Accept": accept, "User-Agent": "Infra-Sentinel/1"}
    if host:
        headers["Host"] = host
    request = Request(url, headers=headers)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


def _decode_payload(encoded: bytes) -> dict[str, Any]:
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("status response must be an object")
    return payload


def _next_f_record(document: str, record_prefix: str) -> Any:
    pattern = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)')
    for quoted in pattern.findall(document):
        decoded = json.loads(quoted)
        if isinstance(decoded, str) and decoded.startswith(record_prefix):
            return json.loads(decoded[len(record_prefix):])
    raise ValueError("FlashDuty status page has no expected public data")


def decode_flashduty_summary(encoded: bytes) -> dict[str, Any]:
    document = html.unescape(encoded.decode("utf-8"))
    record = _next_f_record(document, "1e:")
    if not isinstance(record, list) or len(record) < 4 or not isinstance(record[3], dict):
        raise ValueError("FlashDuty status page data is malformed")
    props = record[3]
    initial_data = props.get("initialData")
    if not isinstance(initial_data, dict):
        raise ValueError("FlashDuty status page has no initial data")
    page = initial_data.get("page")
    if not isinstance(page, dict):
        raise ValueError("FlashDuty status page has no page data")

    raw_components = page.get("components")
    if not isinstance(raw_components, list):
        raise ValueError("FlashDuty status page has no components")
    sections = {
        str(section.get("section_id")): str(section.get("name"))
        for section in page.get("sections", [])
        if isinstance(section, dict) and section.get("section_id") and section.get("name")
    } if isinstance(page.get("sections"), list) else {}
    components = []
    status_by_id: dict[str, str] = {}
    for value in raw_components:
        if not isinstance(value, dict) or value.get("hide_all"):
            continue
        component_id = str(value.get("component_id") or "")
        name = value.get("name")
        if not component_id or not isinstance(name, str):
            continue
        component = {"id": component_id, "name": name, "status": "operational"}
        group = sections.get(str(value.get("section_id") or ""))
        if group:
            component["group"] = group
        components.append(component)
        status_by_id[component_id] = "operational"

    active_changes = initial_data.get("active_changes")
    changes = active_changes if isinstance(active_changes, list) else []
    incidents = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        affected = change.get("affected_components")
        affected_components = affected if isinstance(affected, list) else []
        states = []
        incident_components = []
        for component in affected_components:
            if not isinstance(component, dict):
                continue
            component_id = str(component.get("component_id") or "")
            raw_status = str(component.get("status") or "degraded")
            if component_id in status_by_id:
                status_by_id[component_id] = raw_status
            if component_id:
                incident_components.append({"id": component_id})
            states.append(COMPONENT_STATUS.get(raw_status, "warning"))
        incident_level = _status_max([state for state in states if state in TRACKABLE_STATES])
        updates = change.get("updates")
        latest_update = updates[-1] if isinstance(updates, list) and updates and isinstance(updates[-1], dict) else {}
        updated_epoch = latest_update.get("at_seconds") or change.get("start_at_seconds")
        incidents.append({
            "id": str(change.get("change_id") or "incident"),
            "name": str(change.get("title") or "Service incident"),
            "status": str(change.get("status") or "investigating"),
            "impact": "critical" if incident_level == "critical" else "minor",
            "updated_at": iso_now(float(updated_epoch)) if isinstance(updated_epoch, (int, float)) else None,
            "components": incident_components,
        })
    for component in components:
        component["status"] = status_by_id.get(str(component["id"]), "operational")

    updated_ms = props.get("initialDataUpdatedAt")
    updated_at = iso_now(float(updated_ms) / 1000) if isinstance(updated_ms, (int, float)) else None
    return {
        "page": {"updated_at": updated_at},
        "status": {
            "indicator": "none" if not incidents else "minor",
            "description": "All Systems Operational" if not incidents else "Active service incident",
        },
        "components": components,
        "incidents": incidents,
    }


def fetch_provider_payload(provider: StatusProvider) -> dict[str, Any]:
    if provider.payload_format == "flashduty":
        encoded = _fetch_bytes(provider.summary_url, accept="text/html", host=provider.request_host)
        return decode_flashduty_summary(encoded)
    if provider.payload_format != "statuspage":
        raise ValueError("unsupported upstream status format")
    return fetch_statuspage_json(provider.summary_url)


def _matches_component(name: str, patterns: tuple[str, ...]) -> bool:
    if "*" in patterns:
        return True
    folded = name.casefold()
    return any(pattern.casefold() in folded for pattern in patterns)


def _status_max(states: list[str]) -> str:
    return max(states, key=lambda value: STATUS_RANK.get(value, -1), default="healthy")


def normalize_summary(provider: StatusProvider, payload: dict[str, Any], observed_at: str) -> dict[str, Any]:
    raw_components = payload.get("components")
    if not isinstance(raw_components, list):
        raise ValueError("status response has no components")
    group_names = {
        str(value.get("id")): str(value.get("name"))
        for value in raw_components
        if isinstance(value, dict) and value.get("group") is True and value.get("id") and value.get("name")
    }
    components = []
    component_ids: set[str] = set()
    for value in raw_components:
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        component_id = value.get("id")
        raw_status = value.get("status")
        if value.get("group") is True:
            continue
        if not isinstance(name, str) or not _matches_component(name, provider.component_patterns):
            continue
        if isinstance(component_id, str):
            component_ids.add(component_id)
        component = {
            "id": str(component_id or name),
            "name": name,
            "status": str(raw_status or "unknown"),
            "level": COMPONENT_STATUS.get(str(raw_status), "unknown"),
        }
        explicit_group = value.get("group") if isinstance(value.get("group"), str) else None
        group = value.get("group_name") or explicit_group or group_names.get(str(value.get("group_id") or ""))
        if isinstance(group, str) and group:
            component["group"] = group
        components.append(component)
    if not components:
        raise ValueError("status response has no matching API components")

    incidents = []
    incident_states: list[str] = []
    raw_incidents = payload.get("incidents")
    if isinstance(raw_incidents, list):
        for value in raw_incidents:
            if not isinstance(value, dict):
                continue
            affected = value.get("components")
            affected_ids = {
                str(component.get("id"))
                for component in affected
                if isinstance(component, dict) and component.get("id")
            } if isinstance(affected, list) else set()
            if affected_ids and not affected_ids.intersection(component_ids):
                continue
            if not affected_ids and all(component["level"] == "healthy" for component in components):
                continue
            impact = str(value.get("impact") or "minor")
            level = IMPACT_STATUS.get(impact, "warning")
            incident_states.append(level)
            incidents.append({
                "id": str(value.get("id") or "incident"),
                "name": str(value.get("name") or "Service incident"),
                "status": str(value.get("status") or "investigating"),
                "impact": impact,
                "level": level,
                "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
                "url": value.get("shortlink") if isinstance(value.get("shortlink"), str) else provider.status_url,
            })

    component_states = [str(component["level"]) for component in components]
    trackable = [state for state in component_states + incident_states if state in TRACKABLE_STATES]
    status = _status_max(trackable) if trackable else "unknown"
    if "unknown" in component_states and status == "healthy":
        status = "unknown"
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    return {
        "id": provider.id,
        "label": provider.label,
        "status": status,
        "available": True,
        "description": str((payload.get("status") or {}).get("description") or status)
        if isinstance(payload.get("status"), dict) else status,
        "observed_at": observed_at,
        "official_updated_at": page.get("updated_at") if isinstance(page.get("updated_at"), str) else None,
        "status_url": provider.status_url,
        "components": components,
        "incidents": incidents,
    }


def unavailable_provider(provider: StatusProvider, observed_at: str, error_kind: str) -> dict[str, Any]:
    return {
        "id": provider.id,
        "label": provider.label,
        "status": "unknown",
        "available": False,
        "description": "Official status unavailable",
        "observed_at": observed_at,
        "official_updated_at": None,
        "status_url": provider.status_url,
        "components": [],
        "incidents": [],
        "error_kind": error_kind,
    }


def aggregate_snapshot(items: list[dict[str, Any]], observed_at: str) -> dict[str, Any]:
    healthy = sum(item.get("status") == "healthy" for item in items)
    attention = sum(item.get("status") in {"warning", "critical"} for item in items)
    unknown = sum(item.get("status") == "unknown" for item in items)
    actual = [str(item.get("status")) for item in items if item.get("status") in TRACKABLE_STATES]
    status = _status_max(actual)
    if status == "healthy" and unknown:
        status = "degraded"
    return {
        "schema": UPSTREAM_STATUS_SCHEMA,
        "status": status,
        "total": len(items),
        "healthy": healthy,
        "attention": attention,
        "unknown": unknown,
        "updated_at": observed_at,
        "items": items,
    }


class UpstreamStatusMonitor:
    """Own polling, current snapshot, and trackable provider transitions."""

    def __init__(
        self,
        logger: logging.Logger,
        providers: tuple[StatusProvider, ...] = DEFAULT_PROVIDERS,
        fetch_json: Callable[[str], dict[str, Any]] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._logger = logger
        self._providers = providers
        self._fetch_provider = (
            (lambda provider: fetch_json(provider.summary_url))
            if fetch_json is not None
            else fetch_provider_payload
        )
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._states: dict[str, str] = {}
        self._transitions: deque[dict[str, Any]] = deque()
        self._snapshot = aggregate_snapshot(
            [unavailable_provider(provider, iso_now(), "waiting") for provider in providers],
            iso_now(),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="infra-upstream-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=min(2.0, self._poll_seconds + 0.1))

    def poll_once(self) -> dict[str, Any]:
        observed_at = iso_now()
        items: list[dict[str, Any]] = []
        for provider in self._providers:
            try:
                item = normalize_summary(provider, self._fetch_provider(provider), observed_at)
            except Exception as exc:
                item = unavailable_provider(provider, observed_at, type(exc).__name__)
                self._logger.warning("upstream status failed provider=%s kind=%s", provider.id, type(exc).__name__)
            items.append(item)
        snapshot = aggregate_snapshot(items, observed_at)
        with self._lock:
            self._record_transitions(items, observed_at)
            self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    def drain_transitions(self) -> list[dict[str, Any]]:
        with self._lock:
            transitions = list(self._transitions)
            self._transitions.clear()
        return transitions

    def _record_transitions(self, items: list[dict[str, Any]], observed_at: str) -> None:
        for item in items:
            provider_id = str(item["id"])
            current = str(item.get("status") or "unknown")
            previous = self._states.get(provider_id)
            if current not in TRACKABLE_STATES:
                continue
            self._states[provider_id] = current
            if previous is None or previous == current:
                continue
            if current == "healthy":
                event_type = "recovered"
            elif previous == "critical" and current == "warning":
                event_type = "deescalated"
            else:
                event_type = "alert"
            incidents = item.get("incidents") if isinstance(item.get("incidents"), list) else []
            self._transitions.append({
                "provider_id": provider_id,
                "label": str(item.get("label") or provider_id),
                "previous": previous,
                "level": current,
                "type": event_type,
                "description": str(incidents[0].get("name")) if incidents and isinstance(incidents[0], dict) else str(item.get("description") or current),
                "timestamp": observed_at,
            })

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_seconds)
