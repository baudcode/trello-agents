"""Tests for the SQLite-backed local board provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents_trello.adapters.local.provider import SqliteBoardProvider
from agents_trello.domain.events import CommentAdded, TaskCreated, TaskMoved
from agents_trello.domain.models import Column, TaskId
from agents_trello.infra.db import get_connection


@pytest.fixture
async def provider(tmp_path: Path):
    conn = await get_connection(tmp_path / "mock.db")
    p = await SqliteBoardProvider.create(conn, board_id="local:test")
    yield p
    await conn.close()


@pytest.fixture
async def two_boards(tmp_path: Path):
    """Two providers sharing the same DB but partitioned by board_id."""
    conn = await get_connection(tmp_path / "mock.db")
    a = await SqliteBoardProvider.create(conn, board_id="boardA")
    b = await SqliteBoardProvider.create(conn, board_id="boardB")
    yield a, b
    await conn.close()


async def test_create_and_list(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("My title", description="desc", column=Column.BACKLOG)
    assert task.title == "My title"
    assert task.description == "desc"
    assert task.column == Column.BACKLOG
    assert task.short_id == "1"
    assert (await provider.list_tasks())[0].id == task.id


async def test_short_ids_increment(provider: SqliteBoardProvider) -> None:
    t1 = await provider.create_task("a")
    t2 = await provider.create_task("b")
    t3 = await provider.create_task("c")
    assert [t1.short_id, t2.short_id, t3.short_id] == ["1", "2", "3"]


async def test_move_emits_task_moved(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("t")
    # Drain the TaskCreated emitted by create_task
    initial, cursor = await provider.poll_events()
    assert len(initial) == 1
    assert isinstance(initial[0], TaskCreated)

    await provider.move_task(task.id, Column.IN_PROGRESS)
    events, new_cursor = await provider.poll_events(cursor)
    assert len(events) == 1
    moved = events[0]
    assert isinstance(moved, TaskMoved)
    assert moved.to_column == Column.IN_PROGRESS
    assert moved.from_column == Column.BACKLOG
    assert int(new_cursor) > int(cursor)


async def test_move_no_op_does_not_emit(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("t", column=Column.BACKLOG)
    _, cursor = await provider.poll_events()
    await provider.move_task(task.id, Column.BACKLOG)
    events, _ = await provider.poll_events(cursor)
    assert events == []


async def test_post_comment_does_not_emit_event(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("t")
    _, cursor = await provider.poll_events()
    await provider.post_comment(task.id, "agent reply")
    events, _ = await provider.poll_events(cursor)
    assert events == []


async def test_add_human_comment_emits_event(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("t")
    _, cursor = await provider.poll_events()
    await provider.add_human_comment(task.id, "please fix the typo", author_name="Reviewer")
    events, _ = await provider.poll_events(cursor)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, CommentAdded)
    assert ev.text == "please fix the typo"
    assert ev.author_name == "Reviewer"


async def test_delete_task_removes_comments(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("t")
    await provider.post_comment(task.id, "first")
    await provider.delete_task(task.id)
    assert await provider.get_task(task.id) is None
    assert await provider.get_comments(task.id) == []


async def test_labels_via_helpers(provider: SqliteBoardProvider) -> None:
    task = await provider.create_task("t")
    await provider.set_agent_working(task.id)
    fetched = await provider.get_task(task.id)
    assert fetched is not None
    assert "agent working" in fetched.labels

    await provider.clear_agent_working(task.id)
    fetched = await provider.get_task(task.id)
    assert fetched is not None
    assert "agent working" not in fetched.labels


async def test_boards_are_isolated(
    two_boards: tuple[SqliteBoardProvider, SqliteBoardProvider],
) -> None:
    a, b = two_boards
    task_a = await a.create_task("only on A")
    await b.create_task("only on B")

    assert [t.title for t in await a.list_tasks()] == ["only on A"]
    assert [t.title for t in await b.list_tasks()] == ["only on B"]
    assert await b.get_task(task_a.id) is None


async def test_persistence_across_provider_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.db"
    conn = await get_connection(db_path)
    p1 = await SqliteBoardProvider.create(conn, board_id="b")
    task = await p1.create_task("survives", description="restart")
    await conn.close()

    conn2 = await get_connection(db_path)
    p2 = await SqliteBoardProvider.create(conn2, board_id="b")
    fetched = await p2.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "survives"
    assert fetched.description == "restart"
    await conn2.close()


async def test_poll_cursor_advances(provider: SqliteBoardProvider) -> None:
    await provider.create_task("a")
    events_1, cur_1 = await provider.poll_events()
    assert len(events_1) == 1
    events_2, cur_2 = await provider.poll_events(cur_1)
    assert events_2 == []
    assert cur_2 == cur_1


async def test_parse_webhook_returns_empty(provider: SqliteBoardProvider) -> None:
    assert provider.parse_webhook({}, b"{}") == []


async def test_get_task_returns_none_for_unknown(provider: SqliteBoardProvider) -> None:
    assert await provider.get_task(TaskId("nope")) is None
