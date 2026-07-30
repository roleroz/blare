"""e2e: R19 -- Blare validates the canonical YAML on load and exits non-zero naming
the file and the problem, modifying nothing.

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: R11, R12, R17, R19, ...".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import head_sha, init_repo, write_minimal_state


def test_e2e_refuses_on_malformed_yaml(tmp_path: Path) -> None:
    """A canonical file that fails to parse as YAML exits 1, naming the file, and
    leaves `.blare/` untouched."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    blare_root = repo_dir / ".blare"
    write_minimal_state(blare_root, analyzed_sha=head_sha(repo_dir))
    (blare_root / "system-map.yaml").write_text("not: [valid: yaml\n")
    before = (blare_root / "system-map.yaml").read_bytes()

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(tmp_path / "xdg")}
    )

    assert result.exit_code == 1
    assert "system-map.yaml" in result.output
    assert (blare_root / "system-map.yaml").read_bytes() == before
