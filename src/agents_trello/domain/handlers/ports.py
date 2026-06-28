"""Protocols for handler dependencies: WorktreeManager, AgentRunner, VCS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agents_trello.domain.models import TaskId


@dataclass(frozen=True)
class AgentResult:
    success: bool
    summary: str
    files_changed: list[str]
    error: str | None = None


class WorktreeManager(Protocol):
    async def create(self, task_id: TaskId, slug: str, base: str = "main") -> Path: ...
    async def remove(self, task_id: TaskId) -> None: ...
    async def push(self, task_id: TaskId, remote: str = "origin") -> None: ...
    def get_branch_name(self, task_id: TaskId, slug: str) -> str: ...


class AgentRunner(Protocol):
    async def run_initial(
        self,
        task_id: TaskId,
        worktree_path: Path,
        task_title: str,
        task_description: str,
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult: ...

    async def run_review(
        self,
        task_id: TaskId,
        worktree_path: Path,
        task_title: str,
        task_description: str,
        comments: list[str],
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult: ...

    async def cancel(self, task_id: TaskId) -> None: ...


class VCS(Protocol):
    async def open_draft_pr(self, branch: str, title: str, body: str) -> str: ...
    async def mark_ready_for_review(self, pr_url: str) -> None: ...
    async def enable_auto_merge(self, pr_url: str) -> None: ...
    async def get_pr_status(self, pr_url: str) -> dict[str, object]: ...
