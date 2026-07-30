from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from configuration import (  # noqa: E402
    SETTINGS_SCHEMA,
    read_config,
    read_user_settings,
    settings_payload,
    write_user_settings,
)


def payload(*, billing_mode: str = "both") -> dict[str, object]:
    return {
        "schema": SETTINGS_SCHEMA,
        "monitor": {
            "warning_window_minutes": 7,
            "warning_mib": 320,
            "critical_window_minutes": 12,
            "critical_mib": 1536,
        },
        "remote": {
            "enabled": True,
            "ssh_host": "my-vps",
            "xray_stats_enabled": True,
            "billing_cycle_start_day": 9,
            "billing_mode": billing_mode,
        },
    }


class ConfigurationTests(unittest.TestCase):
    def test_example_is_the_complete_current_schema(self) -> None:
        settings = read_user_settings(PROJECT_ROOT / "config.example.toml")
        self.assertEqual(settings.warning_window_minutes, 5)
        self.assertEqual(settings.warning_mib, 250)
        self.assertFalse(settings.remote_enabled)
        self.assertEqual(settings.ssh_host, "")
        self.assertEqual(settings.billing_mode, "both")

    def test_settings_round_trip_and_runtime_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            saved = write_user_settings(path, payload(billing_mode="outbound"))
            self.assertEqual(settings_payload(read_user_settings(path)), settings_payload(saved))
            runtime = read_config(path)
            self.assertEqual(runtime.monitor.warning_window_seconds, 7 * 60)
            self.assertEqual(runtime.monitor.warning_bytes, 320 * 1024 * 1024)
            self.assertTrue(runtime.vps.enabled)
            self.assertEqual(runtime.vps.ssh_host, "my-vps")
            self.assertTrue(runtime.xray_stats.enabled)
            self.assertEqual(runtime.estimation.vps_billing_legs, 1.0)

    def test_unknown_configuration_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[monitor]\n"
                "warning_window_minutes = 5\n"
                "warning_mib = 250\n"
                "critical_window_minutes = 10\n"
                "critical_mib = 1024\n"
                "unexpected = true\n"
                "\n"
                "[remote]\n"
                "enabled = false\n"
                "ssh_host = \"\"\n"
                "xray_stats_enabled = false\n"
                "billing_cycle_start_day = 1\n"
                "billing_mode = \"both\"\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不支持的字段"):
                read_user_settings(path)

    def test_cli_bridge_writes_and_exports_the_same_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            helper = PROJECT_ROOT / "bin/configuration.py"
            written = subprocess.run(
                [sys.executable, str(helper), "write", str(path)],
                input=json.dumps(payload()),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            exported = subprocess.run(
                [sys.executable, str(helper), "export", str(path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(json.loads(exported.stdout), payload())


if __name__ == "__main__":
    unittest.main()
