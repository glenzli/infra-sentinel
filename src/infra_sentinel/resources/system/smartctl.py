"""Optional, bounded NVMe SMART reads from an already installed smartctl.

Only normalized health scalars leave this adapter. Device paths, model names,
serial numbers and raw command output are never retained in the snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any


DEFAULT_SMART_INTERVAL_SECONDS = 600
MAX_DEVICES = 4
MAX_OUTPUT_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 3.0
_MAX_SAFE_INTEGER = 2 ** 53 - 1
_NVME_DATA_UNIT_BYTES = 512_000
_TOOL_PATHS = (Path("/opt/homebrew/bin/smartctl"), Path("/usr/local/bin/smartctl"))


@dataclass(frozen=True)
class SmartDeviceSnapshot:
    index: int
    state: str
    temperature_celsius: int | None = None
    percentage_used: int | None = None
    available_spare_percent: int | None = None
    lifetime_read_bytes: int | None = None
    lifetime_write_bytes: int | None = None
    power_on_hours: int | None = None
    media_errors: int | None = None


@dataclass(frozen=True)
class DiskSmartSnapshot:
    observed_at: str
    devices: tuple[SmartDeviceSnapshot, ...]
    interval_seconds: int = DEFAULT_SMART_INTERVAL_SECONDS

    @property
    def state(self) -> str:
        states = {device.state for device in self.devices}
        for state in ("critical", "warning"):
            if state in states:
                return state
        return "healthy" if states == {"healthy"} else "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "smartctl",
            "observed_at": self.observed_at,
            "interval_seconds": self.interval_seconds,
            "state": self.state,
            "devices": [asdict(device) for device in self.devices],
        }


def _find_smartctl() -> Path | None:
    # A desktop launch need not inherit Homebrew's PATH. Never invoke a shell,
    # install a tool, or search user-controlled command text for an executable.
    return next((path for path in _TOOL_PATHS if path.is_file() and os.access(path, os.X_OK)), None)


def _integer(value: Any, *, minimum: int = 0, maximum: int = _MAX_SAFE_INTEGER) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        return None
    return value


def _lifetime_bytes(value: Any) -> int | None:
    units = _integer(value, maximum=_MAX_SAFE_INTEGER // _NVME_DATA_UNIT_BYTES)
    return units * _NVME_DATA_UNIT_BYTES if units is not None else None


def _device_name(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(char in value for char in ("\0", "\n", "\r")):
        return None
    if value.startswith("IOService:/") or re.fullmatch(r"(?:/dev/r?disk|disk)\d+", value):
        return value
    return None


def _device_snapshot(data: dict[str, Any], index: int) -> SmartDeviceSnapshot | None:
    raw = data.get("nvme_smart_health_information_log")
    if not isinstance(raw, dict):
        return None
    status = data.get("smart_status")
    passed = status.get("passed") if isinstance(status, dict) else None
    critical = _integer(raw.get("critical_warning"), maximum=255)
    used = _integer(raw.get("percentage_used"), maximum=255)
    spare = _integer(raw.get("available_spare"), maximum=100)
    threshold = _integer(raw.get("available_spare_threshold"), maximum=100)
    errors = _integer(raw.get("media_errors"))
    temperature = _integer(raw.get("temperature"), minimum=-100, maximum=250)
    read_bytes = _lifetime_bytes(raw.get("data_units_read"))
    write_bytes = _lifetime_bytes(raw.get("data_units_written"))
    hours = _integer(raw.get("power_on_hours"))
    if type(passed) is not bool and all(value is None for value in (
        critical, used, spare, errors, temperature, read_bytes, write_bytes, hours,
    )):
        return None
    if passed is False or (critical is not None and critical > 0):
        state = "critical"
    elif (errors is not None and errors > 0) or (used is not None and used >= 100) or (
        spare is not None and threshold is not None and spare < threshold
    ):
        state = "warning"
    else:
        state = "healthy" if passed is True else "unknown"
    return SmartDeviceSnapshot(
        index=index, state=state, temperature_celsius=temperature,
        percentage_used=used, available_spare_percent=spare,
        lifetime_read_bytes=read_bytes, lifetime_write_bytes=write_bytes,
        power_on_hours=hours, media_errors=errors,
    )


class SmartctlMonitor:
    """Discover and read at most four devices within one bounded time budget.

    Both absence and successful snapshots are cached for ten minutes. A failed
    refresh clears old SMART data; the host's native health probe stays separate.
    """

    def __init__(self) -> None:
        self._next_probe_epoch = 0.0
        self._snapshot: DiskSmartSnapshot | None = None

    @staticmethod
    def _query(args: list[str], deadline: float) -> dict[str, Any] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=remaining, check=False,
        )
        # High exit bits report disk health findings, not a failed invocation.
        if result.returncode < 0 or result.returncode & 0b111 or len(result.stdout) > MAX_OUTPUT_BYTES:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None

    def _probe(self, observed_at: str) -> DiskSmartSnapshot | None:
        executable = _find_smartctl()
        if executable is None:
            return None
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        scan = self._query([str(executable), "--scan", "-j"], deadline)
        rows = scan.get("devices") if scan else None
        if not isinstance(rows, list):
            return None
        names: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "nvme":
                continue
            name = _device_name(row.get("name"))
            if name is not None and name not in names:
                names.append(name)
                if len(names) == MAX_DEVICES:
                    break
        devices: list[SmartDeviceSnapshot] = []
        for index, name in enumerate(names, 1):
            try:
                data = self._query([
                    str(executable), "-H", "-A", "-j", "-q", "noserial", name,
                ], deadline)
                device = _device_snapshot(data, index) if data else None
                if device is not None:
                    devices.append(device)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        return DiskSmartSnapshot(observed_at, tuple(devices)) if devices else None

    def read(self, observed_at: str, epoch: float) -> DiskSmartSnapshot | None:
        if epoch < self._next_probe_epoch:
            return self._snapshot
        self._next_probe_epoch = float(epoch) + DEFAULT_SMART_INTERVAL_SECONDS
        try:
            self._snapshot = self._probe(observed_at)
        except (OSError, ValueError, subprocess.SubprocessError):
            self._snapshot = None
        return self._snapshot
