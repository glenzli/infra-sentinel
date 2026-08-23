"""SQLite-backed canonical metric storage and one-time network JSONL import."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from infra_sentinel.core.model import MetricPoint
from infra_sentinel.metrics.aggregation import MetricAccumulator, MetricBucket, bucket_start
from infra_sentinel.resources.network.metrics import local_sample_metrics, vps_sample_metrics, xray_sample_metrics


STORE_SCHEMA = "20260812.3"
STORE_FILENAME = "infra.sqlite3"
LEGACY_NETWORK_IMPORT = "legacy-network-jsonl-20260808.2"
TIERED_METRIC_MIGRATION = "20260812.1"
REMOTE_HISTORY_REBUILD = "remote-network-history-rebuild-20260812.3"
CODEX_JSONL_HISTORY_MIGRATION = "codex-jsonl-history-20260824.2"
MAINTENANCE_EPOCH = "metric-maintenance-20260812.1"
HOT_RESOLUTION_SECONDS = 15 * 60
HOURLY_RESOLUTION_SECONDS = 60 * 60
DAILY_RESOLUTION_SECONDS = 24 * 60 * 60
HOT_RETENTION_SECONDS = 7 * DAILY_RESOLUTION_SECONDS
HOURLY_RETENTION_SECONDS = 90 * DAILY_RESOLUTION_SECONDS
MAINTENANCE_INTERVAL_SECONDS = DAILY_RESOLUTION_SECONDS


class MetricStore:
    """Owns SQLite setup, idempotent metric writes, and bounded legacy import."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / STORE_FILENAME
        self._initialized = False
        self._point_count = 0
        self._legacy_imported = False
        self._remote_history_rebuilt = False
        self._lock = threading.RLock()

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        else:
            connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterable[sqlite3.Connection]:
        connection = self._connect(write=write)
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
        with self._lock:
            if self._initialized:
                return
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self._transaction(write=True) as connection:
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
                    estimated INTEGER NOT NULL CHECK (estimated IN (0, 1)),
                    sample_count INTEGER NOT NULL DEFAULT 1,
                    minimum_value REAL,
                    maximum_value REAL,
                    last_value REAL,
                    last_epoch REAL,
                    resolution_seconds INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS metric_points_time ON metric_points(observed_epoch);
                CREATE INDEX IF NOT EXISTS metric_points_metric_source ON metric_points(metric, source_id, observed_epoch);
            """)
                columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(metric_points)")}
                additions = {
                    "sample_count": "INTEGER NOT NULL DEFAULT 1",
                    "minimum_value": "REAL",
                    "maximum_value": "REAL",
                    "last_value": "REAL",
                    "last_epoch": "REAL",
                    "resolution_seconds": "INTEGER NOT NULL DEFAULT 0",
                }
                for name, declaration in additions.items():
                    if name not in columns:
                        connection.execute(f"ALTER TABLE metric_points ADD COLUMN {name} {declaration}")
                connection.executescript("""
                    CREATE INDEX IF NOT EXISTS metric_points_resource_instrument_time
                    ON metric_points(resource_id, instrument, observed_epoch);
                    CREATE INDEX IF NOT EXISTS metric_points_resolution_time
                    ON metric_points(resolution_seconds, observed_epoch);
                """)
                connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))", (STORE_SCHEMA,))
                self._point_count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
                self._legacy_imported = connection.execute(
                    "SELECT 1 FROM store_metadata WHERE key = ?", (LEGACY_NETWORK_IMPORT,)
                ).fetchone() is not None
                self._remote_history_rebuilt = connection.execute(
                    "SELECT 1 FROM store_metadata WHERE key = ?", (REMOTE_HISTORY_REBUILD,)
                ).fetchone() is not None
            self._initialized = True

    @staticmethod
    def _epoch(point: MetricPoint) -> float:
        if point.observed_epoch is not None:
            return float(point.observed_epoch)
        return datetime.fromisoformat(point.observed_at).timestamp()

    @staticmethod
    def _canonical_dimensions(point: MetricPoint | MetricBucket) -> str:
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
        value = float(point.value)
        return (
            cls._identity(point), point.observed_at, cls._epoch(point), point.metric, point.instrument,
            value, point.unit, point.source_id, point.resource_id, cls._canonical_dimensions(point),
            point.attribution_method, point.confidence, int(point.estimated), 1, value, value, value,
            cls._epoch(point), 0,
        )

    @classmethod
    def _bucket_identity(cls, bucket: MetricBucket) -> str:
        payload = {
            "observed_at": bucket.observed_at,
            "metric": bucket.metric,
            "instrument": bucket.instrument,
            "unit": bucket.unit,
            "source_id": bucket.source_id,
            "resource_id": bucket.resource_id,
            "dimensions": bucket.dimensions,
            "resolution_seconds": bucket.resolution_seconds,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _bucket_row(cls, bucket: MetricBucket) -> tuple[Any, ...]:
        return (
            cls._bucket_identity(bucket), bucket.observed_at, bucket.observed_epoch,
            bucket.metric, bucket.instrument, bucket.value, bucket.unit, bucket.source_id,
            bucket.resource_id, cls._canonical_dimensions(bucket), bucket.attribution_method,
            bucket.confidence, int(bucket.estimated), bucket.sample_count, bucket.minimum_value,
            bucket.maximum_value, bucket.last_value, bucket.last_epoch, bucket.resolution_seconds,
        )

    @staticmethod
    def _insert_columns() -> str:
        return """
            identity, observed_at, observed_epoch, metric, instrument, value, unit,
            source_id, resource_id, dimensions_json, attribution_method, confidence, estimated,
            sample_count, minimum_value, maximum_value, last_value, last_epoch, resolution_seconds
        """

    @classmethod
    def _write_buckets_on(cls, connection: sqlite3.Connection, buckets: Iterable[MetricBucket]) -> int:
        rows = [cls._bucket_row(bucket) for bucket in buckets]
        if not rows:
            return 0
        before = connection.total_changes
        connection.executemany(f"""
            INSERT INTO metric_points({cls._insert_columns()})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity) DO UPDATE SET
                value = CASE
                    WHEN metric_points.instrument = 'counter' THEN metric_points.value + excluded.value
                    WHEN metric_points.instrument = 'gauge' THEN
                        ((metric_points.value * metric_points.sample_count) + (excluded.value * excluded.sample_count))
                        / MAX(1, metric_points.sample_count + excluded.sample_count)
                    ELSE excluded.value
                END,
                sample_count = metric_points.sample_count + excluded.sample_count,
                minimum_value = MIN(COALESCE(metric_points.minimum_value, metric_points.value), excluded.minimum_value),
                maximum_value = MAX(COALESCE(metric_points.maximum_value, metric_points.value), excluded.maximum_value),
                last_value = CASE WHEN excluded.last_epoch >= COALESCE(metric_points.last_epoch, metric_points.observed_epoch)
                                  THEN excluded.last_value ELSE COALESCE(metric_points.last_value, metric_points.value) END,
                last_epoch = MAX(COALESCE(metric_points.last_epoch, metric_points.observed_epoch), excluded.last_epoch),
                resolution_seconds = MAX(metric_points.resolution_seconds, excluded.resolution_seconds),
                estimated = MAX(metric_points.estimated, excluded.estimated)
        """, rows)
        return connection.total_changes - before

    def write(self, points: Iterable[MetricPoint]) -> int:
        rows = [self._row(point) for point in points]
        if not rows:
            return 0
        self.initialize()
        with self._lock, self._transaction(write=True) as connection:
            before = connection.total_changes
            connection.executemany(f"""
                INSERT OR IGNORE INTO metric_points({self._insert_columns()})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            inserted = connection.total_changes - before
            self._point_count += inserted
            return inserted

    def write_buckets(self, buckets: Iterable[MetricBucket]) -> int:
        """Durably merge completed in-memory buckets in one transaction."""
        materialized = tuple(buckets)
        if not materialized:
            return 0
        self.initialize()
        with self._lock, self._transaction(write=True) as connection:
            changed = self._write_buckets_on(connection, materialized)
            self._point_count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
            return changed

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
        return self._legacy_imported

    def _remote_network_points(self) -> list[MetricPoint]:
        """Read exact remote interval logs, including the pre-fleet legacy path."""
        points: list[MetricPoint] = []
        for path in sorted(self.state_dir.glob("vps_samples*.jsonl")):
            for sample in self._iter_jsonl(path):
                points.extend(vps_sample_metrics("legacy", sample))
        for path in sorted(self.state_dir.glob("xray_user_samples*.jsonl")):
            for sample in self._iter_jsonl(path):
                points.extend(xray_sample_metrics("legacy", sample))
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
        return points

    def import_legacy_network(self) -> int:
        """Import prior diagnostics once; retry safely if interrupted before commit."""
        if self._legacy_import_complete():
            return 0
        points: list[MetricPoint] = []
        for path in sorted(self.state_dir.glob("samples*.jsonl")):
            for sample in self._iter_jsonl(path):
                if "kernel" in sample and "timestamp" in sample and "epoch" in sample:
                    points.extend(local_sample_metrics(sample))
        points.extend(self._remote_network_points())
        self.initialize()
        with self._lock, self._transaction(write=True) as connection:
            before = connection.total_changes
            connection.executemany(f"""
                INSERT OR IGNORE INTO metric_points({self._insert_columns()})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [self._row(point) for point in points])
            inserted = connection.total_changes - before
            connection.execute("INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
                               (LEGACY_NETWORK_IMPORT, json.dumps({"inserted": inserted, "schema": STORE_SCHEMA})))
            self._point_count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
            self._legacy_imported = True
            return inserted

    @staticmethod
    def _migration_epoch(value: Any) -> float:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def rebuild_remote_history(self) -> dict[str, Any]:
        """Replace only the tiered-pipeline window with exact remote JSONL facts.

        The 20260812.1 pipeline could merge the same retained five-minute sample
        on every local poll.  The migration timestamp bounds that exposure; all
        older tiers remain untouched.  Replacement is one transaction and only
        proceeds when every affected source has a corresponding raw log.
        """
        self.initialize()
        if self._remote_history_rebuilt:
            return {"status": "current"}
        with self._lock, self._transaction() as connection:
            migration = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE version = ?",
                (TIERED_METRIC_MIGRATION,),
            ).fetchone()
        if migration is None:
            report: dict[str, Any] = {"status": "not-required", "schema": STORE_SCHEMA}
            with self._lock, self._transaction(write=True) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
                    (REMOTE_HISTORY_REBUILD, json.dumps(report, separators=(",", ":"))),
                )
            self._remote_history_rebuilt = True
            return report

        cutoff = bucket_start(self._migration_epoch(migration[0]), HOT_RESOLUTION_SECONDS)
        raw_points = tuple(point for point in self._remote_network_points() if self._epoch(point) >= cutoff)
        raw_groups = {(point.metric, point.source_id) for point in raw_points}
        with self._lock, self._transaction() as connection:
            affected_groups = {
                (str(row[0]), str(row[1]))
                for row in connection.execute("""
                    SELECT DISTINCT metric, source_id
                    FROM metric_points
                    WHERE observed_epoch >= ? AND (
                        (metric = 'network.billable_bytes' AND source_id LIKE 'vps:%') OR
                        (metric = 'network.logical_bytes' AND source_id LIKE 'xray:%')
                    )
                """, (cutoff,))
            }
        missing_groups = sorted(affected_groups - raw_groups)
        if missing_groups:
            return {
                "status": "blocked",
                "reason": "raw-remote-history-missing",
                "cutoff_epoch": cutoff,
                "missing": [{"metric": metric, "source_id": source_id} for metric, source_id in missing_groups],
            }

        with self._lock, self._transaction(write=True) as connection:
            deleted = connection.execute("""
                DELETE FROM metric_points
                WHERE observed_epoch >= ? AND (
                    (metric = 'network.billable_bytes' AND source_id LIKE 'vps:%') OR
                    (metric = 'network.logical_bytes' AND source_id LIKE 'xray:%')
                )
            """, (cutoff,)).rowcount
            before_insert = connection.total_changes
            connection.executemany(f"""
                INSERT OR IGNORE INTO metric_points({self._insert_columns()})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [self._row(point) for point in raw_points])
            inserted = connection.total_changes - before_insert
            report = {
                "status": "rebuilt",
                "schema": STORE_SCHEMA,
                "cutoff_epoch": cutoff,
                "deleted": max(0, int(deleted)),
                "inserted": inserted,
                "source_groups": len(raw_groups),
            }
            connection.execute(
                "INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
                (REMOTE_HISTORY_REBUILD, json.dumps(report, separators=(",", ":"))),
            )
            self._point_count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
        self._remote_history_rebuilt = True
        return report

    def metadata(self, key: str) -> dict[str, Any] | None:
        """Read one store-owned migration report without exposing SQL."""
        self.initialize()
        with self._lock, self._transaction() as connection:
            row = connection.execute("SELECT value FROM store_metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def replace_source_history_once(
        self,
        source_id: str,
        *,
        migration_key: str,
        points: Iterable[MetricPoint] = (),
    ) -> dict[str, Any]:
        """Atomically replace one source's metric projection once.

        Source facts are already backed up by the migration orchestrator. This
        store boundary only owns the scoped delete, optional replacement rows,
        and idempotent marker transaction.
        """
        if not source_id or not migration_key:
            raise ValueError("source_id and migration_key are required")
        materialized = tuple(points)
        if any(point.source_id != source_id for point in materialized):
            raise ValueError("replacement point source does not match")
        self.initialize()
        with self._lock, self._transaction(write=True) as connection:
            marker = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?", (migration_key,),
            ).fetchone()
            if marker is not None:
                try:
                    current = json.loads(str(marker[0]))
                except json.JSONDecodeError:
                    current = {}
                return {"status": "current", **(current if isinstance(current, dict) else {})}
            deleted = max(0, int(connection.execute(
                "DELETE FROM metric_points WHERE source_id = ?", (source_id,),
            ).rowcount))
            before_insert = connection.total_changes
            connection.executemany(f"""
                INSERT OR IGNORE INTO metric_points({self._insert_columns()})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [self._row(point) for point in materialized])
            inserted = connection.total_changes - before_insert
            report = {
                "source_id": source_id,
                "deleted": deleted,
                "inserted": inserted,
                "schema": STORE_SCHEMA,
            }
            connection.execute(
                "INSERT INTO store_metadata(key, value) VALUES (?, ?)",
                (migration_key, json.dumps(report, separators=(",", ":"))),
            )
            self._point_count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
        return {"status": "replaced", **report}

    def summary(self) -> dict[str, Any]:
        self.initialize()
        return {
            "schema": STORE_SCHEMA,
            "kind": "sqlite",
            "status": "ok",
            "metric_points": self._point_count,
            "legacy_import_complete": self._legacy_imported,
            "remote_history_rebuild_complete": self._remote_history_rebuilt,
        }

    @staticmethod
    def _bucket_from_row(row: sqlite3.Row) -> MetricBucket:
        try:
            dimensions = json.loads(row["dimensions_json"])
        except (TypeError, json.JSONDecodeError):
            dimensions = {}
        value = float(row["value"])
        samples = max(1, int(row["sample_count"] or 1))
        instrument = str(row["instrument"])
        return MetricBucket(
            observed_epoch=float(row["observed_epoch"]),
            resolution_seconds=int(row["resolution_seconds"] or 0),
            metric=str(row["metric"]),
            instrument=instrument,
            value_sum=value * samples if instrument == "gauge" else value,
            sample_count=samples,
            minimum_value=float(row["minimum_value"] if row["minimum_value"] is not None else value),
            maximum_value=float(row["maximum_value"] if row["maximum_value"] is not None else value),
            last_value=float(row["last_value"] if row["last_value"] is not None else value),
            last_epoch=float(row["last_epoch"] if row["last_epoch"] is not None else row["observed_epoch"]),
            unit=str(row["unit"]),
            source_id=str(row["source_id"]),
            resource_id=str(row["resource_id"]),
            dimensions=dimensions if isinstance(dimensions, dict) else {},
            attribution_method=str(row["attribution_method"]),
            confidence=str(row["confidence"]),
            estimated=bool(row["estimated"]),
        )

    def _compact_resolution(
        self,
        source_resolution: int,
        target_resolution: int,
        before_epoch: float,
        *,
        offset_seconds: int = 0,
        resource_id: str | None = None,
    ) -> dict[str, int]:
        resource_clause = " AND resource_id = ?" if resource_id is not None else ""
        resource_parameters: tuple[object, ...] = (resource_id,) if resource_id is not None else ()
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT MIN(observed_epoch) FROM metric_points WHERE resolution_seconds = ? AND observed_epoch < ?{resource_clause}",
                (source_resolution, before_epoch, *resource_parameters),
            ).fetchone()
        if row is None or row[0] is None:
            return {"source_points": 0, "target_points": 0}
        chunk_seconds = max(target_resolution, min(DAILY_RESOLUTION_SECONDS, target_resolution * 96))
        start = bucket_start(float(row[0]), target_resolution, offset_seconds)
        source_points = 0
        target_points = 0
        while start < before_epoch:
            end = min(before_epoch, start + chunk_seconds)
            with self._transaction(write=True) as connection:
                rows = connection.execute(f"""
                    SELECT observed_epoch, metric, instrument, value, unit, source_id, resource_id,
                           dimensions_json, attribution_method, confidence, estimated, sample_count,
                           minimum_value, maximum_value, last_value, last_epoch, resolution_seconds
                    FROM metric_points
                    WHERE resolution_seconds = ? AND observed_epoch >= ? AND observed_epoch < ?{resource_clause}
                    ORDER BY observed_epoch, metric, source_id, dimensions_json
                """, (source_resolution, start, end, *resource_parameters)).fetchall()
                if rows:
                    accumulator = MetricAccumulator(target_resolution, offset_seconds=offset_seconds)
                    for candidate in rows:
                        accumulator.add_bucket(self._bucket_from_row(candidate))
                    buckets = accumulator.buckets()
                    self._write_buckets_on(connection, buckets)
                    deleted = connection.execute(
                        f"DELETE FROM metric_points WHERE resolution_seconds = ? AND observed_epoch >= ? AND observed_epoch < ?{resource_clause}",
                        (source_resolution, start, end, *resource_parameters),
                    ).rowcount
                    source_points += max(0, int(deleted))
                    target_points += len(buckets)
            start = end
        return {"source_points": source_points, "target_points": target_points}

    def maintain_history(self, now_epoch: float | None = None, *, force: bool = False) -> dict[str, Any]:
        """Compact completed history into 15-minute, hourly, and daily tiers."""
        self.initialize()
        now = time.time() if now_epoch is None else float(now_epoch)
        with self._lock:
            with self._transaction() as connection:
                row = connection.execute("SELECT value FROM store_metadata WHERE key = ?", (MAINTENANCE_EPOCH,)).fetchone()
            previous = float(row[0]) if row is not None else 0.0
            current_day = datetime.fromtimestamp(now).astimezone().date()
            previous_day = datetime.fromtimestamp(previous).astimezone().date() if previous > 0 else None
            if not force and now - previous < MAINTENANCE_INTERVAL_SECONDS and previous_day == current_day:
                return {"status": "current", "last_epoch": previous}

            hot_cutoff = bucket_start(now, HOT_RESOLUTION_SECONDS)
            local_midnight = datetime.fromtimestamp(now).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            local_midnight_epoch = local_midnight.timestamp()
            ai_hourly_cutoff = bucket_start(local_midnight_epoch, HOURLY_RESOLUTION_SECONDS)
            hourly_cutoff = bucket_start(now - HOT_RETENTION_SECONDS, HOURLY_RESOLUTION_SECONDS)
            daily_offset = int(local_midnight.timestamp()) % DAILY_RESOLUTION_SECONDS
            ai_daily_cutoff = bucket_start(
                local_midnight_epoch,
                DAILY_RESOLUTION_SECONDS,
                daily_offset,
            )
            daily_cutoff = bucket_start(
                now - HOURLY_RETENTION_SECONDS,
                DAILY_RESOLUTION_SECONDS,
                daily_offset,
            )
            raw = self._compact_resolution(0, HOT_RESOLUTION_SECONDS, hot_cutoff)
            ai_hourly = self._compact_resolution(
                HOT_RESOLUTION_SECONDS,
                HOURLY_RESOLUTION_SECONDS,
                ai_hourly_cutoff,
                resource_id="ai_usage",
            )
            ai_daily = self._compact_resolution(
                HOURLY_RESOLUTION_SECONDS,
                DAILY_RESOLUTION_SECONDS,
                ai_daily_cutoff,
                offset_seconds=daily_offset,
                resource_id="ai_usage",
            )
            hourly = self._compact_resolution(HOT_RESOLUTION_SECONDS, HOURLY_RESOLUTION_SECONDS, hourly_cutoff)
            daily = self._compact_resolution(
                HOURLY_RESOLUTION_SECONDS,
                DAILY_RESOLUTION_SECONDS,
                daily_cutoff,
                offset_seconds=daily_offset,
            )
            hourly = {
                key: hourly[key] + ai_hourly[key]
                for key in ("source_points", "target_points")
            }
            daily = {
                key: daily[key] + ai_daily[key]
                for key in ("source_points", "target_points")
            }
            with self._transaction(write=True) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
                    (MAINTENANCE_EPOCH, str(now)),
                )
                self._point_count = int(connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0])
            return {
                "status": "compacted",
                "at_epoch": now,
                "raw_to_15m": raw,
                "15m_to_hour": hourly,
                "hour_to_day": daily,
                "metric_points": self._point_count,
            }

    def vacuum(self) -> dict[str, int]:
        """Reclaim pages after an explicit offline history migration."""
        self.initialize()
        before = self.path.stat().st_size if self.path.exists() else 0
        with self._lock:
            connection = self._connect(write=True)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            finally:
                connection.close()
        after = self.path.stat().st_size if self.path.exists() else 0
        return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after)}

    def query_points(
        self,
        *,
        since_epoch: float,
        until_epoch: float,
        resource_id: str | None = None,
        source_id: str | None = None,
        metric: str | None = None,
        instrument: str | None = None,
        bucket_seconds: int | None = None,
        bucket_offset_seconds: int = 0,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Read canonical points with parameterized filters and optional buckets.

        Query policy lives in ``metric_query``. This owner only executes a
        bounded, deterministic SQLite read over its own schema.
        """
        if until_epoch < since_epoch:
            raise ValueError("until_epoch must not precede since_epoch")
        if limit < 1:
            raise ValueError("limit must be positive")
        if bucket_seconds is not None and not 0 <= bucket_offset_seconds < bucket_seconds:
            raise ValueError("bucket_offset_seconds must be within the bucket")
        clauses = ["observed_epoch >= ?", "observed_epoch <= ?"]
        parameters: list[Any] = [float(since_epoch), float(until_epoch)]
        for column, value in (("resource_id", resource_id), ("source_id", source_id), ("metric", metric), ("instrument", instrument)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = " AND ".join(clauses)
        self.initialize()
        with self._transaction() as connection:
            if bucket_seconds is None:
                rows = connection.execute(f"""
                    SELECT observed_epoch, observed_at, metric, instrument, value, unit,
                           source_id, resource_id, dimensions_json, attribution_method,
                           confidence, estimated, sample_count,
                           COALESCE(minimum_value, value) AS minimum_value,
                           COALESCE(maximum_value, value) AS maximum_value,
                           COALESCE(last_value, value) AS last_value,
                           COALESCE(last_epoch, observed_epoch) AS last_epoch,
                           resolution_seconds
                    FROM metric_points
                    WHERE {where}
                    ORDER BY observed_epoch, metric, source_id, dimensions_json
                    LIMIT ?
                """, (*parameters, limit)).fetchall()
            else:
                aggregation = (
                    "SUM(value * sample_count) / MAX(1, SUM(sample_count))"
                    if instrument == "gauge" else "SUM(value)"
                )
                rows = connection.execute(f"""
                    SELECT CAST((observed_epoch - ?) / ? AS INTEGER) * ? + ? AS bucket_epoch,
                           metric, instrument, {aggregation} AS value, unit,
                           source_id, resource_id, dimensions_json, attribution_method,
                           confidence, MAX(estimated) AS estimated,
                           SUM(sample_count) AS sample_count,
                           MIN(COALESCE(minimum_value, value)) AS minimum_value,
                           MAX(COALESCE(maximum_value, value)) AS maximum_value,
                           MAX(COALESCE(last_value, value)) AS last_value,
                           MAX(COALESCE(last_epoch, observed_epoch)) AS last_epoch,
                           MAX(resolution_seconds) AS resolution_seconds
                    FROM metric_points
                    WHERE {where}
                    GROUP BY bucket_epoch, metric, instrument, unit, source_id, resource_id,
                             dimensions_json, attribution_method, confidence, estimated
                    ORDER BY bucket_epoch, metric, source_id, dimensions_json
                    LIMIT ?
                """, (
                    bucket_offset_seconds, bucket_seconds, bucket_seconds, bucket_offset_seconds,
                    *parameters, limit,
                )).fetchall()
        points: list[dict[str, Any]] = []
        for row in rows:
            try:
                dimensions = json.loads(row["dimensions_json"])
            except (TypeError, json.JSONDecodeError):
                dimensions = {}
            point = {
                "observed_epoch": float(row["observed_epoch"] if bucket_seconds is None else row["bucket_epoch"]),
                "metric": row["metric"],
                "instrument": row["instrument"],
                "value": row["value"],
                "unit": row["unit"],
                "source_id": row["source_id"],
                "resource_id": row["resource_id"],
                "dimensions": dimensions if isinstance(dimensions, dict) else {},
                "attribution_method": row["attribution_method"],
                "confidence": row["confidence"],
                "estimated": bool(row["estimated"]),
                "sample_count": int(row["sample_count"]),
                "minimum_value": row["minimum_value"],
                "maximum_value": row["maximum_value"],
                "last_value": row["last_value"],
                "last_epoch": row["last_epoch"],
                "resolution_seconds": int(row["resolution_seconds"]),
            }
            if bucket_seconds is None:
                point["observed_at"] = row["observed_at"]
            points.append(point)
        return points
