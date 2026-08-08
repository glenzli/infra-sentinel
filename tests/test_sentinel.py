from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from configuration import (  # noqa: E402
    Config,
    MonitorConfig,
    StateConfig,
    read_config,
)
from remote import RemoteServerConfig  # noqa: E402
from sentinel import (  # noqa: E402
    AlertEngine,
    SAMPLE_SCHEMA,
    latest_delta_event,
    totals_for_window,
    write_menubar_state,
)
from sample_timing import (  # noqa: E402
    CATCH_UP_INTERVAL,
    REALTIME_INTERVAL,
    annotate_sample_timing,
    classify_interval,
)
from session import RESET_REQUEST_SCHEMA, SessionMeter, consume_reset_request  # noqa: E402
from snapshot import create_snapshot  # noqa: E402
from traffic_estimation import (  # noqa: E402
    TrafficEstimationConfig,
    estimate_traffic,
    minute_rate_trend,
)
from vps import VPS_SAMPLE_SCHEMA, VpsConfig, VpsCounterTracker, VpsMonitor  # noqa: E402
from xray_stats import XrayStatsConfig  # noqa: E402


def make_config(state_dir: Path) -> Config:
    vps = VpsConfig(False, "", "auto", 300, 1)
    xray = XrayStatsConfig(False, "", "127.0.0.1:10085", "/usr/local/bin/xray", 300)
    return Config(
        monitor=MonitorConfig(5, 300, 100, 600, 1_000),
        state=StateConfig(1024 * 1024, 2),
        remote_servers=(RemoteServerConfig("default", "VPS", vps, xray, TrafficEstimationConfig("both")),),
        state_dir=state_dir,
    )


def sample(
    epoch: float,
    up_bytes: int,
    down_bytes: int,
    *,
    chatgpt: int = 0,
    unattributed: int = 0,
    observed_seconds: float = 5.0,
) -> dict[str, object]:
    total = up_bytes + down_bytes
    services: list[dict[str, object]] = []
    if chatgpt:
        services.append({
            "id": "chatgpt",
            "label": "ChatGPT",
            "up_bytes": chatgpt,
            "down_bytes": 0,
            "total_bytes": chatgpt,
        })
    if unattributed:
        services.append({
            "id": "unattributed",
            "label": "Unattributed",
            "up_bytes": unattributed,
            "down_bytes": 0,
            "total_bytes": unattributed,
        })
    return {
        "schema": SAMPLE_SCHEMA,
        "timestamp": "2026-07-28T12:00:00+08:00",
        "epoch": epoch,
        "observed_seconds": observed_seconds,
        "kernel": {
            "up_bytes": up_bytes,
            "down_bytes": down_bytes,
            "total_bytes": total,
        },
        "services": services,
        "routes": {
            "proxy": {"up_bytes": chatgpt, "down_bytes": 0, "total_bytes": chatgpt},
            "direct": {"up_bytes": 0, "down_bytes": 0, "total_bytes": 0},
            "blocked": {"up_bytes": 0, "down_bytes": 0, "total_bytes": 0},
            "unknown": {"up_bytes": 0, "down_bytes": 0, "total_bytes": 0},
            "unattributed": {
                "up_bytes": unattributed,
                "down_bytes": 0,
                "total_bytes": unattributed,
            },
        },
        "attribution": {
            "observed_bytes": max(0, total - unattributed),
            "unattributed_bytes": unattributed,
            "coverage": (total - unattributed) / total if total else 1.0,
        },
        "active_connections": 1,
    }


class RuntimeConfigTests(unittest.TestCase):
    def test_example_config_maps_fixed_product_behavior(self) -> None:
        config = read_config(PROJECT_ROOT / "config.example.toml")
        self.assertEqual(config.monitor.sample_seconds, 5)
        self.assertEqual(len(config.remote_servers), 0)

    def test_menubar_state_contains_generic_infra_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = make_config(state_dir)
            write_menubar_state(
                config,
                sample(100, 40, 60),
                {"up_bytes": 40, "down_bytes": 60},
                {"up_bytes": 40, "down_bytes": 60},
                "none",
                {"enabled": False, "status": "disabled", "servers": []},
                {"kernel": {"total_bytes": 100}, "vps": {"total_bytes": 0}},
            )
            payload = json.loads((state_dir / "menubar.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "20260808.3")
            self.assertEqual(payload["infra"]["resources"][0]["id"], "network")

class AlertEngineTests(unittest.TestCase):
    def test_alerts_use_exact_mihomo_total_windows(self) -> None:
        config = make_config(PROJECT_ROOT / "state")
        engine = AlertEngine()
        self.assertEqual(
            engine.evaluate(
                {"up_bytes": 101, "down_bytes": 0},
                {"up_bytes": 101, "down_bytes": 0},
                config,
            ),
            ("alert", "warning"),
        )
        self.assertEqual(
            engine.evaluate(
                {"up_bytes": 700, "down_bytes": 400},
                {"up_bytes": 700, "down_bytes": 400},
                config,
            ),
            ("alert", "critical"),
        )
        self.assertEqual(
            engine.evaluate(
                {"up_bytes": 0, "down_bytes": 0},
                {"up_bytes": 0, "down_bytes": 0},
                config,
            ),
            ("recovered", "none"),
        )

    def test_window_totals_sum_kernel_directions(self) -> None:
        samples = [sample(100, 10, 20), sample(200, 30, 40)]
        self.assertEqual(
            totals_for_window(samples, 210, 30),
            {"up_bytes": 30, "down_bytes": 40},
        )

    def test_window_totals_exclude_delayed_catch_up_but_keep_live_samples(self) -> None:
        live = sample(200, 30, 40)
        delayed = sample(205, 3_000, 4_000, observed_seconds=3_600)
        annotate_sample_timing(live, 5)
        annotate_sample_timing(delayed, 5)
        self.assertEqual(live["interval_kind"], REALTIME_INTERVAL)
        self.assertEqual(delayed["interval_kind"], CATCH_UP_INTERVAL)
        self.assertEqual(
            totals_for_window([live, delayed], 210, 30, 5),
            {"up_bytes": 30, "down_bytes": 40},
        )

    def test_interval_classification_allows_one_delayed_cycle_only(self) -> None:
        self.assertEqual(classify_interval(10, 5), REALTIME_INTERVAL)
        self.assertEqual(classify_interval(10.01, 5), CATCH_UP_INTERVAL)


class VpsTrackerTests(unittest.TestCase):
    def test_vps_counters_use_differences_and_never_replay_reset(self) -> None:
        tracker = VpsCounterTracker()
        first = tracker.apply({
            "timestamp": "a",
            "epoch": 1,
            "interface": "eth0",
            "in_bytes": 1000,
            "out_bytes": 2000,
            "in_packets": 10,
            "out_packets": 20,
        })
        second = tracker.apply({
            "timestamp": "b",
            "epoch": 2,
            "interface": "eth0",
            "in_bytes": 1200,
            "out_bytes": 2300,
            "in_packets": 12,
            "out_packets": 23,
        })
        reset = tracker.apply({
            "timestamp": "c",
            "epoch": 3,
            "interface": "eth0",
            "in_bytes": 10,
            "out_bytes": 20,
            "in_packets": 1,
            "out_packets": 2,
        })
        self.assertEqual((first["in_bytes"], first["out_bytes"]), (0, 0))
        self.assertEqual((second["in_bytes"], second["out_bytes"]), (200, 300))
        self.assertEqual((reset["in_bytes"], reset["out_bytes"]), (0, 0))

    def test_forced_vps_poll_reads_an_immediate_session_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            counters = iter([(1000, 2000, 10, 20), (1010, 2020, 11, 22)])
            base = time.time()

            def reader(config: VpsConfig) -> dict[str, object]:
                incoming, outgoing, in_packets, out_packets = next(counters)
                return {
                    "timestamp": "2026-07-28T12:00:00+08:00",
                    "epoch": base,
                    "interface": "eth0",
                    "in_bytes": incoming,
                    "out_bytes": outgoing,
                    "in_packets": in_packets,
                    "out_packets": out_packets,
                }

            monitor = VpsMonitor(
                VpsConfig(True, "sentinel-vps", "auto", 300, 1),
                Path(temporary),
                StateConfig(1024 * 1024, 1),
                reader=reader,
            )
            monitor.maybe_poll(base)
            forced = monitor.maybe_poll(base + 1, force=True)
            self.assertEqual(
                (forced["last_sample"]["in_bytes"], forced["last_sample"]["out_bytes"]),
                (10, 20),
            )


class TrafficEstimationTests(unittest.TestCase):
    def test_rejects_unknown_billing_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "billing_mode"):
            TrafficEstimationConfig("unknown")

    def test_measures_aligned_xray_to_vps_multiplier(self) -> None:
        result = estimate_traffic(
            2_512,
            4,
            2_512,
            1_000,
            True,
            True,
            TrafficEstimationConfig("both"),
        )
        self.assertEqual(result["observed_multiplier"], 2.512)
        self.assertEqual(result["ideal_billable_bytes"], 2_000)
        self.assertEqual(result["billable_overhead_bytes"], 512)
        self.assertAlmostEqual(result["billable_overhead_ratio"], 0.256)
        self.assertTrue(result["packet_breakdown_ready"])

    def test_empirical_analysis_waits_for_xray_coverage(self) -> None:
        result = estimate_traffic(
            2_000,
            10,
            2_000,
            0,
            True,
            False,
            TrafficEstimationConfig(),
        )
        self.assertFalse(result["empirical_ready"])
        self.assertIsNone(result["observed_multiplier"])

    def test_trend_normalizes_services_and_exact_total(self) -> None:
        result = minute_rate_trend([
            {
                "epoch": 120,
                "observed_seconds": 5,
                "services": {"chatgpt": 1_048_576},
                "mihomo_total": 2_097_152,
                "proxy_observed": 1_500_000,
                "unattributed": 500_000,
            },
            {
                "epoch": 125,
                "observed_seconds": 5,
                "services": {"chatgpt": 1_048_576},
                "mihomo_total": 2_097_152,
                "proxy_observed": 1_500_000,
                "unattributed": 500_000,
            },
        ], ("chatgpt",))
        bucket = result["buckets"][0]
        self.assertEqual(bucket["services"]["chatgpt"], 12 * 1_048_576)
        self.assertEqual(bucket["mihomo_total"], 12 * 2_097_152)

    def test_trend_excludes_old_unmarked_catch_up_points(self) -> None:
        result = minute_rate_trend([
            {
                "epoch": 120,
                "observed_seconds": 5,
                "services": {"chatgpt": 100},
                "mihomo_total": 200,
                "proxy_observed": 150,
                "unattributed": 50,
            },
            {
                "epoch": 125,
                "observed_seconds": 3_600,
                "services": {"chatgpt": 10_000},
                "mihomo_total": 20_000,
                "proxy_observed": 15_000,
                "unattributed": 5_000,
            },
        ], ("chatgpt",), expected_interval_seconds=5)
        bucket = result["buckets"][0]
        self.assertEqual(bucket["services"]["chatgpt"], 1_200)
        self.assertEqual(bucket["mihomo_total"], 2_400)


class SessionMeterTests(unittest.TestCase):
    def test_session_aggregates_independent_remote_servers_without_crossing_baselines(self) -> None:
        def remote(epoch: float, first_in: int, first_out: int, second_in: int, second_out: int) -> dict[str, object]:
            return {
                "status": "ok",
                "servers": [
                    {"id": "one", "label": "One", "billing_mode": "both", "vps": {"enabled": True, "last_sample": {
                        "schema": VPS_SAMPLE_SCHEMA, "epoch": epoch, "interval_started_epoch": epoch - 5,
                        "in_bytes": first_in, "out_bytes": first_out}},
                     "xray_stats": {"enabled": True, "ready": True, "intervals": 1, "total_bytes": 80}},
                    {"id": "two", "label": "Two", "billing_mode": "both", "vps": {"enabled": True, "last_sample": {
                        "schema": VPS_SAMPLE_SCHEMA, "epoch": epoch, "interval_started_epoch": epoch - 5,
                        "in_bytes": second_in, "out_bytes": second_out}},
                     "xray_stats": {"enabled": True, "ready": True, "intervals": 1, "total_bytes": 40}},
                ],
            }

        with tempfile.TemporaryDirectory() as temporary:
            meter = SessionMeter(Path(temporary))
            meter.reset(100, "manual")
            meter.set_vps_baseline(remote(100, 0, 0, 0, 0))
            meter.record(sample(105, 10, 20), remote(105, 90, 110, 40, 60))
            snapshot = meter.snapshot(remote(105, 90, 110, 40, 60), now=160)
            self.assertEqual(snapshot["vps"]["total_bytes"], 300)
            self.assertEqual(snapshot["vps"]["in_bytes"], 130)
            self.assertEqual(snapshot["vps"]["out_bytes"], 170)
            self.assertEqual([row["id"] for row in snapshot["remote_servers"]], ["one", "two"])
            self.assertEqual(snapshot["breakdown"]["xray_logical_bytes"], 120)
            self.assertEqual(snapshot["breakdown"]["observed_multiplier"], 2.5)

    def test_fleet_multiplier_excludes_vps_without_an_aligned_xray_pair(self) -> None:
        def remote(epoch: float, first_in: int, first_out: int, second_in: int, second_out: int) -> dict[str, object]:
            return {
                "status": "ok",
                "servers": [
                    {"id": "proxy", "label": "Proxy", "billing_mode": "both", "vps": {"enabled": True, "last_sample": {
                        "schema": VPS_SAMPLE_SCHEMA, "epoch": epoch, "interval_started_epoch": epoch - 5,
                        "in_bytes": first_in, "out_bytes": first_out}},
                     "xray_stats": {"enabled": True, "ready": True, "intervals": 1, "total_bytes": 80}},
                    {"id": "other", "label": "Other", "billing_mode": "both", "vps": {"enabled": True, "last_sample": {
                        "schema": VPS_SAMPLE_SCHEMA, "epoch": epoch, "interval_started_epoch": epoch - 5,
                        "in_bytes": second_in, "out_bytes": second_out}},
                     "xray_stats": {"enabled": False, "ready": False, "intervals": 0, "total_bytes": 0}},
                ],
            }

        with tempfile.TemporaryDirectory() as temporary:
            meter = SessionMeter(Path(temporary))
            meter.reset(100, "manual")
            meter.set_vps_baseline(remote(100, 0, 0, 0, 0))
            meter.record(sample(105, 10, 20), remote(105, 90, 110, 50, 50))
            snapshot = meter.snapshot(remote(105, 90, 110, 50, 50), now=160)

            self.assertEqual(snapshot["vps"]["total_bytes"], 300)
            self.assertEqual(snapshot["breakdown"]["xray_logical_bytes"], 80)
            self.assertEqual(snapshot["breakdown"]["observed_multiplier"], 2.5)
            self.assertEqual(snapshot["breakdown"]["comparable_server_ids"], ["proxy"])
            self.assertEqual(snapshot["breakdown"]["excluded_server_count"], 1)
            self.assertFalse(snapshot["remote_servers"][1]["breakdown"]["empirical_ready"])

    def test_session_accumulates_exact_total_dynamic_services_and_remote_bill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            meter = SessionMeter(state_dir)
            meter.reset(100, "manual")
            meter.set_vps_baseline({
                "status": "ok",
                "last_sample": {
                    "schema": VPS_SAMPLE_SCHEMA,
                    "epoch": 100,
                    "interval_started_epoch": 95,
                    "in_bytes": 0,
                    "out_bytes": 0,
                },
            })
            meter.record(
                sample(105, 100, 0, chatgpt=80, unattributed=20),
                {
                    "status": "ok",
                    "last_sample": {
                        "schema": VPS_SAMPLE_SCHEMA,
                        "epoch": 105,
                        "interval_started_epoch": 100,
                        "in_bytes": 90,
                        "out_bytes": 110,
                    },
                },
            )
            snapshot = meter.snapshot(
                True,
                TrafficEstimationConfig("both"),
                xray_stats={"ready": True, "intervals": 1, "total_bytes": 80},
                now=160,
            )
            self.assertEqual(snapshot["kernel"]["total_bytes"], 100)
            self.assertEqual(snapshot["proxy_observed_total_bytes"], 80)
            self.assertEqual(snapshot["proxy_upper_bound_bytes"], 100)
            self.assertEqual(snapshot["visible_services"][0]["label"], "ChatGPT")
            self.assertEqual(snapshot["attribution"]["coverage"], 0.8)
            self.assertEqual(snapshot["vps"]["total_bytes"], 200)
            self.assertEqual(snapshot["breakdown"]["observed_multiplier"], 2.5)

    def test_outbound_billing_uses_only_vps_tx_as_the_billable_total(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            meter = SessionMeter(Path(temporary))
            meter.reset(100, "manual")
            meter.set_vps_baseline({
                "status": "ok",
                "last_sample": {
                    "schema": VPS_SAMPLE_SCHEMA,
                    "epoch": 100,
                    "interval_started_epoch": 95,
                },
            })
            meter.record(
                sample(105, 100, 0),
                {
                    "status": "ok",
                    "last_sample": {
                        "schema": VPS_SAMPLE_SCHEMA,
                        "epoch": 105,
                        "interval_started_epoch": 100,
                        "in_bytes": 90,
                        "out_bytes": 110,
                    },
                },
            )
            snapshot = meter.snapshot(
                True,
                TrafficEstimationConfig("outbound"),
                xray_stats={"ready": True, "intervals": 1, "total_bytes": 80},
                now=160,
            )
            self.assertEqual(snapshot["vps"]["interface_total_bytes"], 200)
            self.assertEqual(snapshot["vps"]["total_bytes"], 110)
            self.assertEqual(snapshot["breakdown"]["vps_billing_legs"], 1.0)
            self.assertEqual(snapshot["breakdown"]["observed_multiplier"], 1.375)

    def test_consumes_a_dashboard_reset_request_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session-reset.request.json"
            path.write_text(
                json.dumps({"schema": RESET_REQUEST_SCHEMA, "id": "reset-1"}),
                encoding="utf-8",
            )
            self.assertEqual(consume_reset_request(Path(temporary))["id"], "reset-1")
            self.assertIsNone(consume_reset_request(Path(temporary)))

    def test_catch_up_bytes_stay_in_totals_but_not_rate_trend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            meter = SessionMeter(Path(temporary), expected_interval_seconds=5)
            meter.reset(100, "manual")
            delayed = sample(
                105,
                3_000,
                4_000,
                chatgpt=7_000,
                observed_seconds=3_600,
            )
            annotate_sample_timing(delayed, 5)
            meter.record(delayed, {"status": "disabled", "last_sample": {}})
            snapshot = meter.snapshot(
                False,
                TrafficEstimationConfig(),
                now=110,
            )
            self.assertEqual(snapshot["kernel"]["total_bytes"], 7_000)
            self.assertEqual(snapshot["services"][0]["total_bytes"], 7_000)
            self.assertEqual(snapshot["trend"]["buckets"], [])


class SnapshotAndEventTests(unittest.TestCase):
    def test_snapshot_contains_only_aggregate_mihomo_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary) / "state")
            config.state_dir.mkdir()
            event = {
                "id": "event",
                "type": "alert",
                "level": "warning",
                "timestamp": "2026-07-28T12:00:00+08:00",
                "sample": sample(100, 10, 20, chatgpt=30),
            }
            path = create_snapshot(config, event)
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["mihomo"]["services"][0]["label"], "ChatGPT")
            self.assertFalse(snapshot["privacy"]["packet_capture"])
            self.assertFalse(snapshot["privacy"]["request_contents_read"])

    def test_live_event_selector_ignores_prior_sample_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                '{"id":"old","sample":{"schema":4}}\n'
                + '{"id":"new","sample":{"schema":'
                + str(SAMPLE_SCHEMA)
                + "}}\n",
                encoding="utf-8",
            )
            self.assertEqual(latest_delta_event(path)["id"], "new")

    def test_live_event_selector_ignores_catch_up_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            live_sample = sample(100, 10, 20)
            delayed_sample = sample(105, 1_000, 2_000, observed_seconds=3_600)
            path.write_text(
                json.dumps({"id": "live", "sample": live_sample}) + "\n"
                + json.dumps({"id": "catch-up", "sample": delayed_sample}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(latest_delta_event(path)["id"], "live")


if __name__ == "__main__":
    unittest.main()
