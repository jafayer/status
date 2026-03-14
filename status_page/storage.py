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
                CREATE TABLE IF NOT EXISTS check_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id  TEXT    NOT NULL,
                    service_name TEXT   NOT NULL,
                    check_index INTEGER NOT NULL,
                    check_type  TEXT    NOT NULL,
                    passed      INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    message     TEXT    NOT NULL,
                    checked_at  INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_check_events_service_time
                ON check_events(service_id, checked_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_check_events_service_check_time
                ON check_events(service_id, check_index, checked_at)
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Migrate from service_events (one row per probe) to check_events (one row per check)."""
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "service_events" not in tables:
            return
        cols = {row[1] for row in conn.execute("PRAGMA table_info(service_events)")}
        rows = conn.execute(
            "SELECT service_id, service_name, checked_at, checks_json FROM service_events ORDER BY checked_at"
        ).fetchall()
        migrated = []
        for row in rows:
            try:
                checks = json.loads(row["checks_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            for idx, check in enumerate(checks):
                if not check:
                    continue
                # Prefer original_state if written by old SLA-tainting code
                detail = check.get("detail") or {}
                raw_state = str(detail.get("original_state") or check.get("state") or State.RED.value)
                passed = 1 if raw_state == State.GREEN.value else 0
                migrated.append((
                    row["service_id"],
                    row["service_name"],
                    idx,
                    str(check.get("check_type") or "unknown"),
                    passed,
                    int(check.get("duration_ms") or 0),
                    str(check.get("message") or ""),
                    row["checked_at"],
                ))
        if migrated:
            conn.executemany(
                """
                INSERT OR IGNORE INTO check_events
                    (service_id, service_name, check_index, check_type, passed, duration_ms, message, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                migrated,
            )
        conn.execute("DROP TABLE service_events")

    def insert_service_result(self, result: ServiceResult) -> None:
        rows = [
            (
                result.service_id,
                result.name,
                idx,
                check.check_type,
                1 if check.state == State.GREEN else 0,
                check.duration_ms,
                check.message,
                result.checked_at,
            )
            for idx, check in enumerate(result.checks)
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO check_events
                    (service_id, service_name, check_index, check_type, passed, duration_ms, message, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def prune_old(self, retention_hours: int) -> None:
        threshold = int(time.time()) - (retention_hours * 3600)
        with self._connect() as conn:
            conn.execute("DELETE FROM check_events WHERE checked_at < ?", (threshold,))

    def latest_for_services(self) -> dict[str, dict[str, Any]]:
        query = """
            SELECT ce.*
            FROM check_events ce
            INNER JOIN (
                SELECT service_id, MAX(checked_at) AS max_ts
                FROM check_events
                GROUP BY service_id
            ) latest
            ON ce.service_id = latest.service_id AND ce.checked_at = latest.max_ts
            ORDER BY ce.service_id, ce.check_index
        """
        latest: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for row in conn.execute(query):
                sid = row["service_id"]
                if sid not in latest:
                    latest[sid] = {
                        "name": row["service_name"],
                        "checked_at": row["checked_at"],
                        "checks": [],
                    }
                latest[sid]["checks"].append({
                    "check_index": row["check_index"],
                    "check_type": row["check_type"],
                    "passed": row["passed"],
                    "duration_ms": row["duration_ms"],
                    "message": row["message"],
                })
        return latest

    def bucket_uptimes(
        self, service_id: str, since_ts: int, bucket_size: int, bucket_count: int
    ) -> dict[int, list[tuple[int, int]]]:
        """Return {check_index: [(passed_count, total_count), ...]} per bucket."""
        query = """
            SELECT
                check_index,
                (checked_at - :since_ts) / :bucket_size AS bucket_idx,
                SUM(passed)  AS passed_count,
                COUNT(*)     AS total_count
            FROM check_events
            WHERE service_id = :service_id
              AND checked_at >= :since_ts
            GROUP BY check_index, bucket_idx
            ORDER BY check_index, bucket_idx
        """
        result: dict[int, list[list[int]]] = {}
        with self._connect() as conn:
            for row in conn.execute(query, {
                "service_id": service_id,
                "since_ts": since_ts,
                "bucket_size": bucket_size,
            }):
                check_idx = int(row["check_index"])
                bucket_idx = int(row["bucket_idx"])
                if check_idx not in result:
                    result[check_idx] = [[0, 0] for _ in range(bucket_count)]
                if 0 <= bucket_idx < bucket_count:
                    result[check_idx][bucket_idx] = [int(row["passed_count"]), int(row["total_count"])]
        return {
            check_idx: [(b[0], b[1]) for b in buckets]
            for check_idx, buckets in result.items()
        }


def summarize_checks(checks: list[CheckResult]) -> str:
    if not checks:
        return "No checks configured"

    bad = [c for c in checks if c.state in {State.RED, State.YELLOW}]
    if bad:
        return "; ".join(f"{c.check_type}: {c.message}" for c in bad[:2])
    return "All checks passing"
