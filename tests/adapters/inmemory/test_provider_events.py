"""Tests that InMemoryProvider auto-emits events on mutations."""

from __future__ import annotations

from agents_trello.adapters.inmemory.provider import InMemoryProvider
from agents_trello.domain.events import TaskCreated, TaskMoved
from agents_trello.domain.models import Column


async def test_create_task_emits_task_created() -> None:
    p = InMemoryProvider()
    task = await p.create_task("Hello", column=Column.BACKLOG)
    events, _ = await p.poll_events()
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, TaskCreated)
    assert ev.task_id == task.id
    assert ev.title == "Hello"


async def test_move_task_emits_task_moved() -> None:
    p = InMemoryProvider()
    task = await p.create_task("t", column=Column.BACKLOG)
    await p.poll_events()  # drain create event
    await p.move_task(task.id, Column.TODO)
    events, _ = await p.poll_events()
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, TaskMoved)
    assert ev.from_column == Column.BACKLOG
    assert ev.to_column == Column.TODO


async def test_move_to_same_column_emits_nothing() -> None:
    p = InMemoryProvider()
    task = await p.create_task("t", column=Column.BACKLOG)
    await p.poll_events()
    await p.move_task(task.id, Column.BACKLOG)
    events, _ = await p.poll_events()
    assert events == []


async def test_agent_label_helpers() -> None:
    p = InMemoryProvider()
    task = await p.create_task("t")
    await p.set_agent_working(task.id)
    fetched = await p.get_task(task.id)
    assert fetched is not None
    assert "agent working" in fetched.labels
    await p.clear_agent_working(task.id)
    fetched = await p.get_task(task.id)
    assert fetched is not None
    assert "agent working" not in fetched.labels


async def test_delete_task() -> None:
    p = InMemoryProvider()
    task = await p.create_task("t")
    await p.delete_task(task.id)
    assert await p.get_task(task.id) is None
