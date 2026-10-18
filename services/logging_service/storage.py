"""SQLite persistence for sanitized SENTINEL observability events."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Callable, Optional

from schemas.observability import ObservabilityEvent


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    severity TEXT NOT NULL,
    source_layer TEXT NOT NULL,
    source_component TEXT NOT NULL,
    outcome TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    runtime_id TEXT,
    session_id TEXT,
    operation_id TEXT,
    ticket_id TEXT,
    generation_before INTEGER,
    generation_after INTEGER,
    duration_ms REAL,
    payload_json TEXT NOT NULL
)
"""

INSERT_SQL = """
INSERT INTO events (
    event_id, event_type, timestamp_utc, severity, source_layer, source_component,
    outcome, correlation_id, runtime_id, session_id, operation_id, ticket_id,
    generation_before, generation_after, duration_ms, payload_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(event_id) DO NOTHING
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SQLiteEventStorage:
    """Thread-safe SQLite store with idempotent event insertion."""

    def __init__(
        self,
        path: str,
        retention_days: int = 7,
        now_factory: Callable[[], datetime] = _utc_now,
    ) -> None:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 0:
            raise ValueError("retention_days must be a non-negative integer")
        self.path = path
        self.retention_days = retention_days
        self._now_factory = now_factory
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = Lock()

    def open(self) -> None:
        """Open the database and create its schema without deleting existing rows."""
        with self._lock:
            if self._connection is not None:
                return
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.execute(SCHEMA_SQL)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def is_healthy(self) -> bool:
        try:
            with self._lock:
                connection = self._require_connection()
                quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
                if not quick_check or quick_check[0] != "ok":
                    return False
                schema = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
                ).fetchone()
                return schema is not None
        except sqlite3.Error:
            return False

    def persist_events(self, events: list[ObservabilityEvent]) -> tuple[int, int]:
        """Insert events atomically, returning ``(accepted, duplicates)``."""
        with self._lock:
            connection = self._require_connection()
            accepted = 0
            duplicates = 0
            with connection:
                for event in events:
                    payload = event.to_dict()
                    cursor = connection.execute(
                        INSERT_SQL,
                        (
                            event.event_id,
                            event.event_type.value,
                            event.timestamp_utc,
                            event.severity.value,
                            event.source_layer.value,
                            event.source_component,
                            event.outcome.value,
                            event.correlation_id,
                            event.runtime_id,
                            event.session_id,
                            event.operation_id,
                            event.ticket_id,
                            event.generation_before,
                            event.generation_after,
                            event.duration_ms,
                            json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
                        ),
                    )
                    if cursor.rowcount == 1:
                        accepted += 1
                    else:
                        duplicates += 1
            return accepted, duplicates

    def purge_expired(self, now: Optional[datetime] = None) -> int:
        """Delete only rows older than the configured retention window."""
        current = now or self._now_factory()
        if current.tzinfo is None:
            raise ValueError("retention time must be timezone-aware")
        cutoff = current.astimezone(timezone.utc) - timedelta(days=self.retention_days)
        with self._lock:
            connection = self._require_connection()
            with connection:
                cursor = connection.execute(
                    "DELETE FROM events WHERE timestamp_utc < ?",
                    (cutoff.isoformat().replace("+00:00", "Z"),),
                )
            return cursor.rowcount

    def count(self) -> int:
        with self._lock:
            connection = self._require_connection()
            row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
            return int(row[0])

    def get_payload(self, event_id: str) -> Optional[dict[str, object]]:
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT payload_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise sqlite3.ProgrammingError("storage is not open")
        return self._connection
