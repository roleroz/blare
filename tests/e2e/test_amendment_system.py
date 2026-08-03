"""e2e: a system-originated amendment -- a semantic violation at the approval
gate (R2, R3-R5; agent.md's provisional fixture list: "system-originated
amendment (semantic violation at the approval gate)").

Captured for real (2026-08-02, T4.1 continuation) -- despite `agent.md` and
`architecture.md` describing this scenario as already captured on a prior
"second, convergent attempt," the committed fixture had in fact never been
touched since its original 2026-07-30 hand-authored provisional version (a
doc/reality mismatch predating this task, confirmed by inspecting git history
directly); this is therefore this scenario's first genuine real capture, not
a recapture. It took three real attempts: the first organically resolved the
seeded `fm-orphan-injected` violation via an agent-proposed amendment before
the system gate ever ran (the same non-convergence class documented for
`update-load-seeded-repair`); the second hit a transient SDK rate-limit error
mid-run; the third converged to the intended shape.

Bootstraps a real analysis at `genesis` (`kvstore_fixtures.
bootstrap_analyze_happy_path`) and hand-injects the same violation the real
capture injected (`kvstore_fixtures.inject_unmapped_failure_mode`), then
replays the real re-analysis session on top -- mirrors `tests/release/
capture.py`'s own `capture_amendment_system`. The real session's own
re-analysis is far richer than the scenario's original one-failure-mode
premise: rather than reconciling with the 26 pre-existing failure modes, the
model documents ~22 further ones from its own fresh read of the codebase
(distinct IDs, no removals), each needing metric/alert coverage -- producing
several genuine system-originated repair rounds (unmapped failure modes and
linkage inconsistencies) before the seeded `fm-orphan-injected` violation
(among many others) is fully, correctly resolved. No agent-proposed amendment
occurs this time -- the run reaches the final gate with the violation still
unresolved and the system-originated repair is what closes it, the mechanism
this scenario exists to demonstrate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.e2e import kvstore_fixtures
from tests.e2e.pty_harness import PtyProcess, approve_all

_YAML = YAML(typ="safe")


def _load_yaml(path: Path) -> Any:
    # A YAML file's shape isn't statically known; this test's own assertions below
    # are the real type check.
    return _YAML.load(path.read_bytes())


def test_e2e_amendment_system_originated_on_gate_violation(tmp_path: Path) -> None:
    """A real re-analysis over a hand-seeded R4 violation reaches the final
    approval gate with the violation still unresolved (no agent-proposed
    amendment along the way); the gate opens a system-originated unit
    ("invariant repair", no reject offered), and approving the resulting
    repair rounds writes a fully consistent artifact set -- the seeded
    violation included."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/amendment-system/scenario.jsonl")
    )
    assert blare_bin.exists()
    assert fixture_file.exists()

    repo_dir = tmp_path / "repo"
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    genesis_sha = kvstore_fixtures.build_genesis(repo_dir)
    kvstore_fixtures.bootstrap_analyze_happy_path(blare_bin, repo_dir, xdg_state)
    kvstore_fixtures.inject_unmapped_failure_mode(blare_root)

    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    result = approve_all(process)

    assert result.exit_code == 0, result.output
    assert "analysis complete" in result.output
    assert "amendment · invariant repair" in result.output
    # No agent-proposed amendment happens this run (unlike a first, non-
    # convergent attempt at this same scenario) -- confirmed by the absence of
    # the rejectable prompt's own reserved word anywhere in the whole session.
    assert "reject" not in result.output
    # Deterministic replay output: exact final counts.
    assert "63 added · 6 updated · 0 removed" in result.output
    assert "5 alertable · 38 metric-gap · 4 excluded" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == genesis_sha

    # The seeded violation is fully, correctly resolved: alertable, with a
    # real alert covering it on both sides of the coverage ledger.
    failure_modes = {fm["id"]: fm for fm in _load_yaml(blare_root / "failure-modes.yaml")}
    orphan_fm = failure_modes["fm-orphan-injected"]
    assert orphan_fm["coverage_status"] == "alertable"
    coverage = {c["failure_mode_id"]: c for c in _load_yaml(blare_root / "coverage.yaml")}
    orphan_coverage = coverage["fm-orphan-injected"]
    assert orphan_coverage["alert_ids"]
    alerts = {a["id"]: a for a in _load_yaml(blare_root / "alert-recommendations.yaml")}
    for alert_id in orphan_coverage["alert_ids"]:
        assert "fm-orphan-injected" in alerts[alert_id]["failure_mode_ids"]

    # The whole artifact set is internally consistent -- no residual R3-R5
    # violations left over from the repair.
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
