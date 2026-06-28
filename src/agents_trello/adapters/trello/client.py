"""Raw httpx wrapper for the Trello API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_BASE_DELAY = 1.0  # seconds


class TrelloList(BaseModel):
    id: str
    name: str
    closed: bool = False
    pos: float = 0


class TrelloLabel(BaseModel):
    id: str
    name: str
    color: str = ""


class TrelloAttachment(BaseModel):
    id: str
    name: str
    url: str
    bytes: int = 0
    mime_type: str | None = Field(alias="mimeType", default=None)

    model_config = {"populate_by_name": True}


class TrelloCard(BaseModel):
    id: str
    name: str
    desc: str = ""
    id_list: str = Field(alias="idList")
    id_short: int = Field(alias="idShort")
    short_url: str = Field(alias="shortUrl", default="")
    closed: bool = False
    labels: list[TrelloLabel] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class TrelloAction(BaseModel):
    id: str
    type: str
    date: datetime
    data: dict
    member_creator: dict | None = Field(alias="memberCreator", default=None)

    model_config = {"populate_by_name": True}


class TrelloClient:
    """Async HTTP client for Trello REST API v1."""

    def __init__(self, api_key: str, api_token: str) -> None:
        self._api_key = api_key
        self._api_token = api_token
        self._client = httpx.AsyncClient(
            base_url="https://api.trello.com/1/",
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=60,  # close idle connections after 60s
            ),
        )

    @property
    def _auth_params(self) -> dict[str, str]:
        return {"key": self._api_key, "token": self._api_token}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        """Issue an HTTP request with retry on 429 and transient connection errors."""
        merged_params = {**self._auth_params, **(params or {})}
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.request(
                    method,
                    url,
                    params=merged_params,
                    json=json,
                )
            except (
                httpx.ReadError,
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
                httpx.ReadTimeout,
            ) as exc:
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt)
                logger.warning(
                    "Trello connection error (%s), retrying in %.1fs (attempt %d)",
                    type(exc).__name__,
                    delay,
                    attempt + 1,
                )
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 429:
                delay = _BASE_DELAY * (2**attempt)
                logger.warning(
                    "Trello 429 rate-limit, retrying in %.1fs (attempt %d)", delay, attempt + 1
                )
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        # Final attempt — let any error propagate
        if last_exc is not None:
            raise last_exc
        resp = await self._client.request(method, url, params=merged_params, json=json)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Board helpers
    # ------------------------------------------------------------------

    async def get_board_id(self, board_id: str) -> str:
        """Resolve a short board ID/URL to the full board ID."""
        resp = await self._request("GET", f"boards/{board_id}", params={"fields": "id"})
        return resp.json()["id"]

    async def get_board_lists(self, board_id: str) -> list[TrelloList]:
        resp = await self._request("GET", f"boards/{board_id}/lists")
        return [TrelloList.model_validate(item) for item in resp.json()]

    async def get_cards(self, board_id: str) -> list[TrelloCard]:
        resp = await self._request("GET", f"boards/{board_id}/cards")
        return [TrelloCard.model_validate(item) for item in resp.json()]

    async def get_card(self, card_id: str) -> TrelloCard:
        resp = await self._request("GET", f"cards/{card_id}")
        return TrelloCard.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def get_card_actions(
        self,
        card_id: str,
        since: str | None = None,
    ) -> list[TrelloAction]:
        params: dict[str, str] = {}
        if since is not None:
            params["since"] = since
        resp = await self._request("GET", f"cards/{card_id}/actions", params=params)
        return [TrelloAction.model_validate(item) for item in resp.json()]

    async def get_board_actions(
        self,
        board_id: str,
        since: str | None = None,
    ) -> list[TrelloAction]:
        params: dict[str, str] = {}
        if since is not None:
            params["since"] = since
        resp = await self._request("GET", f"boards/{board_id}/actions", params=params)
        return [TrelloAction.model_validate(item) for item in resp.json()]

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    async def get_card_attachments(self, card_id: str) -> list[TrelloAttachment]:
        resp = await self._request("GET", f"cards/{card_id}/attachments")
        return [TrelloAttachment.model_validate(item) for item in resp.json()]

    async def download_attachment(self, url: str) -> bytes:
        """Download attachment content from a Trello attachment URL."""
        headers = {
            "Authorization": (
                f'OAuth oauth_consumer_key="{self._api_key}", oauth_token="{self._api_token}"'
            ),
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            resp = await c.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add_comment(self, card_id: str, text: str) -> None:
        await self._request("POST", f"cards/{card_id}/actions/comments", params={"text": text})

    async def move_card(self, card_id: str, list_id: str) -> None:
        await self._request("PUT", f"cards/{card_id}", params={"idList": list_id})

    async def update_card(self, card_id: str, **fields: str) -> None:
        await self._request("PUT", f"cards/{card_id}", params=fields)

    async def delete_card(self, card_id: str) -> None:
        await self._request("DELETE", f"cards/{card_id}")

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    async def get_board_labels(self, board_id: str) -> list[dict]:
        resp = await self._request("GET", f"boards/{board_id}/labels")
        return resp.json()

    async def create_label(self, board_id: str, name: str, color: str) -> str:
        resp = await self._request(
            "POST", "labels", params={"name": name, "color": color, "idBoard": board_id}
        )
        return resp.json()["id"]

    async def add_label_to_card(self, card_id: str, label_id: str) -> None:
        await self._request("POST", f"cards/{card_id}/idLabels", params={"value": label_id})

    async def remove_label_from_card(self, card_id: str, label_id: str) -> None:
        await self._request("DELETE", f"cards/{card_id}/idLabels/{label_id}")

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    async def create_webhook(self, board_id: str, callback_url: str) -> str:
        """Register a Trello webhook and return the webhook id."""
        resp = await self._request(
            "POST",
            "webhooks",
            params={
                "idModel": board_id,
                "callbackURL": callback_url,
            },
        )
        return resp.json()["id"]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()
