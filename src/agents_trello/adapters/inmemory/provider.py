"""In-memory BoardProvider for tests."""

from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime

from agents_trello.domain.events import (
    CommentAdded,
    DomainEvent,
    EventMeta,
    TaskCreated,
    TaskMoved,
)
from agents_trello.domain.models import Column, Comment, Task, TaskId


class InMemoryProvider:
    def __init__(self) -> None:
        self._tasks: dict[TaskId, Task] = {}
        self._comments: dict[TaskId, list[Comment]] = {}
        self._events: deque[DomainEvent] = deque()
        self._posted_comments: list[tuple[TaskId, str]] = []

    # --- BoardProvider interface ---

    async def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    async def get_task(self, task_id: TaskId) -> Task | None:
        return self._tasks.get(task_id)

    async def get_comments(self, task_id: TaskId, since: str | None = None) -> list[Comment]:
        return self._comments.get(task_id, [])

    async def post_comment(self, task_id: TaskId, text: str) -> None:
        self._posted_comments.append((task_id, text))
        comment = Comment(
            id=uuid.uuid4().hex,
            task_id=task_id,
            author_id="agent",
            author_name="Agent",
            text=text,
            created_at=datetime.now(UTC),
        )
        self._comments.setdefault(task_id, []).append(comment)

    async def move_task(self, task_id: TaskId, column: Column) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        self._tasks[task_id] = Task(
            id=task.id,
            short_id=task.short_id,
            title=task.title,
            description=task.description,
            column=column,
            comments=task.comments,
            branch_name=task.branch_name,
            pr_url=task.pr_url,
        )

    async def update_description(self, task_id: TaskId, description: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        self._tasks[task_id] = Task(
            id=task.id,
            short_id=task.short_id,
            title=task.title,
            description=description,
            column=task.column,
            comments=task.comments,
            branch_name=task.branch_name,
            pr_url=task.pr_url,
        )

    async def poll_events(self, since_cursor: str | None = None) -> tuple[list[DomainEvent], str]:
        events = list(self._events)
        self._events.clear()
        return events, "cursor-0"

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[DomainEvent]:
        return []

    # --- Test helpers ---

    def seed_task(
        self,
        task_id: str = "task-1",
        short_id: str = "1",
        title: str = "Test task",
        description: str = "A test task",
        column: Column = Column.BACKLOG,
        pr_url: str | None = None,
    ) -> Task:
        tid = TaskId(task_id)
        task = Task(
            id=tid,
            short_id=short_id,
            title=title,
            description=description,
            column=column,
            pr_url=pr_url,
        )
        self._tasks[tid] = task
        return task

    def simulate_move(self, task_id: str, from_col: Column, to_col: Column) -> TaskMoved:
        tid = TaskId(task_id)
        event = TaskMoved(
            meta=EventMeta(action_id=uuid.uuid4().hex, timestamp=datetime.now(UTC)),
            task_id=tid,
            from_column=from_col,
            to_column=to_col,
        )
        self._events.append(event)
        # Also update the task state
        task = self._tasks.get(tid)
        if task:
            self._tasks[tid] = Task(
                id=task.id,
                short_id=task.short_id,
                title=task.title,
                description=task.description,
                column=to_col,
                comments=task.comments,
                branch_name=task.branch_name,
                pr_url=task.pr_url,
            )
        return event

    def simulate_comment(
        self,
        task_id: str,
        text: str = "Review comment",
        author_id: str = "human-user",
        author_name: str = "Human",
    ) -> CommentAdded:
        tid = TaskId(task_id)
        comment_id = uuid.uuid4().hex
        event = CommentAdded(
            meta=EventMeta(action_id=uuid.uuid4().hex, timestamp=datetime.now(UTC)),
            task_id=tid,
            comment_id=comment_id,
            author_id=author_id,
            author_name=author_name,
            text=text,
        )
        self._events.append(event)
        comment = Comment(
            id=comment_id,
            task_id=tid,
            author_id=author_id,
            author_name=author_name,
            text=text,
            created_at=datetime.now(UTC),
        )
        self._comments.setdefault(tid, []).append(comment)
        return event

    def simulate_create(
        self,
        task_id: str = "task-1",
        title: str = "Test task",
        column: Column = Column.BACKLOG,
    ) -> TaskCreated:
        tid = TaskId(task_id)
        event = TaskCreated(
            meta=EventMeta(action_id=uuid.uuid4().hex, timestamp=datetime.now(UTC)),
            task_id=tid,
            title=title,
            column=column,
        )
        self._events.append(event)
        return event
