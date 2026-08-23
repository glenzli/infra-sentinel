"""One-time, recoverable migration from Codex SQLite deltas to JSONL ledger."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable

from infra_sentinel.metrics.store import CODEX_JSONL_HISTORY_MIGRATION, MetricStore
from infra_sentinel.resources.ai.codex_sampling import (
    discover_codex_rollout_roots,
    load_codex_rollout_ledger,
    rebuild_codex_rollout_ledger,
    save_codex_rollout_ledger,
)


BACKUP_DIRECTORY = CODEX_JSONL_HISTORY_MIGRATION
LEDGER_FILENAME = "codex-rollout-ledger.json"
LEGACY_CODEX_FILES = ("codex-usage-day.json", "codex-session-events.json")


def _backup_sqlite(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as readable, sqlite3.connect(destination) as backup:
        readable.backup(backup)


def backup_codex_history(state_dir: Path, store: MetricStore, *, now: datetime) -> dict[str, Any]:
    """Create one consistent store backup plus the two legacy Codex checkpoints."""
    store.initialize()
    backup_dir = state_dir / "backups" / BACKUP_DIRECTORY
    manifest_path = backup_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict):
            return {"status": "current", **current}
    backup_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    if store.path.is_file():
        destination = backup_dir / store.path.name
        _backup_sqlite(store.path, destination)
        files.append({"name": destination.name, "bytes": destination.stat().st_size})
    for name in LEGACY_CODEX_FILES:
        source = state_dir / name
        if not source.is_file():
            continue
        destination = backup_dir / name
        shutil.copy2(source, destination)
        files.append({"name": name, "bytes": destination.stat().st_size})
    manifest = {
        "schema": CODEX_JSONL_HISTORY_MIGRATION,
        "created_at": now.astimezone().isoformat(timespec="seconds"),
        "files": files,
    }
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    temporary.replace(manifest_path)
    return {"status": "created", **manifest}


def prepare_codex_jsonl_migration(
    state_dir: Path,
    store: MetricStore,
    *,
    roots: Iterable[Path] | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Build the ledger, back up old state, then remove only old Codex points."""
    ledger_path = state_dir / LEDGER_FILENAME
    ledger = load_codex_rollout_ledger(ledger_path)
    current = store.metadata(CODEX_JSONL_HISTORY_MIGRATION)
    if current is not None and ledger.rebuilt_at:
        return {"status": "current", "store": current, "ledger": ledger.as_payload()["schema"]}
    readable_roots = discover_codex_rollout_roots(roots)
    if not readable_roots:
        return {"status": "blocked", "reason": "codex-rollout-roots-missing"}
    if not ledger.rebuilt_at:
        ledger = rebuild_codex_rollout_ledger(readable_roots, timezone=now.tzinfo, now=now)
        save_codex_rollout_ledger(ledger_path, ledger)
    backup = backup_codex_history(state_dir, store, now=now)
    replacement = store.replace_source_history_once(
        "codex", migration_key=CODEX_JSONL_HISTORY_MIGRATION,
    )
    return {
        "status": "migrated" if replacement.get("status") == "replaced" else "current",
        "ledger_days": len(ledger.days),
        "ledger_tokens": ledger.cumulative().total_tokens,
        "ledger_files": len(ledger.files),
        "backup": backup,
        "store": replacement,
    }
