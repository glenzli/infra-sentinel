"""Read-only local Codex JSONL usage reconstruction."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from infra_sentinel.resources.ai.codex_sampling import audit_codex_rollouts, discover_codex_rollout_roots


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconstruct this machine's visible Codex JSONL token usage.")
    parser.add_argument("--from", dest="start_day", type=_day, required=True, help="first local day, YYYY-MM-DD")
    parser.add_argument("--to", dest="end_day", type=_day, help="last local day; defaults to --from")
    parser.add_argument("--timezone", help="IANA timezone; defaults to the system timezone")
    parser.add_argument("--root", action="append", type=Path, help="rollout root; repeat for live and archived roots")
    parser.add_argument("--json", action="store_true", help="print aggregate JSON")
    return parser


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    end_day = args.end_day or args.start_day
    if end_day < args.start_day:
        parser.error("--to must not precede --from")
    try:
        timezone = ZoneInfo(args.timezone) if args.timezone else datetime.now().astimezone().tzinfo
    except ZoneInfoNotFoundError as error:
        raise SystemExit(f"unknown timezone: {args.timezone}") from error
    if timezone is None:
        raise SystemExit("system timezone is unavailable")
    roots = discover_codex_rollout_roots(args.root)
    audit = audit_codex_rollouts(roots, start_day=args.start_day, end_day=end_day, timezone=timezone)
    if args.json:
        print(json.dumps(audit.as_payload(), ensure_ascii=False, indent=2))
        return 0
    print(f"Codex local visible JSONL usage ({timezone})")
    print(
        f"Coverage: {audit.scanned_files}/{audit.candidate_files} files, "
        f"{_format_bytes(audit.bytes_scanned)}, {audit.duplicate_files} duplicate files, "
        f"{audit.read_errors} read errors"
    )
    for ordinal in range(args.start_day.toordinal(), end_day.toordinal() + 1):
        day = date.fromordinal(ordinal)
        usage = audit.days.get(day.isoformat())
        if usage is None:
            print(f"{day.isoformat()}: 0 tokens")
            continue
        print(f"{day.isoformat()}: {usage.composition.total_tokens:,} tokens")
        sources = ", ".join(f"{source} {tokens:,}" for source, tokens in sorted(usage.source_tokens.items()))
        print(f"  sources: {sources or 'none'}")
        models = ", ".join(f"{model} {tokens:,}" for model, tokens in sorted(
            usage.composition.models.items(), key=lambda item: (-item[1], item[0])
        ))
        print(f"  models: {models or 'none'}")
        print(
            f"  records: {usage.token_records:,}; counted: {usage.composition.events:,}; "
            f"duplicates: {usage.duplicate_snapshots:,}; resets: {usage.counter_resets:,}; "
            f"delta/last mismatches: {usage.delta_last_mismatches:,}"
        )
    print("Scope: this machine's still-visible live and archived rollout metadata; not account billing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
