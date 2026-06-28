"""Tests for the multi-project Trello webhook router."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents_trello.adapters.trello.webhook_route import create_trello_webhook_router
from agents_trello.project import ProjectConfig, ProjectContext, ProjectRegistry

_SECRET = "trello-secret"
_CALLBACK = "http://localhost:8000/trello/webhook"


def _sign(body: bytes, callback: str = _CALLBACK, secret: str = _SECRET) -> str:
    mac = hmac.new(secret.encode(), body + callback.encode(), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


class _FakeProvider:
    def __init__(self, board_id: str) -> None:
        self._board_id = board_id
        self.parsed: list[bytes] = []

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Any]:
        self.parsed.append(body)
        # Return one fake event so we can verify the dispatch path
        return ["evt"]


def _ctx(pid: str, board_id: str) -> tuple[ProjectContext, _FakeProvider]:
    provider = _FakeProvider(board_id)
    cfg = ProjectConfig(
        id=pid,
        name=pid,
        trello_board_id=board_id,
        github_repo=f"org/{pid}",
        worktree_base_dir=Path(f"/tmp/{pid}"),
    )
    ctx = ProjectContext(
        config=cfg,
        provider=provider,  # type: ignore[arg-type]
        worktree=None,  # type: ignore[arg-type]
        agent=None,  # type: ignore[arg-type]
        vcs=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        event_log=None,  # type: ignore[arg-type]
    )
    return ctx, provider


@pytest.fixture
def registry_and_providers() -> tuple[ProjectRegistry, _FakeProvider, _FakeProvider]:
    reg = ProjectRegistry()
    ctx_a, prov_a = _ctx("a", "boardA")
    ctx_b, prov_b = _ctx("b", "boardB")
    reg.add(ctx_a)
    reg.add(ctx_b)
    return reg, prov_a, prov_b


@pytest.fixture
def client_and_dispatched(
    registry_and_providers: tuple[ProjectRegistry, _FakeProvider, _FakeProvider],
) -> tuple[TestClient, list[tuple[list[Any], str]]]:
    registry, _, _ = registry_and_providers
    dispatched: list[tuple[list[Any], str]] = []

    def dispatch(events: list[Any], ctx: ProjectContext) -> None:
        dispatched.append((events, ctx.config.id))

    router = create_trello_webhook_router(
        registry=registry,
        callback_url=_CALLBACK,
        webhook_secret=_SECRET,
        dispatch=dispatch,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), dispatched


def _post(client: TestClient, payload: dict) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        "/trello/webhook",
        content=body,
        headers={"x-trello-webhook": _sign(body)},
    )


def test_routes_to_matching_board(
    client_and_dispatched: tuple[TestClient, list[tuple[list[Any], str]]],
) -> None:
    client, dispatched = client_and_dispatched
    resp = _post(
        client,
        {"action": {"id": "a1", "data": {"board": {"id": "boardA"}}}},
    )
    assert resp.status_code == 200
    assert dispatched == [(["evt"], "a")]


def test_routes_to_other_board(
    client_and_dispatched: tuple[TestClient, list[tuple[list[Any], str]]],
) -> None:
    client, dispatched = client_and_dispatched
    resp = _post(
        client,
        {"action": {"id": "b1", "data": {"board": {"id": "boardB"}}}},
    )
    assert resp.status_code == 200
    assert dispatched == [(["evt"], "b")]


def test_unknown_board_returns_404(
    client_and_dispatched: tuple[TestClient, list[tuple[list[Any], str]]],
) -> None:
    client, _ = client_and_dispatched
    resp = _post(
        client,
        {"action": {"id": "x1", "data": {"board": {"id": "boardX"}}}},
    )
    assert resp.status_code == 404


def test_missing_board_id_returns_400(
    client_and_dispatched: tuple[TestClient, list[tuple[list[Any], str]]],
) -> None:
    client, _ = client_and_dispatched
    resp = _post(client, {"action": {"id": "z"}})
    assert resp.status_code == 400


def test_head_returns_200(
    client_and_dispatched: tuple[TestClient, list[tuple[list[Any], str]]],
) -> None:
    client, _ = client_and_dispatched
    resp = client.head("/trello/webhook")
    assert resp.status_code == 200
