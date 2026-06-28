"""Structured event log for tracking all service actions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)

EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    task_title TEXT,
    branch TEXT,
    details TEXT,
    output TEXT,
    board_id TEXT
)
"""


@dataclass
class LogEntry:
    id: int
    timestamp: str
    event_type: str
    task_id: str | None
    task_title: str | None
    branch: str | None
    details: dict | None
    output: str | None
    board_id: str | None = None


class EventLog:
    """Records and queries structured events for all service actions.

    Pass ``board_id`` in the constructor to tag every record with a project.
    Queries accept an optional ``board_id`` filter so the same EventLog
    instance can be shared across projects (or one-per-project, either works).
    """

    def __init__(
        self,
        conn: aiosqlite.Connection,
        board_id: str | None = None,
    ) -> None:
        self._conn = conn
        self._board_id = board_id

    async def setup(self) -> None:
        await self._conn.executescript(EVENT_SCHEMA)
        # Add board_id column if upgrading an existing DB.
        cursor = await self._conn.execute("PRAGMA table_info(activity_log)")
        rows = await cursor.fetchall()
        cols = {row[1] for row in rows}
        if "board_id" not in cols:
            await self._conn.execute("ALTER TABLE activity_log ADD COLUMN board_id TEXT")
        await self._conn.commit()

    async def record(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        task_title: str | None = None,
        branch: str | None = None,
        details: dict | None = None,
        output: str | None = None,
        board_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        bid = board_id or self._board_id
        await self._conn.execute(
            "INSERT INTO activity_log"
            " (timestamp, event_type, task_id, task_title, branch, details, output, board_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                event_type,
                task_id,
                task_title,
                branch,
                json.dumps(details) if details else None,
                output,
                bid,
            ),
        )
        await self._conn.commit()
        logger.debug(
            "event_logged",
            extra={"event_type": event_type, "task_id": task_id, "board_id": bid},
        )

    async def query(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        board_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LogEntry]:
        conditions: list[str] = []
        params: list[str | int] = []

        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if board_id:
            conditions.append("board_id = ?")
            params.append(board_id)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        cursor = await self._conn.execute(
            f"SELECT id, timestamp, event_type, task_id, task_title, branch, details, "
            f"output, board_id FROM activity_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [
            LogEntry(
                id=r[0],
                timestamp=r[1],
                event_type=r[2],
                task_id=r[3],
                task_title=r[4],
                branch=r[5],
                details=json.loads(r[6]) if r[6] else None,
                output=r[7],
                board_id=r[8],
            )
            for r in rows
        ]

    async def count(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        board_id: str | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[str] = []
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if board_id:
            conditions.append("board_id = ?")
            params.append(board_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._conn.execute(f"SELECT COUNT(*) FROM activity_log{where}", params)
        row = await cursor.fetchone()
        return row[0] if row else 0
