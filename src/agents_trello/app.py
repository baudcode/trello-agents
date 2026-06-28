"""Composition root: builds providers, deps, FastAPI app (multi-project)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agents_trello.adapters.agent.runner import ClaudeAgentRunner
from agents_trello.adapters.github import webhook_route as gh_webhook
from agents_trello.adapters.github.client import GitHubClient
from agents_trello.adapters.trello.client import TrelloClient
from agents_trello.adapters.trello.provider import TrelloBoardProvider
from agents_trello.adapters.trello.webhook_route import create_trello_webhook_router
from agents_trello.adapters.worktree.manager import GitWorktreeManager
from agents_trello.config import Config
from agents_trello.domain.events import CommentAdded, DomainEvent, TaskMoved
from agents_trello.domain.handlers import Dispatcher
from agents_trello.domain.handlers.on_comment_added import OnCommentAdded
from agents_trello.domain.handlers.on_task_moved import OnTaskMoved
from agents_trello.infra.db import get_connection, run_migrations
from agents_trello.infra.logging import setup_logging
from agents_trello.project import ProjectConfig, ProjectContext, ProjectRegistry

logger = logging.getLogger(__name__)


def _build_dispatcher(
    *,
    conn: aiosqlite.Connection,
    board_id: str,
    board: TrelloBoardProvider,
    worktree: GitWorktreeManager,
    agent: ClaudeAgentRunner,
    vcs: GitHubClient,
    notify: object | None = None,
    deploy: object | None = None,
    event_log: object | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher(conn=conn, board_id=board_id)

    on_moved = OnTaskMoved(
        board=board,
        worktree=worktree,
        agent=agent,
        vcs=vcs,
        notify=notify,
        deploy=deploy,
        event_log=event_log,
    )
    dispatcher.register(TaskMoved, on_moved)

    on_comment = OnCommentAdded(
        board=board,
        agent=agent,
        worktree=worktree,
        notify=notify,
        event_log=event_log,
    )
    dispatcher.register(CommentAdded, on_comment)

    return dispatcher


async def _build_project_context(
    *,
    project: ProjectConfig,
    trello_client: TrelloClient,
    conn: aiosqlite.Connection,
    api_base: str,
    agent_max_concurrent: int,
    agent_timeout_seconds: int,
    elog: object,
    notify: object | None,
) -> ProjectContext:
    """Wire the per-project adapters and dispatcher."""
    github_client = GitHubClient(project.github_repo)
    worktree_manager = GitWorktreeManager(
        github_repo=project.github_repo,
        worktree_base_dir=project.worktree_base_dir,
    )
    agent_runner = ClaudeAgentRunner(
        timeout_seconds=agent_timeout_seconds,
        max_concurrent=agent_max_concurrent,
        api_base=api_base,
        board_id=project.trello_board_id,
    )

    deploy_manager = None
    if project.deploy_enabled:
        from agents_trello.adapters.deploy.manager import DeployManager

        deploy_manager = DeployManager(
            github_repo=project.github_repo,
            project_name=project.deploy_project_name,
            registry=project.deploy_registry,
            github_token=os.environ.get("GITHUB_TOKEN", ""),
        )

    provider = await TrelloBoardProvider.create(trello_client, project.trello_board_id)
    dispatcher = _build_dispatcher(
        conn=conn,
        board_id=provider._board_id,
        board=provider,
        worktree=worktree_manager,
        agent=agent_runner,
        vcs=github_client,
        notify=notify,
        deploy=deploy_manager,
        event_log=elog,
    )

    return ProjectContext(
        config=project,
        provider=provider,
        worktree=worktree_manager,
        agent=agent_runner,
        vcs=github_client,
        dispatcher=dispatcher,
        event_log=elog,  # type: ignore[arg-type]
        deploy_manager=deploy_manager,
        notify=notify,
    )


def create_app(config: Config | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    if config is None:
        config = Config.from_env()

    setup_logging(config.log_level, config.log_format)

    if not config.projects:
        raise RuntimeError("No projects configured (projects.yaml or TRELLO_BOARD_ID required).")

    # --- Shared Trello client (credentials are global) ---
    trello_client = TrelloClient(config.trello_api_key, config.trello_api_token)

    # --- Notifications (shared) ---
    ntfy_topic = os.environ.get("NTFY_TOPIC", "")
    notify: object | None = None
    if ntfy_topic:
        from agents_trello.infra.notify import NotifyService

        notify = NotifyService(ntfy_topic)

    # Mutable state populated during lifespan
    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # --- Database ---
        db_path = config.database_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await get_connection(db_path)
        await run_migrations(conn)
        state["conn"] = conn

        # --- Event log (shared, board_id stored per record) ---
        from agents_trello.infra.event_log import EventLog

        elog = EventLog(conn)
        await elog.setup()
        state["event_log"] = elog

        # --- Build one ProjectContext per project ---
        registry = ProjectRegistry()
        api_base = f"http://localhost:{config.port}"
        for project in config.projects:
            try:
                ctx = await _build_project_context(
                    project=project,
                    trello_client=trello_client,
                    conn=conn,
                    api_base=api_base,
                    agent_max_concurrent=config.agent_max_concurrent,
                    agent_timeout_seconds=config.agent_timeout_seconds,
                    elog=elog,
                    notify=notify,
                )
                registry.add(ctx)
                logger.info(
                    "project_loaded",
                    extra={
                        "project_id": project.id,
                        "project_name": project.name,
                        "board_id": project.trello_board_id,
                        "repo": project.github_repo,
                    },
                )
            except Exception:
                logger.exception(
                    "project_load_failed",
                    extra={"project_id": project.id, "project_name": project.name},
                )
        state["registry"] = registry

        if len(registry) == 0:
            raise RuntimeError("All projects failed to load; aborting startup.")

        # --- Single Trello webhook router shared across all projects ---
        callback_url = f"{config.webhook_base_url}/trello/webhook"
        trello_router = create_trello_webhook_router(
            registry=registry,
            callback_url=callback_url,
            webhook_secret=config.trello_webhook_secret,
            dispatch=lambda events, ctx: asyncio.create_task(
                _dispatch_events(ctx.dispatcher, events)
            ),
        )
        app.include_router(trello_router)

        # --- GitHub webhook deps (registry + shared secret) ---
        app.dependency_overrides[gh_webhook._get_registry] = lambda: registry
        app.dependency_overrides[gh_webhook._get_webhook_secret] = lambda: (
            config.github_webhook_secret
        )

        # --- Per-project backfill + polling + resume ---
        from agents_trello.backfill import resume_existing_cards, run_backfill

        poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
        poll_tasks: list[asyncio.Task[None]] = []
        for ctx in registry:
            try:
                await run_backfill(ctx.provider, ctx.dispatcher, conn, ctx.provider._board_id)
            except Exception:
                logger.exception(
                    "backfill_failed",
                    extra={"project_id": ctx.config.id},
                )
            if poll_interval > 0:
                task = asyncio.create_task(
                    _poll_loop(
                        ctx,
                        conn,
                        poll_interval,
                    )
                )
                poll_tasks.append(task)
            asyncio.create_task(resume_existing_cards(ctx.provider, ctx.dispatcher))

        if poll_interval > 0:
            logger.info("Polling enabled every %ds for %d projects", poll_interval, len(registry))

        state["poll_tasks"] = poll_tasks
        logger.info("Application started with %d projects", len(registry))
        yield

        # --- Shutdown ---
        for task in poll_tasks:
            task.cancel()
        for task in poll_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await conn.close()
        await trello_client.close()
        if notify and hasattr(notify, "close"):
            await notify.close()  # type: ignore[attr-defined]
        logger.info("Application shut down")

    templates_dir = Path(__file__).resolve().parent.parent.parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    app = FastAPI(title="agents-trello", lifespan=lifespan)
    app.include_router(gh_webhook.router)

    def _require_registry() -> ProjectRegistry:
        registry = state.get("registry")
        if not isinstance(registry, ProjectRegistry):
            raise HTTPException(status_code=503, detail="registry not ready")
        return registry

    def _require_project(project_id: str) -> ProjectContext:
        registry = _require_registry()
        ctx = registry.get(project_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
        return ctx

    @app.get("/", response_class=HTMLResponse)
    async def board_ui(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "board.html")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # ------------------------------------------------------------------
    # Project registry
    # ------------------------------------------------------------------

    @app.get("/api/projects")
    async def list_projects() -> dict:
        registry = _require_registry()
        return {
            "projects": [
                {
                    "id": ctx.config.id,
                    "name": ctx.config.name,
                    "trello_board_id": ctx.provider._board_id,
                    "github_repo": ctx.config.github_repo,
                }
                for ctx in registry
            ]
        }

    # ------------------------------------------------------------------
    # Per-project card endpoints
    # ------------------------------------------------------------------

    @app.post("/api/projects/{project_id}/cards")
    async def create_card(
        project_id: str,
        title: str,
        description: str = "",
        column: str = "Backlog",
    ) -> dict:
        ctx = _require_project(project_id)
        from agents_trello.domain.models import Column

        try:
            col = Column(column)
        except ValueError:
            return {"error": f"Invalid column: {column}. Use: Backlog, Todo"}
        if col not in (Column.BACKLOG, Column.TODO):
            return {"error": "Agents can only create cards in Backlog or Todo"}
        list_id = ctx.provider._column_to_list_id[col]
        resp = await ctx.provider._client._request(
            "POST",
            "cards",
            params={"name": title, "desc": description, "idList": list_id},
        )
        card = resp.json()
        return {
            "id": card["id"],
            "short_id": card["idShort"],
            "title": card["name"],
            "column": column,
            "url": card["shortUrl"],
        }

    @app.post("/api/projects/{project_id}/cards/{card_id}/move-to-todo")
    async def move_card_to_todo(project_id: str, card_id: str) -> dict:
        ctx = _require_project(project_id)
        from agents_trello.domain.models import Column

        await ctx.provider.move_task(card_id, Column.TODO)
        return {"status": "ok", "card_id": card_id, "column": "Todo"}

    @app.post("/api/projects/{project_id}/cards/{card_id}/move")
    async def move_card(project_id: str, card_id: str, request: Request) -> dict:
        ctx = _require_project(project_id)
        from agents_trello.domain.models import Column

        body = await request.json()
        col_name = body.get("column", "")
        try:
            col = Column(col_name)
        except ValueError:
            return {"error": f"Invalid column: {col_name}"}
        await ctx.provider.move_task(card_id, col)
        return {"status": "ok", "card_id": card_id, "column": col_name}

    @app.delete("/api/projects/{project_id}/cards/{card_id}")
    async def delete_card(project_id: str, card_id: str) -> dict:
        ctx = _require_project(project_id)
        try:
            await ctx.provider._client.delete_card(card_id)
        except Exception as exc:
            logger.error(
                "delete_card_failed",
                extra={"card_id": card_id, "project_id": project_id},
                exc_info=True,
            )
            return {"error": str(exc)}
        return {"status": "ok", "card_id": card_id}

    @app.get("/api/projects/{project_id}/cards/{card_id}")
    async def get_card(project_id: str, card_id: str) -> dict:
        ctx = _require_project(project_id)
        from agents_trello.domain.models import TaskId

        task = await ctx.provider.get_task(TaskId(card_id))
        if task is None:
            return {"error": "card not found"}
        comments = await ctx.provider.get_comments(TaskId(card_id))
        return {
            "id": task.id,
            "short_id": task.short_id,
            "title": task.title,
            "description": task.description,
            "column": task.column.value,
            "labels": task.labels,
            "comments": [
                {
                    "id": c.id,
                    "author_name": c.author_name,
                    "text": c.text,
                    "created_at": c.created_at.isoformat(),
                }
                for c in comments
            ],
        }

    @app.get("/api/projects/{project_id}/board")
    async def get_board(project_id: str) -> dict:
        ctx = _require_project(project_id)
        tasks = await ctx.provider.list_tasks()
        return {
            "cards": [
                {
                    "id": t.id,
                    "short_id": t.short_id,
                    "title": t.title,
                    "column": t.column.value,
                    "labels": t.labels,
                }
                for t in tasks
            ]
        }

    @app.get("/api/projects/{project_id}/events")
    async def get_project_events(
        project_id: str,
        task_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        from dataclasses import asdict

        ctx = _require_project(project_id)
        elog = state.get("event_log")
        if elog is None:
            return {"events": [], "total": 0}
        board_id = ctx.provider._board_id
        entries = await elog.query(  # type: ignore[union-attr]
            task_id=task_id,
            event_type=event_type,
            since=since,
            board_id=board_id,
            limit=limit,
            offset=offset,
        )
        total = await elog.count(  # type: ignore[union-attr]
            task_id=task_id, event_type=event_type, board_id=board_id
        )
        return {
            "events": [asdict(e) for e in entries],
            "total": total,
        }

    @app.get("/api/events/types")
    async def get_event_types() -> dict:
        return {
            "event_types": [
                "agent_started",
                "agent_completed",
                "agent_failed",
                "agent_cancelled",
                "review_started",
                "review_completed",
                "review_failed",
                "card_moved",
                "deploy_started",
                "deploy_completed",
                "deploy_failed",
                "pr_merged",
            ]
        }

    @app.get("/api/projects/{project_id}/deployments")
    async def get_deployments(project_id: str) -> dict:
        ctx = _require_project(project_id)
        result: dict[str, object] = {"deployments": [], "recent_merges": []}
        if ctx.deploy_manager:
            for branch, (container, digest) in ctx.deploy_manager._deployed.items():  # type: ignore[attr-defined]
                url = ctx.deploy_manager.get_deploy_url(branch)  # type: ignore[attr-defined]
                result["deployments"].append(  # type: ignore[union-attr]
                    {
                        "branch": branch,
                        "container": container,
                        "image_digest": digest[:16],
                        "url": url,
                    }
                )
        try:
            prs = await ctx.vcs.get_recent_merged_prs(limit=10)
            result["recent_merges"] = prs
        except Exception:
            logger.warning(
                "get_recent_merged_prs_failed",
                extra={"project_id": project_id},
                exc_info=True,
            )
        return result

    return app


async def _dispatch_events(dispatcher: Dispatcher, events: list[DomainEvent]) -> None:
    for event in events:
        try:
            await dispatcher.dispatch(event)
        except Exception:
            logger.exception("Handler failed", extra={"event": type(event).__name__})


async def _poll_loop(
    ctx: ProjectContext,
    conn: aiosqlite.Connection,
    interval: int,
) -> None:
    """Periodically poll Trello for new events and check deployments (one project)."""
    import re

    from agents_trello.domain.models import Column, TaskId
    from agents_trello.infra.db import get_cursor, set_cursor

    provider = ctx.provider
    dispatcher = ctx.dispatcher
    vcs = ctx.vcs
    deploy = ctx.deploy_manager
    agent_runner = ctx.agent
    notify = ctx.notify
    board_id = provider._board_id

    while True:
        try:
            await asyncio.sleep(interval)

            cursor = await get_cursor(conn, board_id)
            events, new_cursor = await provider.poll_events(since_cursor=cursor)
            if events:
                logger.info(
                    "poll_found_events",
                    extra={"count": len(events), "project_id": ctx.config.id},
                )
                for event in events:
                    try:
                        await dispatcher.dispatch(event)
                    except Exception:
                        logger.exception("poll_handler_error")
                if new_cursor:
                    await set_cursor(conn, new_cursor, board_id)

            if agent_runner and agent_runner._active:
                tasks_all = await provider.list_tasks()
                tasks_by_id = {str(t.id): t for t in tasks_all}
                for task_id in list(agent_runner._active):
                    t = tasks_by_id.get(task_id)
                    if t and "agent working" not in t.labels:
                        logger.info(
                            "label_removed_killing_agent",
                            extra={"task_id": task_id, "title": t.title},
                        )
                        await agent_runner.cancel(TaskId(task_id))
                        if notify:
                            await notify.send(  # type: ignore[attr-defined]
                                title=f"Agent cancelled: {t.title}",
                                message="Agent Working label was removed",
                                tags="stop_sign",
                            )

            tasks = await provider.list_tasks()
            for task in tasks:
                if task.column != Column.REVIEW:
                    continue

                comments = await provider.get_comments(task.id)
                pr_url = None
                for c in comments:
                    match = re.search(r"https://github\.com/[^\s]+/pull/\d+", c.text)
                    if match:
                        pr_url = match.group(0)
                        break

                if pr_url and vcs:
                    try:
                        status = await vcs.get_pr_status(pr_url)
                        if status.get("state") == "MERGED":
                            logger.info(
                                "pr_merged_detected",
                                extra={"task_id": task.id, "pr_url": pr_url},
                            )
                            if deploy:
                                slug = (
                                    re.sub(r"[^a-z0-9]+", "-", task.title.lower())
                                    .strip("-")[:40]
                                    .rstrip("-")
                                )
                                feat_branch = f"feat/{task.id}-{slug}"
                                try:
                                    await deploy.teardown(feat_branch)  # type: ignore[attr-defined]
                                    logger.info(
                                        "deploy_teardown_on_merge",
                                        extra={"branch": feat_branch},
                                    )
                                except Exception:
                                    logger.debug("teardown_failed", exc_info=True)
                            await provider.move_task(task.id, Column.DONE)
                            await provider.post_comment(
                                task.id,
                                f"claude: PR merged! Moved to Done: {pr_url}",
                            )
                            continue
                    except Exception:
                        logger.debug("pr_status_check_failed", exc_info=True)

                if deploy:
                    slug = (
                        re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:40].rstrip("-")
                    )
                    branch = f"feat/{task.id}-{slug}"
                    try:
                        result = await deploy.check_and_deploy(branch)  # type: ignore[attr-defined]
                        if result and result.success:
                            await provider.post_comment(
                                task.id,
                                f"claude: Deployed! Test at: {result.deploy_url}",
                            )
                            set_deployed = getattr(provider, "set_deployed", None)
                            if set_deployed:
                                await set_deployed(task.id)
                            logger.info(
                                "auto_deploy",
                                extra={
                                    "task_id": task.id,
                                    "url": result.deploy_url,
                                },
                            )
                        elif result and not result.success:
                            await provider.post_comment(
                                task.id,
                                f"claude: Deploy failed: {result.error}",
                            )
                    except Exception:
                        logger.debug(
                            "deploy_check_failed",
                            extra={"task_id": task.id},
                            exc_info=True,
                        )

            if deploy:
                try:
                    result = await deploy.check_and_deploy("main")  # type: ignore[attr-defined]
                    if result and result.success:
                        logger.info("main_deployed", extra={"url": result.deploy_url})
                        features = ""
                        if vcs:
                            try:
                                prs = await vcs.get_recent_merged_prs(limit=5)
                                if prs:
                                    features = "\n".join(
                                        f"- #{p['number']} {p['title']}" for p in prs
                                    )
                            except Exception:
                                logger.warning("get_merged_prs_for_notify_failed", exc_info=True)
                        if notify:
                            msg = f"Main deployed: {result.deploy_url}"
                            if features:
                                msg += f"\n\nRecent merges:\n{features}"
                            await notify.send(  # type: ignore[attr-defined]
                                title="Main branch redeployed",
                                message=msg,
                                tags="rocket,white_check_mark",
                            )
                    elif result and not result.success:
                        logger.warning("main_deploy_failed", extra={"error": result.error})
                except Exception:
                    logger.debug("main_deploy_check_failed", exc_info=True)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("poll_error", extra={"project_id": ctx.config.id})


def app() -> FastAPI:
    """Factory callable for uvicorn: ``uvicorn agents_trello.app:app --factory``."""
    return create_app()
