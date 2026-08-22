from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.core.collectors import CollectorContext  # noqa: E402
from infra_sentinel.resources.ai.antigravity import (  # noqa: E402
    AntigravityUsageCollector,
    MAX_METADATA_BYTES,
    _local_day,
    discover_antigravity_cli_conversations,
    discover_antigravity_conversations,
    read_antigravity_cli_history,
    read_antigravity_history,
)
from infra_sentinel.resources.ai.antigravity_pricing import estimate_antigravity_text_api_cost  # noqa: E402


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def _field_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _field_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _generation(
    *,
    timestamp_seconds: int,
    model: str | None,
    display: str | None,
    response_id: str,
    system: int,
    input_tokens: int,
    cache_read: int,
    output_tokens: int,
    reasoning: int,
) -> bytes:
    usage = b"".join((
        _field_varint(1, system),
        _field_varint(2, input_tokens),
        _field_varint(5, cache_read),
        _field_varint(9, output_tokens),
        _field_varint(10, reasoning),
        _field_bytes(11, response_id.encode("utf-8")),
    ))
    timestamp = _field_varint(1, timestamp_seconds) + _field_varint(2, 0)
    generation_timestamp = _field_bytes(4, timestamp)
    chat = _field_bytes(4, usage) + _field_bytes(9, generation_timestamp)
    if model is not None:
        chat += _field_bytes(19, model.encode("utf-8"))
    if display is not None:
        chat += _field_bytes(21, display.encode("utf-8"))
    return _field_bytes(1, chat)


def _conversation(path: Path, rows: list[bytes]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER NOT NULL DEFAULT 0)")
        connection.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, task_details BLOB)")
        connection.execute("INSERT INTO steps VALUES (1, ?)", (b"private prompt must remain unread",))
        connection.executemany(
            "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
            list(enumerate(rows)),
        )
        connection.commit()


class AntigravityUsageTests(unittest.TestCase):
    def test_readonly_cli_metadata_recovers_model_and_deduplicates_response(self) -> None:
        timestamp = int(datetime(2026, 8, 22, 12).timestamp())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "conversation.db"
            _conversation(database, [
                _generation(
                    timestamp_seconds=timestamp, model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                    response_id="response-a", system=100, input_tokens=200, cache_read=500,
                    output_tokens=30, reasoning=10,
                ),
                _generation(
                    timestamp_seconds=timestamp, model=None, display="Gemini 3.7 Flash",
                    response_id="response-b", system=100, input_tokens=10, cache_read=0,
                    output_tokens=20, reasoning=0,
                ),
                _generation(
                    timestamp_seconds=timestamp, model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                    response_id="response-a", system=999, input_tokens=999, cache_read=999,
                    output_tokens=999, reasoning=999,
                ),
            ])

            history = read_antigravity_cli_history(root)

        day = _local_day(timestamp * 1_000)
        usage = history.days[day]["gemini-3.7-flash"]
        self.assertEqual(history.sessions, 1)
        self.assertEqual((usage.input_tokens, usage.cache_read_tokens, usage.output_tokens, usage.reasoning_tokens), (410, 500, 50, 10))
        self.assertEqual(usage.total_tokens, 970)
        self.assertEqual(usage.generations, 2)

    def test_multiple_stores_deduplicate_response_ids_and_skip_oversized_metadata(self) -> None:
        timestamp = int(datetime(2026, 8, 22, 12).timestamp())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "antigravity"
            second = root / "antigravity-ide"
            first.mkdir()
            second.mkdir()
            shared = _generation(
                timestamp_seconds=timestamp, model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                response_id="shared", system=100, input_tokens=200, cache_read=500,
                output_tokens=30, reasoning=10,
            )
            unique = _generation(
                timestamp_seconds=timestamp, model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                response_id="unique", system=10, input_tokens=20, cache_read=0,
                output_tokens=30, reasoning=0,
            )
            _conversation(first / "one.db", [shared])
            _conversation(second / "two.db", [shared, unique, b"x" * (MAX_METADATA_BYTES + 1)])
            history = read_antigravity_history((first, second))

        day = _local_day(timestamp * 1_000)
        self.assertEqual(history.days[day]["gemini-3.7-flash"].total_tokens, 900)
        self.assertEqual(history.stores, 2)
        self.assertEqual(history.sessions, 2)
        self.assertEqual(history.skipped_metadata_rows, 1)

    def test_collector_projects_daily_history_without_retaining_steps_payload(self) -> None:
        epoch = datetime(2026, 8, 22, 12).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _conversation(root / "conversation.db", [
                _generation(
                    timestamp_seconds=int(epoch), model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                    response_id="response-a", system=100, input_tokens=200, cache_read=500,
                    output_tokens=30, reasoning=10,
                ),
            ])
            collector = AntigravityUsageCollector(conversations_finder=lambda: root, poll_seconds=1)
            result = collector.collect(CollectorContext({"epoch": epoch}, {}))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.snapshot["label"], "Antigravity")
        self.assertEqual(result.snapshot["usage"]["today"]["tokens"], 840)
        self.assertEqual(result.snapshot["usage"]["cumulative"]["tokens"], 840)
        self.assertEqual(result.snapshot["models"][0]["id"], "gemini-3.7-flash")
        self.assertEqual(result.snapshot["history"]["daily"][0]["tokens"], 840)
        daily_reference = result.snapshot["pricing"]["daily"][0]["reference"]
        self.assertEqual(daily_reference["kind"], "catalog-text-api-reference")
        self.assertAlmostEqual(daily_reference["cost_usd"], 0.0004125)
        pricing = next(item for item in result.snapshot["details"] if item["id"] == "antigravity-api-reference")
        self.assertEqual(pricing["badge"]["en"], "reference · not billing")
        self.assertAlmostEqual(next(item for item in pricing["metrics"] if item["id"] == "antigravity-api-today")["value"], 0.0004125)
        self.assertNotIn("private prompt", repr(result.snapshot))
        self.assertEqual(result.points, ())

    def test_exact_gemini_price_reference_rejects_unknown_model_ids(self) -> None:
        estimate = estimate_antigravity_text_api_cost({
            "gemini-3.7-flash": {
                "input_tokens": 100,
                "cache_read_tokens": 500,
                "output_tokens": 30,
                "reasoning_tokens": 10,
            },
            "display-name-is-not-an-id": {
                "input_tokens": 1,
                "cache_read_tokens": 2,
                "output_tokens": 3,
                "reasoning_tokens": 4,
            },
        })
        self.assertAlmostEqual(estimate.total_cost_usd, 0.0002625)
        self.assertEqual(estimate.priced_tokens, 640)
        self.assertEqual(estimate.unpriced_tokens, 10)
    def test_explicit_claude_and_gemini_aliases_are_priced_without_guessing_other_aliases(self) -> None:
        estimate = estimate_antigravity_text_api_cost({
            "claude-opus-4-6-thinking": {"input_tokens": 1_000_000},
            "gemini-3.1-pro-low": {"input_tokens": 1_000_000},
            "gemini-pro-default": {"input_tokens": 1_000_000},
        })
        self.assertAlmostEqual(estimate.total_cost_usd, 7.0)
        self.assertEqual(estimate.priced_tokens, 2_000_000)
        self.assertEqual(estimate.unpriced_tokens, 1_000_000)


    def test_collector_deltas_only_new_local_generation_metadata(self) -> None:
        epoch = datetime(2026, 8, 22, 12).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "conversation.db"
            _conversation(database, [
                _generation(
                    timestamp_seconds=int(epoch), model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                    response_id="response-a", system=100, input_tokens=200, cache_read=0,
                    output_tokens=30, reasoning=10,
                ),
            ])
            collector = AntigravityUsageCollector(conversations_finder=lambda: root, poll_seconds=1)
            collector.collect(CollectorContext({"epoch": epoch}, {}))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
                    (1, _generation(
                        timestamp_seconds=int(epoch), model="gemini-3.7-flash", display="Gemini 3.7 Flash",
                        response_id="response-b", system=100, input_tokens=10, cache_read=20,
                        output_tokens=40, reasoning=0,
                    )),
                )
                connection.commit()
            next_result = collector.collect(CollectorContext({"epoch": epoch + 2}, {}))

        self.assertEqual(sum(point.value for point in next_result.points if point.metric == "ai.tokens.total"), 170)
        self.assertEqual(sum(point.value for point in next_result.points if point.metric == "ai.tokens.input"), 110)
        self.assertEqual(sum(point.value for point in next_result.points if point.metric == "ai.tokens.cache_read"), 20)

    def test_discovery_uses_explicit_directory_and_retains_cli_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(discover_antigravity_conversations(directory)[0], directory.resolve())
            self.assertEqual(discover_antigravity_cli_conversations(directory), directory)

    def test_missing_generation_table_fails_closed(self) -> None:
        epoch = datetime(2026, 8, 22, 12).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with closing(sqlite3.connect(root / "invalid.db")):
                pass
            result = AntigravityUsageCollector(conversations_finder=lambda: root).collect(
                CollectorContext({"epoch": epoch}, {}),
            )

        self.assertEqual(result.status, "error")
        self.assertTrue(result.snapshot["available"])


if __name__ == "__main__":
    unittest.main()
