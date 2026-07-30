"""e2e: R2 -- chat at a checkpoint routes through the agent session, the reply
renders inline, and the run then proceeds through the remaining phases to a
completed run.

Uses the analyze-checkpoint-chat replay fixture: identical to analyze-happy-path,
with one chat exchange scripted right after phase 1's turn ends.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"


def test_e2e_checkpoint_chat_routes_and_represents(tmp_path: Path) -> None:
    """Typing free text at phase 1's checkpoint routes to the agent, the reply
    renders inline (the view is not redrawn), the checkpoint prompt re-offers, and
    approving from there proceeds through phases 2-4 to a completed run."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation(
            "blare/tests/fixtures/claude-sdk/analyze-checkpoint-chat/scenario.jsonl"
        )
    )
    assert blare_bin.exists()
    assert fixture_file.exists()

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
    # Phase 1's checkpoint (prompt occurrence 1): chat instead of approving.
    process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    process.send_line("what about the auth service?")
    # The chat reply renders inline and re-offers the same prompt -- occurrence 2
    # is that re-offer, still phase 1's checkpoint, not phase 2's.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=2)
    assert "Noted -- this codebase has no separate auth service" in output
    process.send_line("approve")

    # Phases 2-4's checkpoints are prompt occurrences 3, 4, 5 (the chat re-offer
    # above already consumed occurrence 2).
    for occurrence in (3, 4, 5):
        process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    assert (repo_dir / ".blare" / "state.yaml").is_file()
