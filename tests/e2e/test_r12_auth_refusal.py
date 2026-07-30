"""e2e: R12 -- a run that would invoke the agent, finding no Claude Code
subscription login available, exits non-zero naming the login step.

Needs a real TTY (R22 must pass before step 9's auth preflight can even be
attempted), so this test drives `blare` through the PTY harness rather than a plain
subprocess, replaying a hand-authored fixture whose handshake reports
`auth_required` (`tests/fixtures/claude-sdk/auth-required/`, provisional --
`engineering/modules/agent.md`'s provisional-fixtures list names the auth-failure
handshake shape as one to capture for real in T4.1's release suite).

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: R11, R12, R17, ...".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import run_blare
from tests.e2e.repo_fixtures import init_repo


def test_e2e_refuses_when_not_logged_in(tmp_path: Path) -> None:
    """A handshake reporting `auth_required` exits 1, naming `claude` login as the
    next step (R12)."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/auth-required/scenario.jsonl")
    )
    assert fixture_file.exists(), f"auth-required fixture not found via Rlocation at {fixture_file}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)

    result = run_blare(
        blare_bin,
        ["analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}",
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
        },
    )

    assert result.exit_code == 1
    assert "claude" in result.output
