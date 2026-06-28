"""Startup polling: fetch missed actions since cursor, and resume work on existing cards."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

from agents_trello.domain.events import CommentAdded, EventMeta, TaskMoved
from agents_trello.domain.handlers import Dispatcher
from agents_trello.domain.models import Column
from agents_trello.domain.provider import BoardProvider
from agents_trello.infra.db import get_cursor, set_cursor

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "claude: "


async def run_backfill(
    provider: BoardProvider,
    dispatcher: Dispatcher,
    conn: aiosqlite.Connection,
    board_id: str,
) -> None:
    """Fetch and dispatch events missed since the last known cursor."""
    cursor = await get_cursor(conn, board_id)
    logger.info("backfill_start", extra={"cursor": cursor, "board_id": board_id})

    events, new_cursor = await provider.poll_events(since_cursor=cursor)
    logger.info("backfill_fetched", extra={"count": len(events), "board_id": board_id})

    for event in events:
        try:
            await dispatcher.dispatch(event)
        except Exception:
            logger.exception("backfill_handler_error", extra={"event": type(event).__name__})

    if new_cursor:
        await set_cursor(conn, new_cursor, board_id)
        logger.info(
            "backfill_cursor_updated",
            extra={"new_cursor": new_cursor, "board_id": board_id},
        )


async def resume_existing_cards(
    provider: BoardProvider,
    dispatcher: Dispatcher,
) -> None:
    """Scan all cards and resume work based on their current column.

    - Todo cards: moved to InProgress, then agent triggered directly.
    - InProgress cards: agent triggered directly.
    - Review cards with an unanswered human comment: review loop triggered.

    After processing, the cursor is advanced so the polling loop doesn't
    re-process the Trello actions we just caused.
    """
    tasks = await provider.list_tasks()
    logger.info("resume_scan", extra={"total_cards": len(tasks)})

    for task in tasks:
        # Any card with "Agent Working" label = previous agent was killed.
        # Clear the label. For Backlog cards, just clear — don't move.
        # For other cards, re-trigger the agent.
        if "agent working" in task.labels:
            clear_fn = getattr(provider, "clear_agent_working", None)
            if clear_fn:
                await clear_fn(task.id)
            logger.info(
                "resume_cleared_stale_label",
                extra={"task_id": task.id, "title": task.title, "column": task.column.value},
            )
            if task.column == Column.BACKLOG:
                # Backlog cards were just chatting — no need to re-trigger
                continue
            # Move to InProgress if not already there
            if task.column != Column.IN_PROGRESS:
                await provider.move_task(task.id, Column.IN_PROGRESS)
            event = TaskMoved(
                meta=EventMeta(
                    action_id=f"resume-{uuid.uuid4().hex}",
                    timestamp=datetime.now(UTC),
                ),
                task_id=task.id,
                from_column=task.column,
                to_column=Column.IN_PROGRESS,
            )
            try:
                await dispatcher.dispatch(event)
            except Exception:
                logger.exception(
                    "resume_handler_error",
                    extra={"task_id": task.id, "title": task.title},
                )
            continue

        if task.column == Column.TODO:
            logger.info(
                "resume_todo",
                extra={"task_id": task.id, "title": task.title},
            )
            await provider.move_task(task.id, Column.IN_PROGRESS)

        elif task.column == Column.IN_PROGRESS:
            logger.info(
                "resume_in_progress",
                extra={"task_id": task.id, "title": task.title},
            )
            event = TaskMoved(
                meta=EventMeta(
                    action_id=f"resume-{uuid.uuid4().hex}",
                    timestamp=datetime.now(UTC),
                ),
                task_id=task.id,
                from_column=Column.TODO,
                to_column=Column.IN_PROGRESS,
            )
            try:
                await dispatcher.dispatch(event)
            except Exception:
                logger.exception(
                    "resume_handler_error",
                    extra={"task_id": task.id, "title": task.title},
                )

        elif task.column == Column.REVIEW:
            # Check if the last comment is from a human (needs agent response)
            comments = await provider.get_comments(task.id)
            if not comments:
                continue
            last_comment = comments[0]  # most recent first
            if last_comment.text.startswith(COMMENT_PREFIX):
                continue  # agent already responded

            logger.info(
                "resume_review_feedback",
                extra={
                    "task_id": task.id,
                    "title": task.title,
                    "last_comment": last_comment.text[:80],
                },
            )
            event = CommentAdded(
                meta=EventMeta(
                    action_id=f"resume-{uuid.uuid4().hex}",
                    timestamp=datetime.now(UTC),
                ),
                task_id=task.id,
                comment_id=last_comment.id,
                author_id=last_comment.author_id,
                author_name=last_comment.author_name,
                text=last_comment.text,
            )
            try:
                await dispatcher.dispatch(event)
            except Exception:
                logger.exception(
                    "resume_review_error",
                    extra={"task_id": task.id, "title": task.title},
                )
