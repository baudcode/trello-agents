"""Tests for comment-added handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agents_trello.adapters.inmemory.provider import InMemoryProvider
from agents_trello.domain.handlers.on_comment_added import COMMENT_PREFIX, OnCommentAdded
from agents_trello.domain.handlers.ports import AgentResult
from agents_trello.domain.models import Column, TaskId


@dataclass
class FakeAgentRunner:
    runs: list[TaskId] = field(default_factory=list)
    cancelled: list[TaskId] = field(default_factory=list)
    result: AgentResult = field(
        default_factory=lambda: AgentResult(
            success=True, summary="Applied review feedback", files_changed=["main.py"]
        )
    )

    async def run_initial(
        self,
        task_id: TaskId,
        worktree_path: Path,
        task_title: str,
        task_description: str,
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult:
        self.runs.append(task_id)
        return self.result

    async def run_review(
        self,
        task_id: TaskId,
        worktree_path: Path,
        task_title: str,
        task_description: str,
        comments: list[str],
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult:
        self.runs.append(task_id)
        return self.result

    async def run_chat(
        self,
        task_id: TaskId,
        task_title: str,
        task_description: str,
        comments: list[str],
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult:
        self.runs.append(task_id)
        return self.result

    async def cancel(self, task_id: TaskId) -> None:
        self.cancelled.append(task_id)


@dataclass
class FakeWorktreeManager:
    created: list[tuple[TaskId, str]] = field(default_factory=list)

    async def create(self, task_id: TaskId, slug: str, base: str = "main") -> Path:
        self.created.append((task_id, slug))
        return Path(f"/tmp/worktrees/{task_id}-{slug}")

    async def remove(self, task_id: TaskId) -> None:
        pass

    async def push(self, task_id: TaskId, remote: str = "origin") -> None:
        pass

    def get_branch_name(self, task_id: TaskId, slug: str) -> str:
        return f"feat/{task_id}-{slug}"


@pytest.fixture
def board() -> InMemoryProvider:
    return InMemoryProvider()


@pytest.fixture
def agent() -> FakeAgentRunner:
    return FakeAgentRunner()


@pytest.fixture
def worktree() -> FakeWorktreeManager:
    return FakeWorktreeManager()


@pytest.fixture
def handler(
    board: InMemoryProvider, agent: FakeAgentRunner, worktree: FakeWorktreeManager
) -> OnCommentAdded:
    return OnCommentAdded(board=board, agent=agent, worktree=worktree)


async def test_human_comment_on_review_card_triggers_agent(
    board: InMemoryProvider,
    agent: FakeAgentRunner,
    handler: OnCommentAdded,
) -> None:
    board.seed_task(task_id="t1", column=Column.REVIEW)
    event = board.simulate_comment("t1", text="Please also add a unit test")
    await handler(event)
    await asyncio.sleep(0.1)  # let background task run

    assert len(agent.runs) == 1
    assert agent.runs[0] == TaskId("t1")
    # Agent should have posted response comments (working + result)
    assert len(board._posted_comments) >= 1
    # Card stays in Review (not moved)
    task = await board.get_task(TaskId("t1"))
    assert task is not None
    assert task.column == Column.REVIEW


async def test_comment_with_claude_prefix_is_ignored(
    board: InMemoryProvider,
    agent: FakeAgentRunner,
    handler: OnCommentAdded,
) -> None:
    board.seed_task(task_id="t2", column=Column.REVIEW)
    event = board.simulate_comment("t2", text=f"{COMMENT_PREFIX}I made changes")
    await handler(event)

    assert len(agent.runs) == 0


async def test_comment_on_backlog_card_triggers_agent(
    board: InMemoryProvider,
    agent: FakeAgentRunner,
    handler: OnCommentAdded,
) -> None:
    board.seed_task(task_id="t3", column=Column.BACKLOG)
    event = board.simulate_comment("t3", text="Can you look at this?")
    await handler(event)
    await asyncio.sleep(0.1)

    assert len(agent.runs) == 1


async def test_comment_on_done_card_is_ignored(
    board: InMemoryProvider,
    agent: FakeAgentRunner,
    handler: OnCommentAdded,
) -> None:
    board.seed_task(task_id="t4", column=Column.DONE)
    event = board.simulate_comment("t4", text="A comment")
    await handler(event)

    assert len(agent.runs) == 0
