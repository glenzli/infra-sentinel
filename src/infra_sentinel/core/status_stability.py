"""Small in-memory confirmation windows for user-visible health states.

The helper deliberately stores no samples and writes no checkpoints.  Owners
feed it already-classified states; it only prevents one transient observation
from immediately changing the confirmed state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class StatusDecision:
    status: str
    changed: bool
    pending_status: str | None = None
    pending_count: int = 0
    required_count: int = 0


class StatusStabilizer:
    """Confirm worsening and recovery transitions with consecutive samples."""

    def __init__(
        self,
        initial_status: str,
        ranks: Mapping[str, int],
        *,
        worsen_after: int = 3,
        recover_after: int = 2,
    ) -> None:
        self.status = initial_status
        self.ranks = dict(ranks)
        self.worsen_after = max(1, int(worsen_after))
        self.recover_after = max(1, int(recover_after))
        self.pending_status: str | None = None
        self.pending_count = 0

    def observe(self, candidate: str, *, immediate: bool = False) -> StatusDecision:
        if candidate == self.status:
            self.pending_status = None
            self.pending_count = 0
            return StatusDecision(self.status, False)
        if immediate:
            previous = self.status
            self.status = candidate
            self.pending_status = None
            self.pending_count = 0
            return StatusDecision(self.status, self.status != previous)

        current_rank = self.ranks.get(self.status, 0)
        candidate_rank = self.ranks.get(candidate, current_rank)
        required = self.worsen_after if candidate_rank > current_rank else self.recover_after
        if self.pending_status == candidate:
            self.pending_count += 1
        else:
            self.pending_status = candidate
            self.pending_count = 1
        if self.pending_count < required:
            return StatusDecision(
                self.status,
                False,
                pending_status=candidate,
                pending_count=self.pending_count,
                required_count=required,
            )

        previous = self.status
        self.status = candidate
        self.pending_status = None
        self.pending_count = 0
        return StatusDecision(self.status, self.status != previous)
