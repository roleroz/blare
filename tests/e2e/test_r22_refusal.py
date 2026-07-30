"""e2e: R22 -- the MVP is interactive-only; when stdin is not a TTY, the run exits
non-zero saying so instead of hanging or skipping confirmations, before any agent
session (no login needed).

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: ... R21, R22, R23, ...".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import init_repo


def test_e2e_refuses_non_interactive_stdin(tmp_path: Path) -> None:
    """A fresh repo run through a plain (non-PTY) subprocess exits 1 at the TTY
    check, never attempting login (no `BLARE_SDK_FIXTURES` is set for this test)."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "TTY" in result.output
