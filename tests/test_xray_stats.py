from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from xray_stats import (  # noqa: E402
    XrayStatsConfig,
    XrayStatsMonitor,
    XrayStatsTracker,
    parse_xray_stats,
)


class LogState:
    max_log_bytes = 1024 * 1024
    backups = 1


def config() -> XrayStatsConfig:
    return XrayStatsConfig(
        True,
        "my-vps",
        "127.0.0.1:10085",
        "/usr/local/bin/xray",
        300,
        ("workstation", "phone", "unused"),
        ("legacy-unknown",),
    )


class XrayStatsParserTests(unittest.TestCase):
    def test_extracts_only_user_uplink_and_downlink(self) -> None:
        output = json.dumps({
            "stat": [
                {"name": "user>>>workstation>>>traffic>>>uplink", "value": 120},
                {"name": "user>>>workstation>>>traffic>>>downlink", "value": "340"},
                {"name": "inbound>>>vless>>>traffic>>>uplink", "value": 999},
                {"name": "user>>>phone>>>traffic>>>uplink", "value": 7},
            ]
        })
        self.assertEqual(parse_xray_stats(output), {
            "workstation": {"up_bytes": 120, "down_bytes": 340},
            "phone": {"up_bytes": 7, "down_bytes": 0},
        })

    def test_rejects_non_json_response(self) -> None:
        with self.assertRaisesRegex(ValueError, "未返回 JSON"):
            parse_xray_stats("connection refused")


class XrayStatsTrackerTests(unittest.TestCase):
    @staticmethod
    def raw(
        epoch: float,
        workstation_up: int,
        workstation_down: int,
    ) -> dict[str, object]:
        return {
            "timestamp": "2026-07-28T12:00:00+08:00",
            "epoch": epoch,
            "users": {
                "workstation": {
                    "up_bytes": workstation_up,
                    "down_bytes": workstation_down,
                }
            },
        }

    def test_first_read_is_baseline_then_counts_deltas(self) -> None:
        tracker = XrayStatsTracker()
        first = tracker.apply(self.raw(100, 1_000, 2_000))
        second = tracker.apply(self.raw(400, 1_150, 2_250))
        self.assertEqual(
            first["users"]["workstation"],
            {"up_bytes": 0, "down_bytes": 0},
        )
        self.assertEqual(
            second["users"]["workstation"],
            {"up_bytes": 150, "down_bytes": 250},
        )
        self.assertEqual(second["interval_started_epoch"], 100.0)

    def test_xray_restart_never_replays_counters(self) -> None:
        tracker = XrayStatsTracker()
        tracker.apply(self.raw(100, 1_000, 2_000))
        reset = tracker.apply(self.raw(400, 10, 20))
        after = tracker.apply(self.raw(700, 40, 80))
        self.assertEqual(
            reset["users"]["workstation"],
            {"up_bytes": 0, "down_bytes": 0},
        )
        self.assertEqual(
            after["users"]["workstation"],
            {"up_bytes": 30, "down_bytes": 60},
        )


class XrayStatsMonitorTests(unittest.TestCase):
    @staticmethod
    def reader(values: list[tuple[int, int]], base: float):
        counter = iter(enumerate(values))

        def read(_: XrayStatsConfig) -> dict[str, object]:
            index, (up_bytes, down_bytes) = next(counter)
            return {
                "timestamp": f"sample-{index}",
                "epoch": base + index * 300,
                "users": {
                    "workstation": {
                        "up_bytes": up_bytes,
                        "down_bytes": down_bytes,
                    }
                },
            }

        return read

    def test_accumulates_complete_intervals_and_keeps_expected_zero_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = time.time()
            monitor = XrayStatsMonitor(
                config(),
                Path(temporary),
                LogState(),
                reader=self.reader([(1_000, 2_000), (1_100, 2_300)], base),
            )
            monitor.align_session(base)
            first = monitor.maybe_poll(base)
            second = monitor.maybe_poll(base + 300)
            self.assertEqual(first["status"], "baseline")
            self.assertTrue(second["ready"])
            self.assertEqual(second["total_bytes"], 400)
            rows = {row["id"]: row for row in second["users"]}
            self.assertEqual(rows["workstation"]["total_bytes"], 400)
            self.assertEqual(rows["unused"]["total_bytes"], 0)
            self.assertTrue(rows["legacy-unknown"]["flagged"])

    def test_reset_discards_the_crossing_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = time.time()
            values = iter([
                (base, 1_000),
                (base + 100, 1_100),
                (base + 400, 1_300),
            ])

            def reader(_: XrayStatsConfig) -> dict[str, object]:
                epoch, value = next(values)
                return {
                    "timestamp": "sample",
                    "epoch": epoch,
                    "users": {
                        "workstation": {
                            "up_bytes": value,
                            "down_bytes": 0,
                        }
                    },
                }

            monitor = XrayStatsMonitor(config(), Path(temporary), LogState(), reader=reader)
            monitor.align_session(base)
            monitor.maybe_poll(base)
            reset = monitor.reset_session(base + 50)
            counted = monitor.maybe_poll(base + 400, force=True)
            self.assertEqual(reset["total_bytes"], 0)
            self.assertEqual(counted["total_bytes"], 200)
            self.assertEqual(counted["intervals"], 1)


if __name__ == "__main__":
    unittest.main()
