"""GitHub VCS implementation via gh CLI."""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class GitHubClientError(Exception):
    """Base error for GitHub CLI operations."""

    def __init__(self, command: str, stderr: str, returncode: int) -> None:
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"gh command failed ({returncode}): {command}\n{stderr}")


class PRCreateError(GitHubClientError):
    """Raised when creating a pull request fails."""


class PRReadyError(GitHubClientError):
    """Raised when marking a PR ready for review fails."""


class PRMergeError(GitHubClientError):
    """Raised when enabling auto-merge fails."""


class PRStatusError(GitHubClientError):
    """Raised when fetching PR status fails."""


class GitHubClient:
    """VCS adapter that shells out to the ``gh`` CLI.

    Implements the :class:`~agents_trello.domain.handlers.ports.VCS` protocol.
    """

    def __init__(self, repo: str) -> None:
        self._repo = repo  # e.g. "owner/repo"

    async def _run(
        self,
        *args: str,
        error_cls: type[GitHubClientError] = GitHubClientError,
    ) -> str:
        """Run a ``gh`` sub-command and return its stdout.

        Raises *error_cls* when the process exits with a non-zero code.
        """
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            cmd_str = " ".join(("gh", *args))
            raise error_cls(
                command=cmd_str,
                stderr=stderr.decode(),
                returncode=proc.returncode,
            )

        return stdout.decode()

    # ------------------------------------------------------------------
    # VCS protocol
    # ------------------------------------------------------------------

    async def open_draft_pr(self, branch: str, title: str, body: str) -> str:
        """Create a pull request and return its URL."""
        raw = await self._run(
            "pr",
            "create",
            "--repo",
            self._repo,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            error_cls=PRCreateError,
        )
        # gh pr create prints the PR URL as the last line of stdout
        url = raw.strip().splitlines()[-1]
        logger.info("Opened PR %s for branch %s", url, branch)
        return url

    async def mark_ready_for_review(self, pr_url: str) -> None:
        """Remove draft status from a pull request."""
        await self._run(
            "pr",
            "ready",
            pr_url,
            error_cls=PRReadyError,
        )
        logger.info("Marked PR %s ready for review", pr_url)

    async def enable_auto_merge(self, pr_url: str) -> None:
        """Enable auto-merge (squash) on a pull request."""
        await self._run(
            "pr",
            "merge",
            pr_url,
            "--auto",
            "--squash",
            error_cls=PRMergeError,
        )
        logger.info("Enabled auto-merge for PR %s", pr_url)

    async def get_pr_status(self, pr_url: str) -> dict[str, object]:
        """Return PR state, check-rollup and mergeability."""
        raw = await self._run(
            "pr",
            "view",
            pr_url,
            "--json",
            "state,statusCheckRollup,mergeable",
            error_cls=PRStatusError,
        )
        result: dict[str, object] = json.loads(raw)
        return result

    async def get_recent_merged_prs(self, limit: int = 10) -> list[dict[str, str]]:
        """Get recently merged PRs on main."""
        raw = await self._run(
            "pr",
            "list",
            "--repo",
            self._repo,
            "--state",
            "merged",
            "--base",
            "main",
            "--json",
            "title,url,mergedAt,number",
            "--limit",
            str(limit),
        )
        return json.loads(raw)
