"""e2e: a seam-through run reaching agent session start over a handshake-only
replay fixture, exiting 0 with a placeholder no-op summary.

Traces T1.1's e2e scope: "a seam-through run in a minimal temp repo with a
handshake-only replay fixture that reaches session start and exits 0 with a
placeholder no-op summary (skeleton behavior, superseded by T2.2/T2.3)".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare


def _init_minimal_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text(
        "minimal repo for the T1.1 seam-through e2e test\n"
    )
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial commit"], cwd=repo_dir, check=True)


def test_e2e_seam_through_reaches_session_start(tmp_path: Path) -> None:
    """A run over a minimal git repo, seamed through to the replay client via a
    handshake-only fixture, reaches session start and exits 0."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/handshake/handshake.jsonl")
    )
    assert fixture_file.exists(), f"handshake fixture not found via Rlocation at {fixture_file}"

    repo_dir = tmp_path / "repo"
    _init_minimal_repo(repo_dir)

    result = run_blare(
        blare_bin,
        ["analyze"],
        cwd=repo_dir,
        env={"BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}"},
    )

    assert result.exit_code == 0
    assert "no changes" in result.output
