"""e2e: R15 -- when the recorded `analyzed_sha` does not resolve to a commit in
the repository, or resolves but is not an ancestor of the current commit, `blare
update` refuses and names both recovery options: re-run full analysis, or
hand-edit the recorded SHA in the state file to a real ancestor.

The refusal code itself already existed from T2.2 (`orchestrator.md`'s step 5);
this task (T3.2) adds its e2e coverage per architecture.md's Tasks section
("R15's refusals with both recovery options").

Traces `engineering/architecture.md`'s T3.2 scope: "Traces: R15, ...".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import head_sha, init_repo, write_minimal_state


def _blare_bin() -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert path.exists(), f"blare binary not found via Rlocation at {path}"
    return path


def _write_valid_config(blare_root: Path) -> None:
    (blare_root / "config.yaml").write_text("stack: prometheus\n")


def _current_branch(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_e2e_refuses_when_recorded_sha_does_not_resolve(tmp_path: Path) -> None:
    """A recorded `analyzed_sha` that names no commit in the repository (e.g. a
    hand-edit typo) refuses, naming the state file and both recovery options."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    blare_root = repo_dir / ".blare"
    garbage_sha = "0" * 40
    write_minimal_state(blare_root, analyzed_sha=garbage_sha)
    _write_valid_config(blare_root)

    result = run_blare_noninteractive(
        blare_bin, ["update"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "state.yaml" in result.output
    assert garbage_sha in result.output
    assert "blare analyze" in result.output
    assert "analyzed_sha" in result.output


def test_e2e_refuses_when_recorded_sha_is_not_an_ancestor(tmp_path: Path) -> None:
    """A recorded `analyzed_sha` that resolves to a real commit but is not an
    ancestor of HEAD (e.g. after a rebase or history rewrite) refuses the same
    way, naming both recovery options."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    default_branch = _current_branch(repo_dir)

    # A side branch whose tip is a real commit, but not an ancestor of the
    # default branch's own (later-advanced) HEAD, and vice versa.
    subprocess.run(
        ["git", "checkout", "-b", "side"], cwd=repo_dir, check=True, capture_output=True
    )
    (repo_dir / "side.txt").write_text("side branch work\n")
    subprocess.run(["git", "add", "side.txt"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "side branch commit"], cwd=repo_dir, check=True
    )
    side_sha = head_sha(repo_dir)
    subprocess.run(
        ["git", "checkout", default_branch], cwd=repo_dir, check=True, capture_output=True
    )
    (repo_dir / "main-only.txt").write_text("default branch work\n")
    subprocess.run(["git", "add", "main-only.txt"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "default branch commit"], cwd=repo_dir, check=True
    )

    blare_root = repo_dir / ".blare"
    write_minimal_state(blare_root, analyzed_sha=side_sha)
    _write_valid_config(blare_root)

    result = run_blare_noninteractive(
        blare_bin, ["update"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "state.yaml" in result.output
    assert side_sha in result.output
    assert "blare analyze" in result.output
    assert "analyzed_sha" in result.output
