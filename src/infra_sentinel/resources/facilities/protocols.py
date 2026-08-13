"""Provider-owned facility protocol adapters discovered through Infra Discovery.

Each adapter owns one concrete application wire contract.  The normalized
observation returned here is private to Infra Sentinel and is not an Infra
Protocol application schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from infra_sentinel.resources.facilities.discovery import (
    DiscoveryError,
    DiscoveryOffer,
    DiscoveryPaths,
    Registration,
    UNIX_SOCKET_BINDING,
    resolve_unix_socket,
    validate_private_socket,
)


PROTOCOL_VERSION = "20260810.1"
DEV_MESH_OBSERVER_PROTOCOL_VERSION = "20260812.1"
NORMALIZED_SNAPSHOT_SCHEMA = "infra-sentinel.facility-observation"
NORMALIZED_SNAPSHOT_VERSION = "20260810.1"
MAX_U64 = (1 << 64) - 1
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
INFER_RUNTIME_USAGE_DAILY_SCHEMA = "infer-runtime.usage.daily"
INFER_RUNTIME_USAGE_DAILY_VERSION = "20260813.2"
_INFER_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}$")


class FacilityProtocolError(ValueError):
    """A selected provider violated its own application protocol."""


class FacilityTransportError(FacilityProtocolError):
    """A selected provider could not be reached for a transient observation."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FacilityProtocolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise FacilityProtocolError(f"non-standard JSON number {value!r}")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FacilityProtocolError(f"{name} must be an object")
    return value


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise FacilityProtocolError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FacilityProtocolError(f"{name} is invalid") from error
    if parsed.tzinfo is None:
        raise FacilityProtocolError(f"{name} requires an offset")
    return value


def _loopback_url(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise FacilityProtocolError("console URL is invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise FacilityProtocolError("console URL must use loopback HTTP(S)")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise FacilityProtocolError("console URL must use a literal loopback address") from error
    if not address.is_loopback:
        raise FacilityProtocolError("console URL must use a loopback address")
    return value


def _decode_json(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except FacilityProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FacilityProtocolError("provider response is not strict UTF-8 JSON") from error
    return _object(value, "provider response")


def _normalize_infer_runtime_usage_daily(extension: dict[str, Any]) -> dict[str, Any] | None:
    """Project the only Infer extension consumed by the AI usage collector.

    This is deliberately an optional extension to the Infer status protocol.
    Older Runtime builds stay observable as facilities, but their model usage is
    not eligible for Token projection until it contains the immutable
    ``execution_origin`` fact.  That is the fail-closed boundary which prevents
    a Codex-backed attempt from being counted twice.
    """
    raw_usage = extension.get("usage_daily")
    if not isinstance(raw_usage, dict):
        return None
    if (
        raw_usage.get("schema") != INFER_RUNTIME_USAGE_DAILY_SCHEMA
        or raw_usage.get("schema_version") != INFER_RUNTIME_USAGE_DAILY_VERSION
        or raw_usage.get("calendar") != "host_local"
    ):
        return None
    raw_days = raw_usage.get("days")
    if not isinstance(raw_days, list) or len(raw_days) > 1:
        return None
    normalized_days: list[dict[str, Any]] = []
    for raw_day in raw_days:
        if not isinstance(raw_day, dict) or set(raw_day) != {"date", "models"}:
            return None
        day = raw_day.get("date")
        models = raw_day.get("models")
        if not isinstance(day, str) or not isinstance(models, list) or len(models) > 128:
            return None
        try:
            date.fromisoformat(day)
        except ValueError:
            return None
        normalized_models: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw_model in models:
            if not isinstance(raw_model, dict):
                return None
            # An old ledger row without origin is intentionally invisible.  Do
            # not infer it from an identifier, provider, or display name.
            if set(raw_model) != {
                "id", "execution_origin", "input_tokens", "output_tokens", "total_tokens", "cost_usd",
            }:
                continue
            identifier = raw_model.get("id")
            origin = raw_model.get("execution_origin")
            if not isinstance(identifier, str) or not _INFER_MODEL_ID.fullmatch(identifier):
                return None
            if origin not in {"codex", "other"}:
                continue
            values: dict[str, int] = {}
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                value = raw_model.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_JSON_INTEGER:
                    return None
                values[field] = value
            cost = raw_model.get("cost_usd")
            if (
                isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or not math.isfinite(float(cost))
                or float(cost) < 0
            ):
                return None
            identity = (str(origin), identifier)
            if identity in seen:
                return None
            seen.add(identity)
            normalized_models.append({
                "id": identifier,
                "execution_origin": origin,
                **values,
                "cost_usd": float(cost),
            })
        normalized_days.append({"date": day, "models": normalized_models})
    return {
        "usage_daily": {
            "schema": INFER_RUNTIME_USAGE_DAILY_SCHEMA,
            "schema_version": INFER_RUNTIME_USAGE_DAILY_VERSION,
            "calendar": "host_local",
            "days": normalized_days,
        }
    }


def _exchange_line(
    endpoint: Path,
    request: bytes,
    *,
    response_limit: int,
    timeout: float,
    require_eof: bool,
) -> dict[str, Any]:
    try:
        validate_private_socket(endpoint)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(endpoint))
            connection.sendall(request)
            line = _read_response_frame(
                connection,
                response_limit=response_limit,
                require_eof=require_eof,
            )
    except FacilityProtocolError:
        raise
    except DiscoveryError as error:
        raise FacilityTransportError("provider endpoint is unavailable") from error
    except (OSError, TimeoutError) as error:
        raise FacilityTransportError("provider request failed") from error
    return _decode_json(line)


def _read_response_frame(
    connection: socket.socket,
    *,
    response_limit: int,
    require_eof: bool,
) -> bytes:
    data = bytearray()
    while b"\n" not in data:
        if len(data) >= response_limit:
            raise FacilityProtocolError("provider response exceeds its frame limit")
        chunk = connection.recv(min(65536, response_limit - len(data)))
        if not chunk:
            raise FacilityProtocolError("provider closed before a complete response frame")
        data.extend(chunk)
    line, trailing = bytes(data).split(b"\n", 1)
    if len(line) + 1 > response_limit:
        raise FacilityProtocolError("provider response exceeds its frame limit")
    if trailing:
        raise FacilityProtocolError("provider returned trailing bytes after its response frame")
    if require_eof and connection.recv(1):
        raise FacilityProtocolError("provider returned more than one response frame")
    return line


def _request(schema: str, schema_version: str = PROTOCOL_VERSION) -> bytes:
    return json.dumps(
        {"schema": schema, "schema_version": schema_version, "operation": "snapshot"},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class FacilityObservation:
    label: str
    status: str
    observed_at: str
    sequence: int
    snapshot: dict[str, Any]
    console_url: str | None = None


@dataclass(frozen=True)
class AdapterSelection:
    adapter: "FacilityProtocolAdapter"
    offer: DiscoveryOffer
    protocol_version: str


@dataclass(frozen=True)
class FacilityProtocolAdapter:
    protocol: str
    protocol_version: str
    request_schema: str
    snapshot_schema: str
    error_schema: str
    label: str
    response_limit: int
    timeout_seconds: float
    require_eof: bool
    nested_error: bool
    error_codes: frozenset[str]
    extension_key: str
    required_redactions: frozenset[str]
    allowed_reason_codes: frozenset[str] | None = None
    allowed_metric_ids: frozenset[str] | None = None
    required_headline_metrics: tuple[str, ...] | None = None
    allowed_issue_codes: frozenset[str] | None = None
    required_issue_subject_id: str | None = None
    require_scalar_metric_values: bool = False
    normalize_extension: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None

    def select(self, registration: Registration) -> AdapterSelection | None:
        matches = registration.compatible_offers(
            self.protocol,
            [self.protocol_version],
            [UNIX_SOCKET_BINDING],
        )
        if not matches:
            return None
        offer, version = matches[0]
        return AdapterSelection(self, offer, version)

    def observe(
        self,
        paths: DiscoveryPaths,
        registration: Registration,
        selection: AdapterSelection,
    ) -> FacilityObservation:
        endpoint = resolve_unix_socket(paths, selection.offer)
        raw = _exchange_line(
            endpoint,
            _request(self.request_schema, selection.protocol_version),
            response_limit=self.response_limit,
            timeout=self.timeout_seconds,
            require_eof=self.require_eof,
        )
        if raw.get("schema") == self.error_schema:
            code = self._error_code(raw, selection.protocol_version)
            raise FacilityProtocolError(
                f"provider rejected snapshot request: {code}"
            )
        return self._normalize(raw, registration, selection.protocol_version)

    def _error_code(self, raw: dict[str, Any], protocol_version: str) -> str:
        if raw.get("schema_version") != protocol_version:
            raise FacilityProtocolError("provider error schema version is unsupported")
        if self.nested_error:
            code = _object(raw.get("error"), "provider error").get("code")
        else:
            code = raw.get("code")
            if not isinstance(raw.get("message"), str):
                raise FacilityProtocolError("provider error message is invalid")
        if not isinstance(code, str) or code not in self.error_codes:
            raise FacilityProtocolError("provider error code is invalid")
        return code

    def _normalize(
        self,
        raw: dict[str, Any],
        registration: Registration,
        protocol_version: str,
    ) -> FacilityObservation:
        if raw.get("schema") != self.snapshot_schema or raw.get("schema_version") != protocol_version:
            raise FacilityProtocolError("provider snapshot schema is unsupported")
        service = _object(raw.get("service"), "snapshot.service")
        if (
            service.get("kind") != registration.kind
            or service.get("instance_id") != registration.instance_id
            or service.get("generation") != registration.generation
        ):
            raise FacilityProtocolError("provider snapshot identity does not match discovery")
        sequence = raw.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 <= sequence <= MAX_U64
        ):
            raise FacilityProtocolError("provider snapshot sequence is invalid")
        observed_at = _timestamp(raw.get("captured_at"), "snapshot.captured_at")
        status = _object(raw.get("status"), "snapshot.status")
        state = status.get("state")
        if state not in {"starting", "healthy", "degraded", "unavailable", "stopping"}:
            raise FacilityProtocolError("provider status is invalid")
        reasons = status.get("reason_codes")
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) and 0 < len(reason) <= 128 for reason in reasons
        ):
            raise FacilityProtocolError("provider reason codes are invalid")
        normalized_reasons = [
            reason
            for reason in reasons
            if self.allowed_reason_codes is None or reason in self.allowed_reason_codes
        ]

        raw_metrics = raw.get("metrics")
        if not isinstance(raw_metrics, list):
            raise FacilityProtocolError("provider metrics are invalid")
        metrics: list[dict[str, Any]] = []
        metric_ids: set[str] = set()
        for raw_metric in raw_metrics:
            metric = _object(raw_metric, "snapshot metric")
            metric_id = metric.get("id")
            if not isinstance(metric_id, str) or not 1 <= len(metric_id) <= 128:
                raise FacilityProtocolError("provider metric ID is invalid")
            if metric_id in metric_ids:
                raise FacilityProtocolError("provider metric IDs must be unique")
            metric_ids.add(metric_id)
            if self.allowed_metric_ids is not None and metric_id not in self.allowed_metric_ids:
                continue
            if metric.get("kind") not in {"gauge", "counter", "state"}:
                raise FacilityProtocolError("provider metric kind is invalid")
            value = metric.get("value")
            if value is None:
                raise FacilityProtocolError("provider metric value is invalid")
            if not isinstance(value, (str, int, float, bool)):
                if self.require_scalar_metric_values:
                    raise FacilityProtocolError("provider metric value must be scalar")
                # Provider protocols permit non-null structured JSON values.
                # The private Sentinel projection keeps only displayable scalars.
                continue
            unit = metric.get("unit")
            if unit is not None and (not isinstance(unit, str) or not 1 <= len(unit) <= 64):
                raise FacilityProtocolError("provider metric unit is invalid")
            window = metric.get("window_seconds")
            if window is not None and (
                isinstance(window, bool)
                or not isinstance(window, int)
                or not 0 <= window <= MAX_U64
            ):
                raise FacilityProtocolError("provider metric window is invalid")
            dimensions = metric.get("dimensions")
            if dimensions is not None:
                if not (
                    isinstance(dimensions, dict)
                    and len(dimensions) <= 16
                    and all(
                    isinstance(key, str)
                    and 0 < len(key) <= 64
                    and isinstance(dimension, str)
                    and len(dimension) <= 128
                    for key, dimension in dimensions.items()
                    )
                ):
                    # Dimensions are provider-owned arbitrary JSON in PCP and a
                    # string map in Infer. They are optional presentation data.
                    dimensions = None
            normalized = {
                key: metric[key]
                for key in ("id", "kind", "value", "unit", "window_seconds")
                if key in metric
            }
            if dimensions is not None:
                normalized["dimensions"] = dimensions
            metrics.append(normalized)

        headline = raw.get("headline_metrics")
        if (
            not isinstance(headline, list)
            or len(headline) > 3
            or not all(isinstance(metric_id, str) for metric_id in headline)
            or len(set(headline)) != len(headline)
            or not all(metric_id in metric_ids for metric_id in headline)
        ):
            raise FacilityProtocolError("provider headline metrics are invalid")
        if (
            self.required_headline_metrics is not None
            and tuple(headline) != self.required_headline_metrics
        ):
            raise FacilityProtocolError("provider headline metrics do not match its contract")
        display_metrics = metrics[:512]
        display_metric_ids = {str(metric["id"]) for metric in display_metrics}
        normalized_headline = [metric_id for metric_id in headline if metric_id in display_metric_ids]
        raw_issues = raw.get("issues")
        if not isinstance(raw_issues, list):
            raise FacilityProtocolError("provider issues are invalid")
        issues: list[dict[str, Any]] = []
        for raw_issue in raw_issues[:64]:
            issue = _object(raw_issue, "snapshot issue")
            code = issue.get("code")
            if not isinstance(code, str) or not 1 <= len(code) <= 128:
                raise FacilityProtocolError("provider issue code is invalid")
            if self.allowed_issue_codes is not None and code not in self.allowed_issue_codes:
                continue
            severity = issue.get("severity")
            observed = issue.get("observed_at")
            if severity not in {"info", "warning", "critical"}:
                raise FacilityProtocolError("provider issue severity is invalid")
            _timestamp(observed, "snapshot issue observed_at")
            normalized_issue = {"code": code, "severity": severity, "observed_at": observed}
            subject = issue.get("subject_id")
            if self.required_issue_subject_id is not None:
                if subject != self.required_issue_subject_id:
                    raise FacilityProtocolError("provider issue subject does not match its contract")
                normalized_issue["subject_id"] = subject
            elif subject is not None:
                if not isinstance(subject, str) or not 1 <= len(subject) <= 128:
                    raise FacilityProtocolError("provider issue subject is invalid")
                normalized_issue["subject_id"] = subject
            issues.append(normalized_issue)

        redaction = _object(raw.get("redaction"), "snapshot.redaction")
        excluded = redaction.get("excluded")
        if set(redaction) != {"excluded"} or not isinstance(excluded, list) or not all(
            isinstance(item, str) and 0 < len(item) <= 128 for item in excluded
        ):
            raise FacilityProtocolError("provider redaction declaration is invalid")
        if not self.required_redactions.issubset(excluded):
            raise FacilityProtocolError("provider redaction declaration is incomplete")

        extensions = _object(raw.get("extensions"), "snapshot.extensions")
        provider_extension = _object(
            extensions.get(self.extension_key),
            f"snapshot.extensions.{self.extension_key}",
        )

        console_url: str | None = None
        if "links" in raw:
            links = _object(raw["links"], "snapshot.links")
            if "console_url" in links:
                console_url = _loopback_url(links["console_url"])
        normalized_snapshot = {
            "schema": NORMALIZED_SNAPSHOT_SCHEMA,
            "schema_version": NORMALIZED_SNAPSHOT_VERSION,
            "captured_at": observed_at,
            "sequence": sequence,
            "status": {"state": state, "reason_codes": normalized_reasons},
            "headline_metrics": normalized_headline,
            "metrics": display_metrics,
            "issues": issues,
        }
        if self.normalize_extension is not None:
            normalized_extension = self.normalize_extension(provider_extension)
            if normalized_extension is not None:
                normalized_snapshot["extensions"] = {self.extension_key: normalized_extension}
        return FacilityObservation(
            self.label,
            str(state),
            observed_at,
            sequence,
            normalized_snapshot,
            console_url,
        )


PCP_ADAPTER = FacilityProtocolAdapter(
    protocol="pcp.runtime.observer",
    protocol_version=PROTOCOL_VERSION,
    request_schema="pcp.runtime.observer.request",
    snapshot_schema="pcp.runtime.observer.snapshot",
    error_schema="pcp.runtime.observer.error",
    label="PCP",
    response_limit=1024 * 1024,
    timeout_seconds=5.0,
    require_eof=False,
    nested_error=False,
    error_codes=frozenset({"invalid_request", "snapshot_unavailable", "response_too_large"}),
    extension_key="pcp",
    required_redactions=frozenset({
        "page_content", "query_text", "scope_names", "raw_audit", "storage_paths",
    }),
)

INFER_RUNTIME_ADAPTER = FacilityProtocolAdapter(
    protocol="infer-runtime.status",
    protocol_version=PROTOCOL_VERSION,
    request_schema="infer-runtime.status.request",
    snapshot_schema="infer-runtime.status.snapshot",
    error_schema="infer-runtime.status.error",
    label="Infer Runtime",
    response_limit=256 * 1024,
    timeout_seconds=2.0,
    require_eof=True,
    nested_error=True,
    error_codes=frozenset({"invalid_request", "snapshot_unavailable"}),
    extension_key="infer-runtime",
    required_redactions=frozenset({
        "credentials", "filesystem_paths", "job_identifiers", "job_metadata",
        "payloads", "raw_errors", "usage_ledger",
    }),
    normalize_extension=_normalize_infer_runtime_usage_daily,
)

DEV_MESH_OBSERVER_ADAPTER = FacilityProtocolAdapter(
    protocol="dev-mesh.observer.status",
    protocol_version=DEV_MESH_OBSERVER_PROTOCOL_VERSION,
    request_schema="dev-mesh.observer.status.request",
    snapshot_schema="dev-mesh.observer.status.snapshot",
    error_schema="dev-mesh.observer.status.error",
    label="Dev Mesh Observer",
    response_limit=256 * 1024,
    timeout_seconds=2.0,
    require_eof=True,
    nested_error=True,
    error_codes=frozenset({"invalid_request", "snapshot_unavailable"}),
    extension_key="dev-mesh-observer",
    required_redactions=frozenset({
        "coordination_owner_ids", "workspace_paths", "git_revisions",
        "branch_names", "event_payloads", "raw_errors", "database_paths",
        "claim_scopes",
    }),
    allowed_reason_codes=frozenset({
        "collection_failed", "collection_stale", "workspace_unavailable",
        "integrity_issue", "contention_stalled", "no_workspaces_registered",
    }),
    allowed_metric_ids=frozenset({
        "dev_mesh.workspaces.registered",
        "dev_mesh.workspaces.available",
        "dev_mesh.workspaces.unavailable",
        "dev_mesh.collection.pending_events",
        "dev_mesh.collection.last_success_age",
        "dev_mesh.collection.running",
        "dev_mesh.integrity.issues",
        "dev_mesh.events.mirrored",
        "dev_mesh.contentions.active",
        "dev_mesh.contentions.stalled",
    }),
    required_headline_metrics=(
        "dev_mesh.workspaces.available",
        "dev_mesh.collection.pending_events",
        "dev_mesh.contentions.stalled",
    ),
    allowed_issue_codes=frozenset({
        "dev_mesh.collection.failed",
        "dev_mesh.collection.stale",
        "dev_mesh.workspace.unavailable",
        "dev_mesh.workspace.none_registered",
        "dev_mesh.integrity.issue",
        "dev_mesh.contention.stalled",
        "dev_mesh.collection.backlog",
    }),
    required_issue_subject_id="observer",
    require_scalar_metric_values=True,
)

DEFAULT_ADAPTERS: tuple[FacilityProtocolAdapter, ...] = (
    PCP_ADAPTER,
    INFER_RUNTIME_ADAPTER,
    DEV_MESH_OBSERVER_ADAPTER,
)


def select_adapter(
    registration: Registration,
    adapters: Iterable[FacilityProtocolAdapter] = DEFAULT_ADAPTERS,
) -> AdapterSelection | None:
    for adapter in adapters:
        if selection := adapter.select(registration):
            return selection
    return None
