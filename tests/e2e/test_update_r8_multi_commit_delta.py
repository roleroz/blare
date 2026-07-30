"""e2e: R8 -- diff mode handles a range spanning multiple commits as one delta,
not per-commit. Two commits after the recorded SHA, each touching a different
file, must reach the agent as a single triage message naming both changed
files, and the recorded SHA must land on the range's end commit, not the first.

Traces: R8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import commit_file, head_sha, init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_YAML = YAML(typ="safe")


def _load_yaml(path: Path) -> Any:
    # A YAML file's shape isn't statically known; this test's own assertions below
    # are the real type check.
    return _YAML.load(path.read_bytes())


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
    """A structurally and semantically valid `.blare/`: one excluded failure mode
    needs no metrics/alerts to satisfy every R3-R5 invariant, so step 7's semantic
    check seeds nothing."""
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


def test_e2e_update_multi_commit_range_is_one_delta_with_end_sha_recorded(
    tmp_path: Path,
) -> None:
    """Two commits after the recorded SHA (touching src/a.py, then src/b.py)
    reach the agent as one triage message naming both files -- not two runs, not
    two triage calls -- and the recorded SHA lands on the second (final)
    commit."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    first_sha = head_sha(repo_dir)
    _write_valid_update_state(blare_root, first_sha)

    commit_file(repo_dir, "src/a.py", "# module a\n", "add module a")
    final_sha = commit_file(repo_dir, "src/b.py", "# module b\n", "add module b")
    assert final_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-multi-commit')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "phase 3 " in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    # R6/R8: the recorded SHA is the range's end commit, captured at run start --
    # not the first of the two new commits, and the replayed fixture (which
    # names both src/a.py and src/b.py in one triage message) only matches at
    # all because gitrepo computed one delta over the whole range.
    assert state["analyzed_sha"] == final_sha
    assert state["analyzed_sha"] != first_sha

    metrics = {m["id"]: m for m in _load_yaml(blare_root / "metrics.yaml")}
    assert set(metrics) == {"mx-multi"}
