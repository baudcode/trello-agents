"""Handlers for comment events (review loop)."""

from __future__ import annotations

import asyncio
import logging
import re

from agents_trello.domain.events import CommentAdded
from agents_trello.domain.handlers.ports import AgentRunner, WorktreeManager
from agents_trello.domain.models import Column
from agents_trello.domain.provider import BoardProvider

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "claude: "


class OnCommentAdded:
    def __init__(
        self,
        board: BoardProvider,
        agent: AgentRunner,
        worktree: WorktreeManager,
        notify: object | None = None,
        event_log: object | None = None,
    ) -> None:
        self._board = board
        self._agent = agent
        self._worktree = worktree
        self._notify = notify
        self._event_log = event_log

    async def _log(self, event_type: str, **kwargs: object) -> None:
        if self._event_log:
            await self._event_log.record(event_type, **kwargs)

    async def _comment(self, task_id: object, text: str) -> None:
        await self._board.post_comment(task_id, f"{COMMENT_PREFIX}{text}")  # type: ignore[arg-type]

    async def _set_label(self, task_id: object, working: bool) -> None:
        method = "set_agent_working" if working else "clear_agent_working"
        fn = getattr(self._board, method, None)
        if fn is not None:
            try:
                await fn(task_id)
            except Exception:
                logger.debug("label_%s_failed", method, exc_info=True)

    async def __call__(self, event: CommentAdded) -> None:
        # Ignore comments from the agent itself (prefixed with "claude: ")
        if event.text.startswith(COMMENT_PREFIX):
            logger.debug("ignoring_agent_comment", extra={"task_id": event.task_id})
            return

        task = await self._board.get_task(event.task_id)
        if task is None:
            logger.error("task_not_found", extra={"task_id": event.task_id})
            return

        # Only handle human comments on actionable cards
        if task.column not in (Column.BACKLOG, Column.REVIEW, Column.IN_PROGRESS):
            logger.debug(
                "ignoring_comment_on_inactive_card",
                extra={"task_id": event.task_id, "column": task.column.value},
            )
            return

        logger.info(
            "human_comment",
            extra={
                "task_id": event.task_id,
                "author": event.author_name,
                "column": task.column.value,
                "text": event.text[:100],
            },
        )

        # Backlog: just answer the question (no worktree/branch/PR)
        # Review/InProgress: full review loop with code changes
        if task.column == Column.BACKLOG:
            asyncio.create_task(self._handle_chat(event, task))
        else:
            asyncio.create_task(self._handle_review(event, task))

    async def _handle_chat(self, event: CommentAdded, task: object) -> None:
        """Answer a question on a Backlog card without code changes."""
        try:
            await self._set_label(event.task_id, True)
            await self._comment(event.task_id, "Thinking about this...")

            comments = await self._board.get_comments(event.task_id)
            comment_texts = [c.text for c in comments]

            get_attachments = getattr(self._board, "get_text_attachments", None)
            attachments = await get_attachments(event.task_id) if get_attachments else []

            run_chat = getattr(self._agent, "run_chat", None)
            if run_chat is None:
                logger.warning("agent_has_no_run_chat")
                return

            result = await run_chat(
                task_id=event.task_id,
                task_title=task.title,  # type: ignore[union-attr]
                task_description=task.description,  # type: ignore[union-attr]
                comments=comment_texts,
                labels=task.labels,  # type: ignore[union-attr]
                attachments=attachments,
            )

            await self._set_label(event.task_id, False)

            if result.success and result.summary:
                await self._comment(event.task_id, result.summary)
            elif result.error:
                await self._comment(event.task_id, f"Failed: {result.error}")
        except Exception:
            logger.exception("chat_work_error", extra={"task_id": str(event.task_id)})
            await self._set_label(event.task_id, False)

    async def _handle_review(self, event: CommentAdded, task: object) -> None:
        """Run the review agent in the background."""
        try:
            await self._set_label(event.task_id, True)
            clear_deployed = getattr(self._board, "clear_deployed", None)
            if clear_deployed:
                await clear_deployed(event.task_id)
            await self._comment(event.task_id, "Got your feedback, working on it...")

            await self._log(
                "review_started",
                task_id=str(event.task_id),
                task_title=task.title,  # type: ignore[union-attr]
                details={"author": event.author_name, "comment": event.text[:200]},
            )

            comments = await self._board.get_comments(event.task_id)
            comment_texts = [c.text for c in comments]

            get_attachments = getattr(self._board, "get_text_attachments", None)
            attachments = await get_attachments(event.task_id) if get_attachments else []

            slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:40].rstrip("-")  # type: ignore[union-attr]
            worktree_path = await self._worktree.create(event.task_id, slug)

            result = await self._agent.run_review(
                task_id=event.task_id,
                worktree_path=worktree_path,
                task_title=task.title,
                task_description=task.description,
                comments=comment_texts,
                labels=task.labels,
                attachments=attachments,
            )

            await self._set_label(event.task_id, False)

            if result.success:
                await self._comment(event.task_id, f"Changes applied:\n\n{result.summary}")
                if self._notify:
                    card_url = f"https://trello.com/c/{event.task_id}"
                    await self._notify.review_responded(task.title, card_url)  # type: ignore[union-attr]
                await self._log(
                    "review_completed",
                    task_id=str(event.task_id),
                    task_title=task.title,  # type: ignore[union-attr]
                    details={"files_changed": result.files_changed},
                    output=result.summary[:2000],
                )
            else:
                await self._comment(event.task_id, f"Failed to apply changes: {result.error}")
                if self._notify:
                    await self._notify.agent_failed(task.title, result.error or "Unknown error")  # type: ignore[union-attr]
                await self._log(
                    "review_failed",
                    task_id=str(event.task_id),
                    task_title=task.title,  # type: ignore[union-attr]
                    details={"error": result.error},
                )
        except Exception:
            logger.exception("review_work_error", extra={"task_id": str(event.task_id)})
