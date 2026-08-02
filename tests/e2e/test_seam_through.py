"""e2e: a seam-through run over the analyze-happy-path replay fixture, driving all
four checkpoints to a real completed run.

T1.1's own scenario here was a placeholder ("reaches session start and exits 0 with
a placeholder no-op summary (skeleton behavior, superseded by T2.2/T2.3)"); this is
that supersession -- the same wiring (cli -> orchestrator -> agent -> the fixture
seam -> artifacts) now exercised all the way through the real phase engine and write
path, which is a strictly stronger "does the wiring work" check than the placeholder
it replaces.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess, approve_all
from tests.e2e.repo_fixtures import init_repo


def test_e2e_seam_through_reaches_a_completed_run(tmp_path: Path) -> None:
    """A run over a minimal git repo, seamed through to the replay client via the
    real, live-captured analyze-happy-path fixture (T4.1), driving every real
    checkpoint the session presents (`approve_all`, since an organic mid-run
    amendment means the checkpoint count is no longer a fixed four) to a real
    completed run, exiting 0 with a real completed-run summary."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl")
    )
    assert fixture_file.exists(), f"analyze-happy-path fixture not found at {fixture_file}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)

    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}",
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
        },
    )
    result = approve_all(process)

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    assert (repo_dir / ".blare" / "state.yaml").is_file()
