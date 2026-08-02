"""e2e: `blare update`'s happy path (T3.1) -- triage's affected_verdict seeds
exactly the named phase; only that phase's checkpoint is presented; only its
artifacts change; the recorded SHA advances to the delta's end commit.

Traces: R6, R9.
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
    (needs no metrics/alerts to satisfy every R3-R5 invariant), so step 7's
    semantic check seeds nothing and the run's only affected phase is the one
    triage names."""
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


def test_e2e_update_happy_path_only_affected_phase_pauses_and_changes(
    tmp_path: Path,
) -> None:
    """triage's affected_verdict names phase 3 only: its checkpoint is the only
    one presented, its artifacts are the only ones that change, and the recorded
    SHA advances to the commit that introduced the new metric."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    first_sha = head_sha(repo_dir)
    _write_valid_update_state(blare_root, first_sha)
    before = {
        name: (blare_root / name).read_bytes()
        for name in ("system-map.yaml", "failure-modes.yaml", "coverage.yaml")
    }

    second_sha = commit_file(
        repo_dir, "src/metrics.py", "# metrics module\n", "add metrics module"
    )
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-happy-path')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "phase 3 " in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    assert "1 added · 0 updated · 0 removed" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    metrics = {m["id"]: m for m in _load_yaml(blare_root / "metrics.yaml")}
    assert set(metrics) == {"mx-new"}
    assert metrics["mx-new"]["type"] == "counter"

    # R9: every phase not named by the verdict is byte-for-byte untouched.
    for name, content in before.items():
        assert (blare_root / name).read_bytes() == content, f"{name} changed unexpectedly"
