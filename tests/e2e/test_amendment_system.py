"""e2e: a system-originated amendment -- a semantic violation at the approval
gate (R2, R3-R5; agent.md's provisional fixture list: "system-originated
amendment (semantic violation at the approval gate)").

Uses the amendment-system replay fixture: phase 4 deliberately leaves fm-a
unmapped (no alert, no coverage update), so the approval gate finds an
`UNMAPPED_FAILURE_MODE` violation once all four phases have frozen and opens a
system-originated unit for it -- no `amend_proposal` involved, and no reject
offered at its re-presentation.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_YAML = YAML(typ="safe")


def test_e2e_amendment_system_originated_on_gate_violation(tmp_path: Path) -> None:
    """All four phases approve normally; the gate then opens a system unit
    (origin "invariant repair"), offering no reject wording; approving it writes
    the repaired set."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/amendment-system/scenario.jsonl")
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
    for occurrence in (1, 2, 3, 4):
        process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")

    # The system amendment's prompt reuses the plain (non-rejectable) checkpoint
    # wording -- occurrence 5 is its own presentation, not a 5th ordinary phase.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=5)
    assert "amendment · invariant repair" in output
    assert "reject" not in output
    assert "ar-a" in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    assert "analysis complete" in result.output

    alerts = _YAML.load((repo_dir / ".blare" / "alert-recommendations.yaml").read_bytes())
    assert {ar["id"] for ar in alerts} == {"ar-a"}
    coverage = _YAML.load((repo_dir / ".blare" / "coverage.yaml").read_bytes())
    [cov] = coverage
    assert cov["alert_ids"] == ["ar-a"]
