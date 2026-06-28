"""FastAPI router for Trello webhooks (multi-project: routed by board id)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, Header, Request, Response

from agents_trello.adapters.trello.signature import verify_signature
from agents_trello.domain.events import DomainEvent
from agents_trello.project import ProjectContext, ProjectRegistry

logger = logging.getLogger(__name__)

EventDispatcher = Callable[[list[DomainEvent], ProjectContext], None]


def create_trello_webhook_router(
    registry: ProjectRegistry,
    callback_url: str,
    webhook_secret: str,
    dispatch: EventDispatcher,
) -> APIRouter:
    """Build a router whose POST handler dispatches to the project matching the payload."""
    router = APIRouter()

    @router.head("/trello/webhook")
    async def trello_head() -> Response:
        return Response(status_code=200)

    @router.post("/trello/webhook")
    async def trello_post(
        request: Request,
        x_trello_webhook: str = Header(...),
    ) -> Response:
        body = await request.body()

        if not verify_signature(body, callback_url, webhook_secret, x_trello_webhook):
            logger.warning("Invalid Trello webhook signature")
            return Response(status_code=403)

        try:
            payload = json.loads(body)
        except ValueError:
            logger.warning("trello_webhook_invalid_json")
            return Response(status_code=400)

        board_id = _extract_board_id(payload)
        if not board_id:
            logger.warning("trello_webhook_missing_board_id")
            return Response(status_code=400)

        ctx = registry.get_by_board(board_id)
        if ctx is None:
            logger.warning("trello_webhook_unknown_board", extra={"board_id": board_id})
            return Response(status_code=404)

        events = ctx.provider.parse_webhook(dict(request.headers), body)
        if events:
            dispatch(events, ctx)
        return Response(status_code=200)

    return router


def _extract_board_id(payload: dict) -> str | None:
    """Pull the board id from a Trello webhook envelope."""
    action = payload.get("action") or {}
    data = action.get("data") or {}
    board = data.get("board") or {}
    board_id = board.get("id")
    if isinstance(board_id, str) and board_id:
        return board_id
    # Some webhook payloads carry a top-level "model" with the board info.
    model = payload.get("model") or {}
    if isinstance(model, dict) and model.get("id"):
        return str(model["id"])
    return None
