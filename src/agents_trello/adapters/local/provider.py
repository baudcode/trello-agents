"""SQLite-backed BoardProvider for local mock projects.

Behaves like a Trello board for the purposes of the agent pipeline but
keeps state in a few `mock_*` tables in the shared SQLite database. Spin
up a new project by adding a YAML entry with `backend: sqlite`; the
provider auto-seeds an empty board on first use.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

from agents_trello.domain.events import (
    CommentAdded,
    DomainEvent,
    EventMeta,
    TaskCreated,
    TaskMoved,
)
from agents_trello.domain.models import Column, Comment, Task, TaskId

logger = logging.getLogger(__name__)

AGENT_LABEL = "agent working"
DEPLOYED_LABEL = "deployed"


SCHEMA = """
CREATE TABLE IF NOT EXISTS mock_cards (
    task_id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL,
    short_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    column_name TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    branch_name TEXT,
    pr_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mock_cards_board ON mock_cards (board_id);

CREATE TABLE IF NOT EXISTS mock_comments (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mock_comments_task ON mock_comments (task_id);

CREATE TABLE IF NOT EXISTS mock_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mock_actions_board ON mock_actions (board_id, id);

CREATE TABLE IF NOT EXISTS mock_short_counter (
    board_id TEXT PRIMARY KEY,
    last_short INTEGER NOT NULL DEFAULT 0
);
"""


class SqliteBoardProvider:
    """Persistent local board backed by SQLite tables."""

    def __init__(self, conn: aiosqlite.Connection, board_id: str) -> None:
        self._conn = conn
        self._board_id = board_id

    @classmethod
    async def create(cls, conn: aiosqlite.Connection, board_id: str) -> SqliteBoardProvider:
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn, board_id)

    # ------------------------------------------------------------------
    # BoardProvider interface
    # ------------------------------------------------------------------

    async def list_tasks(self) -> list[Task]:
        cursor = await self._conn.execute(
            "SELECT task_id, short_id, title, description, column_name, labels_json, "
            "branch_name, pr_url FROM mock_cards WHERE board_id = ? ORDER BY created_at",
            (self._board_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def get_task(self, task_id: TaskId) -> Task | None:
        cursor = await self._conn.execute(
            "SELECT task_id, short_id, title, description, column_name, labels_json, "
            "branch_name, pr_url FROM mock_cards WHERE board_id = ? AND task_id = ?",
            (self._board_id, task_id),
        )
        row = await cursor.fetchone()
        return self._row_to_task(row) if row else None

    async def get_comments(self, task_id: TaskId, since: str | None = None) -> list[Comment]:
        query = (
            "SELECT id, task_id, author_id, author_name, text, created_at "
            "FROM mock_comments WHERE board_id = ? AND task_id = ?"
        )
        params: list[str] = [self._board_id, task_id]
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC"
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [
            Comment(
                id=r[0],
                task_id=TaskId(r[1]),
                author_id=r[2],
                author_name=r[3],
                text=r[4],
                created_at=datetime.fromisoformat(r[5]),
            )
            for r in rows
        ]

    async def post_comment(self, task_id: TaskId, text: str) -> None:
        await self._add_comment(
            task_id=task_id,
            author_id="agent",
            author_name="Agent",
            text=text,
            emit_event=False,
        )

    async def add_human_comment(
        self,
        task_id: TaskId,
        text: str,
        author_id: str = "human",
        author_name: str = "Human",
    ) -> Comment:
        """Add a comment as a human reviewer.

        Used by the UI / debug endpoints to drive the review-loop locally.
        Emits a CommentAdded action so the dispatcher's OnCommentAdded
        handler fires on the next poll.
        """
        return await self._add_comment(
            task_id=task_id,
            author_id=author_id,
            author_name=author_name,
            text=text,
            emit_event=True,
        )

    async def move_task(self, task_id: TaskId, column: Column) -> None:
        task = await self.get_task(task_id)
        if task is None:
            return
        if task.column == column:
            return
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "UPDATE mock_cards SET column_name = ?, updated_at = ? "
            "WHERE board_id = ? AND task_id = ?",
            (column.value, now, self._board_id, task_id),
        )
        await self._emit_action(
            event_type="TaskMoved",
            payload={
                "task_id": str(task_id),
                "from_column": task.column.value,
                "to_column": column.value,
                "timestamp": now,
            },
        )
        await self._conn.commit()

    async def update_description(self, task_id: TaskId, description: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "UPDATE mock_cards SET description = ?, updated_at = ? "
            "WHERE board_id = ? AND task_id = ?",
            (description, now, self._board_id, task_id),
        )
        await self._conn.commit()

    async def create_task(
        self,
        title: str,
        description: str = "",
        column: Column = Column.BACKLOG,
    ) -> Task:
        task_id = TaskId(f"mock-{uuid.uuid4().hex[:12]}")
        short_id = await self._next_short_id()
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "INSERT INTO mock_cards (task_id, board_id, short_id, title, description, "
            "column_name, labels_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)",
            (task_id, self._board_id, short_id, title, description, column.value, now, now),
        )
        await self._emit_action(
            event_type="TaskCreated",
            payload={
                "task_id": str(task_id),
                "title": title,
                "column": column.value,
                "timestamp": now,
            },
        )
        await self._conn.commit()
        return Task(
            id=task_id,
            short_id=short_id,
            title=title,
            description=description,
            column=column,
            labels=[],
        )

    async def delete_task(self, task_id: TaskId) -> None:
        await self._conn.execute(
            "DELETE FROM mock_cards WHERE board_id = ? AND task_id = ?",
            (self._board_id, task_id),
        )
        await self._conn.execute(
            "DELETE FROM mock_comments WHERE board_id = ? AND task_id = ?",
            (self._board_id, task_id),
        )
        await self._conn.commit()

    async def poll_events(self, since_cursor: str | None = None) -> tuple[list[DomainEvent], str]:
        last_id = int(since_cursor) if since_cursor and since_cursor.isdigit() else 0
        cursor = await self._conn.execute(
            "SELECT id, action_id, event_type, payload_json FROM mock_actions "
            "WHERE board_id = ? AND id > ? ORDER BY id",
            (self._board_id, last_id),
        )
        rows = await cursor.fetchall()
        events: list[DomainEvent] = []
        newest = last_id
        for row in rows:
            newest = max(newest, int(row[0]))
            event = self._decode_action(row[1], row[2], row[3])
            if event is not None:
                events.append(event)
        return events, str(newest)

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[DomainEvent]:
        return []

    # ------------------------------------------------------------------
    # Label helpers used by domain handlers
    # ------------------------------------------------------------------

    async def set_agent_working(self, task_id: TaskId) -> None:
        await self._toggle_label(task_id, AGENT_LABEL, present=True)

    async def clear_agent_working(self, task_id: TaskId) -> None:
        await self._toggle_label(task_id, AGENT_LABEL, present=False)

    async def set_deployed(self, task_id: TaskId) -> None:
        await self._toggle_label(task_id, DEPLOYED_LABEL, present=True)

    async def clear_deployed(self, task_id: TaskId) -> None:
        await self._toggle_label(task_id, DEPLOYED_LABEL, present=False)

    async def get_text_attachments(self, task_id: TaskId) -> list[tuple[str, str]]:
        return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: tuple) -> Task:
        labels = json.loads(row[5]) if row[5] else []
        return Task(
            id=TaskId(row[0]),
            short_id=row[1],
            title=row[2],
            description=row[3] or "",
            column=Column(row[4]),
            labels=labels,
            branch_name=row[6],
            pr_url=row[7],
        )

    async def _add_comment(
        self,
        *,
        task_id: TaskId,
        author_id: str,
        author_name: str,
        text: str,
        emit_event: bool,
    ) -> Comment:
        comment_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "INSERT INTO mock_comments (id, board_id, task_id, author_id, author_name, "
            "text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (comment_id, self._board_id, task_id, author_id, author_name, text, now),
        )
        if emit_event:
            await self._emit_action(
                event_type="CommentAdded",
                payload={
                    "task_id": str(task_id),
                    "comment_id": comment_id,
                    "author_id": author_id,
                    "author_name": author_name,
                    "text": text,
                    "timestamp": now,
                },
            )
        await self._conn.commit()
        return Comment(
            id=comment_id,
            task_id=task_id,
            author_id=author_id,
            author_name=author_name,
            text=text,
            created_at=datetime.fromisoformat(now),
        )

    async def _emit_action(self, *, event_type: str, payload: dict) -> None:
        await self._conn.execute(
            "INSERT INTO mock_actions (board_id, action_id, event_type, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                self._board_id,
                uuid.uuid4().hex,
                event_type,
                json.dumps(payload),
                datetime.now(UTC).isoformat(),
            ),
        )

    async def _next_short_id(self) -> str:
        cursor = await self._conn.execute(
            "SELECT last_short FROM mock_short_counter WHERE board_id = ?",
            (self._board_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await self._conn.execute(
                "INSERT INTO mock_short_counter (board_id, last_short) VALUES (?, 1)",
                (self._board_id,),
            )
            return "1"
        next_n = int(row[0]) + 1
        await self._conn.execute(
            "UPDATE mock_short_counter SET last_short = ? WHERE board_id = ?",
            (next_n, self._board_id),
        )
        return str(next_n)

    async def _toggle_label(self, task_id: TaskId, label: str, present: bool) -> None:
        task = await self.get_task(task_id)
        if task is None:
            return
        labels = [lbl for lbl in task.labels if lbl != label]
        if present:
            labels.append(label)
        await self._conn.execute(
            "UPDATE mock_cards SET labels_json = ?, updated_at = ? "
            "WHERE board_id = ? AND task_id = ?",
            (json.dumps(labels), datetime.now(UTC).isoformat(), self._board_id, task_id),
        )
        await self._conn.commit()

    def _decode_action(
        self, action_id: str, event_type: str, payload_json: str
    ) -> DomainEvent | None:
        try:
            payload = json.loads(payload_json)
        except ValueError:
            logger.warning("mock_action_invalid_json", extra={"action_id": action_id})
            return None
        ts = payload.get("timestamp")
        meta = EventMeta(
            action_id=action_id,
            timestamp=datetime.fromisoformat(ts) if ts else datetime.now(UTC),
        )
        if event_type == "TaskMoved":
            return TaskMoved(
                meta=meta,
                task_id=TaskId(payload["task_id"]),
                from_column=Column(payload["from_column"]),
                to_column=Column(payload["to_column"]),
            )
        if event_type == "TaskCreated":
            return TaskCreated(
                meta=meta,
                task_id=TaskId(payload["task_id"]),
                title=payload.get("title", ""),
                column=Column(payload["column"]),
            )
        if event_type == "CommentAdded":
            return CommentAdded(
                meta=meta,
                task_id=TaskId(payload["task_id"]),
                comment_id=payload["comment_id"],
                author_id=payload.get("author_id", ""),
                author_name=payload.get("author_name", ""),
                text=payload.get("text", ""),
            )
        logger.warning("mock_action_unknown_type", extra={"event_type": event_type})
        return None


__all__ = ["SqliteBoardProvider"]
