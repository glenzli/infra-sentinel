from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

from mihomo_traffic import (  # noqa: E402
    MihomoTrafficTracker,
    classify_host,
    classify_route,
    combine_samples,
    load_tracker,
    save_tracker,
)


def connection(
    connection_id: str,
    host: str,
    upload: int,
    download: int,
    chains: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": connection_id,
        "upload": upload,
        "download": download,
        "chains": chains or ["DMIT-LAX", "Proxy"],
        "metadata": {"host": host, "destinationIP": "203.0.113.1"},
    }


def payload(
    upload_total: int,
    download_total: int,
    connections: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "uploadTotal": upload_total,
        "downloadTotal": download_total,
        "connections": connections,
    }


class DomainClassificationTests(unittest.TestCase):
    def test_groups_observed_openai_hosts_as_chatgpt(self) -> None:
        self.assertEqual(classify_host("chatgpt.com")[:2], ("chatgpt", "ChatGPT"))
        self.assertEqual(classify_host("ab.chatgpt.com")[:2], ("chatgpt", "ChatGPT"))
        self.assertEqual(classify_host("chat.openai.com")[:2], ("chatgpt", "ChatGPT"))

    def test_labels_google_broadly_instead_of_claiming_one_app(self) -> None:
        self.assertEqual(
            classify_host("optimizationguide-pa.googleapis.com")[:2],
            ("google", "Google"),
        )

    def test_fallback_collapses_subdomains_to_a_stable_site(self) -> None:
        first = classify_host("raw.example.com")
        second = classify_host("api.example.com")
        self.assertEqual(first[:2], second[:2])
        self.assertEqual(first[1], "example.com")

    def test_route_uses_the_actual_mihomo_chain(self) -> None:
        self.assertEqual(classify_route(["DIRECT"]), "direct")
        self.assertEqual(classify_route(["REJECT-DROP"]), "blocked")
        self.assertEqual(classify_route(["DMIT-LAX", "Proxy"]), "proxy")
        self.assertEqual(classify_route([]), "unknown")


class MihomoTrafficTrackerTests(unittest.TestCase):
    def test_first_read_is_only_a_baseline(self) -> None:
        tracker = MihomoTrafficTracker()
        sample = tracker.apply(
            payload(100, 200, [connection("one", "chatgpt.com", 100, 200)]),
            10,
        )
        self.assertEqual(sample["kernel"]["total_bytes"], 0)
        self.assertEqual(sample["services"], [])

    def test_exact_global_delta_is_split_by_domain_and_route(self) -> None:
        tracker = MihomoTrafficTracker()
        tracker.apply(
            payload(
                100,
                200,
                [
                    connection("chat", "chatgpt.com", 80, 150),
                    connection("direct", "example.com", 20, 50, ["DIRECT"]),
                ],
            ),
            10,
        )
        sample = tracker.apply(
            payload(
                160,
                290,
                [
                    connection("chat", "chatgpt.com", 130, 220),
                    connection("direct", "example.com", 30, 70, ["DIRECT"]),
                ],
            ),
            11,
        )
        self.assertEqual(sample["kernel"]["up_bytes"], 60)
        self.assertEqual(sample["kernel"]["down_bytes"], 90)
        chatgpt = next(row for row in sample["services"] if row["id"] == "chatgpt")
        self.assertEqual((chatgpt["up_bytes"], chatgpt["down_bytes"]), (50, 70))
        self.assertEqual(sample["routes"]["proxy"]["total_bytes"], 120)
        self.assertEqual(sample["routes"]["direct"]["total_bytes"], 30)
        self.assertEqual(sample["attribution"]["coverage"], 1.0)

    def test_closed_connection_tail_is_preserved_as_unattributed(self) -> None:
        tracker = MihomoTrafficTracker()
        tracker.apply(
            payload(100, 200, [connection("chat", "chatgpt.com", 100, 200)]),
            10,
        )
        sample = tracker.apply(payload(140, 260, []), 11)
        unattributed = next(row for row in sample["services"] if row["id"] == "unattributed")
        self.assertEqual(unattributed["total_bytes"], 100)
        self.assertEqual(sample["kernel"]["total_bytes"], 100)
        self.assertEqual(sample["attribution"]["coverage"], 0.0)

    def test_new_short_lived_connection_counts_current_counters(self) -> None:
        tracker = MihomoTrafficTracker()
        tracker.apply(payload(100, 200, []), 10)
        sample = tracker.apply(
            payload(130, 240, [connection("new", "ab.chatgpt.com", 30, 40)]),
            11,
        )
        chatgpt = next(row for row in sample["services"] if row["id"] == "chatgpt")
        self.assertEqual(chatgpt["total_bytes"], 70)
        self.assertEqual(sample["attribution"]["coverage"], 1.0)

    def test_snapshot_race_can_never_make_categories_exceed_global_total(self) -> None:
        tracker = MihomoTrafficTracker()
        tracker.apply(payload(100, 100, []), 10)
        sample = tracker.apply(
            payload(
                110,
                120,
                [
                    connection("one", "chatgpt.com", 100, 100),
                    connection("two", "google.com", 100, 100),
                ],
            ),
            11,
        )
        self.assertEqual(sum(row["total_bytes"] for row in sample["services"]), 30)
        self.assertEqual(sample["kernel"]["total_bytes"], 30)

    def test_core_counter_reset_does_not_replay_bytes(self) -> None:
        tracker = MihomoTrafficTracker()
        tracker.apply(payload(1_000, 2_000, []), 10)
        sample = tracker.apply(
            payload(10, 20, [connection("new", "chatgpt.com", 10, 20)]),
            11,
        )
        self.assertEqual(sample["kernel"]["total_bytes"], 0)

    def test_tracker_round_trip_keeps_connection_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.json"
            tracker = MihomoTrafficTracker()
            tracker.apply(
                payload(100, 200, [connection("chat", "chatgpt.com", 100, 200)]),
                10,
            )
            save_tracker(path, tracker)
            restored = load_tracker(path)
            sample = restored.apply(
                payload(120, 230, [connection("chat", "chatgpt.com", 120, 230)]),
                11,
            )
            self.assertEqual(sample["kernel"]["total_bytes"], 50)
            self.assertEqual(sample["services"][0]["total_bytes"], 50)


class CombineSamplesTests(unittest.TestCase):
    def test_combined_interval_keeps_exact_total_and_latest_cumulative(self) -> None:
        tracker = MihomoTrafficTracker()
        tracker.apply(payload(0, 0, []), 0)
        first = tracker.apply(
            payload(10, 20, [connection("one", "chatgpt.com", 10, 20)]),
            1,
        )
        second = tracker.apply(
            payload(15, 25, [connection("one", "chatgpt.com", 15, 25)]),
            2,
        )
        combined = combine_samples([first, second])
        self.assertEqual(combined["kernel"]["total_bytes"], 40)
        self.assertEqual(combined["kernel"]["cumulative_total_bytes"], 40)
        self.assertEqual(combined["services"][0]["total_bytes"], 40)
        self.assertEqual(combined["observed_seconds"], 2)


if __name__ == "__main__":
    unittest.main()
