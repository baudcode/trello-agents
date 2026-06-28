"""Project model: ProjectConfig + ProjectRegistry for multi-tenant support."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents_trello.adapters.agent.runner import ClaudeAgentRunner
    from agents_trello.adapters.github.client import GitHubClient
    from agents_trello.adapters.trello.provider import TrelloBoardProvider
    from agents_trello.adapters.worktree.manager import GitWorktreeManager
    from agents_trello.domain.handlers import Dispatcher
    from agents_trello.infra.event_log import EventLog


@dataclass(frozen=True)
class ProjectConfig:
    """Static configuration for one project (loaded from YAML or env fallback)."""

    id: str
    name: str
    trello_board_id: str
    github_repo: str
    worktree_base_dir: Path
    backend: str = "trello"  # "trello" | "inmemory" | "sqlite"
    deploy_enabled: bool = False
    deploy_project_name: str = "app"
    deploy_registry: str = "ghcr.io"


@dataclass
class ProjectContext:
    """Runtime bundle of adapters + dispatcher for one project."""

    config: ProjectConfig
    provider: TrelloBoardProvider
    worktree: GitWorktreeManager
    agent: ClaudeAgentRunner
    vcs: GitHubClient
    dispatcher: Dispatcher
    event_log: EventLog
    deploy_manager: object | None = None
    notify: object | None = None


@dataclass
class ProjectRegistry:
    """Lookup table for ProjectContexts, keyed by project id."""

    _by_id: dict[str, ProjectContext] = field(default_factory=dict)

    def add(self, ctx: ProjectContext) -> None:
        self._by_id[ctx.config.id] = ctx

    def get(self, project_id: str) -> ProjectContext | None:
        return self._by_id.get(project_id)

    def get_by_board(self, board_id: str) -> ProjectContext | None:
        for ctx in self._by_id.values():
            if ctx.config.trello_board_id == board_id or ctx.provider._board_id == board_id:
                return ctx
        return None

    def get_by_repo(self, repo: str) -> ProjectContext | None:
        """Match by 'owner/name'. Accepts case-insensitive matches."""
        target = repo.lower()
        for ctx in self._by_id.values():
            if ctx.config.github_repo.lower() == target:
                return ctx
        return None

    def __iter__(self) -> Iterator[ProjectContext]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def ids(self) -> list[str]:
        return list(self._by_id.keys())
