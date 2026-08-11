"""Infra Discovery registration parsing and local binding resolution.

This module owns discovery only: runtime-root resolution, owner-only manifest
validation, exact offer matching, and safe endpoint resolution.
Application requests and responses belong to provider-specific adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Iterable


DISCOVERY_SCHEMA = "infra.discovery.registration"
DISCOVERY_VERSION = "20260812.1"
RUNTIME_DIRECTORY_ENV = "INFRA_PROTOCOL_RUNTIME_DIR"
UNIX_SOCKET_BINDING = "infra.local.unix-socket"
WINDOWS_PIPE_BINDING = "infra.local.windows-named-pipe"
MAX_MANIFEST_BYTES = 64 * 1024

_SERVICE_KIND = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_FILE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTRACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@%-]*$")
_CONTRACT_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_UNIX_ENDPOINT = re.compile(r"^sockets/[A-Za-z0-9][A-Za-z0-9._-]{0,15}\.sock$")
_WINDOWS_ENDPOINT = re.compile(
    r"^\\\\\.\\pipe\\infra-protocol\\[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)


class DiscoveryError(ValueError):
    """A runtime root, registration, or endpoint is invalid."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DiscoveryError(f"non-standard JSON number {value!r}")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscoveryError(f"{name} must be an object")
    return value


def _exact_keys(
    value: dict[str, Any], *, allowed: set[str], required: set[str], name: str
) -> None:
    if missing := required - set(value):
        raise DiscoveryError(f"{name} is missing {sorted(missing)!r}")
    if unknown := set(value) - allowed:
        raise DiscoveryError(f"{name} contains unsupported fields {sorted(unknown)!r}")


def _text(value: Any, name: str, maximum: int, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not pattern.fullmatch(value):
        raise DiscoveryError(f"{name} is invalid")
    return value


def _darwin_user_temp_dir() -> Path:
    try:
        result = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiscoveryError("macOS DARWIN_USER_TEMP_DIR is unavailable") from error
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        raise DiscoveryError("macOS DARWIN_USER_TEMP_DIR must be absolute")
    return path


def _windows_local_app_data() -> Path:
    if os.name != "nt":
        raise DiscoveryError("Windows Local App Data is unavailable")
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folder = GUID(
            0xF1B32785,
            0x6FBA,
            0x4FCF,
            (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
        )
        pointer = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder), 0, None, ctypes.byref(pointer)
        )
        if result != 0 or not pointer.value:
            raise DiscoveryError("Windows Local App Data is unavailable")
        try:
            return Path(pointer.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pointer)
    except (AttributeError, OSError, ValueError) as error:
        raise DiscoveryError("Windows Local App Data is unavailable") from error


def runtime_root(
    *,
    platform: str | None = None,
    environment: dict[str, str] | None = None,
    darwin_user_temp_dir: Path | None = None,
    windows_local_app_data: Path | None = None,
) -> Path:
    """Resolve the canonical final Infra Protocol runtime root."""

    current_platform = sys.platform if platform is None else platform
    current_environment = os.environ if environment is None else environment
    if override := current_environment.get(RUNTIME_DIRECTORY_ENV):
        path = Path(override)
        if not path.is_absolute():
            raise DiscoveryError(f"{RUNTIME_DIRECTORY_ENV} must be absolute")
        return path
    if current_platform == "darwin":
        base = darwin_user_temp_dir or _darwin_user_temp_dir()
        if not base.is_absolute():
            raise DiscoveryError("macOS runtime directory must be absolute")
        return base / "infra-protocol"
    if current_platform.startswith("linux"):
        raw = current_environment.get("XDG_RUNTIME_DIR")
        if not raw or not Path(raw).is_absolute():
            raise DiscoveryError("Linux requires XDG_RUNTIME_DIR or an absolute override")
        return Path(raw) / "infra-protocol"
    if current_platform == "win32":
        base = windows_local_app_data or _windows_local_app_data()
        if not base.is_absolute():
            raise DiscoveryError("Windows Local App Data must be absolute")
        return base / "Infra Protocol" / "Runtime"
    raise DiscoveryError(f"unsupported platform {current_platform!r}")


@dataclass(frozen=True)
class DiscoveryPaths:
    root: Path
    registrations: Path
    sockets: Path


def discovery_paths(root: Path | None = None) -> DiscoveryPaths:
    selected = runtime_root() if root is None else root
    if not selected.is_absolute():
        raise DiscoveryError("Infra Protocol runtime root must be absolute")
    return DiscoveryPaths(selected, selected / "registrations", selected / "sockets")


def validate_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise DiscoveryError(f"cannot read discovery directory {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DiscoveryError(f"discovery path is not a directory: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise DiscoveryError(f"discovery directory is owned by another user: {path}")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700:
        raise DiscoveryError(f"discovery directory must use mode 0700: {path}")


def validate_runtime_paths(paths: DiscoveryPaths) -> None:
    validate_private_directory(paths.root)
    validate_private_directory(paths.registrations)
    if os.name != "nt":
        validate_private_directory(paths.sockets)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as error:
        raise DiscoveryError("registration cannot be read") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DiscoveryError("registration must be a non-symlink regular file")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise DiscoveryError("registration exceeds 64 KiB")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise DiscoveryError("registration is owned by another user")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
        raise DiscoveryError("registration must use mode 0600")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except DiscoveryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DiscoveryError("registration is not strict UTF-8 JSON") from error
    return _object(value, "registration")


@dataclass(frozen=True)
class DiscoveryOffer:
    protocol: str
    protocol_versions: tuple[str, ...]
    binding: str
    endpoint: str


@dataclass(frozen=True)
class Registration:
    path: Path
    kind: str
    instance_id: str
    generation: str
    offers: tuple[DiscoveryOffer, ...]

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.instance_id}"

    def compatible_offers(
        self,
        protocol: str,
        versions: Iterable[str],
        bindings: Iterable[str],
    ) -> list[tuple[DiscoveryOffer, str]]:
        version_preference = tuple(dict.fromkeys(versions))
        binding_preference = tuple(dict.fromkeys(bindings))
        matches: list[tuple[int, DiscoveryOffer, str]] = []
        for offer in self.offers:
            if offer.protocol != protocol or offer.binding not in binding_preference:
                continue
            advertised = set(offer.protocol_versions)
            selected = next((version for version in version_preference if version in advertised), None)
            if selected is not None:
                matches.append((binding_preference.index(offer.binding), offer, selected))
        matches.sort(key=lambda item: item[0])
        return [(offer, version) for _, offer, version in matches]


def read_registration(path: Path) -> Registration:
    root = _load_manifest(path)
    _exact_keys(
        root,
        allowed={"schema", "schema_version", "service", "offers"},
        required={"schema", "schema_version", "service", "offers"},
        name="registration",
    )
    if root.get("schema") != DISCOVERY_SCHEMA or root.get("schema_version") != DISCOVERY_VERSION:
        raise DiscoveryError("unsupported discovery registration")
    service = _object(root["service"], "service")
    _exact_keys(
        service,
        allowed={"kind", "instance_id", "generation"},
        required={"kind", "instance_id", "generation"},
        name="service",
    )
    kind = _text(service["kind"], "service.kind", 64, _SERVICE_KIND)
    instance_id = _text(service["instance_id"], "service.instance_id", 96, _FILE_TOKEN)
    generation = _text(service["generation"], "service.generation", 96, _FILE_TOKEN)
    if path.name != f"{kind}--{instance_id}.json":
        raise DiscoveryError("registration filename does not match stable identity")

    raw_offers = root["offers"]
    if not isinstance(raw_offers, list) or not 1 <= len(raw_offers) <= 64:
        raise DiscoveryError("offers must contain 1 to 64 entries")
    offers: list[DiscoveryOffer] = []
    for index, raw_offer in enumerate(raw_offers):
        offer = _object(raw_offer, f"offers[{index}]")
        _exact_keys(
            offer,
            allowed={"protocol", "protocol_versions", "binding", "endpoint"},
            required={"protocol", "protocol_versions", "binding", "endpoint"},
            name=f"offers[{index}]",
        )
        protocol = _text(offer["protocol"], "offer.protocol", 128, _CONTRACT_ID)
        binding = _text(offer["binding"], "offer.binding", 128, _CONTRACT_ID)
        versions = offer["protocol_versions"]
        if not isinstance(versions, list) or not 1 <= len(versions) <= 16:
            raise DiscoveryError("offer.protocol_versions is invalid")
        parsed_versions = tuple(
            _text(version, "offer protocol version", 64, _CONTRACT_VERSION)
            for version in versions
        )
        if len(set(parsed_versions)) != len(parsed_versions):
            raise DiscoveryError("offer.protocol_versions contains duplicates")
        endpoint = offer["endpoint"]
        if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 512:
            raise DiscoveryError("offer.endpoint is invalid")
        if binding == UNIX_SOCKET_BINDING and not _UNIX_ENDPOINT.fullmatch(endpoint):
            raise DiscoveryError("Unix endpoint is invalid")
        if binding == WINDOWS_PIPE_BINDING and not _WINDOWS_ENDPOINT.fullmatch(endpoint):
            raise DiscoveryError("Windows named-pipe endpoint is invalid")
        offers.append(DiscoveryOffer(protocol, parsed_versions, binding, endpoint))
    return Registration(
        path=path,
        kind=kind,
        instance_id=instance_id,
        generation=generation,
        offers=tuple(offers),
    )


def resolve_unix_socket(paths: DiscoveryPaths, offer: DiscoveryOffer) -> Path:
    if offer.binding != UNIX_SOCKET_BINDING or not _UNIX_ENDPOINT.fullmatch(offer.endpoint):
        raise DiscoveryError("offer is not a canonical Unix Socket binding")
    endpoint = paths.root / offer.endpoint
    try:
        endpoint.relative_to(paths.sockets)
    except ValueError as error:
        raise DiscoveryError("Unix endpoint escapes the sockets directory") from error
    capacity = 104 if sys.platform == "darwin" else 108
    required = len(os.fsencode(str(endpoint))) + 1
    if required > capacity:
        raise DiscoveryError(
            f"Unix socket path requires {required} bytes; maximum is {capacity}"
        )
    return endpoint


def validate_private_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise DiscoveryError("Unix Socket endpoint cannot be read") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise DiscoveryError("Unix endpoint must be a non-symlink socket")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise DiscoveryError("Unix Socket endpoint is owned by another user")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
        raise DiscoveryError("Unix Socket endpoint must use mode 0600")
