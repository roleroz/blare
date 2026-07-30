"""e2e: R23 -- an existing config naming an unsupported stack, or a missing config
at `blare update` time, exits non-zero naming the file and the supported values.

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: ... R23, R24, R13".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import head_sha, init_repo, write_minimal_state


def test_e2e_refuses_unsupported_stack(tmp_path: Path) -> None:
    """An existing `.blare/config.yaml` naming an unsupported stack exits 1, naming
    the file and the supported values."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    blare_root = repo_dir / ".blare"
    write_minimal_state(blare_root, analyzed_sha=head_sha(repo_dir))
    (blare_root / "config.yaml").write_text("stack: bogus-stack\n")

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "bogus-stack" in result.output
    assert "prometheus" in result.output


def test_e2e_refuses_missing_config_at_update(tmp_path: Path) -> None:
    """A missing `.blare/config.yaml` at `blare update` time is the same refusal as
    an unsupported stack (R23); `blare analyze` would default instead."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    blare_root = repo_dir / ".blare"
    write_minimal_state(blare_root, analyzed_sha=head_sha(repo_dir))

    result = run_blare_noninteractive(
        blare_bin, ["update"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "config.yaml" in result.output
