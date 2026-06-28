"""Tests for GitHubClient (gh CLI wrapper)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agents_trello.adapters.github.client import (
    GitHubClient,
    PRCreateError,
    PRMergeError,
    PRReadyError,
    PRStatusError,
)


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient(repo="owner/repo")


def _make_process(stdout: str = "", stderr: str = "", returncode: int = 0) -> AsyncMock:
    """Return an ``AsyncMock`` that behaves like an ``asyncio.subprocess.Process``."""
    proc = AsyncMock()
    proc.communicate.return_value = (stdout.encode(), stderr.encode())
    proc.returncode = returncode
    return proc


# ------------------------------------------------------------------
# open_draft_pr
# ------------------------------------------------------------------


async def test_open_draft_pr(client: GitHubClient) -> None:
    pr_url = "https://github.com/owner/repo/pull/42"
    proc = _make_process(stdout=f"Creating PR...\n{pr_url}\n")

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        url = await client.open_draft_pr("feat/my-branch", "Add feature", "PR body text")

    assert url == pr_url

    mock_exec.assert_called_once_with(
        "gh",
        "pr",
        "create",
        "--repo",
        "owner/repo",
        "--base",
        "main",
        "--head",
        "feat/my-branch",
        "--title",
        "Add feature",
        "--body",
        "PR body text",
        stdout=-1,
        stderr=-1,
    )


async def test_open_draft_pr_failure(client: GitHubClient) -> None:
    proc = _make_process(stderr="error: not found", returncode=1)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(PRCreateError) as exc_info:
            await client.open_draft_pr("feat/x", "Title", "Body")

    assert exc_info.value.returncode == 1
    assert "not found" in exc_info.value.stderr


# ------------------------------------------------------------------
# mark_ready_for_review
# ------------------------------------------------------------------


async def test_mark_ready(client: GitHubClient) -> None:
    proc = _make_process()

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        await client.mark_ready_for_review("https://github.com/owner/repo/pull/42")

    mock_exec.assert_called_once_with(
        "gh",
        "pr",
        "ready",
        "https://github.com/owner/repo/pull/42",
        stdout=-1,
        stderr=-1,
    )


async def test_mark_ready_failure(client: GitHubClient) -> None:
    proc = _make_process(stderr="not a draft", returncode=1)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(PRReadyError):
            await client.mark_ready_for_review("https://github.com/owner/repo/pull/42")


# ------------------------------------------------------------------
# enable_auto_merge
# ------------------------------------------------------------------


async def test_enable_auto_merge(client: GitHubClient) -> None:
    proc = _make_process()

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        await client.enable_auto_merge("https://github.com/owner/repo/pull/42")

    mock_exec.assert_called_once_with(
        "gh",
        "pr",
        "merge",
        "https://github.com/owner/repo/pull/42",
        "--auto",
        "--squash",
        stdout=-1,
        stderr=-1,
    )


async def test_enable_auto_merge_failure(client: GitHubClient) -> None:
    proc = _make_process(stderr="merge conflict", returncode=1)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(PRMergeError):
            await client.enable_auto_merge("https://github.com/owner/repo/pull/42")


# ------------------------------------------------------------------
# get_pr_status
# ------------------------------------------------------------------


async def test_get_pr_status(client: GitHubClient) -> None:
    status_json = json.dumps(
        {
            "state": "OPEN",
            "statusCheckRollup": [{"state": "SUCCESS"}],
            "mergeable": "MERGEABLE",
        }
    )
    proc = _make_process(stdout=status_json)

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        result = await client.get_pr_status("https://github.com/owner/repo/pull/42")

    assert result["state"] == "OPEN"
    assert result["mergeable"] == "MERGEABLE"
    assert isinstance(result["statusCheckRollup"], list)

    mock_exec.assert_called_once_with(
        "gh",
        "pr",
        "view",
        "https://github.com/owner/repo/pull/42",
        "--json",
        "state,statusCheckRollup,mergeable",
        stdout=-1,
        stderr=-1,
    )


async def test_get_pr_status_failure(client: GitHubClient) -> None:
    proc = _make_process(stderr="not found", returncode=1)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(PRStatusError):
            await client.get_pr_status("https://github.com/owner/repo/pull/42")
