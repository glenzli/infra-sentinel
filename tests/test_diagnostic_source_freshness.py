from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import (  # noqa: E402
    CallableCollector,
    Collection,
    CollectorCapability,
    CollectorContext,
    CollectorRegistry,
)
from infra_sentinel.resources.ai import antigravity, codex, dsh, opencode  # noqa: E402
from infra_sentinel.resources.network.metrics import network_collector_registry  # noqa: E402
from infra_sentinel.resources.network.vps import VpsConfig, VpsMonitor  # noqa: E402
from infra_sentinel.resources.network.xray import XrayStatsConfig, XrayStatsMonitor  # noqa: E402


EPOCH = datetime.fromisoformat("2026-10-22T10:00:00+08:00").timestamp()


def timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def context(epoch: float, remote: dict | None = None) -> CollectorContext:
    return CollectorContext({"epoch": epoch, "timestamp": timestamp(epoch)}, remote or {})


class CollectorAttemptTimeTests(unittest.TestCase):
    capability = CollectorCapability("test.attempt", "test:attempt", "test", "test", ())

    def test_cached_error_keeps_attempt_time_without_exposing_old_snapshot(self) -> None:
        result = Collection(
            status="error", checked_at=timestamp(EPOCH),
            snapshot={"observed_at": timestamp(EPOCH - 60), "usage": {"tokens": 42}},
        )
        registry = CollectorRegistry((CallableCollector(self.capability, lambda _: result),))

        runs = [registry.collect(context(EPOCH + offset))[0] for offset in (0, 5, 10, 15)]

        self.assertEqual([run.status for run in runs], ["waiting", "waiting", "error", "error"])
        self.assertTrue(all(run.checked_at == timestamp(EPOCH) for run in runs))
        self.assertEqual(runs[-1].as_dict()["checked_at"], timestamp(EPOCH))
        self.assertNotIn("snapshot", runs[-1].as_dict())
        self.assertEqual(runs[-1].points, ())

    def test_missing_error_attempt_is_not_replaced_by_the_current_cycle(self) -> None:
        registry = CollectorRegistry((CallableCollector(
            self.capability, lambda _: Collection(status="error"),
        ),))

        for offset in (0, 5, 10):
            run = registry.collect(context(EPOCH + offset))[0]

        self.assertEqual(run.status, "error")
        self.assertIsNone(run.checked_at)
        self.assertNotIn("checked_at", run.as_dict())

    def test_real_registry_exception_uses_the_current_attempt(self) -> None:
        registry = CollectorRegistry((CallableCollector(
            self.capability, Mock(side_effect=OSError("unreadable")),
        ),))

        for offset in (0, 5, 10, 15):
            run = registry.collect(context(EPOCH + offset))[0]

        self.assertEqual(run.status, "error")
        self.assertEqual(run.error_kind, "OSError")
        self.assertEqual(run.checked_at, timestamp(EPOCH + 15))

    def test_confirmation_preserves_displayed_success_and_actual_failure_times(self) -> None:
        healthy = {"observed_at": timestamp(EPOCH), "usage": {"tokens": 42}}
        outcomes = [
            Collection(snapshot=healthy, checked_at=timestamp(EPOCH)),
            *(Collection(status="error", checked_at=timestamp(EPOCH + offset)) for offset in (60, 120, 180)),
            *(Collection(snapshot=healthy, checked_at=timestamp(EPOCH + offset)) for offset in (240, 300)),
        ]
        registry = CollectorRegistry((CallableCollector(self.capability, Mock(side_effect=outcomes)),))

        runs = [registry.collect(context(EPOCH + offset))[0] for offset in (0, 60, 120, 180, 240, 300)]

        self.assertEqual([run.status for run in runs], ["ok", "ok", "ok", "error", "error", "ok"])
        self.assertEqual([run.checked_at for run in runs[:3]], [timestamp(EPOCH)] * 3)
        self.assertEqual([run.checked_at for run in runs[3:]], [timestamp(EPOCH + offset) for offset in (180, 240, 300)])
        self.assertEqual(runs[1].snapshot, healthy)
        self.assertIsNone(runs[3].snapshot)
        self.assertIsNone(runs[4].snapshot)


class AiAttemptTimeTests(unittest.TestCase):
    def test_each_ai_error_cache_keeps_the_last_real_poll_time(self) -> None:
        for source in ("codex", "opencode", "dsh", "antigravity"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary)
                failure = Mock(side_effect=OSError("unreadable"))
                if source == "codex":
                    collector = codex.CodexUsageCollector(rollout_roots_finder=lambda: (root,), poll_seconds=60)
                    stack.enter_context(patch.object(codex, "rebuild_codex_rollout_ledger", failure))
                elif source == "opencode":
                    collector = opencode.OpenCodeUsageCollector(
                        executable_finder=lambda: "/test/opencode", desktop_database_finder=lambda: None,
                        runner=failure, poll_seconds=60,
                    )
                elif source == "dsh":
                    (root / "one.jsonl").write_text("unreadable", encoding="utf-8")
                    collector = dsh.DshUsageCollector(sessions_finder=lambda: root, poll_seconds=60)
                    stack.enter_context(patch.object(dsh, "read_dsh_session", failure))
                else:
                    collector = antigravity.AntigravityUsageCollector(conversations_finder=lambda: root, poll_seconds=60)
                    stack.enter_context(patch.object(antigravity, "read_antigravity_history", failure))

                first = collector.collect(context(EPOCH))
                cached = collector.collect(context(EPOCH + 5))
                later = collector.collect(context(EPOCH + 60))

                self.assertEqual([first.status, cached.status, later.status], ["error"] * 3)
                self.assertEqual(failure.call_count, 2)
                self.assertEqual(first.checked_at, timestamp(EPOCH))
                self.assertEqual(cached.checked_at, first.checked_at)
                self.assertEqual(later.checked_at, timestamp(EPOCH + 60))
                self.assertEqual(cached.points, ())

    def test_success_cache_does_not_refresh_observation_or_emit_a_second_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = codex.CodexUsageCollector(rollout_roots_finder=lambda: (Path(temporary),), poll_seconds=60)
            first = collector.collect(context(EPOCH))
            cached = collector.collect(context(EPOCH + 5))
            later = collector.collect(context(EPOCH + 60))

        self.assertEqual([first.status, cached.status, later.status], ["ok"] * 3)
        self.assertEqual(first.checked_at, first.snapshot["observed_at"])
        self.assertEqual(cached.checked_at, first.checked_at)
        self.assertEqual(cached.snapshot["observed_at"], first.snapshot["observed_at"])
        self.assertEqual(cached.points, ())
        self.assertEqual(later.checked_at, timestamp(EPOCH + 60))


class LogState:
    max_log_bytes = 1024 * 1024
    backups = 1


class RemoteAttemptTimeTests(unittest.TestCase):
    @staticmethod
    def raw(source: str, epoch: float, counter: int) -> dict:
        raw = {"timestamp": timestamp(epoch - 10), "epoch": epoch}
        if source == "vps":
            raw.update(interface="eth0", in_bytes=counter, out_bytes=counter, in_packets=counter, out_packets=counter)
        else:
            raw["users"] = {"client": {"up_bytes": counter, "down_bytes": counter}}
        return raw

    def test_remote_reader_attempts_advance_time_but_backoff_does_not(self) -> None:
        for source in ("vps", "xray"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                reader = Mock(side_effect=[
                    self.raw(source, EPOCH, 100), OSError("offline"), OSError("offline"),
                    self.raw(source, EPOCH + 900, 200), self.raw(source, EPOCH + 1200, 300),
                ])
                if source == "vps":
                    monitor = VpsMonitor(
                        VpsConfig(True, "test-host", "auto", 300, "one", "One", "both"),
                        Path(temporary), LogState(), reader=reader,
                    )
                else:
                    monitor = XrayStatsMonitor(
                        XrayStatsConfig(True, "test-host", "127.0.0.1:10085", "/test/xray", 300, (), (), "one", "One"),
                        Path(temporary), LogState(), reader=reader,
                    )

                first = monitor.maybe_poll(EPOCH)
                cached = monitor.maybe_poll(EPOCH + 5)
                pending = monitor.maybe_poll(EPOCH + 300)
                failed = monitor.maybe_poll(EPOCH + 600)
                failed_cached = monitor.maybe_poll(EPOCH + 605)
                recovering = monitor.maybe_poll(EPOCH + 900)
                recovered = monitor.maybe_poll(EPOCH + 1200)

                self.assertEqual(reader.call_count, 5)
                self.assertEqual(first["checked_at"], timestamp(EPOCH))
                self.assertEqual(cached["checked_at"], first["checked_at"])
                self.assertNotEqual(first["checked_at"], first["updated_at"])
                self.assertEqual(pending["checked_at"], timestamp(EPOCH + 300))
                self.assertEqual(failed["status"], "error")
                self.assertEqual(failed["checked_at"], timestamp(EPOCH + 600))
                self.assertEqual(failed_cached["checked_at"], failed["checked_at"])
                self.assertEqual(failed["updated_at"], first["updated_at"])
                self.assertEqual(recovering["status"], "error")
                self.assertEqual(recovering["checked_at"], timestamp(EPOCH + 900))
                self.assertEqual(recovered["status"], "ok")
                self.assertEqual(recovered["checked_at"], timestamp(EPOCH + 1200))

    def test_remote_registry_preserves_error_attempt_time_and_unknown_absence(self) -> None:
        for attempt in (timestamp(EPOCH), None):
            with self.subTest(attempt=attempt):
                state = {"status": "error", "last_sample": self.raw("vps", EPOCH - 300, 10)}
                if attempt is not None:
                    state["checked_at"] = attempt
                remote = {"servers": [{"id": "one", "vps": state, "xray_stats": state}]}
                registry = network_collector_registry((("one", "both"),))

                for offset in (0, 5, 10, 15):
                    runs = registry.collect(context(EPOCH + offset, remote))

                for run in runs:
                    if run.capability.source_id == "local-mihomo":
                        continue
                    self.assertEqual(run.status, "error")
                    self.assertEqual(run.checked_at, attempt)
                    self.assertEqual(run.points, ())
                    self.assertIsNone(run.snapshot)

    def test_xray_session_alignment_preserves_attempt_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reader = Mock(return_value=self.raw("xray", EPOCH, 100))
            monitor = XrayStatsMonitor(
                XrayStatsConfig(True, "test-host", "127.0.0.1:10085", "/test/xray", 300, (), ()),
                Path(temporary), LogState(), reader=reader,
            )
            monitor.maybe_poll(EPOCH)
            monitor.align_session(EPOCH + 5)
            state = monitor.maybe_poll(EPOCH + 10)

        self.assertEqual(reader.call_count, 1)
        self.assertEqual(state["checked_at"], timestamp(EPOCH))


if __name__ == "__main__":
    unittest.main()
