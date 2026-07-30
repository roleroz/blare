"""e2e: R18's dynamic phase-queue expansion (T3.2) -- triage names only phase
3; mid-phase-3, the model revises its verdict twice more via a bare
`affected_verdict`, naming phase 2 (behind the run's current position) and
phase 4 (ahead), no amendment involved. Both get their own ordinary checkpoint
afterward, in phase order.

Traces `engineering/architecture.md`'s T3.2 scope: "dynamic expansion (ahead
and behind) ... Traces: ... R18 (dynamic clauses)".
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
    """A structurally and semantically valid `.blare/`: one excluded failure mode,
    so step 7's semantic check seeds nothing -- only the dynamic-expansion
    mechanism itself is under test here."""
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


def test_e2e_update_dynamic_expansion_ahead_and_behind(tmp_path: Path) -> None:
    """triage names phase 3 only; mid-phase-3 the model also flags phase 2
    (behind) and phase 4 (ahead) via a bare `affected_verdict`. All three
    phases pause for their own checkpoint, in phase order, and the recorded
    SHA advances -- no artifacts change since every phase concludes trivially."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    first_sha = head_sha(repo_dir)
    _write_valid_update_state(blare_root, first_sha)

    second_sha = commit_file(
        repo_dir, "src/handlers.py", "# request handlers\n", "add handlers"
    )
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-dynamic-expansion')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    # Occurrence 1: phase 3's own checkpoint (triage's named phase).
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "phase 3 " in output
    process.send_line("approve")
    # Occurrence 2: phase 2 (behind) -- opened mid-phase-3, runs once the
    # queue is re-read after phase 3 freezes.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=2)
    assert "phase 2 " in output
    process.send_line("approve")
    # Occurrence 3: phase 4 (ahead) -- opened the same way, runs last.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=3)
    assert "phase 4 " in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    assert "0 added · 0 updated · 0 removed" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha
