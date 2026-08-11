"""Host backend selection; concrete adapters are validated per target OS."""

from __future__ import annotations

from importlib import import_module
import sys

from infra_sentinel.resources.system.contract import SystemResourceBackend

_BACKENDS = {
    "darwin": ("infra_sentinel.resources.system.backends.macos", "MacOSSystemBackend"),
    "linux": ("infra_sentinel.resources.system.backends.linux", "LinuxSystemBackend"),
    "win32": ("infra_sentinel.resources.system.backends.windows", "WindowsSystemBackend"),
}


def create_system_backend(platform: str | None = None) -> SystemResourceBackend:
    """Select one declared backend without leaking OS branches into collectors."""
    selected = platform or sys.platform
    platform_key = "linux" if selected.startswith("linux") else selected
    try:
        module_name, type_name = _BACKENDS[platform_key]
    except KeyError as error:
        raise RuntimeError(f"system resource collection is unsupported on {selected}") from error
    builder = getattr(import_module(module_name), type_name)
    return builder()


__all__ = ["create_system_backend"]
