from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from status_page.models import CheckResult, ServiceResult, State


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS service_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    checked_at INTEGER NOT NULL,
                    checks_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_service_events_service_time
                ON service_events(service_id, checked_at)
                """
            )

    def insert_service_result(self, result: ServiceResult) -> None:
        checks_json = json.dumps(
            [
                {
                    "check_type": check.check_type,
                    "state": check.state.value,
                    "message": check.message,
                    "duration_ms": check.duration_ms,
                    "detail": check.detail,
                }
                for check in result.checks
            ]
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO service_events(service_id, service_name, state, summary, checked_at, checks_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.service_id,
                    result.name,
                    result.state.value,
                    result.summary,
                    result.checked_at,
                    checks_json,
                ),
            )

    def prune_old(self, retention_hours: int) -> None:
        threshold = int(time.time()) - (retention_hours * 3600)
        with self._connect() as conn:
            conn.execute("DELETE FROM service_events WHERE checked_at < ?", (threshold,))

    def latest_for_services(self) -> dict[str, dict[str, Any]]:
        query = """
            SELECT se1.*
            FROM service_events se1
            JOIN (
                SELECT service_id, MAX(checked_at) AS max_checked_at
                FROM service_events
                GROUP BY service_id
            ) se2
            ON se1.service_id = se2.service_id
            AND se1.checked_at = se2.max_checked_at
        """
        latest: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for row in conn.execute(query):
                latest[row["service_id"]] = {
                    "id": row["service_id"],
                    "name": row["service_name"],
                    "state": row["state"],
                    "summary": row["summary"],
                    "checked_at": row["checked_at"],
                    "checks": json.loads(row["checks_json"]),
                }
        return latest

    def history(self, service_id: str, since_ts: int) -> list[tuple[int, State]]:
        query = """
            SELECT checked_at, state
            FROM service_events
            WHERE service_id = ? AND checked_at >= ?
            ORDER BY checked_at ASC
        """
        rows: list[tuple[int, State]] = []
        with self._connect() as conn:
            for row in conn.execute(query, (service_id, since_ts)):
                rows.append((int(row["checked_at"]), State(row["state"])))
        return rows


def summarize_checks(checks: list[CheckResult]) -> str:
    if not checks:
        return "No checks configured"

    bad = [c for c in checks if c.state in {State.RED, State.YELLOW}]
    if bad:
        return "; ".join(f"{c.check_type}: {c.message}" for c in bad[:2])
    return "All checks passing"
