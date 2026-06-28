"""Trello list-name/action-type to domain type mapping."""

from __future__ import annotations

from datetime import datetime

from agents_trello.domain.events import (
    CommentAdded,
    DomainEvent,
    EventMeta,
    TaskCreated,
    TaskMoved,
    TaskUpdated,
)
from agents_trello.domain.models import Column, TaskId

# Normalised lowercase name -> Column
_NAME_MAP: dict[str, Column] = {col.value.lower(): col for col in Column}


def list_name_to_column(name: str) -> Column | None:
    """Map a Trello list name to a domain Column (case-insensitive)."""
    return _NAME_MAP.get(name.strip().lower())


def _meta(action: dict) -> EventMeta:
    raw_date = action["date"]
    ts = raw_date if isinstance(raw_date, datetime) else datetime.fromisoformat(raw_date)
    return EventMeta(
        action_id=action["id"],
        timestamp=ts,
    )


def action_to_events(
    action: dict,
    list_id_to_column: dict[str, Column],
) -> list[DomainEvent]:
    """Convert a raw Trello action dict to zero or more domain events."""
    action_type: str = action.get("type", "")
    data: dict = action.get("data", {})
    card: dict = data.get("card", {})
    card_id = TaskId(card.get("id", ""))

    events: list[DomainEvent] = []

    if action_type == "updateCard":
        # List change -> TaskMoved
        list_before = data.get("listBefore")
        list_after = data.get("listAfter")
        if list_before and list_after:
            from_col = list_id_to_column.get(list_before["id"])
            to_col = list_id_to_column.get(list_after["id"])
            if from_col and to_col:
                events.append(
                    TaskMoved(
                        meta=_meta(action),
                        task_id=card_id,
                        from_column=from_col,
                        to_column=to_col,
                    )
                )

        # Name or desc change -> TaskUpdated
        old = data.get("old", {})
        new_title: str | None = None
        new_desc: str | None = None
        if "name" in old:
            new_title = card.get("name")
        if "desc" in old:
            new_desc = card.get("desc")
        if new_title is not None or new_desc is not None:
            events.append(
                TaskUpdated(
                    meta=_meta(action),
                    task_id=card_id,
                    title=new_title,
                    description=new_desc,
                )
            )

    elif action_type == "commentCard":
        member = action.get("memberCreator", {})
        events.append(
            CommentAdded(
                meta=_meta(action),
                task_id=card_id,
                comment_id=data.get("action", {}).get("id", action["id"]),
                author_id=member.get("id", ""),
                author_name=member.get("fullName", member.get("username", "")),
                text=data.get("text", ""),
            )
        )

    elif action_type == "createCard":
        list_data = data.get("list", {})
        col = list_id_to_column.get(list_data.get("id", ""))
        if col is not None:
            events.append(
                TaskCreated(
                    meta=_meta(action),
                    task_id=card_id,
                    title=card.get("name", ""),
                    column=col,
                )
            )

    return events
