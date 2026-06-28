"""FastAPI router for GitHub webhooks (multi-project: routed by PR URL repo)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from agents_trello.adapters.worktree.manager import GitWorktreeManager
from agents_trello.domain.models import Column
from agents_trello.domain.provider import BoardProvider
from agents_trello.project import ProjectContext, ProjectRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


_PR_URL_RE = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/\d+")


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def verify_github_signature(payload: bytes, secret: str, signature: str) -> bool:
    """Verify an ``x-hub-signature-256`` HMAC-SHA256 signature."""
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Dependency stubs – overridden at app composition time
# ---------------------------------------------------------------------------


def _get_registry() -> ProjectRegistry:  # pragma: no cover
    raise NotImplementedError("ProjectRegistry not injected")


def _get_webhook_secret() -> str:  # pragma: no cover
    raise NotImplementedError("GITHUB_WEBHOOK_SECRET not injected")


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/github/webhook")
async def github_webhook(
    request: Request,
    registry: Annotated[ProjectRegistry, Depends(_get_registry)],
    secret: Annotated[str, Depends(_get_webhook_secret)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Handle incoming GitHub webhook events, routed to the matching project."""
    body = await request.body()

    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing signature header")
    if not verify_github_signature(body, secret, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload: dict = json.loads(body)
    event = payload.get("event")
    ctx = _resolve_project(registry, payload)
    if ctx is None:
        logger.warning("github_webhook_unknown_project", extra={"payload_keys": list(payload)})
        raise HTTPException(status_code=404, detail="No matching project for payload")

    if event == "deployment_ready":
        pr_url: str = payload["pr_url"]
        deploy_url: str = payload["deploy_url"]
        await _handle_deployment_ready(ctx.provider, pr_url, deploy_url)
        return {"status": "ok", "action": "deployment_ready", "project_id": ctx.config.id}

    if event == "merged":
        pr_url = payload["pr_url"]
        merge_sha: str = payload["merge_sha"]
        await _handle_merged(ctx.provider, ctx.worktree, pr_url, merge_sha)
        return {"status": "ok", "action": "merged", "project_id": ctx.config.id}

    logger.warning("Unhandled GitHub webhook event: %s", event)
    return {"status": "ignored", "event": event or "unknown"}


def _resolve_project(registry: ProjectRegistry, payload: dict) -> ProjectContext | None:
    """Match the payload to a project.

    Priority order:
    1. Explicit ``project_id`` field in the envelope.
    2. ``repository.full_name`` (standard GitHub payload shape).
    3. Repo parsed from ``pr_url`` (custom envelope).
    """
    pid = payload.get("project_id")
    if isinstance(pid, str) and pid:
        ctx = registry.get(pid)
        if ctx:
            return ctx
    repository = payload.get("repository") or {}
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name:
            ctx = registry.get_by_repo(full_name)
            if ctx:
                return ctx
    pr_url = payload.get("pr_url")
    if isinstance(pr_url, str):
        match = _PR_URL_RE.search(pr_url)
        if match:
            return registry.get_by_repo(match.group(1))
    # Fallback: if there is exactly one project, use it (single-project setups).
    if len(registry) == 1:
        return next(iter(registry))
    return None


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


async def _handle_deployment_ready(board: BoardProvider, pr_url: str, deploy_url: str) -> None:
    """Post the deploy preview URL as a comment on the associated Trello card."""
    tasks = await board.list_tasks()
    for task in tasks:
        if task.pr_url == pr_url:
            comment = f"Deploy preview ready: {deploy_url}"
            await board.post_comment(task.id, comment)
            logger.info("Posted deploy URL for PR %s on task %s", pr_url, task.id)
            return
    logger.warning("No task found for PR %s", pr_url)


async def _handle_merged(
    board: BoardProvider,
    worktree: GitWorktreeManager,
    pr_url: str,
    merge_sha: str,
) -> None:
    """Move the associated card to Done and clean up the worktree."""
    tasks = await board.list_tasks()
    for task in tasks:
        if task.pr_url == pr_url:
            await board.move_task(task.id, Column.DONE)
            try:
                await worktree.remove(task.id)
            except KeyError:
                logger.debug("No worktree to clean up for task %s", task.id)
            logger.info(
                "Merged PR %s (sha=%s), moved task %s to Done",
                pr_url,
                merge_sha,
                task.id,
            )
            return
    logger.warning("No task found for PR %s", pr_url)
