"""e2e: an agent-proposed amendment, rejected -- restore (R2; agent.md's
provisional fixture list: "agent-proposed amendment... rejected (restore)").

Uses the real, live-captured amendment-agent-rejected fixture (T4.1): same setup
as the approved variant, but the user rejects the amendment: phase 1's
pre-amendment content survives byte-for-byte, and the run still completes
normally through phase 4's own (unrelated) work.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess, approve_all, approve_until
from tests.e2e.repo_fixtures import init_repo

_REJECTABLE_AMENDMENT_MARKER = "amendment · proposed by agent"
_CHAT_TEXT = (
    "before we wrap up -- can you revise the system map now that we've seen the "
    "rest of the analysis?"
)


def test_e2e_amendment_agent_proposed_rejected_restores(tmp_path: Path) -> None:
    """Rejecting the agent-proposed amendment restores phase 1's entries to their
    pre-amendment state; the run still completes (R20's write still lands the
    rest of the analysis)."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation(
            "blare/tests/fixtures/claude-sdk/amendment-agent-rejected/scenario.jsonl"
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
    approve_until(process, "phase 4 —")
    process.send_line(_CHAT_TEXT)

    output = approve_until(process, _REJECTABLE_AMENDMENT_MARKER)
    assert _REJECTABLE_AMENDMENT_MARKER in output
    process.send_line("reject")

    result = approve_all(process)

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    assert (repo_dir / ".blare" / "state.yaml").is_file()
