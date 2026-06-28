"""BoardProvider implementation for Trello."""

from __future__ import annotations

import logging
from datetime import datetime

from agents_trello.adapters.trello.client import TrelloClient
from agents_trello.adapters.trello.mapping import action_to_events, list_name_to_column
from agents_trello.domain.events import DomainEvent
from agents_trello.domain.models import Column, Comment, Task, TaskId

logger = logging.getLogger(__name__)

AGENT_LABEL_NAME = "Agent Working"
AGENT_LABEL_COLOR = "yellow"
DEPLOYED_LABEL_NAME = "Deployed"
DEPLOYED_LABEL_COLOR = "green"


class TrelloBoardProvider:
    """Implements the BoardProvider protocol backed by a TrelloClient."""

    def __init__(
        self,
        client: TrelloClient,
        board_id: str,
        list_id_to_column: dict[str, Column],
        column_to_list_id: dict[Column, str],
        agent_label_id: str,
        deployed_label_id: str,
    ) -> None:
        self._client = client
        self._board_id = board_id
        self._list_id_to_column = list_id_to_column
        self._column_to_list_id = column_to_list_id
        self._agent_label_id = agent_label_id
        self._deployed_label_id = deployed_label_id

    @classmethod
    async def create(cls, client: TrelloClient, board_id: str) -> TrelloBoardProvider:
        """Factory: resolve Trello lists to columns, fail if any column is missing."""
        # Resolve to full board ID (short IDs don't work for all API calls)
        real_board_id = await client.get_board_id(board_id)
        lists = await client.get_board_lists(real_board_id)

        list_id_to_column: dict[str, Column] = {}
        column_to_list_id: dict[Column, str] = {}

        for tlist in lists:
            col = list_name_to_column(tlist.name)
            if col is not None:
                list_id_to_column[tlist.id] = col
                column_to_list_id[col] = tlist.id

        missing = [col for col in Column if col not in column_to_list_id]
        if missing:
            names = ", ".join(col.value for col in missing)
            raise RuntimeError(f"Board {board_id} is missing required columns: {names}")

        # Ensure "Agent Working" label exists
        labels = await client.get_board_labels(real_board_id)
        agent_label_id = ""
        for label in labels:
            if label.get("name") == AGENT_LABEL_NAME:
                agent_label_id = label["id"]
                break
        if not agent_label_id:
            agent_label_id = await client.create_label(
                real_board_id, AGENT_LABEL_NAME, AGENT_LABEL_COLOR
            )

        # Ensure "Deployed" label exists
        deployed_label_id = ""
        for label in labels:
            if label.get("name") == DEPLOYED_LABEL_NAME:
                deployed_label_id = label["id"]
                break
        if not deployed_label_id:
            deployed_label_id = await client.create_label(
                real_board_id, DEPLOYED_LABEL_NAME, DEPLOYED_LABEL_COLOR
            )

        return cls(
            client,
            real_board_id,
            list_id_to_column,
            column_to_list_id,
            agent_label_id,
            deployed_label_id,
        )

    # ------------------------------------------------------------------
    # Label helpers
    # ------------------------------------------------------------------

    async def set_agent_working(self, task_id: TaskId) -> None:
        await self._client.add_label_to_card(task_id, self._agent_label_id)

    async def clear_agent_working(self, task_id: TaskId) -> None:
        try:
            await self._client.remove_label_from_card(task_id, self._agent_label_id)
        except Exception:
            logger.warning("clear_agent_working_failed", extra={"task_id": task_id}, exc_info=True)

    async def set_deployed(self, task_id: TaskId) -> None:
        await self._client.add_label_to_card(task_id, self._deployed_label_id)

    async def clear_deployed(self, task_id: TaskId) -> None:
        try:
            await self._client.remove_label_from_card(task_id, self._deployed_label_id)
        except Exception:
            logger.warning("clear_deployed_failed", extra={"task_id": task_id}, exc_info=True)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    _ALLOWED_EXTENSIONS = {".py", ".md", ".txt", ".dart", ".html"}
    _MAX_ATTACHMENT_BYTES = 100_000  # 100 KB

    async def get_text_attachments(self, task_id: TaskId) -> list[tuple[str, str]]:
        """Fetch text file attachments from a card. Returns [(filename, content)]."""
        attachments = await self._client.get_card_attachments(task_id)
        results: list[tuple[str, str]] = []
        for att in attachments:
            ext = "." + att.name.rsplit(".", 1)[-1].lower() if "." in att.name else ""
            if ext not in self._ALLOWED_EXTENSIONS:
                continue
            if att.bytes > self._MAX_ATTACHMENT_BYTES:
                continue
            try:
                data = await self._client.download_attachment(att.url)
                results.append((att.name, data.decode("utf-8")))
            except Exception:
                logger.warning(
                    "attachment_download_failed", extra={"name": att.name}, exc_info=True
                )
                continue
        return results

    # ------------------------------------------------------------------
    # BoardProvider interface
    # ------------------------------------------------------------------

    async def list_tasks(self) -> list[Task]:
        cards = await self._client.get_cards(self._board_id)
        tasks: list[Task] = []
        for card in cards:
            col = self._list_id_to_column.get(card.id_list)
            if col is None:
                continue
            card_labels = [lbl.name.lower() for lbl in card.labels if lbl.name]
            tasks.append(
                Task(
                    id=TaskId(card.id),
                    short_id=str(card.id_short),
                    title=card.name,
                    description=card.desc,
                    column=col,
                    labels=card_labels,
                )
            )
        return tasks

    async def get_task(self, task_id: TaskId) -> Task | None:
        try:
            card = await self._client.get_card(task_id)
        except Exception:
            logger.warning("get_task_failed", extra={"task_id": task_id}, exc_info=True)
            return None
        col = self._list_id_to_column.get(card.id_list)
        if col is None:
            return None
        return Task(
            id=TaskId(card.id),
            short_id=str(card.id_short),
            title=card.name,
            description=card.desc,
            column=col,
            labels=[lbl.name.lower() for lbl in card.labels if lbl.name],
        )

    async def get_comments(
        self,
        task_id: TaskId,
        since: str | None = None,
    ) -> list[Comment]:
        actions = await self._client.get_card_actions(task_id, since=since)
        comments: list[Comment] = []
        for act in actions:
            if act.type != "commentCard":
                continue
            member = act.member_creator or {}
            comments.append(
                Comment(
                    id=act.data.get("action", {}).get("id", act.id),
                    task_id=task_id,
                    author_id=member.get("id", ""),
                    author_name=member.get("fullName", member.get("username", "")),
                    text=act.data.get("text", ""),
                    created_at=act.date,
                )
            )
        return comments

    async def post_comment(self, task_id: TaskId, text: str) -> None:
        await self._client.add_comment(task_id, text)

    async def move_task(self, task_id: TaskId, column: Column) -> None:
        list_id = self._column_to_list_id[column]
        await self._client.move_card(task_id, list_id)

    async def update_description(self, task_id: TaskId, description: str) -> None:
        await self._client.update_card(task_id, desc=description)

    async def create_task(
        self,
        title: str,
        description: str = "",
        column: Column = Column.BACKLOG,
    ) -> Task:
        list_id = self._column_to_list_id[column]
        resp = await self._client._request(
            "POST",
            "cards",
            params={"name": title, "desc": description, "idList": list_id},
        )
        card = resp.json()
        return Task(
            id=TaskId(card["id"]),
            short_id=str(card["idShort"]),
            title=card["name"],
            description=card.get("desc", ""),
            column=column,
            labels=[],
        )

    async def delete_task(self, task_id: TaskId) -> None:
        await self._client.delete_card(task_id)

    async def poll_events(
        self,
        since_cursor: str | None = None,
    ) -> tuple[list[DomainEvent], str]:
        actions = await self._client.get_board_actions(self._board_id, since=since_cursor)
        events: list[DomainEvent] = []
        for act in actions:
            events.extend(action_to_events(act.model_dump(), self._list_id_to_column))

        # Cursor = ISO timestamp of the newest action (or pass-through old cursor)
        if actions:
            newest_ts: datetime = max(a.date for a in actions)
            new_cursor = newest_ts.isoformat()
        else:
            new_cursor = since_cursor or ""

        return events, new_cursor

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[DomainEvent]:
        """Parse a raw Trello webhook payload into domain events."""
        import json

        payload = json.loads(body)
        action = payload.get("action", {})
        if not action:
            return []
        return action_to_events(action, self._list_id_to_column)
