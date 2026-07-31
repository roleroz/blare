"""Read-only navigation of the real `~/external_git/miniflux_v2` checkout the release
suite analyzes (spec, Constraints). Every scenario's real code delta comes from
checking out among **miniflux's own, pre-existing commits** -- this module never
creates a commit, amends one, or moves a branch ref: `checkout_commit` always detaches
HEAD onto an existing SHA, and `restore` returns to the branch this module found
checked out when the process started. Blare itself never runs a git write operation
(spec, Artifacts); this module -- which drives Blare, not Blare itself -- holds to the
same rule so the release suite never leaves the target checkout different from how it
found it, `.blare/` and its derived docs aside (those are exactly what a release run is
for).
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path

MINIFLUX_ROOT = Path.home() / "external_git" / "miniflux_v2"


class DirtyRepositoryError(RuntimeError):
    """The target checkout has changes outside `.blare/` -- refuse to touch it."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def current_branch(repo: Path) -> str:
    return _git(repo, "branch", "--show-current")


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def resolve(repo: Path, ref: str) -> str:
    """The full SHA a short ref/relative expression (`HEAD~15`, a short hash, ...)
    resolves to -- callers record the resolved SHA rather than the ref, so a scenario's
    provenance is reproducible independent of what HEAD was when it was chosen."""
    return _git(repo, "rev-parse", ref)


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, check=False
    )
    return result.returncode == 0


def assert_clean_outside_blare(repo: Path) -> None:
    """Raise if the working tree has any change outside `.blare/` -- mirrors R11's own
    check, so a driver bug (or a leftover from a previous, interrupted run) is caught
    before it reaches Blare's own preflight."""
    status = _git(repo, "status", "--porcelain")
    offending = [
        line for line in status.splitlines() if not line[3:].startswith(".blare/")
    ]
    if offending:
        raise DirtyRepositoryError(
            f"{repo} has changes outside .blare/, refusing to drive a scenario there: "
            f"{offending}"
        )


def checkout_commit(repo: Path, sha: str) -> None:
    """Detach HEAD onto `sha` (a real, pre-existing commit) -- never creates, amends, or
    moves a branch ref. This is how a scenario presents Blare with a different, genuine
    point in miniflux's own history without authoring anything."""
    assert_clean_outside_blare(repo)
    subprocess.run(["git", "checkout", "--quiet", "--detach", sha], cwd=repo, check=True)


def restore(repo: Path, ref: str) -> None:
    """Return to `ref` (a branch name, or a bare SHA for a repo found already
    detached) -- restores the exact pre-capture state, since checking out a
    branch never moves it and this module never commits."""
    subprocess.run(["git", "checkout", "--quiet", ref], cwd=repo, check=True)


@contextlib.contextmanager
def on_commit(repo: Path, sha: str) -> Iterator[None]:
    """Checkout `sha` for the duration of the block, then restore whatever ref
    was checked out beforehand -- a branch name, or the bare SHA if `repo` was
    already detached -- regardless of whether the block raises. Every capture
    function should check out a commit through this context manager rather
    than calling `checkout_commit` directly, so the release suite never leaves
    the real checkout on a stray detached HEAD after a run (this module's own
    contract, stated above)."""
    original = current_branch(repo) or head_sha(repo)
    checkout_commit(repo, sha)
    try:
        yield
    finally:
        restore(repo, original)


def blare_root(repo: Path) -> Path:
    return repo / ".blare"


__all__ = [
    "MINIFLUX_ROOT",
    "DirtyRepositoryError",
    "current_branch",
    "head_sha",
    "resolve",
    "is_ancestor",
    "assert_clean_outside_blare",
    "checkout_commit",
    "restore",
    "on_commit",
    "blare_root",
]
