"""Unit tests for blare.gitrepo (T1.1 subset: `discover` only; T1.2 builds the rest)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blare.gitrepo import GitRepo, NotARepositoryError


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)


def test_contract_discover_inside_repo_finds_root(tmp_path: Path) -> None:
    """discover() from inside a git working tree returns that tree's root."""
    _init_repo(tmp_path)

    repo = GitRepo.discover(tmp_path)

    assert repo.worktree_root == tmp_path.resolve()


def test_contract_discover_from_subdirectory_finds_root(tmp_path: Path) -> None:
    """discover() from a subdirectory of a git working tree still finds its root."""
    _init_repo(tmp_path)
    subdir = tmp_path / "sub"
    subdir.mkdir()

    repo = GitRepo.discover(subdir)

    assert repo.worktree_root == tmp_path.resolve()


def test_contract_discover_outside_repo_raises(tmp_path: Path) -> None:
    """discover() outside any git repository raises NotARepositoryError (R11)."""
    with pytest.raises(NotARepositoryError):
        GitRepo.discover(tmp_path)


def test_failure_git_missing_executable(tmp_path: Path) -> None:
    """A nonexistent git_executable raises NotARepositoryError naming it.

    T1.1's discover() maps this to the same exception as "outside a repo" — T1.2's
    full gitrepo module distinguishes it as a GitCommandError; that distinction is
    out of this task's scope.
    """
    _init_repo(tmp_path)

    with pytest.raises(NotARepositoryError) as exc_info:
        GitRepo.discover(tmp_path, git_executable="/no/such/git")

    assert "/no/such/git" in exc_info.value.cause
