"""e2e: R2 -- chat at a checkpoint routes through the agent session, the reply
renders inline, and the run then proceeds through the remaining phases to a
completed run.

Uses the real, live-captured analyze-checkpoint-chat fixture (T4.1): the same
kvstore codebase as analyze-happy-path, with one real chat exchange at phase 1's
own checkpoint (the very first prompt of the run, so no organic amendment can
have preceded it -- capture.py's own design for this scenario).
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess, approve_all, approve_until
from tests.e2e.repo_fixtures import init_repo

_CHAT_TEXT = (
    "what about the admin write path in admin.py -- it's not reachable from "
    "api.py's public surface at all, does that matter here?"
)
_REPLY_MARKER = "It matters, but it cuts the opposite way from exclusion"


def test_e2e_checkpoint_chat_routes_and_represents(tmp_path: Path) -> None:
    """Typing the real chat text at phase 1's checkpoint routes to the agent, the
    real reply renders inline (the view is not redrawn), the checkpoint prompt
    re-offers, and approving from there proceeds through every remaining real
    checkpoint (`approve_all`, since an organic mid-run amendment means the
    checkpoint count is no longer a fixed few) to a completed run."""
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
    # Phase 1's checkpoint is the very first prompt: chat instead of approving.
    approve_until(process, "phase 1 —")
    process.send_line(_CHAT_TEXT)
    # The chat reply renders inline and re-offers the same prompt.
    output = approve_until(process, _REPLY_MARKER)
    assert _REPLY_MARKER in output
    process.send_line("approve")

    result = approve_all(process)

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    assert (repo_dir / ".blare" / "state.yaml").is_file()
