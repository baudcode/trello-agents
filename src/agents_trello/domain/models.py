"""Domain models: Task, Comment, Column, TaskId."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import NewType

TaskId = NewType("TaskId", str)


class Column(enum.Enum):
    BACKLOG = "Backlog"
    TODO = "Todo"
    IN_PROGRESS = "InProgress"
    REVIEW = "Review"
    DONE = "Done"


@dataclass(frozen=True)
class Comment:
    id: str
    task_id: TaskId
    author_id: str
    author_name: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class Task:
    id: TaskId
    short_id: str
    title: str
    description: str
    column: Column
    labels: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    branch_name: str | None = None
    pr_url: str | None = None
