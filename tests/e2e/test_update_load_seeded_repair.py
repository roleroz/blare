"""e2e: R18's load-seeded violation repair (T3.2) -- the loaded state already
violates R4 (an unmapped failure mode, `fm-orphan-injected`, with no alert
coverage); triage settles on an unrelated phase, and the orchestrator's own
proactive post-triage check (`_repair_residual_violations`) surfaces the
violation via `request_repair` right after `triage()` returns, before the
queue is ever drained -- `request_repair` is the only channel that can ever
tell the model about a load-time violation (agent.md: "loaded-state
violations do not travel in RunContext").

Traces `engineering/architecture.md`'s T3.2 scope: "load-seeded violations
discovered during an update run ... Traces: R15, R18 (dynamic clauses)".

Recaptured (2026-08-02, T4.1 continuation) against the fixed bootstrap
(decisions.md: "Bootstrap via replaying analyze-happy-path, not a fresh live
call"): the prior `.blare/` this test needs is built by replaying the
already-captured, real `analyze-happy-path` fixture
(`kvstore_fixtures.bootstrap_analyze_happy_path`), then hand-injecting the
same violation the real capture injected
(`kvstore_fixtures.inject_unmapped_failure_mode`), and the delta is kvstore's
real `docs_update` commit (`kvstore_fixtures.commit_docs_update`).

A previous real attempt (pre-dating this recapture) found the model
resolving the seeded `fm-orphan-injected` violation itself, via an organic
agent-proposed amendment, before the system-originated repair ever got a
turn -- the same non-convergence class documented for `amendment-system`'s
first attempt and `analyze-reanalysis-noop` (agent.md). This recapture
converges to the intended shape: `request_repair` fires immediately after
triage (before any phase runs), the model closes it with a genuine
system-originated "invariant repair", and triage settles on phase 3 (not
phase 2, as an earlier, since-corrected note on this scenario assumed --
`no_impact` is rejected outright while a repair is queued, forcing an
`affected_verdict`, and the model picked phase 3 as the closest match to the
README's subject matter).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e import kvstore_fixtures
from tests.e2e.pty_harness import PtyProcess

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


def test_e2e_update_load_seeded_violation_repaired_proactively(tmp_path: Path) -> None:
    """triage settles on phase 3 only (never naming phase 4, the violation's
    own repair phase); the proactive repair's amendment is presented first
    (system origin, "invariant repair", no reject offered), then phase 3's
    and phase 4's own ordinary checkpoints -- the repair itself lands via
    `request_repair`, not either phase's own turn, and closes the gap for
    good (no residual violation left for the run to end with)."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    kvstore_fixtures.bootstrap_analyze_happy_path(blare_bin, repo_dir, xdg_state)
    kvstore_fixtures.inject_unmapped_failure_mode(blare_root)

    second_sha = kvstore_fixtures.commit_docs_update(repo_dir)
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-load-seeded-repair')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    # Occurrence 1: the proactive repair's own amendment, presented before
    # either phase's ordinary checkpoint -- it already carries the new alert
    # recommendation that closes the seeded gap.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "amendment · invariant repair" in output
    assert "reject" not in output
    assert "ar-orphan-injected-unmapped" in output
    process.send_line("approve")
    # Occurrence 2: phase 3's own ordinary checkpoint (triage's named phase).
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=2)
    assert "phase 3 " in output
    process.send_line("approve")
    # Occurrence 3: phase 4's own ordinary checkpoint -- opened by the repair,
    # not substituted by it (the phase still gets its own checkpoint).
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=3)
    assert "phase 4 " in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    assert "1 added · 2 updated · 0 removed" in result.output
    # The repair closed the gap: all 26 failure modes (25 from analyze-happy-
    # path plus the seeded one) resolve one way or another, none left
    # unmapped for a future run to find.
    assert "4 alertable · 20 metric-gap · 2 excluded" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    alerts = {a["id"]: a for a in _load_yaml(blare_root / "alert-recommendations.yaml")}
    assert "ar-orphan-injected-unmapped" in alerts
    assert alerts["ar-orphan-injected-unmapped"]["failure_mode_ids"] == ["fm-orphan-injected"]
    coverage = {c["failure_mode_id"]: c for c in _load_yaml(blare_root / "coverage.yaml")}
    assert coverage["fm-orphan-injected"]["alert_ids"] == ["ar-orphan-injected-unmapped"]
