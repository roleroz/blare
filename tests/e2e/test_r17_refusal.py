"""e2e: R17 -- `blare update` in a repo without a state file exits non-zero and
names `blare analyze` as the first step.

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: R11, R12, R17, ...".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import init_repo


def test_e2e_refuses_update_without_state_file(tmp_path: Path) -> None:
    """`blare update` on a never-analyzed repo exits 1 and names `blare analyze`."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)

    result = run_blare_noninteractive(
        blare_bin, ["update"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "state.yaml" in result.output
    assert "blare analyze" in result.output
