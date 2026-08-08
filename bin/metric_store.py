"""SQLite-backed canonical metric storage and one-time network JSONL import."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from infra_model import MetricPoint
from network_metrics import local_sample_metrics, vps_sample_metrics, xray_sample_metrics


STORE_SCHEMA = "20260808.2"
STORE_FILENAME = "infra.sqlite3"
LEGACY_NETWORK_IMPORT = "legacy-network-jsonl-20260808.2"


class MetricStore:
    """Owns SQLite setup, idempotent metric writes, and bounded legacy import."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / STORE_FILENAME
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metric_points (
                    identity TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    observed_epoch REAL NOT NULL,
                    metric TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL,
                    attribution_method TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    estimated INTEGER NOT NULL CHECK (estimated IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS metric_points_time ON metric_points(observed_epoch);
                CREATE INDEX IF NOT EXISTS metric_points_metric_source ON metric_points(metric, source_id, observed_epoch);
            """)
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))", (STORE_SCHEMA,))
        self._initialized = True

    @staticmethod
    def _epoch(point: MetricPoint) -> float:
        if point.observed_epoch is not None:
            return float(point.observed_epoch)
        return datetime.fromisoformat(point.observed_at).timestamp()

    @staticmethod
    def _canonical_dimensions(point: MetricPoint) -> str:
        return json.dumps(point.dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _identity(cls, point: MetricPoint) -> str:
        payload = {
            "observed_at": point.observed_at,
            "metric": point.metric,
            "instrument": point.instrument,
            "unit": point.unit,
            "source_id": point.source_id,
            "resource_id": point.resource_id,
            "dimensions": point.dimensions,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _row(cls, point: MetricPoint) -> tuple[Any, ...]:
        return (
            cls._identity(point), point.observed_at, cls._epoch(point), point.metric, point.instrument,
            float(point.value), point.unit, point.source_id, point.resource_id, cls._canonical_dimensions(point),
            point.attribution_method, point.confidence, int(point.estimated),
        )

    def write(self, points: Iterable[MetricPoint]) -> int:
        rows = [self._row(point) for point in points]
        if not rows:
            return 0
        self.initialize()
        with self._transaction() as connection:
            before = connection.total_changes
            connection.executemany("""
                INSERT OR IGNORE INTO metric_points(
                    identity, observed_at, observed_epoch, metric, instrument, value, unit,
                    source_id, resource_id, dimensions_json, attribution_method, confidence, estimated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            return connection.total_changes - before

    @staticmethod
    def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            return
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record

    def _legacy_import_complete(self) -> bool:
        self.initialize()
        with self._transaction() as connection:
            return connection.execute("SELECT 1 FROM store_metadata WHERE key = ?", (LEGACY_NETWORK_IMPORT,)).fetchone() is not None

    def import_legacy_network(self) -> int:
        """Import prior diagnostics once; retry safely if interrupted before commit."""
        if self._legacy_import_complete():
            return 0
        points: list[MetricPoint] = []
        for path in sorted(self.state_dir.glob("samples*.jsonl")):
            for sample in self._iter_jsonl(path):
                if "kernel" in sample and "timestamp" in sample and "epoch" in sample:
                    points.extend(local_sample_metrics(sample))
        remote_root = self.state_dir / "remote"
        for remote_dir in remote_root.iterdir() if remote_root.is_dir() else ():
            if not remote_dir.is_dir():
                continue
            for path in sorted(remote_dir.glob("vps_samples*.jsonl")):
                for sample in self._iter_jsonl(path):
                    points.extend(vps_sample_metrics(remote_dir.name, sample))
            for path in sorted(remote_dir.glob("xray_user_samples*.jsonl")):
                for sample in self._iter_jsonl(path):
                    points.extend(xray_sample_metrics(remote_dir.name, sample))
        self.initialize()
        with self._transaction() as connection:
            before = connection.total_changes
            connection.executemany("""
                INSERT OR IGNORE INTO metric_points(
                    identity, observed_at, observed_epoch, metric, instrument, value, unit,
                    source_id, resource_id, dimensions_json, attribution_method, confidence, estimated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [self._row(point) for point in points])
            inserted = connection.total_changes - before
            connection.execute("INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
                               (LEGACY_NETWORK_IMPORT, json.dumps({"inserted": inserted, "schema": STORE_SCHEMA})))
            return inserted

    def summary(self) -> dict[str, Any]:
        self.initialize()
        with self._transaction() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
            imported = connection.execute("SELECT value FROM store_metadata WHERE key = ?", (LEGACY_NETWORK_IMPORT,)).fetchone()
        return {
            "schema": STORE_SCHEMA,
            "kind": "sqlite",
            "status": "ok",
            "metric_points": count,
            "legacy_import_complete": imported is not None,
        }
