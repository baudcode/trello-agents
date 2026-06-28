"""Claude Code agent subprocess runner."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agents_trello.adapters.agent.prompts import (
    CHAT_PROMPT,
    INITIAL_WORK_PROMPT,
    REVIEW_PROMPT,
    format_attachments,
)
from agents_trello.domain.handlers.ports import AgentResult
from agents_trello.domain.models import TaskId

logger = logging.getLogger(__name__)


class ClaudeAgentRunner:
    """Runs Claude Code as a subprocess to implement tasks."""

    def __init__(
        self,
        timeout_seconds: int = 1800,
        max_concurrent: int = 4,
        api_base: str = "http://localhost:8000",
        board_id: str = "",
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: dict[TaskId, asyncio.subprocess.Process] = {}
        self._api_base = api_base
        self._board_id = board_id

    async def run_initial(
        self,
        task_id: TaskId,
        worktree_path: Path,
        task_title: str,
        task_description: str,
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult:
        """Run the initial work prompt in the worktree."""
        slug = worktree_path.name.split("-", 1)[1] if "-" in worktree_path.name else ""
        prompt = INITIAL_WORK_PROMPT.format(
            task_title=task_title,
            task_description=task_description,
            attachments=format_attachments(attachments or []),
            slug=slug,
            board_url=f"https://trello.com/b/{self._board_id}",
            card_url=f"https://trello.com/c/{task_id}",
            card_id=str(task_id),
            api_base=self._api_base,
        )
        logger.info(
            "agent_initial_start",
            extra={
                "task_id": task_id,
                "title": task_title,
                "labels": labels,
                "cwd": str(worktree_path),
            },
        )
        result = await self._run(task_id, worktree_path, prompt, labels=labels)
        logger.info(
            "agent_initial_done",
            extra={
                "task_id": task_id,
                "success": result.success,
                "files_changed": result.files_changed,
                "error": result.error,
                "summary_len": len(result.summary),
            },
        )
        if result.summary:
            logger.info("agent_output", extra={"task_id": task_id, "summary": result.summary[:500]})
        if result.error:
            logger.error("agent_error", extra={"task_id": task_id, "error": result.error[:500]})
        return result

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
        """Run the review prompt in the worktree."""
        slug = worktree_path.name.split("-", 1)[1] if "-" in worktree_path.name else ""
        formatted_comments = "\n".join(f"- {c}" for c in comments)
        prompt = REVIEW_PROMPT.format(
            task_title=task_title,
            task_description=task_description,
            attachments=format_attachments(attachments or []),
            comments=formatted_comments,
            slug=slug,
            board_url=f"https://trello.com/b/{self._board_id}",
            card_url=f"https://trello.com/c/{task_id}",
            card_id=str(task_id),
            api_base=self._api_base,
        )
        logger.info(
            "agent_review_start",
            extra={
                "task_id": task_id,
                "num_comments": len(comments),
                "labels": labels,
                "cwd": str(worktree_path),
            },
        )
        result = await self._run(task_id, worktree_path, prompt, labels=labels)
        logger.info(
            "agent_review_done",
            extra={
                "task_id": task_id,
                "success": result.success,
                "files_changed": result.files_changed,
                "error": result.error,
            },
        )
        if result.summary:
            logger.info("agent_output", extra={"task_id": task_id, "summary": result.summary[:500]})
        if result.error:
            logger.error("agent_error", extra={"task_id": task_id, "error": result.error[:500]})
        return result

    async def run_chat(
        self,
        task_id: TaskId,
        task_title: str,
        task_description: str,
        comments: list[str],
        labels: list[str] | None = None,
        attachments: list[tuple[str, str]] | None = None,
    ) -> AgentResult:
        """Answer a question without creating worktree/branch. For Backlog cards."""
        formatted_comments = "\n".join(f"- {c}" for c in comments)
        prompt = CHAT_PROMPT.format(
            task_title=task_title,
            task_description=task_description,
            attachments=format_attachments(attachments or []),
            comments=formatted_comments,
            slug="",
            board_url=f"https://trello.com/b/{self._board_id}",
            card_url=f"https://trello.com/c/{task_id}",
            card_id=str(task_id),
            api_base=self._api_base,
        )
        logger.info(
            "agent_chat_start",
            extra={"task_id": task_id, "title": task_title},
        )
        # Run in a temp directory (no worktree needed)
        result = await self._run(task_id, Path.cwd(), prompt, labels=labels)
        logger.info(
            "agent_chat_done",
            extra={"task_id": task_id, "success": result.success},
        )
        if result.summary:
            logger.info(
                "agent_output",
                extra={"task_id": task_id, "summary": result.summary[:500]},
            )
        return result

    async def cancel(self, task_id: TaskId) -> None:
        """Cancel a running agent process for the given task."""
        proc = self._active.get(task_id)
        if proc is not None and proc.returncode is None:
            logger.info("agent_cancel", extra={"task_id": task_id})
            proc.kill()
            await proc.wait()
            self._active.pop(task_id, None)

    async def _run(
        self,
        task_id: TaskId,
        cwd: Path,
        prompt: str,
        labels: list[str] | None = None,
    ) -> AgentResult:
        """Execute claude CLI and capture output."""
        labels = labels or []

        # Build CLI args based on card labels
        cmd: list[str] = ["claude", "--print"]
        if "fast" in labels:
            cmd.extend(["--model", "sonnet"])
        # Effort level: low, medium, high, max
        if "max" in labels:
            cmd.extend(["--effort", "max"])
        elif "high" in labels:
            cmd.extend(["--effort", "high"])
        elif "medium" in labels:
            cmd.extend(["--effort", "medium"])
        elif "low" in labels:
            cmd.extend(["--effort", "low"])

        cmd.extend(["-p", prompt])

        async with self._semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                )
                self._active[task_id] = proc
                logger.info(
                    "agent_process_spawned",
                    extra={"task_id": task_id, "pid": proc.pid, "labels": labels},
                )

                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=self._timeout_seconds,
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    logger.error(
                        "agent_timeout",
                        extra={"task_id": task_id, "timeout": self._timeout_seconds},
                    )
                    return AgentResult(
                        success=False,
                        summary="",
                        files_changed=[],
                        error=f"Agent timed out after {self._timeout_seconds}s",
                    )

                stdout = stdout_bytes.decode() if stdout_bytes else ""
                stderr = stderr_bytes.decode() if stderr_bytes else ""

                if proc.returncode != 0:
                    return AgentResult(
                        success=False,
                        summary=stdout,
                        files_changed=[],
                        error=stderr or f"Agent exited with code {proc.returncode}",
                    )

                files_changed = await self._get_files_changed(cwd)

                return AgentResult(
                    success=True,
                    summary=stdout,
                    files_changed=files_changed,
                )
            finally:
                self._active.pop(task_id, None)

    @staticmethod
    async def _get_files_changed(cwd: Path) -> list[str]:
        """Get list of files changed from git diff."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "HEAD~1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout:
            return []
        return [line for line in stdout.decode().strip().split("\n") if line]
