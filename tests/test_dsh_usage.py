from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import CollectorContext  # noqa: E402
from infra_sentinel.resources.ai.dsh import DshUsageCollector, read_dsh_session  # noqa: E402


EPOCH = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()


def header(**fields: object) -> str:
    return json.dumps({"type": "session", "version": 4, "id": "private-session-id", "isSeeded": False, "cwd": "/private/workspace", **fields}) + "\n"


def event(kind: str, seq: int, data: dict[str, object], *, epoch: float = EPOCH) -> str:
    return json.dumps({"type": kind, "seq": seq, "time": int(epoch * 1000), "data": data}) + "\n"


def usage(seq: int, *, step: int = 1, output: int = 30, chunk: bool = False, epoch: float = EPOCH) -> str:
    counters = {"inputTokens": 10, "outputTokens": output, "cacheReadTokens": 100, "cacheWriteTokens": 5, "totalTokens": 115 + output}
    data = {"turn": 1, "step": step}
    if chunk:
        data["chunk"] = {"type": "usage", "usage": counters}
    else:
        data.update({"usage": counters, "message": {"content": "private-response-text", "source": {"provider": "deepseek-account", "model": "deepseek-flash"}}})
    return event("assistant/chunk" if chunk else "assistant/message", seq, data, epoch=epoch)


class DshUsageTests(unittest.TestCase):
    def test_streaming_final_and_retry_are_folded_per_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.v4.jsonl"
            path.write_text(header() + usage(1, output=40, chunk=True) + usage(2, output=30)
                            + event("llm/retry-started", 3, {"turn": 1, "step": 1})
                            + usage(4, output=20) + usage(5, step=2, output=10), encoding="utf-8")
            requests = read_dsh_session(path).requests
        self.assertEqual([request.usage["output"] for request in requests], [30, 20, 10])
        self.assertEqual(len({request.marker for request in requests}), 3)

    def test_history_is_event_dated_and_duplicate_files_do_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = header() + usage(1, epoch=EPOCH - 86400)
            (root / "one.v4.jsonl").write_text(content, encoding="utf-8")
            (root / "copied.v4.jsonl").write_text(content, encoding="utf-8")
            result = DshUsageCollector(sessions_finder=lambda: root).collect(CollectorContext({"epoch": EPOCH}, {}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.points, ())
        self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 0)
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 145)
        self.assertEqual(result.snapshot["history"]["daily"][0]["tokens"], 145)
        self.assertEqual(result.snapshot["models"][0]["id"], "deepseek-account/deepseek-flash")
        self.assertEqual(result.snapshot["history"]["hourly"], [])

    def test_new_requests_emit_once_across_checkpoint_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            path = root / "one.v4.jsonl"
            path.write_text(header() + usage(1), encoding="utf-8")
            checkpoint = Path(temporary) / "seen.json"
            collector = DshUsageCollector(sessions_finder=lambda: root, checkpoint_path=checkpoint, poll_seconds=1)
            collector.collect(CollectorContext({"epoch": EPOCH}, {}))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(usage(2, step=2, output=10))
            added = collector.collect(CollectorContext({"epoch": EPOCH + 2}, {}))
            restarted = DshUsageCollector(sessions_finder=lambda: root, checkpoint_path=checkpoint)
            repeated = restarted.collect(CollectorContext({"epoch": EPOCH + 3}, {}))
            saved = checkpoint.read_text()
        self.assertEqual([point.value for point in added.points], [125])
        self.assertEqual(repeated.points, ())
        self.assertEqual(repeated.snapshot["usage"]["cumulative"]["tokens"], 270)
        self.assertEqual(sum(row["tokens"] for row in repeated.snapshot["history"]["hourly"]), 270)
        for private in ("private-session-id", "private-response-text", "/private/workspace"):
            self.assertNotIn(private, saved)
            self.assertNotIn(private, json.dumps(repeated.snapshot))

    def test_unfinished_stream_is_not_persisted_as_a_settled_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "one.v4.jsonl"
            path.write_text(header() + usage(1, output=40, chunk=True), encoding="utf-8")
            collector = DshUsageCollector(sessions_finder=lambda: root, poll_seconds=1)
            initial = collector.collect(CollectorContext({"epoch": EPOCH}, {}))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(usage(2, output=30))
            final = collector.collect(CollectorContext({"epoch": EPOCH + 2}, {}))
        self.assertEqual(initial.points, ())
        self.assertEqual(initial.snapshot["usage"]["today"]["tokens"], 155)
        self.assertEqual([point.value for point in final.points], [145])

    def test_explicit_inherited_prefix_is_excluded_but_resume_marker_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "one.v4.jsonl"
            path.write_text(header(parentSession="private-parent", seedLength=2) + usage(1, output=1000)
                            + usage(2, step=2) + event("session/end-seed", 3, {}), encoding="utf-8")
            requests = read_dsh_session(path).requests
        self.assertEqual([request.usage["output"] for request in requests], [30])

    def test_unknown_format_or_inheritance_and_invalid_counters_fail_closed(self) -> None:
        for content in (
            header(version=99) + usage(1), header(isSeeded=True) + usage(1),
            header() + usage(1).replace('"inputTokens": 10', '"inputTokens": true'),
            header() + usage(1).replace('"totalTokens": 145', '"totalTokens": 999'),
        ):
            with self.subTest(content=content[:30]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "one.v4.jsonl").write_text(content, encoding="utf-8")
                result = DshUsageCollector(sessions_finder=lambda: root).collect(CollectorContext({"epoch": EPOCH}, {}))
                self.assertEqual(result.status, "error")
                self.assertFalse(result.snapshot["available"])

    def test_incomplete_append_keeps_last_valid_snapshot_with_degraded_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "one.v4.jsonl"
            path.write_text(header() + usage(1), encoding="utf-8")
            collector = DshUsageCollector(sessions_finder=lambda: root, poll_seconds=1)
            collector.collect(CollectorContext({"epoch": EPOCH}, {}))
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"type":')
            result = collector.collect(CollectorContext({"epoch": EPOCH + 2}, {}))
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 145)
        self.assertEqual(result.points, ())

    def test_concatenated_zstd_frames_are_read_without_touching_source(self) -> None:
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("stdlib Zstd is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "one.v4.jsonl.zstd"
            content = zstd.compress(header().encode()) + zstd.compress(usage(1).encode())
            path.write_bytes(content)
            result = DshUsageCollector(sessions_finder=lambda: root).collect(CollectorContext({"epoch": EPOCH}, {}))
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 145)

    def test_corrupt_zstd_is_unavailable_instead_of_a_zero_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.v4.jsonl.zstd").write_bytes(b"invalid zstd")
            result = DshUsageCollector(sessions_finder=lambda: root).collect(CollectorContext({"epoch": EPOCH}, {}))
        self.assertEqual(result.status, "error")
        self.assertFalse(result.snapshot["available"])

    def test_corrupt_checkpoint_rebaselines_without_replaying_old_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            (root / "one.v4.jsonl").write_text(header() + usage(1), encoding="utf-8")
            checkpoint = Path(temporary) / "seen.json"
            checkpoint.write_text('[]', encoding="utf-8")
            result = DshUsageCollector(sessions_finder=lambda: root, checkpoint_path=checkpoint).collect(CollectorContext({"epoch": EPOCH}, {}))
        self.assertEqual(result.points, ())
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 145)

    def test_missing_dsh_is_unavailable(self) -> None:
        result = DshUsageCollector(sessions_finder=lambda: None).collect(CollectorContext({"epoch": EPOCH}, {}))
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.snapshot["available"])


if __name__ == "__main__":
    unittest.main()
