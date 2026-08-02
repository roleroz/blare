"""e2e: R18's redirect at the no-impact confirmation (T3.2) -- triage
concludes `no_impact`; chat at that confirmation issues a bare
`affected_verdict` naming a phase, withdrawing the conclusion for good (no
amendment involved, so there is no reject/restore path back to it). The
withdrawn conclusion's own prompt is mooted (never re-offered), and the newly
opened phase gets its own ordinary checkpoint before the write.

Uses the real, live-captured update-no-impact-redirect fixture (T4.1): kvstore's
real docs_update delta (adds a "Metrics" section to the README documenting the
existing counter's own known coverage gap), with a directive real chat redirect
naming that gap.

Traces `engineering/architecture.md`'s T3.2 scope: "a redirect path when chat
happens during the no-impact confirmation ... Traces: ... R18 (dynamic
clauses)".
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e import kvstore_fixtures
from tests.e2e.pty_harness import PtyProcess, approve_all, approve_until

_YAML = YAML(typ="safe")
_REDIRECT_TEXT = (
    "wait -- the metrics section this commit adds even says the counter doesn't "
    "cover staleness, unbounded cache growth, or storage collisions. I think a "
    "coverage gap that's known and left undocumented is worth its own failure "
    "mode in phase 2 (an operator can't detect any of those bug classes from "
    "this metric alone) -- can you open phase 2 for this delta?"
)


def _blare_bin() -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert path.exists()
    return path


def _fixture_dir(name: str) -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(runfiles.Rlocation(f"blare/tests/fixtures/claude-sdk/{name}/scenario.jsonl")).parent
    assert (path / "scenario.jsonl").exists()
    return path


def _write_valid_update_state(blare_root: Path, analyzed_sha: str) -> None:
    """A structurally and semantically valid `.blare/`: one excluded failure
    mode, so step 7's semantic check seeds nothing -- only the redirect
    mechanism itself is under test here. This fixture's edits are all newly
    added IDs (self-consistent within the session, no bootstrap-analyze IDs
    referenced), so this starting content need not match any real prior
    analysis."""
    blare_root.mkdir(parents=True, exist_ok=True)
    (blare_root / "state.yaml").write_text(
        f'analyzed_sha: "{analyzed_sha}"\nschema_version: 1\n'
    )
    (blare_root / "config.yaml").write_text("stack: prometheus\n")
    (blare_root / "system-map.yaml").write_text("[]\n")
    (blare_root / "failure-modes.yaml").write_text(
        "- id: fm-timeout\n"
        "  title: upstream timeout\n"
        "  description: a call to an upstream service times out\n"
        "  severity: warning\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: excluded\n"
        "  exclusion_reason: not independently detectable\n"
    )
    (blare_root / "metrics.yaml").write_text("[]\n")
    (blare_root / "metric-recommendations.yaml").write_text("[]\n")
    (blare_root / "alert-recommendations.yaml").write_text("[]\n")
    (blare_root / "coverage.yaml").write_text(
        "- failure_mode_id: fm-timeout\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids: []\n"
    )


def test_e2e_update_no_impact_redirect_withdraws_conclusion(tmp_path: Path) -> None:
    """The no-impact screen is shown, the real chat redirect withdraws it into
    phase 2, and that phase's own ordinary checkpoint is what the run pauses at
    next -- the no-impact prompt itself never returns."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    _write_valid_update_state(blare_root, first_sha)

    second_sha = kvstore_fixtures.commit_docs_update(repo_dir)
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-no-impact-redirect')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    # The no-impact confirmation itself.
    output = approve_until(process, "no changes needed")
    assert "no changes needed" in output
    process.send_line(_REDIRECT_TEXT)

    # Phase 2's own ordinary checkpoint -- the no-impact prompt never comes
    # back around; "no changes needed" only ever appeared once (its own header,
    # never a second, stale presentation of the withdrawn conclusion).
    output = approve_until(process, "phase 2 —")
    assert output.count("no changes needed") == 1

    result = approve_all(process)

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == second_sha
