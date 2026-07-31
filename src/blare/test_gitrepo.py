"""Unit tests for blare.gitrepo, per engineering/modules/gitrepo.md's test plan.

Contract tests use real git against temporary repositories built per test. Failure-mode
tests use real git too, except where the failure needs an injected stub executable (via
`git_executable`) to produce output real git cannot be made to produce.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from blare.gitrepo import ChangedFile, GitCommandError, GitRepo, NoCommitsError, NotARepositoryError


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    """Init a repo at `path` with a local identity so commits succeed without global config."""
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")


def _write(path: Path, name: str, content: str) -> Path:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def _commit(path: Path, message: str = "commit", *, allow_empty: bool = False) -> str:
    _git(path, "add", "-A")
    if allow_empty:
        _git(path, "commit", "--quiet", "--allow-empty", "-m", message)
    else:
        _git(path, "commit", "--quiet", "-m", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _stub_git(tmp_path: Path, script_body: str) -> str:
    """Write an executable stub 'git' that proxies to real git except where overridden."""
    stub = tmp_path / "stub-git"
    stub.write_text(f"#!/usr/bin/env bash\nset -e\n{script_body}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


# --- Contract tests -----------------------------------------------------------------


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


def test_contract_head_sha_returns_commit(tmp_path: Path) -> None:
    """head_sha() returns the current HEAD commit's full SHA."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.head_sha() == sha


def test_contract_head_sha_unborn_head_raises(tmp_path: Path) -> None:
    """head_sha() on a repository with no commits raises NoCommitsError (R11)."""
    _init_repo(tmp_path)
    repo = GitRepo.discover(tmp_path)

    with pytest.raises(NoCommitsError):
        repo.head_sha()


def test_contract_resolves_true_for_real_sha(tmp_path: Path) -> None:
    """resolves() returns True for a SHA that names a real commit."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.resolves(sha) is True


def test_contract_resolves_false_for_garbage(tmp_path: Path) -> None:
    """resolves() returns False for a string that is not a SHA at all."""
    _init_repo(tmp_path)
    _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.resolves("not-a-sha-at-all") is False


def test_contract_resolves_false_for_truncated_unknown_sha(tmp_path: Path) -> None:
    """resolves() returns False for a well-formed but nonexistent short SHA prefix."""
    _init_repo(tmp_path)
    _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.resolves("abc1234") is False


def test_contract_is_ancestor_true(tmp_path: Path) -> None:
    """is_ancestor() is True when the first commit precedes the second."""
    _init_repo(tmp_path)
    first = _commit(tmp_path, allow_empty=True)
    second = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.is_ancestor(first, second) is True


def test_contract_is_ancestor_false(tmp_path: Path) -> None:
    """is_ancestor() is False when the first commit does not precede the second."""
    _init_repo(tmp_path)
    first = _commit(tmp_path, allow_empty=True)
    second = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.is_ancestor(second, first) is False


def test_contract_is_ancestor_self(tmp_path: Path) -> None:
    """is_ancestor() is True when both arguments name the same commit."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.is_ancestor(sha, sha) is True


def test_contract_dirty_modified_tracked_file_listed(tmp_path: Path) -> None:
    """A modified tracked file is listed by dirty_paths_outside."""
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    _commit(tmp_path)
    _write(tmp_path, "file.txt", "changed\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.dirty_paths_outside(".blare") == ["file.txt"]


def test_contract_dirty_deleted_tracked_file_listed(tmp_path: Path) -> None:
    """A deleted tracked file is listed by dirty_paths_outside."""
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    _commit(tmp_path)
    (tmp_path / "file.txt").unlink()
    repo = GitRepo.discover(tmp_path)

    assert repo.dirty_paths_outside(".blare") == ["file.txt"]


def test_contract_dirty_untracked_file_listed(tmp_path: Path) -> None:
    """A new untracked file is listed by dirty_paths_outside."""
    _init_repo(tmp_path)
    _commit(tmp_path, allow_empty=True)
    _write(tmp_path, "new.txt", "hi\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.dirty_paths_outside(".blare") == ["new.txt"]


def test_contract_dirty_ignored_file_not_listed(tmp_path: Path) -> None:
    """A git-ignored file never appears in dirty_paths_outside (R11)."""
    _init_repo(tmp_path)
    _write(tmp_path, ".gitignore", "*.log\n")
    _commit(tmp_path)
    _write(tmp_path, "ignored.log", "noise\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.dirty_paths_outside(".blare") == []


def test_contract_dirty_excluded_dir_not_listed(tmp_path: Path) -> None:
    """A change confined to the excluded directory does not appear in dirty_paths_outside."""
    _init_repo(tmp_path)
    _commit(tmp_path, allow_empty=True)
    _write(tmp_path, ".blare/state.yaml", "sha: abc\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.dirty_paths_outside(".blare") == []


def test_contract_dirty_clean_tree_empty(tmp_path: Path) -> None:
    """A clean working tree yields an empty dirty_paths_outside list."""
    _init_repo(tmp_path)
    _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.dirty_paths_outside(".blare") == []


def test_contract_delta_lists_added_modified_deleted(tmp_path: Path) -> None:
    """effective_delta lists each file with its correct add/modify/delete status."""
    _init_repo(tmp_path)
    _write(tmp_path, "keep.txt", "same\n")
    _write(tmp_path, "modme.txt", "before\n")
    _write(tmp_path, "delme.txt", "gone\n")
    base = _commit(tmp_path)
    _write(tmp_path, "modme.txt", "after\n")
    (tmp_path / "delme.txt").unlink()
    _write(tmp_path, "added.txt", "new\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    delta = repo.effective_delta(base, end, ".blare")

    assert set(delta.files) == {
        ChangedFile(path="modme.txt", status="modified"),
        ChangedFile(path="delme.txt", status="deleted"),
        ChangedFile(path="added.txt", status="added"),
    }


def test_contract_delta_same_sha_empty(tmp_path: Path) -> None:
    """effective_delta between a commit and itself is empty (zero diff)."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    delta = repo.effective_delta(sha, sha, ".blare")

    assert delta.is_empty


def test_contract_delta_change_plus_revert_empty(tmp_path: Path) -> None:
    """A change followed by its exact revert nets to an empty delta (R7)."""
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    base = _commit(tmp_path)
    _write(tmp_path, "file.txt", "changed\n")
    _commit(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    delta = repo.effective_delta(base, end, ".blare")

    assert delta.is_empty


def test_contract_delta_excludes_dir(tmp_path: Path) -> None:
    """Files under the excluded directory never appear in the delta."""
    _init_repo(tmp_path)
    base = _commit(tmp_path, allow_empty=True)
    _write(tmp_path, ".blare/state.yaml", "sha: abc\n")
    _write(tmp_path, "real.txt", "content\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    delta = repo.effective_delta(base, end, ".blare")

    assert delta.files == (ChangedFile(path="real.txt", status="added"),)


def test_contract_delta_rename_is_added_plus_deleted(tmp_path: Path) -> None:
    """A rename surfaces as added-plus-deleted regardless of the diff.renames config."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "diff.renames", "true")
    _write(tmp_path, "orig.txt", "same content\n")
    base = _commit(tmp_path)
    _git(tmp_path, "mv", "orig.txt", "renamed.txt")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    delta = repo.effective_delta(base, end, ".blare")

    assert set(delta.files) == {
        ChangedFile(path="orig.txt", status="deleted"),
        ChangedFile(path="renamed.txt", status="added"),
    }


def test_contract_delta_typechange_is_modified(tmp_path: Path) -> None:
    """A file replaced by a symlink at the same path maps to status 'modified' (T)."""
    _init_repo(tmp_path)
    _write(tmp_path, "path.txt", "a file\n")
    base = _commit(tmp_path)
    (tmp_path / "path.txt").unlink()
    (tmp_path / "path.txt").symlink_to("/nonexistent")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    delta = repo.effective_delta(base, end, ".blare")

    assert delta.files == (ChangedFile(path="path.txt", status="modified"),)


def test_contract_patch_text_returns_real_diff_content(tmp_path: Path) -> None:
    """patch_text returns the real unified diff, containing the changed lines."""
    _init_repo(tmp_path)
    _write(tmp_path, "modme.txt", "before\n")
    base = _commit(tmp_path)
    _write(tmp_path, "modme.txt", "after\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    text = repo.patch_text(base, end, ".blare")

    assert "-before" in text
    assert "+after" in text
    assert "modme.txt" in text


def test_contract_patch_text_same_sha_empty(tmp_path: Path) -> None:
    """patch_text between a commit and itself is empty (zero diff)."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.patch_text(sha, sha, ".blare") == ""


def test_contract_patch_text_change_plus_revert_empty(tmp_path: Path) -> None:
    """A change followed by its exact revert nets to empty patch text (R7 semantics)."""
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    base = _commit(tmp_path)
    _write(tmp_path, "file.txt", "changed\n")
    _commit(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    assert repo.patch_text(base, end, ".blare") == ""


def test_contract_patch_text_excludes_dir(tmp_path: Path) -> None:
    """Files under the excluded directory never appear in the patch text."""
    _init_repo(tmp_path)
    base = _commit(tmp_path, allow_empty=True)
    _write(tmp_path, ".blare/state.yaml", "sha: abc\n")
    _write(tmp_path, "real.txt", "content\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    text = repo.patch_text(base, end, ".blare")

    assert "real.txt" in text
    assert ".blare" not in text
    assert "state.yaml" not in text


def test_contract_patch_text_independent_of_diff_renames_config(tmp_path: Path) -> None:
    """patch_text's content does not depend on the user's diff.renames config."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "diff.renames", "true")
    _write(tmp_path, "orig.txt", "same content\n")
    base = _commit(tmp_path)
    _git(tmp_path, "mv", "orig.txt", "renamed.txt")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    text = repo.patch_text(base, end, ".blare")

    # --no-renames always shows a rename as a full delete-plus-add, regardless of
    # the user's diff.renames config (mirrors effective_delta's own guarantee).
    assert "rename from" not in text
    assert "orig.txt" in text
    assert "renamed.txt" in text


def test_contract_repo_id_stable_across_calls(tmp_path: Path) -> None:
    """repo_id() returns the same value on repeated calls against the same repo."""
    _init_repo(tmp_path)
    repo = GitRepo.discover(tmp_path)

    assert repo.repo_id() == repo.repo_id()


def test_contract_repo_id_stable_across_invocation_directories(tmp_path: Path) -> None:
    """repo_id() is the same whether discovered from the root or a subdirectory."""
    _init_repo(tmp_path)
    subdir = tmp_path / "sub"
    subdir.mkdir()

    from_root = GitRepo.discover(tmp_path)
    from_subdir = GitRepo.discover(subdir)

    assert from_root.repo_id() == from_subdir.repo_id()


def test_contract_repo_id_differs_between_checkouts(tmp_path: Path) -> None:
    """repo_id() differs between two distinct checkouts, even with identical content."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _init_repo(first)
    _init_repo(second)

    repo_first = GitRepo.discover(first)
    repo_second = GitRepo.discover(second)

    assert repo_first.repo_id() != repo_second.repo_id()


def test_contract_tree_matches_true_on_clean_tree(tmp_path: Path) -> None:
    """tree_matches() is True comparing a clean working tree against HEAD."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    repo = GitRepo.discover(tmp_path)

    assert repo.tree_matches(sha, ".blare") is True


def test_contract_tree_matches_false_after_tracked_edit(tmp_path: Path) -> None:
    """tree_matches() is False once a tracked file is edited after the compared commit."""
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    sha = _commit(tmp_path)
    _write(tmp_path, "file.txt", "changed\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.tree_matches(sha, ".blare") is False


def test_contract_tree_matches_false_after_untracked_file_appears(tmp_path: Path) -> None:
    """tree_matches() is False once an untracked file appears outside the excluded dir."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    _write(tmp_path, "new.txt", "surprise\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.tree_matches(sha, ".blare") is False


def test_contract_tree_matches_false_after_new_commit(tmp_path: Path) -> None:
    """tree_matches() is False when the working tree has advanced past the compared commit."""
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "original\n")
    sha = _commit(tmp_path)
    _write(tmp_path, "file.txt", "advanced\n")
    _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)

    assert repo.tree_matches(sha, ".blare") is False


def test_contract_tree_matches_ignores_excluded_dir_edits(tmp_path: Path) -> None:
    """tree_matches() stays True when only the excluded directory changed."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    _write(tmp_path, ".blare/state.yaml", "sha: abc\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.tree_matches(sha, ".blare") is True


def test_contract_tree_matches_ignores_gitignored_file(tmp_path: Path) -> None:
    """tree_matches() stays True when only a git-ignored file appears."""
    _init_repo(tmp_path)
    _write(tmp_path, ".gitignore", "*.log\n")
    sha = _commit(tmp_path)
    _write(tmp_path, "ignored.log", "noise\n")
    repo = GitRepo.discover(tmp_path)

    assert repo.tree_matches(sha, ".blare") is True


# --- Failure-mode tests (dependency = the git subprocess) ----------------------------


def test_failure_git_missing_executable(tmp_path: Path) -> None:
    """A nonexistent git_executable raises GitCommandError naming it."""
    _init_repo(tmp_path)

    with pytest.raises(GitCommandError) as exc_info:
        GitRepo.discover(tmp_path, git_executable="/no/such/git")

    assert "/no/such/git" in exc_info.value.cause


def test_failure_git_command_error(tmp_path: Path) -> None:
    """A corrupted object store surfaces git's stderr as a GitCommandError (effective_delta).

    `patch_text` shares this identical subprocess/error path (same failure-mode
    dependency -- the git subprocess -- no separate test needed: the two-set
    testing rule enumerates failure modes per dependency, not per method).
    """
    _init_repo(tmp_path)
    _write(tmp_path, "file.txt", "one\n")
    base = _commit(tmp_path)
    _write(tmp_path, "file.txt", "two\n")
    end = _commit(tmp_path)
    repo = GitRepo.discover(tmp_path)
    objects_dir = tmp_path / ".git" / "objects"
    for child in objects_dir.iterdir():
        if child.is_dir() and len(child.name) == 2:
            for obj in child.iterdir():
                obj.unlink()
            child.rmdir()

    with pytest.raises(GitCommandError) as exc_info:
        repo.effective_delta(base, end, ".blare")

    assert "bad object" in exc_info.value.cause


def test_failure_git_answer_set_exceeded(tmp_path: Path) -> None:
    """A stub git exiting 2 on merge-base --is-ancestor raises, rather than answering False."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, allow_empty=True)
    stub = _stub_git(
        tmp_path,
        'if [ "$1" = "merge-base" ] && [ "$2" = "--is-ancestor" ]; then exit 2; fi\n'
        'exec git "$@"',
    )
    repo = GitRepo.discover(tmp_path, git_executable=stub)

    with pytest.raises(GitCommandError):
        repo.is_ancestor(sha, sha)


def test_failure_git_unparseable_status(tmp_path: Path) -> None:
    """A stub git emitting malformed -z status output raises rather than silently misparsing."""
    _init_repo(tmp_path)
    _commit(tmp_path, allow_empty=True)
    stub = _stub_git(
        tmp_path,
        'if [ "$1" = "status" ]; then printf "not-well-formed-output-at-all"; exit 0; fi\n'
        'exec git "$@"',
    )
    repo = GitRepo.discover(tmp_path, git_executable=stub)

    with pytest.raises(GitCommandError):
        repo.dirty_paths_outside(".blare")


def test_failure_git_unknown_status_letter(tmp_path: Path) -> None:
    """A stub git emitting a status letter outside the mapping raises GitCommandError."""
    _init_repo(tmp_path)
    base = _commit(tmp_path, allow_empty=True)
    end = _commit(tmp_path, allow_empty=True)
    stub = _stub_git(
        tmp_path,
        'if [ "$1" = "diff" ]; then printf "X\\0somefile.txt\\0"; exit 0; fi\n'
        'exec git "$@"',
    )
    repo = GitRepo.discover(tmp_path, git_executable=stub)

    with pytest.raises(GitCommandError):
        repo.effective_delta(base, end, ".blare")
