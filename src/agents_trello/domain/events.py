"""Domain event types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agents_trello.domain.models import Column, TaskId


@dataclass(frozen=True)
class EventMeta:
    action_id: str
    timestamp: datetime


@dataclass(frozen=True)
class TaskCreated:
    meta: EventMeta
    task_id: TaskId
    title: str
    column: Column


@dataclass(frozen=True)
class TaskMoved:
    meta: EventMeta
    task_id: TaskId
    from_column: Column
    to_column: Column


@dataclass(frozen=True)
class TaskUpdated:
    meta: EventMeta
    task_id: TaskId
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class CommentAdded:
    meta: EventMeta
    task_id: TaskId
    comment_id: str
    author_id: str
    author_name: str
    text: str


DomainEvent = TaskCreated | TaskMoved | TaskUpdated | CommentAdded
