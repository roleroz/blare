"""e2e: R7 -- `blare update`'s empty-effective-delta short-circuit. T2.2 already
wired the code (`orchestrator._execute`'s step 6); this is the end-to-end proof
T3.1 supplies: same-commit delta exits 0, produces zero diff anywhere under
`.blare/`, never invokes the agent (no login needed, no transcript), and never
rewrites the recorded SHA.

Traces: R7.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import head_sha, init_repo, write_minimal_state

_CONFIG_CONTENT = "stack: prometheus\n"


def _blare_bin() -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert path.exists()
    return path


def test_e2e_update_empty_delta_is_up_to_date_with_zero_diff(tmp_path: Path) -> None:
    """The recorded SHA equals HEAD: `blare update` reports up to date, exits 0,
    writes nothing under `.blare/`, and needs no `BLARE_SDK_FIXTURES` seam at all
    (R7: "never invokes the agent")."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    sha = head_sha(repo_dir)
    blare_root = repo_dir / ".blare"
    write_minimal_state(blare_root, sha)
    (blare_root / "config.yaml").write_text(_CONFIG_CONTENT)

    before = {p.name: p.read_bytes() for p in blare_root.glob("*.yaml")}

    result = run_blare_noninteractive(
        blare_bin,
        ["update"],
        cwd=repo_dir,
        env={"XDG_STATE_HOME": str(tmp_path / "xdg")},
    )

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    # R14: the sessionless path states no transcript.
    assert "transcript" not in result.output

    after = {p.name: p.read_bytes() for p in blare_root.glob("*.yaml")}
    assert after == before
    assert head_sha(repo_dir) == sha
