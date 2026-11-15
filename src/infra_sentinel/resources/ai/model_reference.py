"""Recognized effort markers, without changing native model identities."""

from dataclasses import dataclass
import re


_EFFORT = re.compile(r"(?P<base>[A-Za-z0-9][A-Za-z0-9._:/+-]*)(?P<marker>-(?:none|minimal|low|medium|high|xhigh|max))\Z")
_THINKING = re.compile(
    r"(?P<base>gemini-[0-9]+(?:\.[0-9]+)?-(?:argon|carbon|hydrogen|pro|flash(?:-lite)?)"
    r"|claude-(?:opus|sonnet|haiku|fable)-[0-9]+(?:-[0-9]+)?)(?P<marker>-thinking)\Z"
)


@dataclass(frozen=True)
class EffortVariant:
    base: str
    marker: str


def effort_variant(identifier: str) -> EffortVariant | None:
    """Known levels apply to new families; other suffixes stay exact."""
    match = _EFFORT.fullmatch(identifier) or _THINKING.fullmatch(identifier)
    return EffortVariant(match["base"], match["marker"]) if match else None
