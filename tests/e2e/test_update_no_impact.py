"""e2e: `blare update`'s R18 no-impact flow (T3.1) -- triage concludes the
non-empty delta affects no artifacts; the run presents that conclusion for
confirmation; approval is the final confirmation for the run and changes
exactly the recorded SHA (and any derived-doc restoration) -- no entry file
changes.

Uses the real, live-captured update-no-impact fixture (T4.1): kvstore's real
test_only_change delta (adds a tests/ dir, touches no production file).

Traces: R18.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e import kvstore_fixtures
from tests.e2e.pty_harness import PtyProcess, approve_all

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
    (needs no metrics/alerts to satisfy every R3-R5 invariant), so step 7's
    semantic check seeds nothing. This scenario's fixture makes no propose_edits
    calls at all (a genuine no_impact conclusion), so unlike the other update-mode
    e2e tests this starting content need not match any real bootstrap-analyze
    state -- nothing in the fixture references it by ID."""
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


def test_e2e_update_no_impact_confirmed_changes_only_the_sha(tmp_path: Path) -> None:
    """A real, test-only delta: the agent concludes no_impact, the no-impact
    screen is presented, and approving it advances only the recorded SHA --
    every canonical entry file keeps its exact bytes."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    _write_valid_update_state(blare_root, first_sha)
    before = {
        name: (blare_root / name).read_bytes()
        for name in (
            "system-map.yaml",
            "failure-modes.yaml",
            "metrics.yaml",
            "metric-recommendations.yaml",
            "alert-recommendations.yaml",
            "coverage.yaml",
        )
    }
    before_config = (blare_root / "config.yaml").read_bytes()

    second_sha = kvstore_fixtures.commit_test_only_change(repo_dir)
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-no-impact')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    result = approve_all(process)

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    assert "0 added · 0 updated · 0 removed" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    for name, content in before.items():
        assert (blare_root / name).read_bytes() == content, f"{name} changed on a no-impact run"
    assert (blare_root / "config.yaml").read_bytes() == before_config
