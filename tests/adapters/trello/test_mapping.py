"""Tests for Trello action-to-domain-event mapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents_trello.adapters.trello.mapping import action_to_events, list_name_to_column
from agents_trello.domain.events import CommentAdded, TaskCreated, TaskMoved, TaskUpdated
from agents_trello.domain.models import Column, TaskId

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"

# Mapping of Trello list IDs to domain columns used across fixtures
LIST_ID_TO_COLUMN: dict[str, Column] = {
    "list_backlog": Column.BACKLOG,
    "list_todo": Column.TODO,
    "list_inprogress": Column.IN_PROGRESS,
    "list_review": Column.REVIEW,
    "list_done": Column.DONE,
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------------- #
# list_name_to_column
# --------------------------------------------------------------------- #


class TestListNameToColumn:
    def test_exact_match(self) -> None:
        assert list_name_to_column("Backlog") == Column.BACKLOG

    def test_case_insensitive(self) -> None:
        assert list_name_to_column("inprogress") == Column.IN_PROGRESS
        assert list_name_to_column("DONE") == Column.DONE

    def test_whitespace_stripped(self) -> None:
        assert list_name_to_column("  Todo  ") == Column.TODO

    def test_unknown_returns_none(self) -> None:
        assert list_name_to_column("Archived") is None


# --------------------------------------------------------------------- #
# action_to_events — updateCard (move)
# --------------------------------------------------------------------- #


class TestUpdateCardMove:
    @pytest.fixture()
    def action(self) -> dict:
        return _load("trello_update_card_move.json")

    def test_produces_task_moved(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        moved = [e for e in events if isinstance(e, TaskMoved)]
        assert len(moved) == 1
        evt = moved[0]
        assert evt.task_id == TaskId("card_001")
        assert evt.from_column == Column.TODO
        assert evt.to_column == Column.IN_PROGRESS

    def test_meta_fields(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        evt = events[0]
        assert evt.meta.action_id == "act_move_001"
        assert evt.meta.timestamp.year == 2026


# --------------------------------------------------------------------- #
# action_to_events — commentCard
# --------------------------------------------------------------------- #


class TestCommentCard:
    @pytest.fixture()
    def action(self) -> dict:
        return _load("trello_comment_card.json")

    def test_produces_comment_added(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        assert len(events) == 1
        evt = events[0]
        assert isinstance(evt, CommentAdded)
        assert evt.task_id == TaskId("card_002")
        assert evt.text == "Looks good, ready for merge."
        assert evt.author_name == "Alice Smith"
        assert evt.comment_id == "comment_id_abc"

    def test_meta_fields(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        assert events[0].meta.action_id == "act_comment_001"


# --------------------------------------------------------------------- #
# action_to_events — createCard
# --------------------------------------------------------------------- #


class TestCreateCard:
    @pytest.fixture()
    def action(self) -> dict:
        return _load("trello_create_card.json")

    def test_produces_task_created(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        assert len(events) == 1
        evt = events[0]
        assert isinstance(evt, TaskCreated)
        assert evt.task_id == TaskId("card_003")
        assert evt.title == "Set up CI pipeline"
        assert evt.column == Column.BACKLOG

    def test_meta_fields(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        assert events[0].meta.action_id == "act_create_001"


# --------------------------------------------------------------------- #
# action_to_events — updateCard (rename)
# --------------------------------------------------------------------- #


class TestUpdateCardRename:
    @pytest.fixture()
    def action(self) -> dict:
        return _load("trello_update_card_rename.json")

    def test_produces_task_updated(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        assert len(events) == 1
        evt = events[0]
        assert isinstance(evt, TaskUpdated)
        assert evt.task_id == TaskId("card_001")
        assert evt.title == "Implement OAuth2 user auth"
        assert evt.description is None

    def test_meta_fields(self, action: dict) -> None:
        events = action_to_events(action, LIST_ID_TO_COLUMN)
        assert events[0].meta.action_id == "act_rename_001"
