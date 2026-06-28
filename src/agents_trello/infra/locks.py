"""Per-task SQLite-backed locking."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiosqlite


@asynccontextmanager
async def task_lock(conn: aiosqlite.Connection, task_id: str) -> AsyncIterator[None]:
    """Acquire an exclusive per-task lock backed by the card_locks table.

    Uses BEGIN IMMEDIATE to serialise concurrent callers on the same database.
    The lock row is deleted on exit so the table stays clean.
    """
    await conn.execute("BEGIN IMMEDIATE")
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        "INSERT OR REPLACE INTO card_locks (task_id, locked_at) VALUES (?, ?)",
        (task_id, now),
    )
    try:
        yield
    finally:
        await conn.execute("DELETE FROM card_locks WHERE task_id = ?", (task_id,))
        await conn.commit()
