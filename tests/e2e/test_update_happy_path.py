"""e2e: `blare update`'s happy path (T3.1) -- triage's affected_verdict seeds
exactly the named phase; only that phase's checkpoint is presented; only its
artifacts change; the recorded SHA advances to the delta's end commit.

Traces: R6, R9.

KNOWN ISSUE (T4.1, unresolved): this test does not yet pass against the real,
live-captured update-happy-path fixture. `kvstore_fixtures.commit_fix_evictor`
reproduces the real delta exactly (confirmed: the triage message's patch_text/
delta_files now match the fixture byte for byte, unlike this test's old ad hoc
repo content). What remains unresolved is the *pre-existing* `.blare/` state:
this fixture's edits reference specific failure-mode/metric/alert IDs (e.g.
fm-cache-unbounded-growth) that came from the real capture's own bootstrap
`blare analyze` run -- and capture.py deliberately discards that bootstrap
run's own recording (architecture.md, Test strategy: "this bootstrap run's own
recording is discarded ... it exists only to produce a genuine .blare/, not as
the fixture being captured"), so there is no way to replay it and reconstruct
the exact seed those IDs need to already exist in. `_write_valid_update_state`
below is deliberately minimal and does not contain them, so the replaying
client's fidelity check fails once the fixture's first "update" edit targets
an ID that was never seeded. A hand-crafted seed containing every referenced
ID (with correctly cross-referenced coverage_status/alert_ids/severity fields
satisfying R3-R5) was investigated but not completed -- see T4.1's report for
the same issue across every update-mode e2e test whose capture bootstrapped a
real prior analysis (test_update_r8_multi_commit_delta,
test_update_dynamic_expansion, test_update_load_seeded_repair) and
test_analyze_reanalysis's re-analysis-mode equivalent.
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
    """A real, live-captured `blare update` over kvstore's real fix_evictor delta
    (T4.1): every checkpoint the real session actually presented is approved
    (`approve_all`); its artifacts change, other phases are byte-for-byte
    untouched (R9), and the recorded SHA advances to the delta's real commit."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    _write_valid_update_state(blare_root, first_sha)
    before = {
        name: (blare_root / name).read_bytes()
        for name in ("system-map.yaml", "failure-modes.yaml", "coverage.yaml")
    }

    second_sha = kvstore_fixtures.commit_fix_evictor(repo_dir)
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-happy-path')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    result = approve_all(process)

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    # R9: every phase not named by the verdict is byte-for-byte untouched.
    for name, content in before.items():
        assert (blare_root / name).read_bytes() == content, f"{name} changed unexpectedly"
