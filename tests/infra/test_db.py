"""Tests for the SQLite persistence layer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents_trello.domain.events import EventMeta, TaskCreated
from agents_trello.domain.handlers import Dispatcher
from agents_trello.domain.models import Column, TaskId
from agents_trello.infra.db import (
    get_connection,
    get_cursor,
    is_action_processed,
    log_event,
    mark_action_processed,
    run_migrations,
    set_cursor,
)


@pytest.fixture
async def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    connection = await get_connection(db_path)
    await run_migrations(connection)
    yield connection
    await connection.close()


async def test_migration_roundtrip(tmp_path: Path) -> None:
    """Running migrations twice must not raise."""
    db_path = tmp_path / "test.db"
    connection = await get_connection(db_path)
    await run_migrations(connection)
    await run_migrations(connection)
    await connection.close()


async def test_mark_and_check_processed(conn) -> None:
    assert await is_action_processed(conn, "act-1") is False
    await mark_action_processed(conn, "act-1")
    assert await is_action_processed(conn, "act-1") is True


async def test_cursor_get_set(conn) -> None:
    board = "board-1"
    assert await get_cursor(conn, board) is None
    await set_cursor(conn, "cursor-abc", board)
    assert await get_cursor(conn, board) == "cursor-abc"
    # Overwrite
    await set_cursor(conn, "cursor-def", board)
    assert await get_cursor(conn, board) == "cursor-def"


async def test_cursor_is_per_board(conn) -> None:
    await set_cursor(conn, "cursor-a", "board-A")
    await set_cursor(conn, "cursor-b", "board-B")
    assert await get_cursor(conn, "board-A") == "cursor-a"
    assert await get_cursor(conn, "board-B") == "cursor-b"


async def test_event_log(conn) -> None:
    await log_event(conn, '{"type": "TaskCreated"}')
    await log_event(conn, '{"type": "TaskMoved"}', error="boom")

    cur = await conn.execute("SELECT event_json, error FROM event_log ORDER BY id")
    rows = await cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == '{"type": "TaskCreated"}'
    assert rows[0][1] is None
    assert rows[1][1] == "boom"


async def test_dispatcher_skips_duplicate_action_ids(conn) -> None:
    """Dispatching the same event twice should invoke the handler only once."""
    call_count = 0

    async def counting_handler(event: TaskCreated) -> None:
        nonlocal call_count
        call_count += 1

    dispatcher = Dispatcher(conn=conn)
    dispatcher.register(TaskCreated, counting_handler)

    event = TaskCreated(
        meta=EventMeta(action_id="dup-1", timestamp=datetime.now(UTC)),
        task_id=TaskId("t1"),
        title="Test",
        column=Column.BACKLOG,
    )

    await dispatcher.dispatch(event)
    await dispatcher.dispatch(event)

    assert call_count == 1
