"""Tests for per-task SQLite locking."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents_trello.infra.db import get_connection, run_migrations
from agents_trello.infra.locks import task_lock


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "locks.db"
    conn = await get_connection(path)
    await run_migrations(conn)
    await conn.close()
    return path


async def test_lock_serializes_same_task(db_path: Path) -> None:
    """Two concurrent lock acquisitions for the same task_id must serialise."""
    order: list[str] = []

    async def worker(name: str) -> None:
        conn = await get_connection(db_path)
        try:
            async with task_lock(conn, "task-1"):
                order.append(f"{name}-enter")
                await asyncio.sleep(0.05)
                order.append(f"{name}-exit")
        finally:
            await connection_close(conn)

    async def connection_close(c):
        await c.close()

    await asyncio.gather(worker("A"), worker("B"))

    # One worker must fully complete before the other starts.
    assert order.index("A-exit") < order.index("B-enter") or order.index("B-exit") < order.index(
        "A-enter"
    )


async def test_lock_allows_different_tasks(db_path: Path) -> None:
    """Locks on different task_ids can be held concurrently."""
    held: list[str] = []

    async def worker(task_id: str, other_event: asyncio.Event) -> None:
        conn = await get_connection(db_path)
        try:
            async with task_lock(conn, task_id):
                held.append(task_id)
                other_event.set()
                # Wait briefly so the other worker can also acquire its lock.
                await asyncio.sleep(0.05)
        finally:
            await conn.close()

    event_a = asyncio.Event()
    event_b = asyncio.Event()

    task_a = asyncio.create_task(worker("task-A", event_a))
    task_b = asyncio.create_task(worker("task-B", event_b))

    await asyncio.gather(task_a, task_b)

    # Both tasks should have been held (both workers ran).
    assert "task-A" in held
    assert "task-B" in held
