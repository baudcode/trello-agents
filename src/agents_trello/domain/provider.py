"""BoardProvider protocol."""

from __future__ import annotations

from typing import Protocol

from agents_trello.domain.events import DomainEvent
from agents_trello.domain.models import Column, Comment, Task, TaskId


class BoardProvider(Protocol):
    async def list_tasks(self) -> list[Task]: ...

    async def get_task(self, task_id: TaskId) -> Task | None: ...

    async def get_comments(self, task_id: TaskId, since: str | None = None) -> list[Comment]: ...

    async def post_comment(self, task_id: TaskId, text: str) -> None: ...

    async def move_task(self, task_id: TaskId, column: Column) -> None: ...

    async def update_description(self, task_id: TaskId, description: str) -> None: ...

    async def create_task(
        self,
        title: str,
        description: str = "",
        column: Column = Column.BACKLOG,
    ) -> Task: ...

    async def delete_task(self, task_id: TaskId) -> None: ...

    async def poll_events(self, since_cursor: str | None = None) -> tuple[list[DomainEvent], str]:
        """Returns (events, new_cursor)."""
        ...

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[DomainEvent]: ...
