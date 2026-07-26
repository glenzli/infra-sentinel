from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from codex_activity import CodexActivityConfig, CodexActivityMeter, drain_hook_inbox  # noqa: E402
from codex_hook import build_privacy_safe_record, hooks_status, install_hooks, summarize_runtime_hooks  # noqa: E402
from config_migration import migrate_config  # noqa: E402
from sentinel import (  # noqa: E402
    AlertEngine,
    Config,
    GroupConfig,
    LocalCycleMeter,
    MonitorConfig,
    ProcessDeltaTracker,
    SAMPLE_SCHEMA,
    StateConfig,
    aggregate_groups,
    latest_delta_event,
    parse_nettop_csv,
    parse_nettop_connections_csv,
    read_config,
)
from proxy_segments import PROXY_SEGMENT_SCHEMA, ProxyCycleMeter, ProxySegmentTracker  # noqa: E402
from session import RESET_REQUEST_SCHEMA, SessionMeter, consume_reset_request  # noqa: E402
from snapshot import create_snapshot  # noqa: E402
from traffic_estimation import TrafficEstimationConfig, estimate_traffic, minute_rate_trend  # noqa: E402
from vps import (  # noqa: E402
    VPS_SAMPLE_SCHEMA,
    VpsConfig,
    VpsCounterTracker,
    VpsMonitor,
    billing_cycle_start_epoch,
)


def make_config(state_dir: Path, vps_enabled: bool = False) -> Config:
    return Config(
        monitor=MonitorConfig(5, 300, 100, 600, 1000, "codex"),
        groups=(
            GroupConfig("codex", "Codex", "attribution", ("codex",)),
            GroupConfig("antigravity", "Antigravity", "attribution", ("antigravity",)),
            GroupConfig("proxy", "本地代理", "observer", ("proxy",)),
        ),
        codex_activity=CodexActivityConfig(True, "codex", 4, 10),
        state=StateConfig(1024, 1),
        vps=VpsConfig(vps_enabled, "my-vps", "auto", 300, 1),
        estimation=TrafficEstimationConfig("proxy", 2.0, 0.20),
        state_dir=state_dir,
    )


class NettopParserTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "nettop.csv"
        self.rows = parse_nettop_csv(fixture.read_text(encoding="utf-8"))

    def test_reads_raw_in_and_out_bytes(self) -> None:
        self.assertEqual(len(self.rows), 4)
        self.assertEqual(self.rows[0], {"name": "Codex (Service)", "pid": 451, "down_bytes": 1_048_576, "up_bytes": 2_097_152})

    def test_accepts_proc_name_header_variant(self) -> None:
        rows = parse_nettop_csv("proc_name,pid,rxbytes,txbytes\nCodex (Service),9,7,11\n")
        self.assertEqual(rows, [{"name": "Codex (Service)", "pid": 9, "down_bytes": 7, "up_bytes": 11}])

    def test_accepts_macos_blank_process_column(self) -> None:
        rows = parse_nettop_csv("time,,interface,bytes_in,bytes_out\n12:00,Codex (Service).451,,7,11\n")
        self.assertEqual(rows, [{"name": "Codex (Service)", "pid": 451, "down_bytes": 7, "up_bytes": 11}])

    def test_preserves_blank_process_column_when_header_has_trailing_blank(self) -> None:
        rows = parse_nettop_csv("time,,interface,state,bytes_in,bytes_out,arch,\n12:00,Codex (Service).451,,,7,11,,\n")
        self.assertEqual(rows, [{"name": "Codex (Service)", "pid": 451, "down_bytes": 7, "up_bytes": 11}])

    def test_associates_detailed_socket_rows_with_preceding_process(self) -> None:
        output = (
            "time,,interface,state,bytes_in,bytes_out\n"
            "12:00,verge-mihomo.9,,,100,200\n"
            "12:00,tcp4 127.0.0.1:7897<->127.0.0.1:58454,lo0,Established,7,11\n"
            "12:00,tcp4 192.168.1.5:58455<->203.0.113.10:443,en0,Established,13,17\n"
            "12:00,Codex (Service).10,,,3,4\n"
        )
        self.assertEqual(len(parse_nettop_csv(output)), 2)
        self.assertEqual(parse_nettop_connections_csv(output), [
            {"name": "verge-mihomo", "pid": 9, "interface": "lo0", "connection": "tcp4 127.0.0.1:7897<->127.0.0.1:58454", "down_bytes": 7, "up_bytes": 11},
            {"name": "verge-mihomo", "pid": 9, "interface": "en0", "connection": "tcp4 192.168.1.5:58455<->203.0.113.10:443", "down_bytes": 13, "up_bytes": 17},
        ])

    def test_assigns_overlapping_rule_to_first_group_only(self) -> None:
        groups = (
            GroupConfig("codex", "Codex", "attribution", ("codex",)),
            GroupConfig("other", "Other", "attribution", ("service",)),
        )
        totals, processes = aggregate_groups(self.rows, groups)
        self.assertEqual(totals["codex"]["up_bytes"], 2_098_176)
        self.assertEqual(totals["other"]["up_bytes"], 0)
        self.assertEqual([item["pid"] for item in processes["codex"]], [451, 452])


class ConfigurationTests(unittest.TestCase):
    def test_template_has_configured_ai_groups_and_low_frequency_vps(self) -> None:
        config = read_config(PROJECT_ROOT / "config.example.toml")
        self.assertEqual([group.id for group in config.groups], ["codex", "antigravity", "proxy"])
        self.assertEqual(config.vps.poll_seconds, 300)
        self.assertFalse(config.vps.enabled)
        self.assertEqual(config.estimation.proxy_group, "proxy")
        self.assertEqual(config.estimation.vps_billing_legs, 2.0)
        self.assertEqual(config.estimation.link_overhead_ratio, 0.20)
        self.assertEqual(config.estimation.effective_multiplier, 2.4)
        self.assertTrue(config.codex_activity.enabled)
        self.assertEqual(config.codex_activity.process_group, "codex")

    def test_migrates_legacy_tables_and_preserves_user_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                '[[process_groups]]\nid = "proxy"\nlabel = "Mine"\nrole = "observer"\npatterns = ["mine"]\n\n'
                '[vps.diagnostics]\nenabled = true\n\n'
                '[reconciliation]\nreference_group = "proxy"\nexpected_vps_multiplier = 2.0\n',
                encoding="utf-8",
            )
            self.assertTrue(migrate_config(path))
            migrated = path.read_text(encoding="utf-8")
            self.assertIn('label = "Mine"', migrated)
            self.assertIn("[estimation]", migrated)
            self.assertIn('proxy_group = "proxy"', migrated)
            self.assertNotIn("[vps.diagnostics]", migrated)
            self.assertNotIn("[reconciliation]", migrated)
            self.assertTrue(path.with_suffix(".toml.pre-estimation").exists())


class CodexHookTests(unittest.TestCase):
    def test_capture_record_discards_all_sensitive_content(self) -> None:
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "model": "gpt-5.6-terra",
            "tool_name": "Bash",
            "tool_input": {"command": "sed -n '1,100p' /private/project/secret.txt"},
            "tool_response": {"output": "do-not-store-this-file-content"},
            "prompt": "do-not-store-this-prompt",
            "last_assistant_message": "do-not-store-this-message",
        }
        record = build_privacy_safe_record(payload, epoch=100)
        serialized = json.dumps(record)
        self.assertEqual(record["model"], "gpt-5.6-terra")
        self.assertTrue(record["read_like"])
        self.assertEqual(len(record["input_fingerprint"]), 64)
        self.assertNotIn("secret.txt", serialized)
        self.assertNotIn("do-not-store", serialized)
        self.assertNotIn("tool_input", record)
        self.assertNotIn("tool_response", record)

    def test_installer_preserves_existing_hooks_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_dir = root / ".codex"
            support_dir = root / "support"
            codex_dir.mkdir()
            existing = {
                "description": "existing",
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "echo existing"}]}
                    ]
                },
            }
            (codex_dir / "hooks.json").write_text(json.dumps(existing), encoding="utf-8")
            install_hooks(support_dir, codex_dir, PROJECT_ROOT / "bin" / "codex_hook.py")
            install_hooks(support_dir, codex_dir, PROJECT_ROOT / "bin" / "codex_hook.py")
            payload = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["description"], "existing")
            post_commands = [
                hook["command"]
                for group in payload["hooks"]["PostToolUse"]
                for hook in group["hooks"]
            ]
            self.assertIn("echo existing", post_commands)
            self.assertEqual(sum("--traffic-sentinel-capture" in command for command in post_commands), 1)
            self.assertEqual(hooks_status(codex_dir)["status"], "installed")

    def test_runtime_status_distinguishes_pending_review_from_trusted(self) -> None:
        def response(statuses: list[str]) -> dict[str, object]:
            return {
                "result": {
                    "data": [{
                        "cwd": str(PROJECT_ROOT),
                        "errors": [],
                        "warnings": [],
                        "hooks": [
                            {
                                "command": f"/usr/bin/python3 hook.py --traffic-sentinel-capture {index}",
                                "trustStatus": status,
                            }
                            for index, status in enumerate(statuses)
                        ],
                    }]
                }
            }

        pending = summarize_runtime_hooks(response(["untrusted"] * 9))
        self.assertEqual(pending["status"], "review_required")
        self.assertEqual(pending["review_required"], 9)

        trusted = summarize_runtime_hooks(response(["trusted"] * 9))
        self.assertEqual(trusted["status"], "trusted")
        self.assertEqual(trusted["trusted"], 9)


class CodexActivityTests(unittest.TestCase):
    @staticmethod
    def sample(epoch: float, total_bytes: int) -> dict[str, object]:
        return {
            "epoch": epoch,
            "groups": {
                "codex": {"up_bytes": total_bytes // 2, "down_bytes": total_bytes - total_bytes // 2}
            },
        }

    def test_tracks_subagents_tools_and_model_traffic_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            meter = CodexActivityMeter(state_dir, CodexActivityConfig(True, "codex", 4, 10))
            start_sol = {
                "schema": 1,
                "epoch": 100,
                "event": "UserPromptSubmit",
                "session_id": "main",
                "turn_id": "turn-sol",
                "model": "gpt-5.6-sol",
            }
            sol_read = {
                "schema": 1,
                "epoch": 101,
                "event": "PostToolUse",
                "session_id": "main",
                "turn_id": "turn-sol",
                "model": "gpt-5.6-sol",
                "tool_name": "Bash",
                "tool_input_bytes": 30,
                "tool_response_bytes": 470,
                "read_like": True,
                "input_fingerprint": "same-read",
            }
            meter.record(self.sample(105, 1_000), [start_sol, sol_read])
            first = meter.snapshot(105)
            sol = first["models"][0]
            self.assertEqual(sol["label"], "Sol")
            self.assertEqual(sol["traffic_bytes"], 1_000)
            self.assertEqual(sol["traffic_quality"], "exclusive")

            start_terra_agent = {
                "schema": 1,
                "epoch": 106,
                "event": "SubagentStart",
                "session_id": "main",
                "turn_id": "turn-sol",
                "model": "gpt-5.6-terra",
                "agent_id": "agent-1",
                "agent_type": "worker",
            }
            terra_tool = {
                "schema": 1,
                "epoch": 107,
                "event": "PostToolUse",
                "session_id": "agent-1",
                "turn_id": "turn-terra",
                "model": "gpt-5.6-terra",
                "tool_name": "Bash",
                "tool_input_bytes": 100,
                "tool_response_bytes": 900,
                "read_like": False,
            }
            repeated_sol_read = dict(sol_read, epoch=108)
            meter.record(self.sample(110, 1_000), [start_terra_agent, terra_tool, repeated_sol_read])
            mixed = meter.snapshot(110)
            self.assertEqual(mixed["active_subagents"], 1)
            self.assertEqual(mixed["total_subagents"], 1)
            self.assertEqual(mixed["peak_active_subagents"], 1)
            self.assertEqual(mixed["tool_calls"], 3)
            self.assertEqual(mixed["read_like_calls"], 2)
            self.assertEqual(mixed["repeated_read_calls"], 1)
            self.assertEqual(sum(model["traffic_bytes"] for model in mixed["models"]), 2_000)
            terra = next(model for model in mixed["models"] if model["label"] == "Terra")
            self.assertEqual(terra["traffic_quality"], "mixed_estimate")

    def test_drains_each_hook_record_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            inbox = state_dir / "codex-hook-inbox"
            inbox.mkdir()
            (inbox / "one.json").write_text(json.dumps({
                "schema": 1,
                "epoch": 1,
                "event": "SessionStart",
                "session_id": "one",
                "model": "gpt-5.6-sol",
            }), encoding="utf-8")
            self.assertEqual(len(drain_hook_inbox(state_dir)), 1)
            self.assertEqual(drain_hook_inbox(state_dir), [])

    def test_reset_keeps_subagents_that_are_still_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            meter = CodexActivityMeter(state_dir, CodexActivityConfig(True, "codex", 4, 10))
            meter.record(self.sample(100, 0), [{
                "schema": 1,
                "epoch": 100,
                "event": "SubagentStart",
                "session_id": "main",
                "turn_id": "turn-terra",
                "model": "gpt-5.6-terra",
                "agent_id": "agent-1",
                "agent_type": "worker",
            }])

            meter.reset(110)
            snapshot = meter.snapshot(110)

            self.assertEqual(snapshot["active_subagents"], 1)
            self.assertEqual(snapshot["total_subagents"], 1)
            self.assertEqual(snapshot["peak_active_subagents"], 1)
            terra = next(model for model in snapshot["models"] if model["label"] == "Terra")
            self.assertEqual(terra["subagents"], 1)


class AlertEngineTests(unittest.TestCase):
    def test_alerts_only_on_state_transitions(self) -> None:
        engine = AlertEngine()
        config = make_config(PROJECT_ROOT / "state")
        self.assertEqual(engine.evaluate({"up_bytes": 101, "down_bytes": 0}, {"up_bytes": 101, "down_bytes": 0}, config), ("alert", "warning"))
        self.assertIsNone(engine.evaluate({"up_bytes": 101, "down_bytes": 0}, {"up_bytes": 101, "down_bytes": 0}, config))
        self.assertEqual(engine.evaluate({"up_bytes": 700, "down_bytes": 400}, {"up_bytes": 700, "down_bytes": 400}, config), ("alert", "critical"))
        self.assertEqual(engine.evaluate({"up_bytes": 0, "down_bytes": 0}, {"up_bytes": 0, "down_bytes": 0}, config), ("recovered", "none"))


class DeltaTrackerTests(unittest.TestCase):
    @staticmethod
    def raw_sample(codex: list[dict[str, object]], proxy: list[dict[str, object]] | None = None) -> dict[str, object]:
        return {
            "timestamp": "2026-07-26T12:00:00+08:00",
            "epoch": 1.0,
            "sample_seconds": 5,
            "groups": {
                "codex": {"label": "Codex", "role": "attribution"},
                "proxy": {"label": "本地代理", "role": "observer"},
            },
            "processes": {"codex": codex, "proxy": proxy or []},
        }

    def test_first_observation_creates_zero_baseline(self) -> None:
        tracker = ProcessDeltaTracker()
        result = tracker.apply(self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 1000, "down_bytes": 2000}]))
        self.assertEqual(result["groups"]["codex"], {"label": "Codex", "role": "attribution", "up_bytes": 0, "down_bytes": 0})

    def test_counts_only_the_difference_between_same_process_counters(self) -> None:
        tracker = ProcessDeltaTracker()
        tracker.apply(self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 1000, "down_bytes": 2000}]))
        result = tracker.apply(self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 1075, "down_bytes": 2250}]))
        self.assertEqual(result["groups"]["codex"]["up_bytes"], 75)
        self.assertEqual(result["groups"]["codex"]["down_bytes"], 250)
        self.assertEqual(result["processes"]["codex"][0]["up_bytes"], 75)

    def test_records_actual_observation_duration_for_rate_display(self) -> None:
        tracker = ProcessDeltaTracker()
        first = self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 1000, "down_bytes": 2000}])
        first["epoch"] = 10.0
        second = self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 1010, "down_bytes": 2020}])
        second["epoch"] = 16.5
        tracker.apply(first)
        result = tracker.apply(second)
        self.assertEqual(result["observed_seconds"], 6.5)

    def test_pid_reuse_or_counter_reset_never_replays_historical_bytes(self) -> None:
        tracker = ProcessDeltaTracker()
        tracker.apply(self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 1000, "down_bytes": 2000}]))
        result = tracker.apply(self.raw_sample([
            {"pid": 10, "name": "codex", "up_bytes": 12, "down_bytes": 18},
            {"pid": 11, "name": "codex", "up_bytes": 5000, "down_bytes": 8000},
        ]))
        self.assertEqual(result["groups"]["codex"]["up_bytes"], 0)
        self.assertEqual(result["groups"]["codex"]["down_bytes"], 0)

    def test_proxy_is_delta_tracked_separately(self) -> None:
        tracker = ProcessDeltaTracker()
        tracker.apply(self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 10, "down_bytes": 20}], [{"pid": 20, "name": "proxy", "up_bytes": 100, "down_bytes": 200}]))
        result = tracker.apply(self.raw_sample([{"pid": 10, "name": "codex", "up_bytes": 15, "down_bytes": 27}], [{"pid": 20, "name": "proxy", "up_bytes": 140, "down_bytes": 260}]))
        self.assertEqual(result["groups"]["codex"]["up_bytes"], 5)
        self.assertEqual(result["groups"]["proxy"], {"label": "本地代理", "role": "observer", "up_bytes": 40, "down_bytes": 60})


class VpsTrackerTests(unittest.TestCase):
    def test_vps_counters_use_differences_and_never_replay_reset(self) -> None:
        tracker = VpsCounterTracker()
        first = tracker.apply({"timestamp": "a", "epoch": 1, "interface": "eth0", "in_bytes": 1000, "out_bytes": 2000})
        second = tracker.apply({"timestamp": "b", "epoch": 2, "interface": "eth0", "in_bytes": 1200, "out_bytes": 2300})
        reset = tracker.apply({"timestamp": "c", "epoch": 3, "interface": "eth0", "in_bytes": 10, "out_bytes": 20})
        self.assertEqual((first["in_bytes"], first["out_bytes"]), (0, 0))
        self.assertEqual((second["in_bytes"], second["out_bytes"]), (200, 300))
        self.assertEqual((reset["in_bytes"], reset["out_bytes"]), (0, 0))
        self.assertEqual(second["interval_started_epoch"], 1.0)

    def test_vps_monitor_accumulates_only_between_polls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            counter = iter([(1000, 2000), (1150, 2300)])
            base = time.time()
            def reader(config: VpsConfig) -> dict[str, object]:
                incoming, outgoing = next(counter)
                return {"timestamp": "2026-07-26T12:00:00+08:00", "epoch": base, "interface": "eth0", "in_bytes": incoming, "out_bytes": outgoing}
            monitor = VpsMonitor(VpsConfig(True, "my-vps", "auto", 300, 1), Path(temporary), StateConfig(1024 * 1024, 1), reader=reader)
            first = monitor.maybe_poll(base)
            second = monitor.maybe_poll(base + 301)
            self.assertEqual(first["status"], "baseline")
            self.assertEqual(second["cycle"]["in_bytes"], 150)
            self.assertEqual(second["cycle"]["out_bytes"], 300)

    def test_forced_vps_poll_reads_an_immediate_session_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            counter = iter([(1000, 2000), (1010, 2020)])
            base = time.time()
            def reader(config: VpsConfig) -> dict[str, object]:
                incoming, outgoing = next(counter)
                return {"timestamp": "2026-07-26T12:00:00+08:00", "epoch": base, "interface": "eth0", "in_bytes": incoming, "out_bytes": outgoing}
            monitor = VpsMonitor(VpsConfig(True, "my-vps", "auto", 300, 1), Path(temporary), StateConfig(1024 * 1024, 1), reader=reader)
            monitor.maybe_poll(base)
            forced = monitor.maybe_poll(base + 1, force=True)
            self.assertEqual((forced["last_sample"]["in_bytes"], forced["last_sample"]["out_bytes"]), (10, 20))


class LocalCycleTests(unittest.TestCase):
    def test_persists_per_group_totals_for_the_billing_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            meter = LocalCycleMeter(config)
            now = time.time()
            meter.record({
                "epoch": now,
                "groups": {
                    "codex": {"up_bytes": 100, "down_bytes": 200},
                    "antigravity": {"up_bytes": 3, "down_bytes": 4},
                    "proxy": {"up_bytes": 300, "down_bytes": 400},
                },
            })
            snapshot = meter.snapshot()
            codex = next(group for group in snapshot["groups"] if group["id"] == "codex")
            self.assertEqual(codex["total_bytes"], 300)
            restored = LocalCycleMeter(config).snapshot()
            proxy = next(group for group in restored["groups"] if group["id"] == "proxy")
            self.assertEqual(proxy["total_bytes"], 700)

class ProxySegmentTests(unittest.TestCase):
    @staticmethod
    def counter(category: str, up_bytes: int, down_bytes: int) -> dict[str, object]:
        return {
            "name": "proxy",
            "pid": 20,
            "category": category,
            "up_bytes": up_bytes,
            "down_bytes": down_bytes,
        }

    def test_separates_filtered_external_loopback_and_other_deltas(self) -> None:
        tracker = ProxySegmentTracker()
        tracker.apply("a", 1, [
            self.counter("external", 100, 200),
            self.counter("loopback", 10, 20),
            self.counter("other", 30, 40),
        ])
        result = tracker.apply("b", 2, [
            self.counter("external", 120, 260),
            self.counter("loopback", 15, 27),
            self.counter("other", 41, 53),
        ])
        self.assertEqual(result["categories"]["external"], {"up_bytes": 20, "down_bytes": 60})
        self.assertEqual(result["categories"]["loopback"], {"up_bytes": 5, "down_bytes": 7})
        self.assertEqual(result["categories"]["other"], {"up_bytes": 11, "down_bytes": 13})

    def test_proxy_cycle_keeps_external_separate_from_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = make_config(state_dir)
            now = time.time()
            cycle_start = billing_cycle_start_epoch(config.vps.billing_cycle_start_day, now)
            meter = ProxyCycleMeter(state_dir, config.state, cycle_start)
            initial_sample = {"schema": PROXY_SEGMENT_SCHEMA, "timestamp": "a", "epoch": now, "categories": {"external": {"up_bytes": 0, "down_bytes": 0}, "loopback": {"up_bytes": 0, "down_bytes": 0}, "other": {"up_bytes": 0, "down_bytes": 0}}}
            local_sample = {"schema": PROXY_SEGMENT_SCHEMA, "timestamp": "b", "epoch": now + 2, "categories": {"external": {"up_bytes": 40, "down_bytes": 60}, "loopback": {"up_bytes": 999, "down_bytes": 999}, "other": {"up_bytes": 999, "down_bytes": 999}}}
            meter.record(initial_sample, cycle_start)
            meter.record(local_sample, cycle_start)
            result = meter.snapshot()
            external = next(category for category in result["categories"] if category["id"] == "external")
            loopback = next(category for category in result["categories"] if category["id"] == "loopback")
            self.assertEqual(external["total_bytes"], 100)
            self.assertEqual(loopback["total_bytes"], 1998)


class TrafficEstimationTests(unittest.TestCase):
    def test_applies_two_billing_legs_and_twenty_percent_overhead_once(self) -> None:
        config = TrafficEstimationConfig("proxy", 2.0, 0.20)
        result = estimate_traffic(1_000, 800, 2_900, True, config)
        self.assertEqual(result["effective_multiplier"], 2.4)
        self.assertEqual(result["local_other_estimated_bytes"], 200)
        self.assertEqual(result["local_vps_billable_ceiling_bytes"], 2_400)
        self.assertEqual(result["other_devices_billable_estimated_bytes"], 500)
        self.assertEqual(result["other_devices_logical_estimated_bytes"], 208)

    def test_other_devices_never_goes_negative(self) -> None:
        result = estimate_traffic(1_000, 900, 2_000, True, TrafficEstimationConfig("proxy"))
        self.assertEqual(result["other_devices_billable_estimated_bytes"], 0)
        self.assertEqual(result["other_devices_logical_estimated_bytes"], 0)

    def test_trend_normalizes_partial_minutes_to_mib_per_minute(self) -> None:
        result = minute_rate_trend([
            {"epoch": 120, "observed_seconds": 5, "groups": {"codex": 1_048_576}, "proxy_external": 2_097_152},
            {"epoch": 125, "observed_seconds": 5, "groups": {"codex": 1_048_576}, "proxy_external": 2_097_152},
        ], ("codex",))
        self.assertEqual(result["unit"], "bytes_per_minute")
        self.assertEqual(result["buckets"][0]["groups"]["codex"], 12 * 1_048_576)
        self.assertEqual(result["buckets"][0]["proxy_external"], 12 * 2_097_152)


class SessionMeterTests(unittest.TestCase):
    def test_vps_estimate_waits_for_a_complete_post_reset_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            meter = SessionMeter(Path(temporary), ("codex", "proxy"))
            meter.reset(100, "manual")
            meter.set_vps_baseline({
                "status": "ok",
                "last_sample": {"schema": VPS_SAMPLE_SCHEMA, "epoch": 100, "interval_started_epoch": 95},
            })
            snapshot = meter.snapshot(
                {"codex": "Codex", "proxy": "本地代理"},
                {"codex": "attribution", "proxy": "observer"},
                True,
                TrafficEstimationConfig("proxy"),
                now=101,
            )
            self.assertFalse(snapshot["vps_ready"])
            self.assertIsNone(snapshot["breakdown"]["other_devices_billable_estimated_bytes"])

    def test_manual_session_uses_one_aligned_vps_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            meter = SessionMeter(state_dir, ("codex", "proxy"))
            meter.reset(100, "manual")
            meter.set_vps_baseline({"status": "ok", "last_sample": {"schema": VPS_SAMPLE_SCHEMA, "epoch": 100, "interval_started_epoch": 95, "in_bytes": 7, "out_bytes": 8}})
            meter.record(
                {"epoch": 105, "observed_seconds": 5, "groups": {"codex": {"up_bytes": 40, "down_bytes": 60}, "proxy": {"up_bytes": 1, "down_bytes": 2}}},
                {"categories": {"external": {"up_bytes": 20, "down_bytes": 80}, "loopback": {"up_bytes": 3, "down_bytes": 4}, "other": {"up_bytes": 0, "down_bytes": 0}}},
                {"status": "ok", "last_sample": {"schema": VPS_SAMPLE_SCHEMA, "epoch": 105, "interval_started_epoch": 100, "in_bytes": 90, "out_bytes": 110}},
            )
            snapshot = meter.snapshot(
                {"codex": "Codex", "proxy": "本地代理"},
                {"codex": "attribution", "proxy": "observer"},
                True,
                TrafficEstimationConfig("proxy", 2.0, 0.20),
                now=160,
            )
            self.assertEqual(snapshot["proxy_external_total_bytes"], 100)
            self.assertEqual(snapshot["vps"]["total_bytes"], 200)
            self.assertEqual(snapshot["vps"]["intervals"], 1)
            self.assertTrue(snapshot["vps_ready"])
            self.assertEqual(snapshot["started_reason"], "manual")
            self.assertEqual(snapshot["duration_seconds"], 60)
            self.assertEqual(snapshot["breakdown"]["project_total_bytes"], 100)
            self.assertEqual(snapshot["breakdown"]["local_other_estimated_bytes"], 0)
            self.assertEqual(snapshot["breakdown"]["other_devices_billable_estimated_bytes"], 0)
            self.assertEqual(snapshot["breakdown"]["other_devices_logical_estimated_bytes"], 0)
            self.assertEqual(snapshot["breakdown"]["effective_multiplier"], 2.4)

    def test_consumes_a_dashboard_reset_request_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session-reset.request.json"
            path.write_text(json.dumps({"schema": RESET_REQUEST_SCHEMA, "id": "reset-1"}), encoding="utf-8")
            self.assertEqual(consume_reset_request(Path(temporary))["id"], "reset-1")
            self.assertIsNone(consume_reset_request(Path(temporary)))


class EventCompatibilityTests(unittest.TestCase):
    def test_live_event_selector_ignores_prior_schema_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text('{"id":"old","sample":{"schema":2}}\n' + '{"id":"new","sample":{"schema":' + str(SAMPLE_SCHEMA) + '}}\n', encoding="utf-8")
            self.assertEqual(latest_delta_event(path)["id"], "new")


class SnapshotTests(unittest.TestCase):
    def test_records_only_process_and_connection_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary) / "state")
            event = {
                "id": "test-event",
                "type": "alert",
                "level": "warning",
                "timestamp": "2026-07-26T12:00:00+08:00",
                "alert_group": "codex",
                "sample": {"processes": {"codex": [{"pid": None, "name": "codex", "up_bytes": 1, "down_bytes": 2}]}},
            }
            result = create_snapshot(config, event)
            snapshot = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["process_deltas"][0]["name"], "codex")
            self.assertFalse(snapshot["privacy"]["file_contents_read"])
            self.assertFalse(snapshot["privacy"]["workspace_traversal"])


if __name__ == "__main__":
    unittest.main()
