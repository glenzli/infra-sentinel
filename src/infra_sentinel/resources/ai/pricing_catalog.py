"""Validated, release-backed API price catalog with an offline fallback.

The updater downloads the same public Release assets for every check. It does
not send observed model identifiers, token counts, or any other local usage
data. Only a fully validated catalog replaces the last known-good local copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import ssl
import threading
import time
import tomllib
from typing import Callable, Protocol
from urllib.request import Request, urlopen

import certifi


CATALOG_SCHEMA = "api-price/v1"
MANIFEST_SCHEMA = "api-price-manifest/v1"
CATALOG_ASSET = "api-price.toml"
LATEST_RELEASE_BASE_URL = "https://github.com/glenzli/api-price/releases/latest/download"
LATEST_MANIFEST_URL = f"{LATEST_RELEASE_BASE_URL}/api-price.manifest.json"
LATEST_CATALOG_URL = f"{LATEST_RELEASE_BASE_URL}/{CATALOG_ASSET}"
MAX_CATALOG_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
PERIODIC_CHECK_SECONDS = 24 * 60 * 60
MISSING_MODEL_CHECK_SECONDS = 6 * 60 * 60
REMOTE_TIMEOUT_SECONDS = 4

_ROOT_KEYS = {"schema", "catalog_version", "updated_at", "currency", "unit", "references", "prices"}
_REFERENCE_KEYS = {"id", "url", "checked_at"}
_PRICE_KEYS = {
    "scope", "model", "reference", "valid_from", "valid_until", "input_per_million",
    "cached_input_per_million", "cache_write_per_million", "output_per_million", "note",
}
_REQUIRED_PRICE_KEYS = {
    "scope", "model", "reference", "valid_from", "input_per_million",
    "cached_input_per_million", "output_per_million",
}
_MANIFEST_KEYS = {"schema", "catalog_schema", "catalog_version", "asset", "size", "sha256"}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")
_VERSION_RE = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class TextTokenPrice:
    """One effective text-token rate in USD per million tokens."""

    input_per_million: float
    cached_input_per_million: float
    cache_write_per_million: float | None
    output_per_million: float
    reference: str
    note: str | None = None


@dataclass(frozen=True)
class _PricePeriod:
    scope: str
    model: str
    valid_from: date
    valid_until: date | None
    price: TextTokenPrice


class PriceCatalogLookup(Protocol):
    @property
    def catalog_version(self) -> str: ...

    @property
    def checked_at(self) -> str: ...

    def price_for(self, scope: str, model: str, usage_date: str | date | None = None) -> TextTokenPrice | None: ...


class PricingCatalog:
    """Immutable exact-ID catalog selected by inclusive effective dates."""

    def __init__(
        self,
        *,
        catalog_version: str,
        updated_at: str,
        checked_at: str,
        periods: tuple[_PricePeriod, ...],
        raw: bytes,
    ) -> None:
        self.catalog_version = catalog_version
        self.updated_at = updated_at
        self.checked_at = checked_at
        self.periods = periods
        self.raw = raw
        self.sha256 = hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "PricingCatalog":
        if not raw or len(raw) > MAX_CATALOG_BYTES:
            raise ValueError("catalog size is invalid")
        document = tomllib.loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("catalog root must be a table")
        _exact_keys(document, _ROOT_KEYS, where="catalog")
        if document["schema"] != CATALOG_SCHEMA:
            raise ValueError("unsupported catalog schema")
        version = _text(document["catalog_version"], where="catalog_version")
        if not _VERSION_RE.fullmatch(version):
            raise ValueError("catalog_version must use YYYY.MM.DD.N")
        updated_at = _text(document["updated_at"], where="updated_at")
        datetime.fromisoformat(updated_at)
        if document["currency"] != "USD" or document["unit"] != "per_million_tokens":
            raise ValueError("catalog supports only USD per-million-token rates")

        raw_references = document["references"]
        if not isinstance(raw_references, list) or not raw_references:
            raise ValueError("references must be a non-empty array")
        references: set[str] = set()
        checked_days: list[date] = []
        for index, reference in enumerate(raw_references):
            if not isinstance(reference, dict):
                raise ValueError(f"references[{index}] must be a table")
            _exact_keys(reference, _REFERENCE_KEYS, where=f"references[{index}]")
            identifier = _text(reference["id"], where=f"references[{index}].id")
            if not _ID_RE.fullmatch(identifier) or identifier in references:
                raise ValueError(f"references[{index}].id is invalid or duplicated")
            url = _text(reference["url"], where=f"references[{index}].url")
            if not url.startswith("https://") or "@" in url.split("/", 3)[2]:
                raise ValueError(f"references[{index}].url must be public HTTPS")
            checked_days.append(_date(reference["checked_at"], where=f"references[{index}].checked_at"))
            references.add(identifier)

        raw_prices = document["prices"]
        if not isinstance(raw_prices, list) or not raw_prices:
            raise ValueError("prices must be a non-empty array")
        periods: list[_PricePeriod] = []
        grouped: dict[tuple[str, str], list[_PricePeriod]] = {}
        for index, row in enumerate(raw_prices):
            if not isinstance(row, dict):
                raise ValueError(f"prices[{index}] must be a table")
            _exact_keys(row, _PRICE_KEYS, required=_REQUIRED_PRICE_KEYS, where=f"prices[{index}]")
            scope = _text(row["scope"], where=f"prices[{index}].scope")
            model = _text(row["model"], where=f"prices[{index}].model")
            reference = _text(row["reference"], where=f"prices[{index}].reference")
            if scope not in {"codex", "antigravity"} or not _ID_RE.fullmatch(model):
                raise ValueError(f"prices[{index}] has an unsupported scope or model")
            if reference not in references:
                raise ValueError(f"prices[{index}] uses an unknown reference")
            start = _date(row["valid_from"], where=f"prices[{index}].valid_from")
            end = _date(row["valid_until"], where=f"prices[{index}].valid_until") if "valid_until" in row else None
            if end is not None and end < start:
                raise ValueError(f"prices[{index}] has an inverted date range")
            period = _PricePeriod(
                scope=scope,
                model=model,
                valid_from=start,
                valid_until=end,
                price=TextTokenPrice(
                    input_per_million=_rate(row["input_per_million"], where=f"prices[{index}].input_per_million"),
                    cached_input_per_million=_rate(row["cached_input_per_million"], where=f"prices[{index}].cached_input_per_million"),
                    cache_write_per_million=(
                        _rate(row["cache_write_per_million"], where=f"prices[{index}].cache_write_per_million")
                        if "cache_write_per_million" in row else None
                    ),
                    output_per_million=_rate(row["output_per_million"], where=f"prices[{index}].output_per_million"),
                    reference=reference,
                    note=_text(row["note"], where=f"prices[{index}].note") if "note" in row else None,
                ),
            )
            periods.append(period)
            grouped.setdefault((scope, model), []).append(period)

        for key, values in grouped.items():
            values.sort(key=lambda item: item.valid_from)
            for previous, current in zip(values, values[1:]):
                if previous.valid_until is None or current.valid_from <= previous.valid_until:
                    raise ValueError(f"overlapping date ranges for {key[0]}:{key[1]}")
        return cls(
            catalog_version=version,
            updated_at=updated_at,
            checked_at=max(checked_days).isoformat(),
            periods=tuple(periods),
            raw=raw,
        )

    def price_for(
        self,
        scope: str,
        model: str,
        usage_date: str | date | None = None,
    ) -> TextTokenPrice | None:
        when = date.today() if usage_date is None else (
            usage_date if isinstance(usage_date, date) else date.fromisoformat(usage_date)
        )
        for period in self.periods:
            if period.scope != scope or period.model != model or when < period.valid_from:
                continue
            if period.valid_until is None or when <= period.valid_until:
                return period.price
        return None


@lru_cache(maxsize=1)
def bundled_pricing_catalog() -> PricingCatalog:
    return PricingCatalog.from_bytes(Path(__file__).with_name(CATALOG_ASSET).read_bytes())


Fetcher = Callable[[str, int], bytes]


class PricingCatalogManager:
    """Own the installed catalog, update cadence, and last-known-good files."""

    def __init__(
        self,
        state_dir: Path,
        *,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._catalog_path = state_dir / CATALOG_ASSET
        self._previous_path = state_dir / "api-price.previous.toml"
        self._state_path = state_dir / "api-price-state.json"
        self._fetcher = fetcher or _fetch
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._catalog, self._source = self._load_best_available()

    @property
    def catalog_version(self) -> str:
        with self._lock:
            return self._catalog.catalog_version

    @property
    def checked_at(self) -> str:
        with self._lock:
            return self._catalog.checked_at

    def price_for(
        self,
        scope: str,
        model: str,
        usage_date: str | date | None = None,
    ) -> TextTokenPrice | None:
        with self._lock:
            price = self._catalog.price_for(scope, model, usage_date)
        if price is None:
            self.schedule_refresh_if_due(MISSING_MODEL_CHECK_SECONDS)
        return price

    def status(self) -> dict[str, object]:
        with self._lock:
            state = self._read_state()
            return {
                "catalog_version": self._catalog.catalog_version,
                "checked_at": self._catalog.checked_at,
                "source": self._source,
                "last_remote_check_at": state.get("last_remote_check_at"),
                "last_error": state.get("last_error"),
            }

    def schedule_periodic_refresh(self) -> bool:
        return self.schedule_refresh_if_due(PERIODIC_CHECK_SECONDS)

    def schedule_refresh_if_due(self, interval_seconds: float) -> bool:
        with self._lock:
            if self._refreshing or not self._is_due(interval_seconds):
                return False
            self._refreshing = True
        thread = threading.Thread(target=self._background_refresh, name="api-price-refresh", daemon=True)
        thread.start()
        return True

    def _background_refresh(self) -> None:
        try:
            result = self.refresh(force=False)
            self._logger.info(
                "API price catalog remote check status=%s version=%s",
                result["status"], result["catalog_version"],
            )
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self, *, force: bool = False) -> dict[str, object]:
        if not self._refresh_lock.acquire(blocking=False):
            return {"status": "checking", **self.status()}
        try:
            if not force and not self._is_due(MISSING_MODEL_CHECK_SECONDS):
                return {"status": "current", **self.status()}
            now = self._clock()
            try:
                raw_manifest = self._fetcher(LATEST_MANIFEST_URL, MAX_MANIFEST_BYTES)
                manifest = _manifest(raw_manifest)
                with self._lock:
                    current = self._catalog
                if manifest["sha256"] == current.sha256:
                    self._write_state(now, last_error=None)
                    return {"status": "current", **self.status()}
                if manifest["catalog_version"] == current.catalog_version:
                    raise ValueError("catalog content changed without a new version")
                raw_catalog = self._fetcher(LATEST_CATALOG_URL, MAX_CATALOG_BYTES)
                if len(raw_catalog) != manifest["size"] or hashlib.sha256(raw_catalog).hexdigest() != manifest["sha256"]:
                    raise ValueError("downloaded catalog does not match manifest")
                catalog = PricingCatalog.from_bytes(raw_catalog)
                if catalog.catalog_version != manifest["catalog_version"]:
                    raise ValueError("downloaded catalog version does not match manifest")
                if _version_key(catalog.catalog_version) < _version_key(current.catalog_version):
                    raise ValueError("latest release would downgrade the active catalog")
                if self._catalog_path.exists():
                    try:
                        previous = PricingCatalog.from_bytes(self._catalog_path.read_bytes())
                    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
                        previous = None
                    if previous is not None:
                        _atomic_write(self._previous_path, previous.raw)
                _atomic_write(self._catalog_path, raw_catalog)
                with self._lock:
                    self._catalog = catalog
                    self._source = "downloaded"
                self._write_state(now, last_error=None)
                return {"status": "updated", **self.status()}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
                self._write_state(now, last_error=f"{type(exc).__name__}: {exc}")
                return {"status": "error", **self.status()}
        finally:
            self._refresh_lock.release()

    def _load_best_available(self) -> tuple[PricingCatalog, str]:
        candidates: list[tuple[PricingCatalog, str]] = [(bundled_pricing_catalog(), "bundled")]
        for path, source in ((self._catalog_path, "downloaded"), (self._previous_path, "previous")):
            try:
                candidates.append((PricingCatalog.from_bytes(path.read_bytes()), source))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
                continue
        return max(candidates, key=lambda item: _version_key(item[0].catalog_version))

    def _is_due(self, interval_seconds: float) -> bool:
        raw = self._read_state().get("last_remote_check_epoch")
        if not isinstance(raw, (int, float)):
            return True
        elapsed = self._clock() - float(raw)
        return elapsed < 0 or elapsed >= interval_seconds

    def _read_state(self) -> dict[str, object]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, epoch: float, *, last_error: str | None) -> None:
        timestamp = datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
        payload: dict[str, object] = {
            "schema": 1,
            "last_remote_check_epoch": epoch,
            "last_remote_check_at": timestamp,
        }
        if last_error:
            payload["last_error"] = last_error[:500]
        _atomic_write(self._state_path, (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


def _fetch(url: str, maximum: int) -> bytes:
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "infra-sentinel-api-price/1"})
    tls_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS, context=tls_context) as response:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > maximum:
            raise ValueError("remote asset is too large")
        payload = response.read(maximum + 1)
    if not payload or len(payload) > maximum:
        raise ValueError("remote asset size is invalid")
    return payload


def _manifest(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest size is invalid")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    _exact_keys(value, _MANIFEST_KEYS, where="manifest")
    if value["schema"] != MANIFEST_SCHEMA or value["catalog_schema"] != CATALOG_SCHEMA:
        raise ValueError("unsupported manifest schema")
    if value["asset"] != CATALOG_ASSET:
        raise ValueError("manifest asset name is invalid")
    version = _text(value["catalog_version"], where="manifest.catalog_version")
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("manifest catalog version is invalid")
    size = value["size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_CATALOG_BYTES:
        raise ValueError("manifest catalog size is invalid")
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError("manifest SHA-256 is invalid")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _version_key(value: str) -> tuple[int, int, int, int]:
    if not _VERSION_RE.fullmatch(value):
        raise ValueError("invalid catalog version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _exact_keys(
    value: dict[str, object],
    allowed: set[str],
    *,
    where: str,
    required: set[str] | None = None,
) -> None:
    unknown = set(value) - allowed
    missing = (required or allowed) - set(value)
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{where} is missing keys: {', '.join(sorted(missing))}")


def _text(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError(f"{where} must be a non-empty bounded string")
    return value


def _date(value: object, *, where: str) -> date:
    try:
        return date.fromisoformat(_text(value, where=where))
    except ValueError as exc:
        raise ValueError(f"{where} must be an ISO date") from exc


def _rate(value: object, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 10_000:
        raise ValueError(f"{where} is outside the allowed range")
    return parsed
