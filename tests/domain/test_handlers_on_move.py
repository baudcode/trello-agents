"""Tests for task-moved handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agents_trello.adapters.inmemory.provider import InMemoryProvider
from agents_trello.domain.handlers.on_task_moved import OnTaskMoved
from agents_trello.domain.handlers.ports import AgentResult
from agents_trello.domain.models import Column, TaskId

# --- Fakes ---


@dataclass
class FakeWorktreeManager:
    created: list[tuple[TaskId, str]] = field(default_factory=list)
    removed: list[TaskId] = field(default_factory=list)
    pushed: list[TaskId] = field(default_factory=list)

    async def create(self, task_id: TaskId, slug: str, base: str = "main") -> Path:
        self.created.append((task_id, slug))
        return Path(f"/tmp/worktrees/{task_id}-{slug}")

    async def remove(self, task_id: TaskId) -> None:
        self.removed.append(task_id)

    async def push(self, task_id: TaskId, remote: str = "origin") -> None:
        self.pushed.append(task_id)

    def get_branch_name(self, task_id: TaskId, slug: str) -> str:
        return f"feat/{task_id}-{slug}"


@dataclass
class FakeAgentRunner:
    runs: list[TaskId] = field(default_factory=list)
    cancelled: list[TaskId] = field(default_factory=list)
    result: AgentResult = field(
        default_factory=lambda: AgentResult(success=True, summary="Done", files_changed=["main.py"])
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

    async def cancel(self, task_id: TaskId) -> None:
        self.cancelled.append(task_id)


@dataclass
class FakeVCS:
    opened_prs: list[tuple[str, str, str]] = field(default_factory=list)
    marked_ready: list[str] = field(default_factory=list)
    auto_merged: list[str] = field(default_factory=list)

    async def open_draft_pr(self, branch: str, title: str, body: str) -> str:
        self.opened_prs.append((branch, title, body))
        return "https://github.com/test/repo/pull/1"

    async def mark_ready_for_review(self, pr_url: str) -> None:
        self.marked_ready.append(pr_url)

    async def enable_auto_merge(self, pr_url: str) -> None:
        self.auto_merged.append(pr_url)

    async def get_pr_status(self, pr_url: str) -> dict[str, object]:
        return {"state": "open", "checks_passed": True, "mergeable": True}


# --- Fixtures ---


@pytest.fixture
def board() -> InMemoryProvider:
    return InMemoryProvider()


@pytest.fixture
def worktree() -> FakeWorktreeManager:
    return FakeWorktreeManager()


@pytest.fixture
def agent() -> FakeAgentRunner:
    return FakeAgentRunner()


@pytest.fixture
def vcs() -> FakeVCS:
    return FakeVCS()


@pytest.fixture
def handler(
    board: InMemoryProvider,
    worktree: FakeWorktreeManager,
    agent: FakeAgentRunner,
    vcs: FakeVCS,
) -> OnTaskMoved:
    return OnTaskMoved(board=board, worktree=worktree, agent=agent, vcs=vcs)


# --- Tests ---


async def test_todo_to_in_progress_triggers_worktree_and_agent(
    board: InMemoryProvider,
    worktree: FakeWorktreeManager,
    agent: FakeAgentRunner,
    vcs: FakeVCS,
    handler: OnTaskMoved,
) -> None:
    board.seed_task(task_id="t1", title="Add health endpoint", column=Column.TODO)
    event = board.simulate_move("t1", Column.TODO, Column.IN_PROGRESS)
    await handler(event)
    await asyncio.sleep(0.1)  # let background task run

    assert len(worktree.created) == 1
    assert worktree.created[0][0] == TaskId("t1")
    assert len(agent.runs) == 1
    assert len(vcs.opened_prs) == 1
    assert len(worktree.pushed) == 1


async def test_review_to_done_is_noop(
    board: InMemoryProvider,
    vcs: FakeVCS,
    handler: OnTaskMoved,
) -> None:
    board.seed_task(task_id="t2", title="Fix bug", column=Column.REVIEW)
    event = board.simulate_move("t2", Column.REVIEW, Column.DONE)
    await handler(event)

    assert len(vcs.marked_ready) == 0
    assert len(vcs.auto_merged) == 0


async def test_any_to_backlog_cancels_agent(
    board: InMemoryProvider,
    agent: FakeAgentRunner,
    handler: OnTaskMoved,
) -> None:
    board.seed_task(task_id="t3", column=Column.IN_PROGRESS)
    event = board.simulate_move("t3", Column.IN_PROGRESS, Column.BACKLOG)
    await handler(event)

    assert agent.cancelled == [TaskId("t3")]


async def test_todo_auto_promotes_to_in_progress(
    board: InMemoryProvider,
    worktree: FakeWorktreeManager,
    agent: FakeAgentRunner,
    vcs: FakeVCS,
    handler: OnTaskMoved,
) -> None:
    board.seed_task(task_id="t4", column=Column.BACKLOG)
    event = board.simulate_move("t4", Column.BACKLOG, Column.TODO)
    await handler(event)

    # Card moved to InProgress but agent NOT triggered yet (polling will do that)
    task = await board.get_task(TaskId("t4"))
    assert task is not None
    assert task.column == Column.IN_PROGRESS
    assert len(agent.runs) == 0


async def test_done_transition_is_noop(
    board: InMemoryProvider,
    worktree: FakeWorktreeManager,
    agent: FakeAgentRunner,
    vcs: FakeVCS,
    handler: OnTaskMoved,
) -> None:
    board.seed_task(task_id="t5", column=Column.REVIEW)
    event = board.simulate_move("t5", Column.REVIEW, Column.DONE)
    await handler(event)

    assert len(worktree.created) == 0
    assert len(agent.runs) == 0
