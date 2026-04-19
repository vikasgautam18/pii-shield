"""SQLite-backed audit log for application lifecycle events.

Records when applications are registered, modified, or deleted.
DB location is configurable via the AUDIT_DB_PATH env var
(default: data/audit.db).
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DB_PATH = os.getenv("AUDIT_DB_PATH", "data/audit.db")

# Valid action types
ACTIONS = (
    "APP_REGISTERED",
    "APP_DELETED",
    "CONFIG_UPDATED",
    "ALLOW_LIST_UPDATED",
    "ENTITY_TYPE_ALLOW_LIST_UPDATED",
)


def _ensure_db_dir() -> None:
    Path(AUDIT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection (creates file + table on first call)."""
    _ensure_db_dir()
    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id          TEXT PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            action      TEXT NOT NULL,
            app_id      TEXT NOT NULL,
            app_name    TEXT NOT NULL DEFAULT '',
            details     TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_app_id ON audit_log(app_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"
    )
    conn.commit()
    return conn


def record_event(
    action: str,
    app_id: str,
    app_name: str = "",
    details: str | dict = "",
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Insert an audit record and return it as a dict."""
    close_after = conn is None
    conn = conn or get_connection()
    try:
        record = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "app_id": app_id,
            "app_name": app_name,
            "details": json.dumps(details) if isinstance(details, dict) else str(details),
        }
        conn.execute(
            """
            INSERT INTO audit_log (id, timestamp, action, app_id, app_name, details)
            VALUES (:id, :timestamp, :action, :app_id, :app_name, :details)
            """,
            record,
        )
        conn.commit()
        return record
    finally:
        if close_after:
            conn.close()


def get_audit_log(
    app_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Query audit records with optional filters. Returns newest first."""
    close_after = conn is None
    conn = conn or get_connection()
    try:
        clauses: list[str] = []
        params: list = []
        if app_id:
            clauses.append("app_id = ?")
            params.append(app_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close_after:
            conn.close()
