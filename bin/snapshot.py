#!/usr/bin/env python3
"""Executable wrapper for privacy-bounded evidence snapshots."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from infra_sentinel.cli.snapshot import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
