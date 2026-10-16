from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra_sentinel.core.collectors import CollectorContext
from infra_sentinel.resources.ai.performance import PerformanceSample, performance_details
from infra_sentinel.resources.ai.opencode import OpenCodeUsageCollector, read_opencode_turn_performance
from infra_sentinel.resources.ai.dsh import DshUsageCollector
from infra_sentinel.resources.ai.antigravity import AntigravityUsageCollector

EPOCH = 1791711466


def metrics(snapshot):
    return {m["id"]: m["value"] for g in snapshot["details"] for m in g.get("metrics", [])}


def varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output + bytes([value]))


def number(field, value):
    return varint(field << 3) + varint(value)


def message(field, value):
    return varint((field << 3) | 2) + varint(len(value)) + value


def generation(*, ttft=True, streaming_nanos=0):
    usage = number(1, 20) + number(2, 30) + number(5, 40) + number(9, 100) + number(10, 20)
    usage += message(11, b"private-response-id")
    chat = message(4, usage) + message(19, b"test-model")
    chat += message(9, message(4, number(1, EPOCH)))
    if ttft:
        chat += message(11, number(1, 2))
    chat += message(12, number(1, 3) + number(2, streaming_nanos))
    return message(1, chat)


class PerformanceTests(unittest.TestCase):
    def test_scopes_units_and_invalid_timing_stay_separate(self):
        sample = PerformanceSample(EPOCH, "model", 120, 5000, 2000, 3000)
        groups = performance_details(sample)
        self.assertEqual([g["id"] for g in groups], ["request-performance", "client-stream-performance"])
        self.assertEqual(groups[0]["sample"], groups[1]["sample"])
        self.assertEqual(groups[0]["sample"]["model"], "model")
        self.assertEqual(set(groups[0]["sample"]), {"model", "recorded_at"})
        values = metrics({"details": groups})
        self.assertEqual(values["request-output-rate"], 24)
        self.assertEqual(values["client-stream-output-rate"], 40)
        self.assertEqual(values["first-token-latency"], 2)
        self.assertEqual(performance_details(sample, scope="turn")[0]["id"], "turn-performance")
        for bad in [None, True, -1, float("nan"), float("inf")]:
            with self.subTest(bad=bad):
                self.assertEqual([g["id"] for g in performance_details(replace(sample, duration_ms=bad))],
                                 ["client-stream-performance"])
        for bad in [None, True, -1, 1.5]:
            self.assertEqual(performance_details(replace(sample, output_tokens=bad)), [])

    def test_zero_is_known_and_missing_stream_is_not_zero(self):
        values = metrics({"details": performance_details(PerformanceSample(EPOCH, "m", 0, 1000, 0, None))})
        self.assertEqual(values["request-output-rate"], 0)
        self.assertEqual(values["first-token-latency"], 0)
        self.assertNotIn("client-stream-output-rate", values)

    def test_opencode_completed_parent_turn_includes_reasoning_and_tool_gaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "opencode.db"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
                def add(identifier, role, created, **data):
                    payload = {"role": role, "time": {"created": int((EPOCH + created) * 1000)}, **data}
                    connection.execute("INSERT INTO message VALUES (?, 'session', ?, ?, ?)",
                                       (identifier, int((EPOCH + created) * 1000), int((EPOCH + created) * 1000), json.dumps(payload)))
                add("private-user", "user", 0)
                add("private-a1", "assistant", 1, parentID="private-user", modelID="model", providerID="p",
                    finish="tool-calls", time={"created": (EPOCH + 1)*1000, "completed": (EPOCH + 3)*1000},
                    tokens={"input": 10, "output": 100, "reasoning": 50, "cache": {"read": 0, "write": 0}})
                add("private-a2", "assistant", 5, parentID="private-user", modelID="model", providerID="p",
                    finish="stop", time={"created": (EPOCH + 5)*1000, "completed": (EPOCH + 10)*1000},
                    tokens={"input": 10, "output": 200, "reasoning": 50, "cache": {"read": 0, "write": 0}})
                connection.commit()
                sample = read_opencode_turn_performance(db)
                self.assertEqual((sample.output_tokens, sample.duration_ms), (400, 10000))
                result = OpenCodeUsageCollector(desktop_database_finder=lambda: db).collect(
                    CollectorContext({"epoch": EPOCH + 20}, {}))
                self.assertEqual(result.status, "ok")
                self.assertEqual(metrics(result.snapshot)["turn-output-rate"], 40)
                self.assertNotIn("request-output-rate", metrics(result.snapshot))
                self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 420)
                self.assertNotIn("private-user", json.dumps(result.snapshot))
                # A stop from an earlier assistant message cannot finish a still-open parent turn.
                add("private-a3", "assistant", 12, parentID="private-user", modelID="model",
                    tokens={"output": 1, "reasoning": 0})
                connection.commit()
                self.assertIsNone(read_opencode_turn_performance(db))

    def dsh_result(self, *, retry=False, corrupt=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        rows = [{"type": "session", "version": 4, "id": "private-session", "isSeeded": False}]
        def event(kind, seq, data, ms):
            return {"type": kind, "seq": seq, "time": EPOCH*1000+ms, "data": data}
        rows.append(event("step/start", 0, {"turn": 1, "step": 1}, 0))
        if retry:
            rows.append(event("assistant/attempt", 1, {"turn": 1, "step": 1, "stream": []}, 500))
        stream = [
            {"type": "reasoning-chunks", "time0": EPOCH*1000+1000, "index": 0,
             "dt": [-1 if corrupt else 200], "texts": ["private-reasoning", "private-reasoning"]},
            {"type": "text-chunks", "time0": EPOCH*1000+1500, "index": 1,
             "dt": [], "texts": ["private-answer"]},
            {"type": "chunk", "time": EPOCH*1000+3000, "chunk": {"type": "finish", "reason": "stop"}},
        ]
        rows.append(event("assistant/message", 2, {"turn": 1, "step": 1, "stream": stream,
                    "usage": {"inputTokens": 10, "outputTokens": 120, "cacheReadTokens": 0,
                              "cacheWriteTokens": 0, "totalTokens": 130},
                    "message": {"source": {"provider": "p", "model": "m"}}}, 3000))
        (root/"one.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
        checkpoint = root/"seen.json"
        result = DshUsageCollector(sessions_finder=lambda: root, checkpoint_path=checkpoint).collect(
            CollectorContext({"epoch": EPOCH + 5}, {}))
        for private in ["private-session", "private-reasoning", "private-answer"]:
            self.assertNotIn(private, json.dumps(result.snapshot))
            self.assertNotIn(private, checkpoint.read_text())
        return result

    def test_dsh_stream_correlates_one_usage_without_tools_or_accounting_changes(self):
        result = self.dsh_result()
        values = metrics(result.snapshot)
        self.assertEqual(values["request-output-rate"], 40)
        self.assertEqual(values["client-stream-output-rate"], 60)
        self.assertEqual(values["first-token-latency"], 1)
        self.assertEqual(result.points, ())
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 130)

    def test_dsh_unknown_retry_start_keeps_only_observed_stream(self):
        values = metrics(self.dsh_result(retry=True).snapshot)
        self.assertNotIn("request-output-rate", values)
        self.assertEqual(values["client-stream-output-rate"], 60)

    def test_dsh_bad_stream_does_not_drop_token_usage(self):
        result = self.dsh_result(corrupt=True)
        self.assertNotIn("client-stream-output-rate", metrics(result.snapshot))
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 130)

    def antigravity_result(self, blob):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with closing(sqlite3.connect(root/"one.db")) as connection:
            connection.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            connection.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO gen_metadata VALUES (?, ?)", [(1, blob), (2, blob)])
            connection.commit()
        return AntigravityUsageCollector(conversations_finder=lambda: root).collect(
            CollectorContext({"epoch": EPOCH + 10}, {}))

    def test_antigravity_reported_durations_dedup_and_reasoning(self):
        result = self.antigravity_result(generation())
        values = metrics(result.snapshot)
        self.assertEqual(values["request-output-rate"], 24)
        self.assertEqual(values["client-stream-output-rate"], 40)
        self.assertEqual(values["first-token-latency"], 2)
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 210)
        self.assertNotIn("private-response-id", json.dumps(result.snapshot))

    def test_antigravity_missing_ttft_never_guesses_request_duration(self):
        values = metrics(self.antigravity_result(generation(ttft=False)).snapshot)
        self.assertNotIn("request-output-rate", values)
        self.assertEqual(values["client-stream-output-rate"], 40)

    def test_antigravity_invalid_duration_does_not_drop_usage(self):
        result = self.antigravity_result(generation(streaming_nanos=1000000000))
        self.assertNotIn("client-stream-output-rate", metrics(result.snapshot))
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 210)


if __name__ == "__main__":
    unittest.main()
