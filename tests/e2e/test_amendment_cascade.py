"""e2e: an amendment cascade -- a unit spanning multiple phases, approved and
rejected as one unit (R2; agent.md's provisional fixture list: "amendment
cascade: a unit spanning multiple phases, approved; and rejected as one unit").

Uses the amendment-cascade-approved/rejected replay fixtures: phase 4's
checkpoint chat proposes an amendment naming phase 2 (failure modes); the
repair renames fm-a, which `metric_recommendations`' mr-x references, cascading
the unit into phase 3 (metric coverage) via `referencing_phases` -- reachable
here only because this is the *closing* checkpoint's chat (phase 3 already
froze); an earlier trigger point would find phase 3 still open, ineligible for
the frozen-only cascade.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_REJECTABLE_AMENDMENT_PROMPT = "$ approve · reject · abort · anything else is chat"
_YAML = YAML(typ="safe")


def _drive_to_amendment_prompt(process: PtyProcess) -> str:
    for occurrence in (1, 2, 3):
        process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")
    process.read_until(_CHECKPOINT_PROMPT, occurrence=4)
    process.send_line("actually let's rename fm-a while it's fresh")
    return process.read_until(_REJECTABLE_AMENDMENT_PROMPT, occurrence=1)


def test_e2e_amendment_cascade_approved(tmp_path: Path) -> None:
    """The cascaded unit (failure modes + metric coverage) is re-presented once,
    naming both phases; approval re-freezes both and the rename lands."""
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
    assert "phase 2" in output
    assert "phase 3" in output
    assert "web returns errors (renamed)" in output
    process.send_line("approve")

    process.read_until(_CHECKPOINT_PROMPT, occurrence=5)
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    failure_modes = _YAML.load((repo_dir / ".blare" / "failure-modes.yaml").read_bytes())
    [fm] = failure_modes
    assert fm["title"] == "web returns errors (renamed)"


def test_e2e_amendment_cascade_rejected_restores_both_phases(tmp_path: Path) -> None:
    """Rejecting the cascaded unit restores both phases as one unit: the rename
    reverts and metric coverage (which the cascade only pulled in, untouched by
    any repair) is unaffected either way."""
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

    process.read_until(_CHECKPOINT_PROMPT, occurrence=5)
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    failure_modes = _YAML.load((repo_dir / ".blare" / "failure-modes.yaml").read_bytes())
    [fm] = failure_modes
    assert fm["title"] == "web returns errors"
    metric_recommendations = _YAML.load(
        (repo_dir / ".blare" / "metric-recommendations.yaml").read_bytes()
    )
    assert {mr["id"] for mr in metric_recommendations} == {"mr-x"}
