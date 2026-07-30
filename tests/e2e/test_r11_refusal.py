"""e2e: R11's first refusal clause -- outside a git repository, exit 1.

Traces T1.1's e2e scope: "the R11 refusal (outside a git repository, exit 1)".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare


def test_e2e_refuses_outside_git_repository(tmp_path: Path) -> None:
    """Running `blare analyze` outside any git repository exits 1 and names why."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    result = run_blare(blare_bin, ["analyze"], cwd=tmp_path)

    assert result.exit_code == 1
    assert "not inside a git repository" in result.output
