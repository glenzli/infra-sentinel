"""Single-writer diagnostic journal and bounded, read-only history queries.

Only ``observe_projection`` creates or writes a database. Queries open an
existing file with SQLite's read-only mode, so a CLI inspection cannot create
state or initialize any resource. Connections are short-lived and confined to
one operation; command-service reads may run concurrently with sampling.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any

from infra_sentinel.core.diagnostics import (
    CATEGORIES, CODE_TEXT, DIAGNOSTIC_SCHEMA, FRESHNESS_SECONDS,
    KNOWN_SOURCE_LABELS, OBSERVATION_STATUSES, SAFE_SOURCE_LABELS, SEVERITIES,
    iso_at, normalize_diagnostics, valid_epoch,
)


RETENTION_DAYS = 30
RETENTION_SECONDS = RETENTION_DAYS * 86400
MAX_EVENTS = 10000
MAX_ACTIVE = 512
MAX_CHECKS = 1024
DEFAULT_EVENT_LIMIT = 50
MAX_QUERY_LIMIT = 200
TRANSITIONS = frozenset({"raised", "changed", "recovered"})
QUERY_FIELDS = frozenset({"category", "source_id", "severity", "transition", "since_epoch", "until_epoch", "limit"})
_OPAQUE_SOURCE_RE = re.compile(r"(?:source|vps|xray|facility|upstream):[0-9a-f]{24}\Z")
_DIAGNOSTIC_ID_RE = re.compile(r"diagnostic:[0-9a-f]{32}\Z")


def _source_id_valid(value: Any) -> bool:
    return isinstance(value, str) and (value in KNOWN_SOURCE_LABELS or _OPAQUE_SOURCE_RE.fullmatch(value) is not None)


def _now(value: Any = None) -> float:
    if value is None:
        return time.time()
    epoch = valid_epoch(value)
    if epoch is None:
        raise ValueError("invalid now_epoch")
    return epoch


def _query(payload: Any, now: float) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - QUERY_FIELDS:
        raise ValueError("diagnostics query contains unsupported fields")
    result: dict[str, Any] = {}
    for key, allowed in (("category", CATEGORIES), ("severity", SEVERITIES), ("transition", TRANSITIONS)):
        if key in payload:
            value = payload[key]
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(f"invalid diagnostics {key}")
            result[key] = value
    if "source_id" in payload:
        if not _source_id_valid(payload["source_id"]):
            raise ValueError("invalid diagnostics source_id")
        result["source_id"] = payload["source_id"]
    limit = payload.get("limit", DEFAULT_EVENT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError("diagnostics limit must be between 1 and 200")
    result["limit"] = limit
    for key in ("since_epoch", "until_epoch"):
        if key in payload:
            epoch = valid_epoch(payload[key])
            if epoch is None:
                raise ValueError(f"invalid diagnostics {key}")
            result[key] = epoch
    until = result.get("until_epoch", now)
    since = result.get("since_epoch", max(0, until - RETENTION_SECONDS))
    if until < since or until - since > RETENTION_SECONDS:
        raise ValueError("diagnostics time range must be ordered and at most 30 days")
    result["since_epoch"], result["until_epoch"] = since, until
    return result


def _empty(now: float, error_code: str) -> dict[str, Any]:
    return {
        "schema": DIAGNOSTIC_SCHEMA, "available": False,
        "observed_at": None, "generated_at": iso_at(now),
        "history_started_at": None, "retention_days": RETENTION_DAYS,
        "active_count": 0, "active": [], "events": [], "coverage": [],
        "freshness": "unknown", "truncated": False,
        "history_truncated": False, "sample_truncated": False,
        "error_code": error_code,
    }


def _metadata_epoch(metadata: Mapping[str, str], key: str) -> float | None:
    try:
        value = float(metadata[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return valid_epoch(value)


def _freshness(observed_epoch: float | None, now: float, seconds: int = FRESHNESS_SECONDS) -> str:
    if observed_epoch is None:
        return "unknown"
    return "current" if -FRESHNESS_SECONDS <= now - observed_epoch <= seconds else "stale"


def _record(row: sqlite3.Row) -> dict[str, Any]:
    code = row["code"]
    if (
        code not in CODE_TEXT or row["category"] not in CATEGORIES
        or not _source_id_valid(row["source_id"])
        or not _DIAGNOSTIC_ID_RE.fullmatch(row["diagnostic_id"])
        or row["severity"] not in SEVERITIES
    ):
        raise sqlite3.DataError("invalid diagnostic journal row")
    title, title_zh, detail, detail_zh = CODE_TEXT[code]
    label = row["source_label"] if row["source_label"] in SAFE_SOURCE_LABELS else "Observed source"
    return {
        "id": row["diagnostic_id"], "category": row["category"],
        "source_id": row["source_id"], "source_label": label,
        "code": code, "severity": row["severity"],
        "observed_at": iso_at(float(row["observed_epoch"])),
        "started_at": iso_at(float(row["started_epoch"])),
        "title": title, "title_zh": title_zh, "detail": detail, "detail_zh": detail_zh,
    }


class DiagnosticStore:
    """Persist existing confirmed conditions; errors stay local to this owner.

    ``path`` names the database file, normally state_dir/diagnostics.sqlite3.
    No explicit close is needed. ``query`` rejects invalid requests with
    ValueError, while storage faults return fixed unavailable metadata.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = threading.Lock()

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=0.25, isolation_level=None)
        else:
            # Opening a WAL database in mode=ro can still create shared-memory
            # sidecars. This journal uses rollback mode so a read is also
            # read-only at the directory boundary. Refuse unexpected WAL mode
            # before SQLite opens it; never ignore a live WAL with immutable.
            with self.path.open("rb") as source:
                header = source.read(20)
            if header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x02\x02":
                raise sqlite3.DatabaseError("unsupported diagnostic journal mode")
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 250")
            if write:
                connection.execute("PRAGMA journal_mode = DELETE")
            else:
                connection.execute("PRAGMA query_only = ON")
            return connection
        except sqlite3.Error:
            connection.close()
            raise

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1}:
            raise sqlite3.DatabaseError("unsupported diagnostic store version")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS diagnostic_metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        row = connection.execute("SELECT value FROM diagnostic_metadata WHERE key = 'schema'").fetchone()
        if row is not None and row[0] != DIAGNOSTIC_SCHEMA:
            raise sqlite3.DatabaseError("unsupported diagnostic schema")
        connection.execute("INSERT OR IGNORE INTO diagnostic_metadata VALUES ('schema', ?)", (DIAGNOSTIC_SCHEMA,))
        connection.execute("""
            CREATE TABLE IF NOT EXISTS diagnostic_checks (
                diagnostic_id TEXT PRIMARY KEY,
                category TEXT NOT NULL, source_id TEXT NOT NULL, source_label TEXT NOT NULL,
                code TEXT NOT NULL, observation_status TEXT NOT NULL,
                observed_epoch REAL, last_seen_epoch REAL NOT NULL,
                freshness_seconds INTEGER NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS diagnostic_active (
                diagnostic_id TEXT PRIMARY KEY,
                category TEXT NOT NULL, source_id TEXT NOT NULL, source_label TEXT NOT NULL,
                code TEXT NOT NULL, severity TEXT NOT NULL,
                observed_epoch REAL NOT NULL, started_epoch REAL NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS diagnostic_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                diagnostic_id TEXT NOT NULL,
                category TEXT NOT NULL, source_id TEXT NOT NULL, source_label TEXT NOT NULL,
                code TEXT NOT NULL, severity TEXT NOT NULL, transition TEXT NOT NULL,
                observed_epoch REAL NOT NULL, started_epoch REAL NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS diagnostic_events_time ON diagnostic_events(observed_epoch DESC, event_id DESC)")
        connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _event(connection: sqlite3.Connection, row: dict[str, Any], transition: str, now: float) -> None:
        connection.execute("""
            INSERT INTO diagnostic_events (
                diagnostic_id, category, source_id, source_label, code, severity,
                transition, observed_epoch, started_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["diagnostic_id"], row["category"], row["source_id"], row["source_label"],
            row["code"], row["severity"], transition, now, row["started_epoch"],
        ))

    @staticmethod
    def _retain(connection: sqlite3.Connection, now: float) -> None:
        deleted_events = connection.execute("DELETE FROM diagnostic_events WHERE observed_epoch < ?", (now - RETENTION_SECONDS,)).rowcount
        deleted_events += connection.execute("""
            DELETE FROM diagnostic_events WHERE event_id IN (
                SELECT event_id FROM diagnostic_events
                ORDER BY observed_epoch DESC, event_id DESC LIMIT -1 OFFSET ?
            )
        """, (MAX_EVENTS,)).rowcount
        # Unresolved observations survive age-based history retention. A
        # separate hard cap bounds source churn without inventing recoveries.
        deleted_active = connection.execute("""
            DELETE FROM diagnostic_active WHERE diagnostic_id IN (
                SELECT diagnostic_id FROM diagnostic_active
                ORDER BY observed_epoch DESC, diagnostic_id LIMIT -1 OFFSET ?
            )
        """, (MAX_ACTIVE,)).rowcount
        connection.execute("""
            DELETE FROM diagnostic_checks WHERE last_seen_epoch < ?
            AND diagnostic_id NOT IN (SELECT diagnostic_id FROM diagnostic_active)
        """, (now - RETENTION_SECONDS,))
        connection.execute("""
            DELETE FROM diagnostic_checks WHERE diagnostic_id IN (
                SELECT diagnostic_id FROM diagnostic_checks
                WHERE diagnostic_id NOT IN (SELECT diagnostic_id FROM diagnostic_active)
                ORDER BY last_seen_epoch DESC, diagnostic_id LIMIT -1 OFFSET ?
            )
        """, (max(0, MAX_CHECKS - int(connection.execute("SELECT COUNT(*) FROM diagnostic_active").fetchone()[0])),))
        if deleted_events or deleted_active:
            connection.execute("INSERT OR REPLACE INTO diagnostic_metadata VALUES ('history_truncated', '1')")

    def observe_projection(self, projection: dict[str, Any], now_epoch: float | None = None) -> dict[str, Any]:
        """Atomically retain one sample's active state and actual transitions.

        Explicit now_epoch wins, then the full Projection's epoch. Replayed
        older observations are ignored, so an old checkpoint cannot recover
        a newer condition or make the journal look freshly sampled.
        """
        if not isinstance(projection, dict):
            raise ValueError("projection must be an object")
        now = _now(now_epoch if now_epoch is not None else valid_epoch(projection.get("epoch")))
        observation = normalize_diagnostics(projection, now)
        try:
            with self._write_lock:
                connection = self._connect(write=True)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._initialize(connection)
                    metadata = dict(connection.execute("SELECT key, value FROM diagnostic_metadata"))
                    previous = _metadata_epoch(metadata, "observed_epoch")
                    if previous is None or now >= previous:
                        connection.execute("INSERT OR IGNORE INTO diagnostic_metadata VALUES ('history_started_epoch', ?)", (str(now),))
                        connection.execute("INSERT OR REPLACE INTO diagnostic_metadata VALUES ('observed_epoch', ?)", (str(now),))
                        connection.execute("INSERT OR REPLACE INTO diagnostic_metadata VALUES ('sample_truncated', ?)", ("1" if observation.truncated else "0",))
                        connection.execute("UPDATE diagnostic_checks SET observation_status = 'unobserved'")
                        active = {row["diagnostic_id"]: dict(row) for row in connection.execute("SELECT * FROM diagnostic_active")}
                        for check in observation.checks:
                            prior = active.get(check.id)
                            observation_status = check.observation_status
                            if (
                                observation_status == "observed" and prior is not None
                                and check.observed_epoch is not None
                                and check.observed_epoch < prior["observed_epoch"]
                            ):
                                # Sampling time can advance while an async
                                # source replays older cached evidence. That
                                # evidence cannot change a newer condition.
                                observation_status = "unknown"
                            connection.execute("""
                                INSERT INTO diagnostic_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(diagnostic_id) DO UPDATE SET
                                    observation_status = excluded.observation_status,
                                    observed_epoch = COALESCE(excluded.observed_epoch, diagnostic_checks.observed_epoch),
                                    last_seen_epoch = excluded.last_seen_epoch,
                                    freshness_seconds = excluded.freshness_seconds
                            """, (
                                check.id, check.category, check.source_id, check.source_label, check.code,
                                observation_status, check.observed_epoch, now, check.freshness_seconds,
                            ))
                            if observation_status != "observed":
                                continue
                            if check.severity is None:
                                if prior is not None:
                                    self._event(connection, prior, "recovered", now)
                                    connection.execute("DELETE FROM diagnostic_active WHERE diagnostic_id = ?", (check.id,))
                                continue
                            row = {
                                "diagnostic_id": check.id, "category": check.category,
                                "source_id": check.source_id, "source_label": check.source_label,
                                "code": check.code, "severity": check.severity,
                                "observed_epoch": check.observed_epoch if check.observed_epoch is not None else now,
                                "started_epoch": prior["started_epoch"] if prior is not None else now,
                            }
                            if prior is None or prior["severity"] != check.severity:
                                self._event(connection, row, "raised" if prior is None else "changed", now)
                            connection.execute("""
                                INSERT INTO diagnostic_active VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(diagnostic_id) DO UPDATE SET
                                    severity = excluded.severity,
                                    observed_epoch = excluded.observed_epoch
                            """, tuple(row[key] for key in (
                                "diagnostic_id", "category", "source_id", "source_label", "code",
                                "severity", "observed_epoch", "started_epoch",
                            )))
                        self._retain(connection, now)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
        except (sqlite3.Error, OSError, OverflowError):
            return _empty(now, "storage_unavailable")
        return self.snapshot(now_epoch=now)

    def snapshot(self, *, now_epoch: float | None = None, event_limit: int = DEFAULT_EVENT_LIMIT) -> dict[str, Any]:
        return self.query({"limit": event_limit}, now_epoch=now_epoch)

    def query(self, payload: dict[str, Any], *, now_epoch: float | None = None) -> dict[str, Any]:
        """Read a bounded snapshot without creating or changing any files.

        Category, source, and severity filter active conditions and events;
        active_count is the filtered unresolved count. Inclusive time bounds
        and transition filter only events. Coverage follows category/source,
        retaining clear and unknown checks regardless of severity.
        """
        now = _now(now_epoch)
        query = _query(payload, now)
        try:
            if not self.path.is_file():
                return _empty(now, "history_unavailable")
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                metadata = dict(connection.execute("SELECT key, value FROM diagnostic_metadata"))
                if metadata.get("schema") != DIAGNOSTIC_SCHEMA:
                    raise sqlite3.DatabaseError("unsupported diagnostic schema")
                clauses: list[str] = []
                parameters: list[Any] = []
                for key in ("category", "source_id", "severity"):
                    if key in query:
                        clauses.append(f"{key} = ?")
                        parameters.append(query[key])
                active_where = " AND ".join(clauses) or "1"
                active_rows = connection.execute(f"""
                    SELECT * FROM diagnostic_active WHERE {active_where}
                    ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                             started_epoch DESC, diagnostic_id LIMIT ?
                """, (*parameters, MAX_ACTIVE)).fetchall()
                coverage_clauses, coverage_parameters = [], []
                for key in ("category", "source_id"):
                    if key in query:
                        coverage_clauses.append(f"{key} = ?")
                        coverage_parameters.append(query[key])
                coverage_where = " AND ".join(coverage_clauses) or "1"
                coverage_rows = connection.execute(f"""
                    SELECT * FROM diagnostic_checks WHERE {coverage_where}
                    ORDER BY category, source_id, code, diagnostic_id LIMIT ?
                """, (*coverage_parameters, MAX_CHECKS)).fetchall()
                event_clauses = [*clauses, "observed_epoch >= ?", "observed_epoch <= ?"]
                event_parameters = [*parameters, query["since_epoch"], query["until_epoch"]]
                if "transition" in query:
                    event_clauses.append("transition = ?")
                    event_parameters.append(query["transition"])
                events = connection.execute(f"""
                    SELECT * FROM diagnostic_events WHERE {' AND '.join(event_clauses)}
                    ORDER BY observed_epoch DESC, event_id DESC LIMIT ?
                """, (*event_parameters, query["limit"] + 1)).fetchall()
            finally:
                connection.close()
            coverage = []
            statuses = {}
            for row in coverage_rows:
                if (
                    row["code"] not in CODE_TEXT or row["category"] not in CATEGORIES
                    or not _source_id_valid(row["source_id"])
                    or row["observation_status"] not in OBSERVATION_STATUSES
                ):
                    raise sqlite3.DataError("invalid diagnostic coverage")
                observed = valid_epoch(row["observed_epoch"])
                freshness = _freshness(observed, now, int(row["freshness_seconds"]))
                status = row["observation_status"]
                if status == "observed" and freshness != "current":
                    status = "unknown"
                statuses[row["diagnostic_id"]] = status
                coverage.append({
                    "id": row["diagnostic_id"], "category": row["category"],
                    "source_id": row["source_id"],
                    "source_label": row["source_label"] if row["source_label"] in SAFE_SOURCE_LABELS else "Observed source",
                    "code": row["code"], "observation_status": status,
                    "observed_at": iso_at(observed) if observed is not None else None,
                    "freshness": freshness,
                })
            active = [{**_record(row), "observation_status": statuses.get(row["diagnostic_id"], "unobserved")} for row in active_rows]
            event_rows = []
            for row in events[:query["limit"]]:
                if row["transition"] not in TRANSITIONS:
                    raise sqlite3.DataError("invalid diagnostic transition")
                record = _record(row)
                record.update({
                    "id": f"event:{int(row['event_id'])}", "diagnostic_id": row["diagnostic_id"],
                    "transition": row["transition"], "observation_status": "observed",
                })
                event_rows.append(record)
            observed_epoch = _metadata_epoch(metadata, "observed_epoch")
            started_epoch = _metadata_epoch(metadata, "history_started_epoch")
            history_truncated = metadata.get("history_truncated") == "1"
            return {
                "schema": DIAGNOSTIC_SCHEMA, "available": True,
                "observed_at": iso_at(observed_epoch) if observed_epoch is not None else None,
                "generated_at": iso_at(now),
                "history_started_at": iso_at(started_epoch) if started_epoch is not None else None,
                "retention_days": RETENTION_DAYS, "active_count": len(active),
                "active": active, "events": event_rows, "coverage": coverage,
                "freshness": _freshness(observed_epoch, now),
                "truncated": len(events) > query["limit"],
                "history_truncated": history_truncated,
                "sample_truncated": metadata.get("sample_truncated") == "1",
            }
        except (sqlite3.Error, OSError, OverflowError, TypeError, ValueError):
            return _empty(now, "storage_unavailable")
