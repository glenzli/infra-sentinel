from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from remote import RemoteFleetMonitor, RemoteServerConfig  # noqa: E402
from traffic_estimation import TrafficEstimationConfig  # noqa: E402
from vps import VpsConfig  # noqa: E402
from xray_stats import XrayStatsConfig  # noqa: E402


class _State:
    max_log_bytes = 1024 * 1024
    backups = 2


def server(server_id: str, label: str, host: str) -> RemoteServerConfig:
    return RemoteServerConfig(
        server_id,
        label,
        VpsConfig(True, host, "auto", 300, server_id, label, "both"),
        XrayStatsConfig(True, host, "127.0.0.1:10085", "/usr/local/bin/xray", 300, (), (), server_id, label),
        TrafficEstimationConfig("both"),
    )


class RemoteFleetTests(unittest.TestCase):
    def test_each_server_is_polled_and_projected_separately(self) -> None:
        vps_values = {"one": 10, "two": 20}
        xray_values = {"one": 7, "two": 13}

        def vps_reader(config: VpsConfig) -> dict[str, object]:
            return {"timestamp": config.server_id, "epoch": 100.0, "interface": "eth0",
                    "in_bytes": vps_values[config.server_id], "out_bytes": 0,
                    "in_packets": 1, "out_packets": 0}

        def xray_reader(config: XrayStatsConfig) -> dict[str, object]:
            return {"timestamp": config.server_id, "epoch": 100.0,
                    "users": {"client": {"up_bytes": xray_values[config.server_id], "down_bytes": 0}}}

        with tempfile.TemporaryDirectory() as temporary:
            monitor = RemoteFleetMonitor(
                (server("one", "One", "host-one"), server("two", "Two", "host-two")),
                Path(temporary), _State(), vps_reader=vps_reader, xray_reader=xray_reader,
            )
            state = monitor.maybe_poll(100.0, force=True)
            self.assertEqual([row["id"] for row in state["servers"]], ["one", "two"])
            self.assertEqual([row["label"] for row in state["servers"]], ["One", "Two"])
            self.assertEqual([row["vps"]["last_sample"]["in_bytes"] for row in state["servers"]], [0, 0])
            self.assertTrue((Path(temporary) / "remote" / "one" / "vps_samples.jsonl").exists())
            self.assertTrue((Path(temporary) / "remote" / "two" / "vps_samples.jsonl").exists())
            self.assertEqual([row["xray_stats"]["total_bytes"] for row in state["servers"]], [0, 0])
            self.assertEqual([row["vps"]["status"] for row in state["servers"]], ["baseline", "baseline"])
            self.assertEqual(state["cycle"]["total_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
