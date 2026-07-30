"""e2e: R11's refusal clauses -- outside a git repository, and no commits yet.

Traces T1.1's e2e scope: "the R11 refusal (outside a git repository, exit 1)" (the
first clause). The no-commits clause is new in T2.2 (`engineering/architecture.md`'s
Tasks section: "R1's inverse refusal ... included. Traces: R11 ...") -- T1.1's
preflight never called `gitrepo.head_sha`, so an unborn-HEAD repo passed through
uncaught until T2.2's full nine-step sequence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive


def test_e2e_refuses_outside_git_repository(tmp_path: Path) -> None:
    """Running `blare analyze` outside any git repository exits 1 and names why."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    result = run_blare_noninteractive(blare_bin, ["analyze"], cwd=tmp_path)

    assert result.exit_code == 1
    assert "not inside a git repository" in result.output


def test_e2e_refuses_repository_with_no_commits(tmp_path: Path) -> None:
    """Running `blare analyze` in a git repository with no commits yet exits 1 and
    names why (R11's second clause), before ever reaching the lock or artifacts."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo_dir, check=True)

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "no commits" in result.output
