"""e2e: R24 -- a state file whose schema version does not match the running
Blare's exits non-zero, naming both versions.

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: ... R24, R13".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from blare.artifacts import CURRENT_SCHEMA_VERSION
from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import init_repo


def test_e2e_refuses_on_schema_version_mismatch(tmp_path: Path) -> None:
    """A recorded schema version newer than this Blare understands exits 1, naming
    both versions."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    blare_root = repo_dir / ".blare"
    blare_root.mkdir()
    (blare_root / "state.yaml").write_text('analyzed_sha: "deadbeef"\nschema_version: 999\n')

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "999" in result.output
    assert f"version {CURRENT_SCHEMA_VERSION}" in result.output
