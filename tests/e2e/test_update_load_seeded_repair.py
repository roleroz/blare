"""e2e: R18's load-seeded violation repair (T3.2) -- the loaded state already
violates R4 (an unmapped failure mode, `fm-orphan-injected`, with no alert
coverage); triage settles on an unrelated phase, and the orchestrator's own
proactive post-triage check (`_repair_residual_violations`) surfaces the
violation via `request_repair` right after `triage()` returns, before the queue
is ever drained -- `request_repair` is the only channel that can ever tell the
model about a load-time violation (agent.md: "loaded-state violations do not
travel in RunContext").

T4.1: this fixture is a release-suite capture of the live Claude Agent SDK
against `~/external_git/miniflux_v2`'s real analyzed state (an already-analyzed
catalog, hand-seeded with one extra, deliberately fabricated unmapped failure
mode) -- the real session needed three repair rounds before converging: two
phase-4-only patches were each rejected as `linkage_inconsistency` (no real
alert can have genuine detection linkage to a fabricated failure mode), until
the model diagnosed the actual defect was the phase-2 `coverage_status` value
itself, escalated via `amend_proposal` to cross into phase 2, and reclassified
the entry as `excluded` instead. The seed data below carries just enough of the
real catalog (the two failure modes and the one alert the real capture's edits
reference) for the replay to succeed.

Traces `engineering/architecture.md`'s T3.2 scope: "load-seeded violations
discovered during an update run ... Traces: R15, R18 (dynamic clauses)".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import head_sha, init_repo

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


def _write_update_state_with_semantic_violation(blare_root: Path, analyzed_sha: str) -> None:
    """A structurally valid but semantically *violating* `.blare/`, carrying the
    subset of the real capture's catalog its edits actually reference:
    `fm-orphan-injected` (non-excluded, no alert coverage -- step 7's semantic
    check seeds its repair phase, 4, per `model._REPAIR_PHASE`), plus the two
    pre-existing failure modes and the one alert the repair rounds read and
    rewrite (`ar-scrape-target-down`, initially covering the other two only)."""
    blare_root.mkdir(parents=True, exist_ok=True)
    (blare_root / "state.yaml").write_text(
        f'analyzed_sha: "{analyzed_sha}"\nschema_version: 1\n'
    )
    (blare_root / "config.yaml").write_text("stack: prometheus\n")
    (blare_root / "system-map.yaml").write_text("[]\n")
    (blare_root / "failure-modes.yaml").write_text(
        "- id: fm-daemon-process-down\n"
        "  title: Daemon process down / unreachable\n"
        "  description: the process is not running or unreachable\n"
        "  severity: critical\n"
        "  user_visible: true\n"
        "  caused_by: []\n"
        "  coverage_status: alertable\n"
        "- id: fm-metrics-scrape-failure\n"
        "  title: Prometheus cannot scrape /metrics\n"
        "  description: prometheus cannot successfully scrape /metrics\n"
        "  severity: critical\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: alertable\n"
        "- id: fm-orphan-injected\n"
        "  title: hand-injected unmapped failure mode (T4.1 update-load-seeded-repair capture)\n"
        "  description: deliberately hand-added with coverage_status alertable but no alert\n"
        "    coverage\n"
        "  severity: warning\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: alertable\n"
    )
    (blare_root / "metrics.yaml").write_text("[]\n")
    (blare_root / "metric-recommendations.yaml").write_text("[]\n")
    (blare_root / "alert-recommendations.yaml").write_text(
        "- id: ar-scrape-target-down\n"
        "  name: MinifluxScrapeTargetDown\n"
        '  expr: up{job="miniflux"} == 0\n'
        "  for_duration: 2m\n"
        "  severity: critical\n"
        "  failure_mode_ids:\n"
        "  - fm-daemon-process-down\n"
        "  - fm-metrics-scrape-failure\n"
        "  annotations:\n"
        "    summary: Miniflux is not responding to Prometheus scrapes\n"
        "    description: the miniflux scrape target has been unreachable\n"
    )
    (blare_root / "coverage.yaml").write_text(
        "- failure_mode_id: fm-daemon-process-down\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids:\n"
        "  - ar-scrape-target-down\n"
        "- failure_mode_id: fm-metrics-scrape-failure\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids:\n"
        "  - ar-scrape-target-down\n"
        "- failure_mode_id: fm-orphan-injected\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids: []\n"
    )


def _commit_two_files(repo_dir: Path, message: str) -> str:
    """Commit two files in one commit -- the real capture's delta touched both
    `server.go` and `server_test.go` in the same commit (`92057dde`), and the
    replaying client's byte-exact comparison needs this test's synthetic delta
    to name exactly the same file list the recorded triage event carries."""
    (repo_dir / "internal" / "http" / "server").mkdir(parents=True, exist_ok=True)
    (repo_dir / "internal/http/server/server.go").write_text("// socket perms\n")
    (repo_dir / "internal/http/server/server_test.go").write_text("// socket perms test\n")
    subprocess.run(
        ["git", "add", "internal/http/server/server.go", "internal/http/server/server_test.go"],
        cwd=repo_dir,
        check=True,
    )
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo_dir, check=True)
    return head_sha(repo_dir)


def test_e2e_update_load_seeded_violation_repaired_proactively(tmp_path: Path) -> None:
    """triage settles on phase 2 (a real Unix-socket-permission delta); the
    proactive repair for the load-seeded `fm-orphan-injected` violation
    presents before either phase's ordinary checkpoint, and needs three rounds
    -- two phase-4-only patches rejected as `linkage_inconsistency`, then an
    escalation into phase 2 that reclassifies the entry as `excluded` -- before
    the run reaches a clean invariant state and completes."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    first_sha = head_sha(repo_dir)
    _write_update_state_with_semantic_violation(blare_root, first_sha)
    coverage_before = {
        c["failure_mode_id"]: c for c in _load_yaml(blare_root / "coverage.yaml")
    }
    assert coverage_before["fm-orphan-injected"]["alert_ids"] == []

    second_sha = _commit_two_files(repo_dir, "chmod fix")
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-load-seeded-repair')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    # The real capture's proactive repair needed three rounds (two rejected
    # phase-4-only patches, then an escalation into phase 2) before the
    # amendment closed and the ordinary phase 2/4 checkpoints could present --
    # approve every prompt in sequence until the run completes, rather than
    # hardcoding which occurrence is which view (the real interaction is
    # richer than a single amendment-then-two-checkpoints sequence).
    output = ""
    seen_invariant_repair = False
    for occurrence in range(1, 30):
        try:
            output = process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence, timeout=10.0)
        except TimeoutError:
            break
        if "amendment · invariant repair" in output:
            seen_invariant_repair = True
        process.send_line("approve")
    result = process.read_all_until_exit()

    assert seen_invariant_repair, "the proactive repair's system amendment never appeared"
    assert result.exit_code == 0, result.output
    assert "update complete" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    # The repair converged by reclassifying fm-orphan-injected as excluded
    # (the structurally correct fix, per the real capture's own diagnosis) --
    # not by leaving it as a residual, still-unmapped violation.
    failure_modes = {fm["id"]: fm for fm in _load_yaml(blare_root / "failure-modes.yaml")}
    assert failure_modes["fm-orphan-injected"]["coverage_status"] == "excluded"
    assert failure_modes["fm-orphan-injected"]["exclusion_reason"]

    alerts = {a["id"]: a for a in _load_yaml(blare_root / "alert-recommendations.yaml")}
    assert alerts["ar-scrape-target-down"]["failure_mode_ids"] == [
        "fm-daemon-process-down",
        "fm-metrics-scrape-failure",
    ]
    coverage = {c["failure_mode_id"]: c for c in _load_yaml(blare_root / "coverage.yaml")}
    assert coverage["fm-orphan-injected"]["alert_ids"] == []

    # A new failure mode from the real delta itself (the Unix-socket permission
    # change), excluded with its own reasoning -- distinct from the repair.
    assert "fm-unix-socket-permission-mismatch" in failure_modes
    assert failure_modes["fm-unix-socket-permission-mismatch"]["coverage_status"] == "excluded"
