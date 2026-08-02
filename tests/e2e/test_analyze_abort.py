"""e2e: R20 -- aborting at a checkpoint exits 3, writes nothing under `.blare/`, and
the summary names the transcript path with a discarded entry-count split.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess, approve_until
from tests.e2e.repo_fixtures import init_repo


def test_e2e_abort_at_second_checkpoint_writes_nothing(tmp_path: Path) -> None:
    """Approve every real checkpoint up to (not including) phase 2's own -- the
    real, live-captured analyze-happy-path fixture (T4.1) may present other real
    checkpoints (an organic amendment) before phase 2 ever comes up, all approved
    along the way -- then abort there: exit 3, `.blare/` never created, the
    summary reports "aborted" with a discarded entry-count split and the
    transcript path."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl")
    )
    assert blare_bin.exists()
    assert fixture_file.exists()

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"

    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    approve_until(process, "phase 2 —")
    process.send_line("abort")
    result = process.read_all_until_exit()

    assert result.exit_code == 3
    assert "aborted" in result.output
    # By the time phase 2's checkpoint presents, its edits already landed in the
    # pending candidate (checkpoints follow run_phase) -- the real, live-captured
    # session's actual phase 1 (plus whatever real amendment activity happened
    # along the way) and phase 2 content, all discarded by the abort.
    assert "discarded: 59 added · 0 updated · 0 removed" in result.output
    assert not (repo_dir / ".blare").exists()

    # R14: the transcript still exists and is named, even though nothing under
    # .blare/ was written.
    [repo_id_dir] = list((xdg_state / "blare").iterdir())
    [transcript_path] = list((repo_id_dir / "transcripts").glob("*.jsonl"))
    assert str(transcript_path) in result.output
    assert transcript_path.is_file()
