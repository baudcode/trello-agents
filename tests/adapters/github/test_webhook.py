"""Tests for GitHub webhook route and HMAC verification."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents_trello.adapters.github.webhook_route import (
    _get_registry,
    _get_webhook_secret,
    router,
    verify_github_signature,
)
from agents_trello.adapters.inmemory.provider import InMemoryProvider
from agents_trello.domain.models import Column
from agents_trello.project import ProjectConfig, ProjectContext, ProjectRegistry

# ------------------------------------------------------------------
# HMAC verification unit tests
# ------------------------------------------------------------------

_SECRET = "test-webhook-secret"


def _sign(payload: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_verify_signature_valid() -> None:
    payload = b'{"event": "ping"}'
    signature = _sign(payload)
    assert verify_github_signature(payload, _SECRET, signature) is True


def test_verify_signature_invalid() -> None:
    payload = b'{"event": "ping"}'
    bad_signature = "sha256=0000000000000000000000000000000000000000000000000000000000000000"
    assert verify_github_signature(payload, _SECRET, bad_signature) is False


def test_verify_signature_wrong_secret() -> None:
    payload = b'{"event": "ping"}'
    signature = _sign(payload, secret="wrong-secret")
    assert verify_github_signature(payload, _SECRET, signature) is False


# ------------------------------------------------------------------
# Fake worktree manager for webhook tests
# ------------------------------------------------------------------


class FakeWorktreeManager:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def remove(self, task_id: str) -> None:
        self.removed.append(task_id)


# ------------------------------------------------------------------
# Integration tests with FastAPI TestClient
# ------------------------------------------------------------------


@pytest.fixture
def board() -> InMemoryProvider:
    return InMemoryProvider()


@pytest.fixture
def worktree_mgr() -> FakeWorktreeManager:
    return FakeWorktreeManager()


def _make_registry(
    board: InMemoryProvider, worktree_mgr: FakeWorktreeManager, repo: str = "owner/repo"
) -> ProjectRegistry:
    from pathlib import Path

    cfg = ProjectConfig(
        id="test",
        name="Test",
        trello_board_id="b1",
        github_repo=repo,
        worktree_base_dir=Path("/tmp"),
    )
    ctx = ProjectContext(
        config=cfg,
        provider=board,  # type: ignore[arg-type]
        worktree=worktree_mgr,  # type: ignore[arg-type]
        agent=None,  # type: ignore[arg-type]
        vcs=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        event_log=None,  # type: ignore[arg-type]
    )
    reg = ProjectRegistry()
    reg.add(ctx)
    return reg


@pytest.fixture
def test_client(board: InMemoryProvider, worktree_mgr: FakeWorktreeManager) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    registry = _make_registry(board, worktree_mgr)
    app.dependency_overrides[_get_registry] = lambda: registry
    app.dependency_overrides[_get_webhook_secret] = lambda: _SECRET

    return TestClient(app)


def test_webhook_missing_signature(test_client: TestClient) -> None:
    resp = test_client.post(
        "/github/webhook",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_webhook_invalid_signature(test_client: TestClient) -> None:
    payload = json.dumps({"event": "ping"}).encode()
    resp = test_client.post(
        "/github/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": "sha256=bad",
        },
    )
    assert resp.status_code == 401


def test_webhook_deployment_ready(
    test_client: TestClient,
    board: InMemoryProvider,
) -> None:
    pr_url = "https://github.com/owner/repo/pull/7"
    deploy_url = "https://preview.example.com/abc123"

    board.seed_task(task_id="card-77", title="My task", column=Column.IN_PROGRESS, pr_url=pr_url)

    payload = json.dumps(
        {"event": "deployment_ready", "pr_url": pr_url, "deploy_url": deploy_url}
    ).encode()
    signature = _sign(payload)

    resp = test_client.post(
        "/github/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": signature,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "deployment_ready"

    # Verify the comment was posted
    assert len(board._posted_comments) == 1
    task_id, comment_text = board._posted_comments[0]
    assert task_id == "card-77"
    assert deploy_url in comment_text


def test_webhook_merged(
    test_client: TestClient,
    board: InMemoryProvider,
) -> None:
    pr_url = "https://github.com/owner/repo/pull/9"
    board.seed_task(task_id="card-99", title="Fix bug", column=Column.REVIEW, pr_url=pr_url)

    payload = json.dumps(
        {"event": "merged", "pr_url": pr_url, "merge_sha": "abc123def456"}
    ).encode()
    signature = _sign(payload)

    resp = test_client.post(
        "/github/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": signature,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "merged"

    # Verify the task was moved to Done
    import asyncio

    task = asyncio.get_event_loop().run_until_complete(board.get_task("card-99"))
    assert task is not None
    assert task.column == Column.DONE


def test_webhook_unknown_event(test_client: TestClient) -> None:
    payload = json.dumps({"event": "unknown_event"}).encode()
    signature = _sign(payload)

    resp = test_client.post(
        "/github/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": signature,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"


def test_webhook_routes_by_repo_with_multiple_projects() -> None:
    """When the registry has multiple projects, route by repo parsed from pr_url."""
    app = FastAPI()
    app.include_router(router)

    board_a = InMemoryProvider()
    board_b = InMemoryProvider()
    pr_url_b = "https://github.com/org/beta/pull/4"
    board_a.seed_task(task_id="card-A", title="A", column=Column.REVIEW, pr_url="x")
    board_b.seed_task(task_id="card-B", title="B", column=Column.REVIEW, pr_url=pr_url_b)

    registry = ProjectRegistry()
    from pathlib import Path

    registry.add(
        ProjectContext(
            config=ProjectConfig(
                id="alpha",
                name="A",
                trello_board_id="bA",
                github_repo="org/alpha",
                worktree_base_dir=Path("/tmp/a"),
            ),
            provider=board_a,  # type: ignore[arg-type]
            worktree=FakeWorktreeManager(),  # type: ignore[arg-type]
            agent=None,  # type: ignore[arg-type]
            vcs=None,  # type: ignore[arg-type]
            dispatcher=None,  # type: ignore[arg-type]
            event_log=None,  # type: ignore[arg-type]
        )
    )
    registry.add(
        ProjectContext(
            config=ProjectConfig(
                id="beta",
                name="B",
                trello_board_id="bB",
                github_repo="org/beta",
                worktree_base_dir=Path("/tmp/b"),
            ),
            provider=board_b,  # type: ignore[arg-type]
            worktree=FakeWorktreeManager(),  # type: ignore[arg-type]
            agent=None,  # type: ignore[arg-type]
            vcs=None,  # type: ignore[arg-type]
            dispatcher=None,  # type: ignore[arg-type]
            event_log=None,  # type: ignore[arg-type]
        )
    )

    app.dependency_overrides[_get_registry] = lambda: registry
    app.dependency_overrides[_get_webhook_secret] = lambda: _SECRET
    client = TestClient(app)

    payload = json.dumps(
        {"event": "deployment_ready", "pr_url": pr_url_b, "deploy_url": "https://preview/x"}
    ).encode()
    resp = client.post(
        "/github/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": _sign(payload),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["project_id"] == "beta"
    # Only the beta board received the comment
    assert len(board_a._posted_comments) == 0
    assert len(board_b._posted_comments) == 1
