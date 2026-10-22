"""Bounded read-only diagnostic queries for local users and agents.

This entry point only reads the Agent-owned journal. It does not initialize
state, contact a provider, start the Agent, or submit a mutating command.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time
from typing import Sequence

from infra_sentinel.app.configuration import state_directory_for_config
from infra_sentinel.metrics.diagnostics import DiagnosticStore


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.timestamp()
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("use an ISO 8601 timestamp with a timezone") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read Infra Sentinel diagnostic history as JSON")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--config", type=Path, help="existing Infra Sentinel configuration")
    location.add_argument("--state-dir", type=Path, help="existing Agent state directory")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=float, help="look back up to 720 hours; defaults to 24")
    window.add_argument("--since", type=_timestamp, help="inclusive timestamp with timezone")
    parser.add_argument("--until", type=_timestamp, help="inclusive timestamp with timezone")
    parser.add_argument("--category", choices=("source", "network", "system", "facility", "upstream"))
    parser.add_argument("--source-id", help="source identity returned by a previous diagnostic query")
    parser.add_argument("--severity", choices=("info", "warning", "critical"))
    parser.add_argument("--transition", choices=("raised", "changed", "recovered"))
    parser.add_argument("--limit", type=int, default=50, help="maximum returned events, 1 to 200")
    args = parser.parse_args(argv)
    now = time.time()
    until = args.until if args.until is not None else now
    query: dict[str, object] = {"until_epoch": until, "limit": args.limit}
    if args.since is not None:
        query["since_epoch"] = args.since
    else:
        hours = args.hours if args.hours is not None else 24.0
        if not math.isfinite(hours) or not 0 < hours <= 720:
            parser.error("--hours must be greater than 0 and at most 720")
        query["since_epoch"] = max(0.0, until - hours * 3600)
    for key in ("category", "source_id", "severity", "transition"):
        value = getattr(args, key)
        if value is not None:
            query[key] = value
    try:
        state_dir = args.state_dir if args.state_dir is not None else state_directory_for_config(args.config)
        result = DiagnosticStore(state_dir.expanduser() / "diagnostics.sqlite3").query(query, now_epoch=now)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result.get("available") else 1
    except (OSError, ValueError, TypeError) as exc:
        print(f"Invalid diagnostic query: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
