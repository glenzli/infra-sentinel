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
    CONFIG_SCHEMA,
    SETTINGS_SCHEMA,
    read_config,
    read_user_settings,
    settings_payload,
    write_user_settings,
)


def payload(*, billing_mode: str = "both") -> dict[str, object]:
    return {
        "schema": SETTINGS_SCHEMA,
        "app": {"menu_bar_mode": "health"},
        "integrations": {
            "ssh_executable": "", "opencode_executable": "",
            "opencode_database": "", "codex_database": "",
        },
        "policies": [{
            "id": "network-traffic-alerts", "kind": "traffic.threshold", "resource_id": "network",
            "warning_window_minutes": 7, "warning_mib": 320,
            "critical_window_minutes": 12, "critical_mib": 1536,
        }],
        "sources": [
            {"id": "local-mihomo", "kind": "network.mihomo", "enabled": True},
            {"id": "primary", "kind": "network.linux-xray", "label": "Primary VPS", "enabled": True,
             "ssh_host": "my-vps", "xray_stats_enabled": True,
             "billing_mode": billing_mode},
        ],
    }


class ConfigurationTests(unittest.TestCase):
    def test_example_is_the_complete_date_versioned_contract(self) -> None:
        settings = read_user_settings(PROJECT_ROOT / "config.example.toml")
        self.assertEqual(CONFIG_SCHEMA, "20260811.1")
        self.assertEqual(settings.warning_window_minutes, 5)
        self.assertEqual(settings.warning_mib, 250)
        self.assertEqual(settings.remote_servers, ())

    def test_settings_round_trip_and_runtime_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            saved = write_user_settings(path, payload(billing_mode="outbound"))
            self.assertEqual(settings_payload(read_user_settings(path)), settings_payload(saved))
            document = path.read_text(encoding="utf-8")
            self.assertIn('schema_version = "20260811.1"', document)
            self.assertIn('kind = "network.linux-xray"', document)
            runtime = read_config(path)
            self.assertEqual(runtime.monitor.warning_window_seconds, 7 * 60)
            self.assertEqual(runtime.monitor.warning_bytes, 320 * 1024 * 1024)
            self.assertEqual(len(runtime.remote_servers), 1)
            self.assertTrue(runtime.remote_servers[0].vps.enabled)
            self.assertEqual(runtime.remote_servers[0].vps.ssh_host, "my-vps")
            self.assertTrue(runtime.remote_servers[0].xray.enabled)
            self.assertEqual(runtime.remote_servers[0].estimation.vps_billing_legs, 1.0)

    def test_absolute_local_integration_paths_are_mapped_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = payload()
            data["integrations"] = {
                "ssh_executable": str(root / "ssh.exe"),
                "opencode_executable": str(root / "opencode.exe"),
                "opencode_database": str(root / "opencode.db"),
                "codex_database": str(root / "codex.sqlite"),
            }
            path = root / "config.toml"
            write_user_settings(path, data)
            runtime = read_config(path)
            self.assertEqual(runtime.integrations.as_payload(), data["integrations"])
            self.assertEqual(runtime.remote_servers[0].vps.ssh_executable, str(root / "ssh.exe"))
            self.assertEqual(runtime.remote_servers[0].xray.ssh_executable, str(root / "ssh.exe"))

    def test_relative_local_integration_path_is_rejected(self) -> None:
        data = payload()
        data["integrations"]["opencode_database"] = "portable/opencode.db"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                write_user_settings(Path(temporary) / "config.toml", data)

    def test_multiple_remote_sources_keep_independent_identity_and_billing(self) -> None:
        data = payload()
        data["sources"].append({
            "id": "secondary", "kind": "network.linux-xray", "label": "Secondary VPS", "enabled": True,
            "ssh_host": "my-vps-2", "xray_stats_enabled": False,
            "billing_mode": "outbound",
        })
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_user_settings(path, data)
            runtime = read_config(path)
            self.assertEqual([server.id for server in runtime.remote_servers], ["primary", "secondary"])
            self.assertEqual(runtime.remote_servers[1].estimation.billing_mode, "outbound")

    def test_vps_daily_usage_guard_is_an_independent_source_policy(self) -> None:
        data = payload()
        data["policies"].append({
            "id": "primary-daily-usage", "kind": "network.daily.usage", "source_id": "primary",
            "warning_gib": 600, "critical_gib": 800,
        })
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_user_settings(path, data)
            runtime = read_config(path)
            self.assertEqual(len(runtime.remote_billing_policies), 1)
            budget = runtime.remote_billing_policies[0]
            self.assertEqual((budget.source_id, budget.warning_bytes, budget.critical_bytes),
                             ("primary", 600 * 1024 ** 3, 800 * 1024 ** 3))
            exported = settings_payload(read_user_settings(path))
            self.assertIn("primary-daily-usage", [policy["id"] for policy in exported["policies"]])

    def test_prior_date_schema_is_migrated_once_before_runtime_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            data = payload()
            data["policies"].append({
                "id": "primary-daily-usage", "kind": "network.daily.usage", "source_id": "primary",
                "warning_gib": 600, "critical_gib": 800,
            })
            write_user_settings(path, data)
            previous = path.read_text(encoding="utf-8").replace('schema_version = "20260811.1"', 'schema_version = "20260808.4"')
            start = previous.index("[integrations]")
            end = previous.index("[[policies]]", start)
            previous = previous[:start] + previous[end:]
            path.write_text(previous, encoding="utf-8")
            settings = read_user_settings(path)
            self.assertEqual(settings.remote_servers[0]["id"], "primary")
            self.assertTrue(settings.remote_servers[0]["usage_alert_enabled"])
            self.assertEqual(settings.integrations.as_payload()["ssh_executable"], "")
            self.assertTrue((Path(temporary) / "config.pre-20260811.1.toml").exists())
            migrated = path.read_text(encoding="utf-8")
            self.assertIn('schema_version = "20260811.1"', migrated)
            self.assertIn('kind = "network.daily.usage"', migrated)
            self.assertIn("[integrations]", migrated)

    def test_legacy_config_migrates_once_with_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[monitor]\nwarning_window_minutes = 5\nwarning_mib = 250\ncritical_window_minutes = 10\ncritical_mib = 1024\n\n[remote]\nservers = []\n",
                encoding="utf-8",
            )
            settings = read_user_settings(path)
            self.assertEqual(settings.remote_servers, ())
            self.assertTrue((Path(temporary) / "config.pre-20260811.1.toml").exists())
            self.assertIn('schema_version = "20260811.1"', path.read_text(encoding="utf-8"))

    def test_unknown_configuration_fields_are_rejected(self) -> None:
        data = payload()
        data["app"]["unexpected"] = True
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "不支持的字段"):
                write_user_settings(Path(temporary) / "config.toml", data)

    def test_cli_bridge_writes_and_exports_the_same_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            helper = PROJECT_ROOT / "bin/configuration.py"
            written = subprocess.run([sys.executable, str(helper), "write", str(path)], input=json.dumps(payload()), text=True, capture_output=True, check=False)
            self.assertEqual(written.returncode, 0, written.stderr)
            exported = subprocess.run([sys.executable, str(helper), "export", str(path)], text=True, capture_output=True, check=False)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(json.loads(exported.stdout), payload())


if __name__ == "__main__":
    unittest.main()
