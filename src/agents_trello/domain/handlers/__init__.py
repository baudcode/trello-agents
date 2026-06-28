"""Event dispatcher and handler registration."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from agents_trello.domain.events import DomainEvent
from agents_trello.infra.db import is_action_processed, mark_action_processed

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class Dispatcher:
    def __init__(
        self,
        conn: aiosqlite.Connection | None = None,
        board_id: str | None = None,
    ) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)
        self._conn = conn
        self._board_id = board_id

    def register(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def dispatch(self, event: DomainEvent) -> None:
        action_id = event.meta.action_id

        # Idempotency check: skip if already processed (requires a db connection).
        if self._conn is not None:
            if await is_action_processed(self._conn, action_id):
                logger.info(
                    "duplicate_action_skipped",
                    extra={"action_id": action_id, "board_id": self._board_id},
                )
                return

        event_type = type(event)
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            logger.warning("no_handler_registered", extra={"event_type": event_type.__name__})
            return
        for handler in handlers:
            await handler(event)

        # Mark action as processed after all handlers succeed.
        if self._conn is not None:
            await mark_action_processed(self._conn, action_id, board_id=self._board_id)
