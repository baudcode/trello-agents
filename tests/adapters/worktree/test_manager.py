"""Tests for GitWorktreeManager."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agents_trello.adapters.worktree.manager import (
    GitWorktreeManager,
    PushError,
    slugify,
)
from agents_trello.domain.models import TaskId


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "checkout", "-b", "main"],
    ]:
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    return repo


# ── slugify ──────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Hello World") == "hello-world"

    def test_unicode(self) -> None:
        assert slugify("Über cool Feäture") == "ber-cool-fe-ture"

    def test_very_long_title(self) -> None:
        result = slugify("a" * 100)
        assert len(result) <= 40

    def test_leading_trailing_dashes(self) -> None:
        assert slugify("---hello---") == "hello"

    def test_empty_string(self) -> None:
        assert slugify("") == ""

    def test_special_characters(self) -> None:
        assert slugify("fix: bug #123 (urgent!)") == "fix-bug-123-urgent"

    def test_max_len_no_trailing_dash(self) -> None:
        # If truncation would leave a trailing dash, it should be stripped
        result = slugify("abcde-fghij", max_len=6)
        assert result == "abcde"
        assert not result.endswith("-")


# ── branch naming ────────────────────────────────────────────────────


class TestBranchNaming:
    def test_get_branch_name(self) -> None:
        manager = GitWorktreeManager(github_repo="test/repo")
        task_id = TaskId("ABC123")
        branch = manager.get_branch_name(task_id, "my-feature")
        assert branch == "feat/ABC123-my-feature"

    def test_get_branch_name_short_id(self) -> None:
        manager = GitWorktreeManager(github_repo="test/repo")
        task_id = TaskId("42")
        branch = manager.get_branch_name(task_id, "fix-login")
        assert branch == "feat/42-fix-login"


# ── create & remove ─────────────────────────────────────────────────


async def test_create_and_remove_worktree(git_repo: Path, tmp_path: Path) -> None:
    worktree_dir = tmp_path / "worktrees"
    manager = GitWorktreeManager(
        github_repo="test/repo",
        worktree_base_dir=worktree_dir,
        clone_base_dir=tmp_path / "clones",
    )
    # Point the clone dir at our test repo so ensure_repo skips cloning
    manager._clone_dir = git_repo
    manager._clone_ready = True

    task_id = TaskId("T1")
    slug = "my-feature"

    path = await manager.create(task_id, slug)
    assert path.exists()
    assert path == worktree_dir / f"{task_id}-{slug}"

    await manager.remove(task_id)
    assert not path.exists()


# ── push failure ─────────────────────────────────────────────────────


async def test_push_failure_raises_error(tmp_path: Path) -> None:
    manager = GitWorktreeManager(github_repo="test/repo", worktree_base_dir=tmp_path)
    task_id = TaskId("T2")
    slug = "fail-push"
    branch = manager.get_branch_name(task_id, slug)
    wt_path = tmp_path / f"{task_id}-{slug}"
    wt_path.mkdir(parents=True)
    manager._worktrees[task_id] = (wt_path, branch)

    async def fake_run_git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
        return 1, "", "fatal: remote error"

    with patch("agents_trello.adapters.worktree.manager._run_git", side_effect=fake_run_git):
        with pytest.raises(PushError, match="Push failed"):
            await manager.push(task_id)
