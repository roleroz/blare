"""e2e: R1's inverse refusal -- `blare analyze` with no state file, but canonical
entry-based files already present, exits non-zero naming them rather than
overwriting them.

Traces `engineering/architecture.md`'s T2.2 scope: "R1's inverse refusal (orphaned
canonical files) ... included".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import init_repo


def test_e2e_refuses_analyze_over_orphaned_canonical_files(tmp_path: Path) -> None:
    """A `.blare/failure-modes.yaml` with no `.blare/state.yaml` refuses analyze,
    naming the orphaned file, and creates no state file."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    blare_root = repo_dir / ".blare"
    blare_root.mkdir()
    (blare_root / "failure-modes.yaml").write_text("[]\n")

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "failure-modes.yaml" in result.output
    assert not (blare_root / "state.yaml").exists()
