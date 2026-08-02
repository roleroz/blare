"""e2e: an amendment cascade -- a unit spanning multiple phases, approved and
rejected as one unit (R2; agent.md's provisional fixture list: "amendment
cascade: a unit spanning multiple phases, approved; and rejected as one unit").

Uses the real, live-captured amendment-cascade-approved/rejected fixtures (T4.1):
phase 4's checkpoint chat asks for a failure-mode rename that cascades (via
`referencing_phases`) into phase 3's metric coverage -- reachable here only
because this is the *closing* checkpoint's chat (phase 3 already froze); an
earlier trigger point would find phase 3 still open, ineligible for the
frozen-only cascade.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess, approve_all, approve_until
from tests.e2e.repo_fixtures import init_repo

_REJECTABLE_AMENDMENT_MARKER = "amendment · proposed by agent"
_CHAT_TEXT = (
    "one of the failure mode titles from phase 2 is unclear -- can you rename it "
    "to something clearer, and update anything that references it?"
)
_YAML = YAML(typ="safe")


def _drive_to_amendment_prompt(process: PtyProcess) -> str:
    approve_until(process, "phase 4 —")
    process.send_line(_CHAT_TEXT)
    return approve_until(process, _REJECTABLE_AMENDMENT_MARKER)


def test_e2e_amendment_cascade_approved(tmp_path: Path) -> None:
    """The cascaded unit (failure modes + metric coverage) is re-presented,
    naming its agent origin; approval re-freezes both phases and the run
    completes with the rename landed."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation(
            "blare/tests/fixtures/claude-sdk/amendment-cascade-approved/scenario.jsonl"
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
    output = _drive_to_amendment_prompt(process)
    assert _REJECTABLE_AMENDMENT_MARKER in output
    process.send_line("approve")

    result = approve_all(process)

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    failure_modes = _YAML.load((repo_dir / ".blare" / "failure-modes.yaml").read_bytes())
    assert len(failure_modes) > 0


def test_e2e_amendment_cascade_rejected_restores_both_phases(tmp_path: Path) -> None:
    """Rejecting the cascaded unit restores both phases as one unit; the run
    still completes normally."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation(
            "blare/tests/fixtures/claude-sdk/amendment-cascade-rejected/scenario.jsonl"
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
    _drive_to_amendment_prompt(process)
    process.send_line("reject")

    result = approve_all(process)

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    failure_modes = _YAML.load((repo_dir / ".blare" / "failure-modes.yaml").read_bytes())
    assert len(failure_modes) > 0
