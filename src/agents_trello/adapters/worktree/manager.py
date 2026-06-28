"""Git worktree management."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from agents_trello.domain.models import TaskId

logger = logging.getLogger(__name__)


class PushError(Exception):
    """Raised when a git push operation fails."""

    def __init__(self, task_id: TaskId, stderr: str) -> None:
        self.task_id = task_id
        self.stderr = stderr
        super().__init__(f"Push failed for task {task_id}: {stderr}")


def slugify(title: str, max_len: int = 40) -> str:
    """Lowercase, replace non-alphanum with dashes, strip leading/trailing dashes, truncate."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug


async def _run_git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    stdout_b, stderr_b = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode(), stderr_b.decode()


class GitWorktreeManager:
    """Manages git worktrees for task branches.

    On first use, clones the repo to ``~/.trello/<repo_name>/`` if it
    doesn't already exist.  All worktree operations run against that clone.
    """

    def __init__(
        self,
        github_repo: str,
        worktree_base_dir: Path | None = None,
        clone_base_dir: Path | None = None,
    ) -> None:
        self._github_repo = github_repo  # e.g. "owner/repo"
        repo_name = github_repo.split("/")[-1] if "/" in github_repo else github_repo
        self._clone_dir = (clone_base_dir or Path.home() / ".trello") / repo_name
        self._worktree_base = worktree_base_dir or (self._clone_dir / "worktrees")
        self._worktrees: dict[TaskId, tuple[Path, str]] = {}
        self._clone_ready = False

    async def ensure_repo(self) -> Path:
        """Clone the repo if needed, pull latest main. Returns the clone dir."""
        if not self._clone_ready:
            if not (self._clone_dir / ".git").exists():
                logger.info("Cloning %s to %s", self._github_repo, self._clone_dir)
                self._clone_dir.parent.mkdir(parents=True, exist_ok=True)
                url = f"git@github.com:{self._github_repo}.git"
                rc, _, stderr = await _run_git("clone", url, str(self._clone_dir))
                if rc != 0:
                    raise RuntimeError(f"Clone failed: {stderr}")
            else:
                logger.info("Repo already cloned at %s", self._clone_dir)

            # Pull latest main
            rc, _, stderr = await _run_git("fetch", "origin", "main", cwd=self._clone_dir)
            if rc == 0:
                rc, _, stderr = await _run_git(
                    "reset", "--hard", "origin/main", cwd=self._clone_dir
                )
            if rc != 0:
                logger.warning("Pull main failed (non-fatal): %s", stderr)

            self._clone_ready = True
        return self._clone_dir

    async def _pull_latest(self, repo_dir: Path) -> None:
        """Fetch and reset main to origin/main."""
        rc, _, stderr = await _run_git("fetch", "origin", "main", cwd=repo_dir)
        if rc == 0:
            await _run_git("checkout", "main", cwd=repo_dir)
            await _run_git("reset", "--hard", "origin/main", cwd=repo_dir)
            logger.info("Pulled latest main")
        else:
            logger.warning("fetch origin main failed: %s", stderr)

    async def create(self, task_id: TaskId, slug: str, base: str = "main") -> Path:
        """Create a new worktree for the given task. Handles re-entry gracefully."""
        repo_dir = await self.ensure_repo()
        await self._pull_latest(repo_dir)
        branch = self.get_branch_name(task_id, slug)
        path = self._worktree_base / f"{task_id}-{slug}"
        path.parent.mkdir(parents=True, exist_ok=True)

        # If worktree already exists, reuse it
        if path.exists() and (path / ".git").exists():
            logger.info("Reusing existing worktree at %s", path)
            self._worktrees[task_id] = (path, branch)
            return path

        # Clean up stale branch if it exists from a previous failed attempt
        await _run_git("branch", "-D", branch, cwd=repo_dir)

        rc, _, stderr = await _run_git(
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            base,
            cwd=repo_dir,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create worktree for task {task_id}: {stderr}")

        self._worktrees[task_id] = (path, branch)
        return path

    async def remove(self, task_id: TaskId) -> None:
        """Remove the worktree for the given task."""
        repo_dir = await self.ensure_repo()
        path, _ = self._worktrees[task_id]

        rc, _, stderr = await _run_git(
            "worktree",
            "remove",
            str(path),
            "--force",
            cwd=repo_dir,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to remove worktree for task {task_id}: {stderr}")

        del self._worktrees[task_id]

    async def push(self, task_id: TaskId, remote: str = "origin") -> None:
        """Push the worktree branch to the remote."""
        path, branch = self._worktrees[task_id]

        rc, _, stderr = await _run_git("push", remote, branch, cwd=path)
        if rc != 0:
            raise PushError(task_id, stderr)

    def get_branch_name(self, task_id: TaskId, slug: str) -> str:
        """Return the branch name for a task."""
        return f"feat/{task_id}-{slug}"
