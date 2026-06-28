"""Handlers for task column transitions."""

from __future__ import annotations

import asyncio
import logging
import re

from agents_trello.domain.events import TaskMoved
from agents_trello.domain.handlers.ports import VCS, AgentRunner, WorktreeManager
from agents_trello.domain.models import Column
from agents_trello.domain.provider import BoardProvider

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "claude: "


def _slugify(title: str, max_len: int = 40) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len].rstrip("-")


class OnTaskMoved:
    def __init__(
        self,
        board: BoardProvider,
        worktree: WorktreeManager,
        agent: AgentRunner,
        vcs: VCS,
        notify: object | None = None,
        deploy: object | None = None,
        event_log: object | None = None,
    ) -> None:
        self._board = board
        self._worktree = worktree
        self._agent = agent
        self._vcs = vcs
        self._notify = notify
        self._deploy = deploy
        self._event_log = event_log
        self._active: set[str] = set()

    async def _log(self, event_type: str, **kwargs: object) -> None:
        if self._event_log:
            await self._event_log.record(event_type, **kwargs)

    async def _set_label(self, task_id: object, working: bool) -> None:
        method = "set_agent_working" if working else "clear_agent_working"
        fn = getattr(self._board, method, None)
        if fn is not None:
            try:
                await fn(task_id)
            except Exception:
                logger.debug("label_%s_failed", method, exc_info=True)

    async def _comment(self, task_id: object, text: str) -> None:
        await self._board.post_comment(task_id, f"{COMMENT_PREFIX}{text}")  # type: ignore[arg-type]

    async def __call__(self, event: TaskMoved) -> None:
        logger.info(
            "task_moved",
            extra={
                "task_id": event.task_id,
                "from": event.from_column.value,
                "to": event.to_column.value,
            },
        )

        if event.to_column == Column.BACKLOG:
            await self._handle_move_to_backlog(event)
        elif event.to_column == Column.TODO:
            logger.info("auto_promote_todo", extra={"task_id": event.task_id})
            await self._board.move_task(event.task_id, Column.IN_PROGRESS)
        elif event.to_column == Column.IN_PROGRESS:
            task_key = str(event.task_id)
            if task_key in self._active:
                logger.info("skipping_already_active", extra={"task_id": event.task_id})
                return
            # Check if agent is already working (label on card)
            task = await self._board.get_task(event.task_id)
            if task and "agent working" in task.labels:
                logger.info("skipping_agent_working_label", extra={"task_id": event.task_id})
                return
            # Launch agent work in background — don't block the poll loop
            asyncio.create_task(self._handle_move_to_in_progress(event))
        else:
            logger.info(
                "unhandled_column_transition",
                extra={"to": event.to_column.value},
            )

    async def _handle_move_to_backlog(self, event: TaskMoved) -> None:
        logger.info("cancelling_agent", extra={"task_id": event.task_id})
        await self._set_label(event.task_id, False)
        await self._agent.cancel(event.task_id)
        await self._log("agent_cancelled", task_id=str(event.task_id))

    async def _handle_move_to_in_progress(self, event: TaskMoved) -> None:
        task_key = str(event.task_id)
        self._active.add(task_key)
        try:
            task = await self._board.get_task(event.task_id)
            if task is None:
                logger.error("task_not_found", extra={"task_id": event.task_id})
                return

            await self._set_label(event.task_id, True)
            await self._comment(event.task_id, "Starting work on this task...")
            if self._notify:
                await self._notify.agent_started(task.title)

            slug = _slugify(task.title)
            worktree_path = await self._worktree.create(event.task_id, slug)
            branch = self._worktree.get_branch_name(event.task_id, slug)

            get_attachments = getattr(self._board, "get_text_attachments", None)
            attachments = await get_attachments(event.task_id) if get_attachments else []

            await self._log(
                "agent_started",
                task_id=task_key,
                task_title=task.title,
                branch=branch,
                details={
                    "labels": task.labels,
                    "worktree": str(worktree_path),
                    "attachments": [a[0] for a in attachments],
                },
            )

            result = await self._agent.run_initial(
                task_id=event.task_id,
                worktree_path=worktree_path,
                task_title=task.title,
                task_description=task.description,
                labels=task.labels,
                attachments=attachments,
            )

            await self._set_label(event.task_id, False)

            if result.success:
                await self._worktree.push(event.task_id)
                if self._notify:
                    await self._notify.branch_pushed(task.title, branch)
                pr_url = await self._vcs.open_draft_pr(
                    branch=branch,
                    title=task.title,
                    body=result.summary,
                )
                await self._comment(
                    event.task_id,
                    f"Done! PR opened: {pr_url}\n\n{result.summary}",
                )
                if self._notify:
                    await self._notify.pr_opened(task.title, pr_url)
                await self._board.move_task(event.task_id, Column.REVIEW)
                await self._log(
                    "agent_completed",
                    task_id=task_key,
                    task_title=task.title,
                    branch=branch,
                    details={
                        "pr_url": pr_url,
                        "files_changed": result.files_changed,
                    },
                    output=result.summary[:2000],
                )
            else:
                await self._comment(event.task_id, f"Failed: {result.error}")
                if self._notify:
                    await self._notify.agent_failed(task.title, result.error or "Unknown error")
                await self._log(
                    "agent_failed",
                    task_id=task_key,
                    task_title=task.title,
                    branch=branch,
                    details={"error": result.error},
                    output=result.summary[:2000],
                )
        except Exception:
            logger.exception("agent_work_error", extra={"task_id": task_key})
        finally:
            self._active.discard(task_key)
