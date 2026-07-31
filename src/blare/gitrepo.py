"""All git access for a run (architecture: no other module invokes git).

Pure queries: this module never mutates the repository, matching the never-commits
decision (architecture's Overview). Every git invocation runs with an explicit `cwd`,
never inherits the caller's, and never uses `shell=True`.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from blare.model import BlareError


class NotARepositoryError(BlareError):
    """Raised by `discover` when `path` is not inside a git working tree (R11)."""


class NoCommitsError(BlareError):
    """Raised by `head_sha` when HEAD is unborn — the repository has no commits (R11)."""


class GitCommandError(BlareError):
    """A git invocation whose exit code fell outside its command's answer set.

    Also raised when the git executable is missing, or when output a command was
    expected to produce cannot be parsed regardless of exit code. The message carries
    the exact command line and git's stderr verbatim (or, for unparseable output, the
    offending output) so the orchestrator's R13 rendering shows the user exactly what
    happened.
    """


@dataclass(frozen=True)
class ChangedFile:
    """One file's status within a `Delta`."""

    path: str
    status: Literal["added", "modified", "deleted"]


@dataclass(frozen=True)
class Delta:
    """The effective delta between two commits, excluding a directory (R6, R7)."""

    files: tuple[ChangedFile, ...]

    @property
    def is_empty(self) -> bool:
        return len(self.files) == 0


# `git diff --name-status` letters this module recognizes. `--no-renames` (always
# passed) keeps `R`/`C` (rename/copy) out of the output entirely — a rename surfaces
# as a `D` plus an `A` instead. `T` (typechange, e.g. file replaced by a symlink at the
# same path) maps to "modified" per gitrepo.md.
_STATUS_LETTERS: dict[str, Literal["added", "modified", "deleted"]] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "modified",
}


def _command_line(git_executable: str, args: Sequence[str]) -> str:
    return " ".join([git_executable, *args])


def _run(git_executable: str, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one git subprocess with an explicit `cwd`; never `shell=True`.

    Raises `GitCommandError` when the executable itself cannot be found — this is the
    one failure every call site shares, so it lives here rather than being duplicated
    at each call site.
    """
    command = [git_executable, *args]
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise GitCommandError(
            cause=(
                f"git executable {git_executable!r} not found "
                f"(running: {_command_line(git_executable, args)})"
            ),
            next_action="Install git and ensure it is on PATH.",
        ) from exc


def _command_error(
    git_executable: str, args: Sequence[str], result: subprocess.CompletedProcess[str]
) -> GitCommandError:
    """Build the GitCommandError for a command whose exit code fell outside its answer set."""
    return GitCommandError(
        cause=(
            f"`{_command_line(git_executable, args)}` exited {result.returncode}: "
            f"{result.stderr.strip()}"
        ),
        next_action="Investigate the git error above; the repository may be corrupted.",
    )


def _parse_porcelain_entries(
    raw: str, git_executable: str, args: Sequence[str]
) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` output into (status, path) pairs.

    Each NUL-separated token is two status characters, a space, then the path (`-z`
    disables path quoting, so the path is exactly the raw bytes git has for it).
    """
    entries: list[tuple[str, str]] = []
    for token in raw.split("\0"):
        if token == "":
            continue
        if len(token) < 4 or token[2] != " ":
            raise GitCommandError(
                cause=(
                    f"unparseable output from `{_command_line(git_executable, args)}`: "
                    f"{token!r}"
                ),
                next_action="Report this; git's status output did not match the expected format.",
            )
        entries.append((token[:2], token[3:]))
    return entries


def _parse_name_status(
    raw: str, git_executable: str, args: Sequence[str]
) -> list[ChangedFile]:
    """Parse `git diff --name-status -z --no-renames` output into `ChangedFile`s."""
    tokens = [token for token in raw.split("\0") if token != ""]
    if len(tokens) % 2 != 0:
        raise GitCommandError(
            cause=f"unparseable output from `{_command_line(git_executable, args)}`: {raw!r}",
            next_action="Report this; git's diff output did not match the expected format.",
        )
    files: list[ChangedFile] = []
    for i in range(0, len(tokens), 2):
        letter, path = tokens[i], tokens[i + 1]
        status = _STATUS_LETTERS.get(letter)
        if status is None:
            raise GitCommandError(
                cause=(
                    f"unknown status letter {letter!r} for {path!r} from "
                    f"`{_command_line(git_executable, args)}`"
                ),
                next_action="Report this; git returned a status this module does not recognize.",
            )
        files.append(ChangedFile(path=path, status=status))
    return files


def _under_excluded(path: str, exclude: str) -> bool:
    trimmed = exclude.rstrip("/")
    return path == trimmed or path.startswith(f"{trimmed}/")


class GitRepo:
    """A discovered git working tree; all queries run against it via subprocess."""

    def __init__(self, worktree_root: Path, git_executable: str = "git") -> None:
        self._worktree_root = worktree_root
        self._git_executable = git_executable

    @property
    def worktree_root(self) -> Path:
        return self._worktree_root

    def _git(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return _run(self._git_executable, args, cwd=self._worktree_root)

    @classmethod
    def discover(cls, path: Path, git_executable: str = "git") -> GitRepo:
        """Walk up from `path` to the worktree root; raise outside a repo.

        `git_executable` exists for failure injection in gitrepo's own test plan and
        is never varied in production.
        """
        args = ["rev-parse", "--show-toplevel"]
        result = _run(git_executable, args, cwd=path)
        if result.returncode != 0:
            raise NotARepositoryError(
                cause=f"{path} is not inside a git repository",
                next_action="Run blare from inside a git-managed codebase.",
            )
        return cls(worktree_root=Path(result.stdout.strip()), git_executable=git_executable)

    def repo_id(self) -> str:
        """First 16 hex chars of SHA-256 of the resolved worktree root path (R21)."""
        digest = hashlib.sha256(str(self._worktree_root).encode()).hexdigest()
        return digest[:16]

    def head_sha(self) -> str:
        """The current HEAD commit's SHA; raises `NoCommitsError` on an unborn HEAD (R11)."""
        result = self._git(["rev-parse", "HEAD"])
        if result.returncode != 0:
            raise NoCommitsError(
                cause="the repository has no commits yet",
                next_action="Create an initial commit before running blare.",
            )
        return result.stdout.strip()

    def resolves(self, sha: str) -> bool:
        """True when `sha` names a real commit in this repository."""
        args = ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"]
        result = self._git(args)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise _command_error(self._git_executable, args, result)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True when `ancestor` is an ancestor of (or equal to) `descendant` (R15)."""
        args = ["merge-base", "--is-ancestor", ancestor, descendant]
        result = self._git(args)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise _command_error(self._git_executable, args, result)

    def dirty_paths_outside(self, exclude: str) -> list[str]:
        """Every working-tree difference from HEAD outside `exclude` (R11).

        Tracked files modified or deleted, plus untracked files; git-ignored files
        are never counted (`git status` omits them by default, without `--ignored`).
        """
        args = ["status", "--porcelain=v1", "-z", "--no-renames"]
        result = self._git(args)
        if result.returncode != 0:
            raise _command_error(self._git_executable, args, result)
        entries = _parse_porcelain_entries(result.stdout, self._git_executable, args)
        return [path for _, path in entries if not _under_excluded(path, exclude)]

    def effective_delta(self, base_sha: str, end_sha: str, exclude: str) -> Delta:
        """The net range diff from `base_sha` to `end_sha`, excluding `exclude` (R6, R7).

        `end_sha` is a parameter rather than implicitly HEAD so the orchestrator can
        pass the SHA captured at run start. `--no-renames` makes the output
        independent of the user's `diff.renames` config: a rename is always
        added-plus-deleted.
        """
        pathspec = f":(exclude){exclude}"
        args = [
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            base_sha,
            end_sha,
            "--",
            ".",
            pathspec,
        ]
        result = self._git(args)
        if result.returncode != 0:
            raise _command_error(self._git_executable, args, result)
        files = _parse_name_status(result.stdout, self._git_executable, args)
        return Delta(files=tuple(files))

    def patch_text(self, base_sha: str, end_sha: str, exclude: str) -> str:
        """The same range's full unified diff text (T4.4): real diff content for
        triage, not just the file/status pairs `effective_delta` returns.

        Same net-diff semantics as `effective_delta` over the identical range --
        empty for a same-SHA range and for a change-plus-revert -- for the same
        reason: it must answer "what would triage see," not "what happened
        commit-by-commit." No output parsing beyond the subprocess call: the
        text is opaque to this module, consumed only by the model
        (`RunContext.patch_text`, agent.md). No size cap (gitrepo.md's
        Decisions).
        """
        pathspec = f":(exclude){exclude}"
        args = ["diff", "--no-renames", base_sha, end_sha, "--", ".", pathspec]
        result = self._git(args)
        if result.returncode != 0:
            raise _command_error(self._git_executable, args, result)
        return result.stdout

    def tree_matches(self, sha: str, exclude: str) -> bool:
        """True when the working tree outside `exclude` is byte-identical to `sha` (R20).

        Also false if any untracked file exists outside `exclude`: `git diff` alone
        never reports untracked files, so the same porcelain scan `dirty_paths_outside`
        uses covers that case. Git-ignored files never affect the result, exactly as
        in `dirty_paths_outside`.
        """
        pathspec = f":(exclude){exclude}"
        diff_args = ["diff", "--quiet", sha, "--", ".", pathspec]
        diff_result = self._git(diff_args)
        if diff_result.returncode not in (0, 1):
            raise _command_error(self._git_executable, diff_args, diff_result)
        if diff_result.returncode == 1:
            return False

        status_args = ["status", "--porcelain=v1", "-z", "--no-renames"]
        status_result = self._git(status_args)
        if status_result.returncode != 0:
            raise _command_error(self._git_executable, status_args, status_result)
        entries = _parse_porcelain_entries(status_result.stdout, self._git_executable, status_args)
        return not any(
            status == "??" and not _under_excluded(path, exclude) for status, path in entries
        )
