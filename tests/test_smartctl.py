from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.resources.system.smartctl import (  # noqa: E402
    DEFAULT_SMART_INTERVAL_SECONDS, MAX_DEVICES, MAX_OUTPUT_BYTES,
    SmartctlMonitor, _find_smartctl,
)


OWNER = "infra_sentinel.resources.system.smartctl"
OBSERVED = "2026-10-19T20:00:00+08:00"
TOOL = Path("/opt/homebrew/bin/smartctl")


def completed(data: object, status: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], status, json.dumps(data).encode(), b"")


def scan(*names: str) -> subprocess.CompletedProcess:
    return completed({"devices": [{"name": name, "type": "nvme"} for name in names or ("disk0",)]})


def health(**changes: object) -> dict:
    log = {
        "critical_warning": 0, "temperature": 35, "percentage_used": 3,
        "available_spare": 100, "available_spare_threshold": 99,
        "data_units_read": 10, "data_units_written": 20,
        "power_on_hours": 1253, "media_errors": 0,
    }
    log.update(changes)
    return {
        "smart_status": {"passed": True}, "nvme_smart_health_information_log": log,
        "serial_number": "PRIVATE-SERIAL", "model_name": "PRIVATE-MODEL",
        "device": {"name": "IOService:/PRIVATE-DEVICE"},
    }


class SmartctlTests(unittest.TestCase):
    def probe(self, payload: dict, status: int = 0):
        with patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
            f"{OWNER}.subprocess.run", side_effect=[scan(), completed(payload, status)],
        ):
            return SmartctlMonitor().read(OBSERVED, 0)

    def test_discovers_homebrew_install_without_relying_on_gui_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing"
            executable = Path(folder) / "smartctl"
            executable.write_text("fixture")
            executable.chmod(0o755)
            with patch(f"{OWNER}._TOOL_PATHS", (missing, executable)), patch.dict("os.environ", {"PATH": ""}):
                self.assertEqual(_find_smartctl(), executable)

    def test_missing_or_nonexecutable_tool_does_not_run_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "smartctl"
            file.write_text("not executable")
            with patch(f"{OWNER}._TOOL_PATHS", (file,)), patch(f"{OWNER}.subprocess.run") as run:
                self.assertIsNone(SmartctlMonitor().read(OBSERVED, 0))
                run.assert_not_called()

    def test_successful_metadata_is_cached_and_commands_are_only_read_operations(self) -> None:
        monitor = SmartctlMonitor()
        with patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
            f"{OWNER}.subprocess.run", side_effect=[scan(), completed(health())],
        ) as run:
            snapshot = monitor.read(OBSERVED, 0)
            self.assertIs(monitor.read("later", DEFAULT_SMART_INTERVAL_SECONDS - 1), snapshot)
            self.assertEqual(run.call_count, 2)
        self.assertEqual(snapshot.state, "healthy")
        device = snapshot.devices[0]
        self.assertEqual((device.temperature_celsius, device.percentage_used, device.media_errors), (35, 3, 0))
        self.assertEqual((device.lifetime_read_bytes, device.lifetime_write_bytes), (5_120_000, 10_240_000))
        self.assertEqual(snapshot.observed_at, OBSERVED)
        self.assertEqual(run.call_args_list[0].args[0], [str(TOOL), "--scan", "-j"])
        self.assertEqual(run.call_args_list[1].args[0], [str(TOOL), "-H", "-A", "-j", "-q", "noserial", "disk0"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertLessEqual(call.kwargs["timeout"], 3)
            self.assertNotIn("shell", call.kwargs)

    def test_absence_is_cached_but_new_install_is_discovered_on_refresh(self) -> None:
        monitor = SmartctlMonitor()
        with patch(f"{OWNER}._find_smartctl", side_effect=[None, TOOL]) as find, patch(
            f"{OWNER}.subprocess.run", side_effect=[scan(), completed(health())],
        ) as run:
            self.assertIsNone(monitor.read(OBSERVED, 0))
            self.assertIsNone(monitor.read(OBSERVED, 599))
            self.assertEqual(find.call_count, 1)
            run.assert_not_called()
            self.assertIsNotNone(monitor.read(OBSERVED, 600))

    def test_disappearance_clears_old_metadata(self) -> None:
        monitor = SmartctlMonitor()
        with patch(f"{OWNER}._find_smartctl", side_effect=[TOOL, None]), patch(
            f"{OWNER}.subprocess.run", side_effect=[scan(), completed(health())],
        ):
            self.assertIsNotNone(monitor.read(OBSERVED, 0))
            self.assertIsNone(monitor.read(OBSERVED, 600))

    def test_permission_errors_and_timeouts_are_optional_absence(self) -> None:
        for error in [PermissionError(), FileNotFoundError(), subprocess.TimeoutExpired("smartctl", 3)]:
            with self.subTest(error=type(error).__name__), patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
                f"{OWNER}.subprocess.run", side_effect=error,
            ) as run:
                monitor = SmartctlMonitor()
                self.assertIsNone(monitor.read(OBSERVED, 0))
                self.assertIsNone(monitor.read(OBSERVED, 1))
                self.assertEqual(run.call_count, 1)

    def test_command_failures_do_not_turn_missing_data_into_healthy_zeroes(self) -> None:
        for status in [1, 2, 4, -9]:
            with self.subTest(status=status):
                self.assertIsNone(self.probe(health(), status))

    def test_failed_smart_health_is_not_lost_to_exit_status_bit_eight(self) -> None:
        payload = health()
        payload["smart_status"]["passed"] = False
        self.assertEqual(self.probe(payload, 8).state, "critical")

    def test_critical_warning_overrides_reported_pass(self) -> None:
        self.assertEqual(self.probe(health(critical_warning=1)).state, "critical")

    def test_media_errors_spare_and_endurance_exhaustion_need_attention(self) -> None:
        for changes in [{"media_errors": 1}, {"percentage_used": 100}, {"percentage_used": 150}, {"available_spare": 98}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.probe(health(**changes)).state, "warning")

    def test_missing_fields_stay_missing_and_not_false_zero(self) -> None:
        payload = {"nvme_smart_health_information_log": {"temperature": 35}}
        snapshot = self.probe(payload)
        self.assertEqual(snapshot.state, "unknown")
        self.assertIsNone(snapshot.devices[0].media_errors)
        self.assertIsNone(snapshot.devices[0].percentage_used)
        self.assertIsNone(self.probe({"nvme_smart_health_information_log": {}}))
        self.assertIsNone(self.probe({"nvme_smart_health_information_log": []}))

    def test_invalid_scalars_are_not_accepted_as_measurements(self) -> None:
        snapshot = self.probe(health(temperature=True, percentage_used=-1, media_errors="bad", data_units_written=2 ** 128))
        device = snapshot.devices[0]
        self.assertIsNone(device.temperature_celsius)
        self.assertIsNone(device.percentage_used)
        self.assertIsNone(device.media_errors)
        self.assertIsNone(device.lifetime_write_bytes)

    def test_snapshot_contains_only_normalized_scalars(self) -> None:
        result = self.probe(health()).as_dict()
        encoded = json.dumps(result)
        for private in ["PRIVATE", "serial", "IOService", "/dev/", "model_name"]:
            self.assertNotIn(private, encoded)
        self.assertEqual(result["source"], "smartctl")

    def test_unreadable_device_does_not_hide_another_readable_device(self) -> None:
        with patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
            f"{OWNER}.subprocess.run", side_effect=[scan("disk0", "disk1"), PermissionError(), completed(health())],
        ):
            snapshot = SmartctlMonitor().read(OBSERVED, 0)
        self.assertEqual(len(snapshot.devices), 1)
        self.assertEqual(snapshot.devices[0].index, 2)

    def test_only_bounded_unique_safe_nvme_devices_are_queried(self) -> None:
        rows = [{"name": "--smart=on", "type": "nvme"}, {"name": "disk0", "type": "ata"}, None]
        rows += [{"name": f"disk{n}", "type": "nvme"} for n in [0, 0, 1, 2, 3, 4, 5]]
        with patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
            f"{OWNER}.subprocess.run", side_effect=[completed({"devices": rows})] + [completed(health())] * MAX_DEVICES,
        ) as run:
            snapshot = SmartctlMonitor().read(OBSERVED, 0)
        self.assertEqual(len(snapshot.devices), MAX_DEVICES)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list[1:]], ["disk0", "disk1", "disk2", "disk3"])

    def test_malformed_and_oversized_output_is_ignored(self) -> None:
        for output in [b"bad json", b"[]", b"{}" * MAX_OUTPUT_BYTES]:
            with self.subTest(output=output[:10]), patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
                f"{OWNER}.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, b""),
            ):
                self.assertIsNone(SmartctlMonitor().read(OBSERVED, 0))

    def test_all_commands_share_one_timeout_budget(self) -> None:
        with patch(f"{OWNER}._find_smartctl", return_value=TOOL), patch(
            f"{OWNER}.time.monotonic", side_effect=[0, 1, 4],
        ), patch(f"{OWNER}.subprocess.run", return_value=scan()) as run:
            self.assertIsNone(SmartctlMonitor().read(OBSERVED, 0))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
