"""All git access for a run (architecture: no other module invokes git).

T1.1 scope: only what the walking skeleton's R11-refusal e2e test needs —
`GitRepo.discover` raising `NotARepositoryError` outside a git repository. The full
module (`repo_id`, `head_sha`, `dirty_paths_outside`, `effective_delta`,
`tree_matches`, `is_ancestor`, the `Delta`/`ChangedFile` data structures, and the
full failure taxonomy) is built out in task T1.2 per `engineering/modules/gitrepo.md`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from blare.model import BlareError


class NotARepositoryError(BlareError):
    """Raised by `discover` when `path` is not inside a git working tree (R11)."""


class GitRepo:
    """A discovered git working tree. T1.1 exposes only `worktree_root`."""

    def __init__(self, worktree_root: Path) -> None:
        self._worktree_root = worktree_root

    @property
    def worktree_root(self) -> Path:
        return self._worktree_root

    @classmethod
    def discover(cls, path: Path, git_executable: str = "git") -> GitRepo:
        """Walk up from `path` to the worktree root; raise outside a repo.

        `git_executable` exists for failure injection in gitrepo's own test plan
        (T1.2) and is never varied in production.
        """
        try:
            result = subprocess.run(
                [git_executable, "rev-parse", "--show-toplevel"],
                cwd=path,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise NotARepositoryError(
                cause=f"git executable {git_executable!r} not found",
                next_action="Install git and ensure it is on PATH.",
            ) from exc
        if result.returncode != 0:
            raise NotARepositoryError(
                cause=f"{path} is not inside a git repository",
                next_action="Run blare from inside a git-managed codebase.",
            )
        return cls(worktree_root=Path(result.stdout.strip()))
