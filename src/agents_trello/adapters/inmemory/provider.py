"""In-memory BoardProvider for tests and local mock projects."""

from __future__ import annotations

import itertools
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

AGENT_LABEL = "agent working"
DEPLOYED_LABEL = "deployed"


class InMemoryProvider:
    """A `BoardProvider` that keeps everything in process memory.

    Used by tests and by `backend: inmemory` projects in `projects.yaml`.
    Mutations (`move_task`, `post_comment`, `create_task`) record domain
    events so the polling loop picks them up and the dispatcher reacts.
    """

    def __init__(self, board_id: str = "inmemory") -> None:
        self._board_id = board_id
        self._tasks: dict[TaskId, Task] = {}
        self._comments: dict[TaskId, list[Comment]] = {}
        self._events: deque[DomainEvent] = deque()
        self._posted_comments: list[tuple[TaskId, str]] = []
        self._short_counter = itertools.count(1)

    # --- BoardProvider interface ---

    async def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    async def get_task(self, task_id: TaskId) -> Task | None:
        return self._tasks.get(task_id)

    async def get_comments(self, task_id: TaskId, since: str | None = None) -> list[Comment]:
        return list(self._comments.get(task_id, []))

    async def post_comment(self, task_id: TaskId, text: str) -> None:
        self._posted_comments.append((task_id, text))
        now = datetime.now(UTC)
        comment = Comment(
            id=uuid.uuid4().hex,
            task_id=task_id,
            author_id="agent",
            author_name="Agent",
            text=text,
            created_at=now,
        )
        self._comments.setdefault(task_id, []).append(comment)
        # External callers (handlers themselves) re-emit no event; only human
        # comments — surfaced via `simulate_comment` — should produce events.

    async def move_task(self, task_id: TaskId, column: Column) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        previous = task.column
        self._tasks[task_id] = Task(
            id=task.id,
            short_id=task.short_id,
            title=task.title,
            description=task.description,
            column=column,
            labels=list(task.labels),
            comments=task.comments,
            branch_name=task.branch_name,
            pr_url=task.pr_url,
        )
        if previous != column:
            self._events.append(
                TaskMoved(
                    meta=EventMeta(action_id=uuid.uuid4().hex, timestamp=datetime.now(UTC)),
                    task_id=task_id,
                    from_column=previous,
                    to_column=column,
                )
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
            labels=list(task.labels),
            comments=task.comments,
            branch_name=task.branch_name,
            pr_url=task.pr_url,
        )

    async def create_task(
        self,
        title: str,
        description: str = "",
        column: Column = Column.BACKLOG,
    ) -> Task:
        task_id = TaskId(f"mock-{uuid.uuid4().hex[:12]}")
        short_id = str(next(self._short_counter))
        task = Task(
            id=task_id,
            short_id=short_id,
            title=title,
            description=description,
            column=column,
            labels=[],
        )
        self._tasks[task_id] = task
        self._events.append(
            TaskCreated(
                meta=EventMeta(action_id=uuid.uuid4().hex, timestamp=datetime.now(UTC)),
                task_id=task_id,
                title=title,
                column=column,
            )
        )
        return task

    async def delete_task(self, task_id: TaskId) -> None:
        self._tasks.pop(task_id, None)
        self._comments.pop(task_id, None)

    async def poll_events(self, since_cursor: str | None = None) -> tuple[list[DomainEvent], str]:
        events = list(self._events)
        self._events.clear()
        return events, datetime.now(UTC).isoformat()

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[DomainEvent]:
        return []

    # --- Label helpers used by domain handlers ---

    async def set_agent_working(self, task_id: TaskId) -> None:
        self._toggle_label(task_id, AGENT_LABEL, present=True)

    async def clear_agent_working(self, task_id: TaskId) -> None:
        self._toggle_label(task_id, AGENT_LABEL, present=False)

    async def set_deployed(self, task_id: TaskId) -> None:
        self._toggle_label(task_id, DEPLOYED_LABEL, present=True)

    async def clear_deployed(self, task_id: TaskId) -> None:
        self._toggle_label(task_id, DEPLOYED_LABEL, present=False)

    async def get_text_attachments(self, task_id: TaskId) -> list[tuple[str, str]]:
        return []

    def _toggle_label(self, task_id: TaskId, label: str, present: bool) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        labels = [lbl for lbl in task.labels if lbl != label]
        if present:
            labels.append(label)
        self._tasks[task_id] = Task(
            id=task.id,
            short_id=task.short_id,
            title=task.title,
            description=task.description,
            column=task.column,
            labels=labels,
            comments=task.comments,
            branch_name=task.branch_name,
            pr_url=task.pr_url,
        )

    # --- Test/utility helpers ---

    def seed_task(
        self,
        task_id: str = "task-1",
        short_id: str = "1",
        title: str = "Test task",
        description: str = "A test task",
        column: Column = Column.BACKLOG,
        pr_url: str | None = None,
        labels: list[str] | None = None,
    ) -> Task:
        tid = TaskId(task_id)
        task = Task(
            id=tid,
            short_id=short_id,
            title=title,
            description=description,
            column=column,
            labels=list(labels or []),
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
        task = self._tasks.get(tid)
        if task:
            self._tasks[tid] = Task(
                id=task.id,
                short_id=task.short_id,
                title=task.title,
                description=task.description,
                column=to_col,
                labels=list(task.labels),
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
