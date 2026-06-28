"""Environment-driven configuration."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agents_trello.project import ProjectConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    # Trello credentials (shared across projects)
    trello_api_key: str
    trello_api_token: str
    trello_webhook_secret: str

    # GitHub credentials (shared across projects)
    github_webhook_secret: str

    # Agent
    agent_max_concurrent: int
    agent_timeout_seconds: int

    # Server
    webhook_base_url: str
    host: str
    port: int

    # Database
    database_path: Path

    # Logging
    log_level: str
    log_format: str

    # Projects
    projects: list[ProjectConfig] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        projects = _load_projects()
        needs_trello = any(p.backend == "trello" for p in projects)
        if needs_trello:
            api_key = os.environ["TRELLO_API_KEY"]
            api_token = os.environ["TRELLO_API_TOKEN"]
        else:
            api_key = os.environ.get("TRELLO_API_KEY", "")
            api_token = os.environ.get("TRELLO_API_TOKEN", "")
        return cls(
            trello_api_key=api_key,
            trello_api_token=api_token,
            trello_webhook_secret=os.environ.get("TRELLO_WEBHOOK_SECRET", ""),
            github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
            agent_max_concurrent=int(os.environ.get("AGENT_MAX_CONCURRENT", "4")),
            agent_timeout_seconds=int(os.environ.get("AGENT_TIMEOUT_SECONDS", "1800")),
            webhook_base_url=os.environ.get("WEBHOOK_BASE_URL", "http://localhost:8000"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8000")),
            database_path=Path(os.environ.get("DATABASE_PATH", "./data/agents_trello.db")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            log_format=os.environ.get("LOG_FORMAT", "console"),
            projects=projects,
        )


def _load_projects() -> list[ProjectConfig]:
    """Load projects from projects.yaml; fall back to a single env-based project."""
    yaml_path = Path(os.environ.get("PROJECTS_FILE", "projects.yaml"))
    if yaml_path.is_file():
        return _load_projects_from_yaml(yaml_path)
    if os.environ.get("TRELLO_BOARD_ID"):
        logger.info("projects.yaml not found; using single project from env vars")
        return [_default_project_from_env()]
    raise RuntimeError(f"No projects configured. Create {yaml_path} or set TRELLO_BOARD_ID in env.")


def _load_projects_from_yaml(path: Path) -> list[ProjectConfig]:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    raw_projects = data.get("projects") or []
    if not raw_projects:
        raise RuntimeError(f"{path} must define at least one project under 'projects:'")
    projects: list[ProjectConfig] = []
    seen_ids: set[str] = set()
    for entry in raw_projects:
        proj = _project_from_yaml_entry(entry)
        if proj.id in seen_ids:
            raise RuntimeError(f"Duplicate project id in {path}: {proj.id}")
        seen_ids.add(proj.id)
        projects.append(proj)
    return projects


def _project_from_yaml_entry(entry: dict) -> ProjectConfig:
    backend = str(entry.get("backend", "trello")).lower()
    if backend not in {"trello", "inmemory", "sqlite"}:
        raise RuntimeError(f"Unknown backend '{backend}' (expected trello|inmemory|sqlite)")

    try:
        pid = str(entry["id"])
        name = str(entry.get("name", pid))
        github_repo = str(entry["github_repo"])
    except KeyError as exc:
        raise RuntimeError(f"Project entry missing required field: {exc}") from exc

    # Trello-backed projects must point at a real board; local backends
    # may omit trello_board_id (we synthesize one from the project id).
    raw_board_id = entry.get("trello_board_id")
    if backend == "trello":
        if not raw_board_id:
            raise RuntimeError(
                f"Project '{pid}' uses backend=trello but is missing trello_board_id"
            )
        trello_board_id = str(raw_board_id)
    else:
        trello_board_id = str(raw_board_id) if raw_board_id else f"local:{pid}"

    worktree_base_dir = Path(
        entry.get("worktree_base_dir") or f"~/agents/worktrees/{pid}"
    ).expanduser()
    deploy_enabled = bool(entry.get("deploy_enabled", False))
    deploy_project_name = str(entry.get("deploy_project_name", "app"))
    deploy_registry = str(entry.get("deploy_registry", "ghcr.io"))
    return ProjectConfig(
        id=pid,
        name=name,
        trello_board_id=trello_board_id,
        github_repo=github_repo,
        worktree_base_dir=worktree_base_dir,
        backend=backend,
        deploy_enabled=deploy_enabled,
        deploy_project_name=deploy_project_name,
        deploy_registry=deploy_registry,
    )


def _default_project_from_env() -> ProjectConfig:
    return ProjectConfig(
        id="default",
        name=os.environ.get("PROJECT_NAME", "default"),
        trello_board_id=os.environ["TRELLO_BOARD_ID"],
        github_repo=os.environ.get("GITHUB_REPO", ""),
        worktree_base_dir=Path(
            os.environ.get("WORKTREE_BASE_DIR", "~/agents/worktrees")
        ).expanduser(),
        deploy_enabled=bool(os.environ.get("DEPLOY_ENABLED", "")),
        deploy_project_name=os.environ.get("DEPLOY_PROJECT", "app"),
        deploy_registry=os.environ.get("DEPLOY_REGISTRY", "ghcr.io"),
    )
