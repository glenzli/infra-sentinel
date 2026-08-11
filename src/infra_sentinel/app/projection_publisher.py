"""Live projection stream with a bounded, low-frequency disk checkpoint."""

from __future__ import annotations

from pathlib import Path
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from infra_sentinel.app.protocol import (
    encode_projection,
    projection_document,
    write_projection_checkpoint,
)


PROJECTION_STREAM_ENV = "INFRA_SENTINEL_PROJECTION_STREAM"
PROJECTION_STREAM_STDIO = "stdio"
PROJECTION_CHECKPOINT_SECONDS = 5 * 60


class ProjectionPublisher:
    """Own projection delivery, flushing, and recovery-checkpoint cadence."""

    def __init__(
        self,
        state_dir: Path,
        *,
        stream: BinaryIO | None = None,
        checkpoint_seconds: float = PROJECTION_CHECKPOINT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = state_dir
        self.stream = stream
        self.checkpoint_seconds = max(1.0, float(checkpoint_seconds))
        self.clock = clock
        self._last_checkpoint_epoch: float | None = None
        self._latest_document: dict[str, Any] | None = None

    def publish(self, payload: dict[str, Any], *, epoch: float | None = None) -> dict[str, Any]:
        """Publish one live frame and checkpoint it only when the cadence is due."""
        document = projection_document(payload)
        if self.stream is not None:
            encoded = encode_projection(document)
            self.stream.write((encoded + "\n").encode("utf-8"))
            self.stream.flush()
        self._latest_document = document
        now = self.clock() if epoch is None else float(epoch)
        if (
            self._last_checkpoint_epoch is None
            or now - self._last_checkpoint_epoch >= self.checkpoint_seconds
        ):
            self.checkpoint(epoch=now)
        return document

    def checkpoint(self, *, epoch: float | None = None) -> bool:
        """Persist the latest complete frame for restart recovery."""
        if self._latest_document is None:
            return False
        write_projection_checkpoint(self.state_dir, self._latest_document)
        self._last_checkpoint_epoch = self.clock() if epoch is None else float(epoch)
        return True
