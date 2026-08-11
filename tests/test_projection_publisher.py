from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.app.projection_publisher import ProjectionPublisher  # noqa: E402
from infra_sentinel.app.protocol import PROJECTION_SCHEMA  # noqa: E402


class ProjectionPublisherTests(unittest.TestCase):
    def test_every_frame_is_streamed_but_checkpoint_is_low_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            stream = BytesIO()
            publisher = ProjectionPublisher(
                state_dir,
                stream=stream,
                checkpoint_seconds=300,
                clock=lambda: 100.0,
            )

            publisher.publish({"updated_at": "first", "infra": {}}, epoch=100.0)
            first_checkpoint = (state_dir / "projection.json").stat().st_mtime_ns
            publisher.publish({"updated_at": "second", "infra": {}}, epoch=105.0)

            frames = [json.loads(line) for line in stream.getvalue().decode("utf-8").splitlines()]
            self.assertEqual([frame["updated_at"] for frame in frames], ["first", "second"])
            self.assertEqual(frames[-1]["schema"], PROJECTION_SCHEMA)
            checkpoint = json.loads((state_dir / "projection.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["updated_at"], "first")
            self.assertEqual((state_dir / "projection.json").stat().st_mtime_ns, first_checkpoint)

            publisher.publish({"updated_at": "third", "infra": {}}, epoch=400.0)
            checkpoint = json.loads((state_dir / "projection.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["updated_at"], "third")

    def test_shutdown_checkpoint_persists_the_latest_streamed_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            publisher = ProjectionPublisher(
                state_dir,
                stream=BytesIO(),
                checkpoint_seconds=300,
            )
            publisher.publish({"updated_at": "first"}, epoch=100.0)
            publisher.publish({"updated_at": "latest"}, epoch=105.0)

            self.assertTrue(publisher.checkpoint(epoch=110.0))

            checkpoint = json.loads((state_dir / "projection.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["updated_at"], "latest")


if __name__ == "__main__":
    unittest.main()
