"""Tests for projects.yaml loading and env-fallback in Config."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agents_trello.config import _default_project_from_env, _load_projects_from_yaml


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_yaml_two_projects(tmp_path: Path) -> None:
    yaml = _write_yaml(
        tmp_path / "projects.yaml",
        """
projects:
  - id: alpha
    name: Alpha Project
    trello_board_id: board_a
    github_repo: org/alpha
    worktree_base_dir: /tmp/alpha
  - id: beta
    name: Beta
    trello_board_id: board_b
    github_repo: org/beta
""".lstrip(),
    )
    projects = _load_projects_from_yaml(yaml)
    assert [p.id for p in projects] == ["alpha", "beta"]
    assert projects[0].trello_board_id == "board_a"
    assert projects[0].worktree_base_dir == Path("/tmp/alpha")
    # Default worktree dir uses the project id under ~/agents/worktrees
    assert projects[1].worktree_base_dir == Path("~/agents/worktrees/beta").expanduser()


def test_yaml_missing_required_field(tmp_path: Path) -> None:
    yaml = _write_yaml(
        tmp_path / "projects.yaml",
        """
projects:
  - id: bad
    name: missing board id
    github_repo: org/repo
""".lstrip(),
    )
    with pytest.raises(RuntimeError, match="missing required field"):
        _load_projects_from_yaml(yaml)


def test_yaml_duplicate_ids(tmp_path: Path) -> None:
    yaml = _write_yaml(
        tmp_path / "projects.yaml",
        """
projects:
  - id: dup
    trello_board_id: b1
    github_repo: org/a
  - id: dup
    trello_board_id: b2
    github_repo: org/b
""".lstrip(),
    )
    with pytest.raises(RuntimeError, match="Duplicate"):
        _load_projects_from_yaml(yaml)


def test_yaml_empty(tmp_path: Path) -> None:
    yaml = _write_yaml(tmp_path / "projects.yaml", "projects: []\n")
    with pytest.raises(RuntimeError, match="at least one project"):
        _load_projects_from_yaml(yaml)


def test_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "PROJECT_NAME",
        "GITHUB_REPO",
        "WORKTREE_BASE_DIR",
        "DEPLOY_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRELLO_BOARD_ID", "envBoard")
    monkeypatch.setenv("GITHUB_REPO", "org/legacy")
    proj = _default_project_from_env()
    assert proj.id == "default"
    assert proj.trello_board_id == "envBoard"
    assert proj.github_repo == "org/legacy"
    assert (
        proj.worktree_base_dir == Path(os.environ.get("HOME", "~"), "agents/worktrees")
        or proj.worktree_base_dir == Path("~/agents/worktrees").expanduser()
    )
