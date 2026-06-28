"""Tests for ProjectRegistry lookup semantics."""

from __future__ import annotations

from pathlib import Path

from agents_trello.project import ProjectConfig, ProjectContext, ProjectRegistry


class _StubProvider:
    def __init__(self, board_id: str) -> None:
        self._board_id = board_id


def _make_ctx(pid: str, board_id: str, repo: str) -> ProjectContext:
    cfg = ProjectConfig(
        id=pid,
        name=pid.title(),
        trello_board_id=board_id,
        github_repo=repo,
        worktree_base_dir=Path(f"/tmp/{pid}"),
    )
    return ProjectContext(
        config=cfg,
        provider=_StubProvider(board_id),  # type: ignore[arg-type]
        worktree=None,  # type: ignore[arg-type]
        agent=None,  # type: ignore[arg-type]
        vcs=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        event_log=None,  # type: ignore[arg-type]
    )


def test_get_by_id() -> None:
    reg = ProjectRegistry()
    reg.add(_make_ctx("a", "boardA", "org/a"))
    reg.add(_make_ctx("b", "boardB", "org/b"))
    assert reg.get("a") is not None
    assert reg.get("a").config.id == "a"
    assert reg.get("missing") is None


def test_get_by_board_id() -> None:
    reg = ProjectRegistry()
    reg.add(_make_ctx("a", "boardA", "org/a"))
    reg.add(_make_ctx("b", "boardB", "org/b"))
    assert reg.get_by_board("boardA").config.id == "a"
    assert reg.get_by_board("boardB").config.id == "b"
    assert reg.get_by_board("nope") is None


def test_get_by_repo_case_insensitive() -> None:
    reg = ProjectRegistry()
    reg.add(_make_ctx("a", "boardA", "Org/App"))
    assert reg.get_by_repo("org/app").config.id == "a"
    assert reg.get_by_repo("ORG/APP").config.id == "a"
    assert reg.get_by_repo("Org/App").config.id == "a"
    assert reg.get_by_repo("other/thing") is None


def test_iter_and_len() -> None:
    reg = ProjectRegistry()
    reg.add(_make_ctx("a", "boardA", "org/a"))
    reg.add(_make_ctx("b", "boardB", "org/b"))
    ids = [ctx.config.id for ctx in reg]
    assert sorted(ids) == ["a", "b"]
    assert len(reg) == 2


def test_replace_same_id() -> None:
    reg = ProjectRegistry()
    reg.add(_make_ctx("a", "boardA", "org/a"))
    reg.add(_make_ctx("a", "boardA2", "org/a2"))
    assert len(reg) == 1
    assert reg.get("a").config.trello_board_id == "boardA2"
