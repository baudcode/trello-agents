"""Tests for ClaudeAgentRunner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents_trello.adapters.agent.runner import ClaudeAgentRunner
from agents_trello.domain.models import TaskId


def _make_mock_proc(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> AsyncMock:
    """Create a mock subprocess with preset outputs."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (stdout, stderr)
    mock_proc.returncode = returncode
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    return mock_proc


async def test_runner_captures_output(tmp_path: Path) -> None:
    runner = ClaudeAgentRunner(timeout_seconds=10, max_concurrent=2)
    task_id = TaskId("T1")
    worktree = tmp_path / "T1-test-feature"
    worktree.mkdir()

    agent_proc = _make_mock_proc(stdout=b"Task completed successfully", returncode=0)
    diff_proc = _make_mock_proc(stdout=b"file1.py\nfile2.py\n", returncode=0)

    call_count = 0

    async def mock_create_subprocess(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return agent_proc
        return diff_proc

    with patch(
        "agents_trello.adapters.agent.runner.asyncio.create_subprocess_exec",
        side_effect=mock_create_subprocess,
    ):
        result = await runner.run_initial(task_id, worktree, "Test Feature", "Implement the thing")

    assert result.success is True
    assert result.summary == "Task completed successfully"
    assert result.files_changed == ["file1.py", "file2.py"]
    assert result.error is None


async def test_runner_timeout(tmp_path: Path) -> None:
    runner = ClaudeAgentRunner(timeout_seconds=1, max_concurrent=2)
    task_id = TaskId("T2")
    worktree = tmp_path / "T2-slow-task"
    worktree.mkdir()

    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    async def slow_communicate() -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return b"", b""

    mock_proc.communicate = slow_communicate

    with patch(
        "agents_trello.adapters.agent.runner.asyncio.create_subprocess_exec",
        return_value=mock_proc,
    ):
        result = await runner.run_initial(task_id, worktree, "Slow Task", "Takes too long")

    assert result.success is False
    assert "timed out" in (result.error or "")
    mock_proc.kill.assert_called_once()


async def test_cancel_kills_process(tmp_path: Path) -> None:
    runner = ClaudeAgentRunner(timeout_seconds=30, max_concurrent=2)
    task_id = TaskId("T3")
    worktree = tmp_path / "T3-cancel-me"
    worktree.mkdir()

    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    started = asyncio.Event()

    async def slow_communicate() -> tuple[bytes, bytes]:
        started.set()
        await asyncio.sleep(30)
        return b"", b""

    mock_proc.communicate = slow_communicate

    with patch(
        "agents_trello.adapters.agent.runner.asyncio.create_subprocess_exec",
        return_value=mock_proc,
    ):
        task = asyncio.create_task(
            runner.run_initial(task_id, worktree, "Cancel Task", "Will be cancelled")
        )

        # Wait for the process to start
        await asyncio.wait_for(started.wait(), timeout=5)

        # Cancel it
        await runner.cancel(task_id)

    mock_proc.kill.assert_called()

    # Clean up the task - it should resolve due to the kill
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_runner_nonzero_exit(tmp_path: Path) -> None:
    runner = ClaudeAgentRunner(timeout_seconds=10, max_concurrent=2)
    task_id = TaskId("T4")
    worktree = tmp_path / "T4-fail-task"
    worktree.mkdir()

    agent_proc = _make_mock_proc(
        stdout=b"partial output", stderr=b"something went wrong", returncode=1
    )

    with patch(
        "agents_trello.adapters.agent.runner.asyncio.create_subprocess_exec",
        return_value=agent_proc,
    ):
        result = await runner.run_initial(task_id, worktree, "Fail Task", "Will fail")

    assert result.success is False
    assert result.summary == "partial output"
    assert result.error == "something went wrong"
