"""e2e: R18's dynamic phase-queue expansion (T3.2) -- triage names phases 2, 3,
and 4; a chat nudge at the first checkpoint asks the model to double check
whether any other phase also needs revisiting, and it flags phase 1 (system
map) too, via a bare `affected_verdict` -- no amendment involved for that part.
Every named phase, including the dynamically-added one, gets its own ordinary
checkpoint, in phase order.

Traces `engineering/architecture.md`'s T3.2 scope: "dynamic expansion (ahead
and behind) ... Traces: ... R18 (dynamic clauses)".

Recaptured (2026-08-02, T4.1 continuation) against the fixed bootstrap
(decisions.md: "Bootstrap via replaying analyze-happy-path, not a fresh live
call"): the prior `.blare/` this test needs is built by replaying the
already-captured, real `analyze-happy-path` fixture
(`kvstore_fixtures.bootstrap_analyze_happy_path`), and the delta is kvstore's
real `dynamic_expansion_delta` commit
(`kvstore_fixtures.commit_dynamic_expansion_delta`).

This real session demonstrates only the "behind" dynamic-expansion clause
(phase 1, behind triage's earliest named phase, 2) -- triage already named
every phase from 2 through 4, so there is no "ahead" phase left for the
model to add this time; a real capture is not scriptable into demonstrating
both in the same run. The real session also needed a genuine, multi-round
system-originated "invariant repair" once phases 3/4 first landed with an
unrecognized field name for failure-mode/alert linkage (`failure_mode_ids`
is the real field; the model tried several wrong guesses and self-corrected
via `run_control`'s `amend_complete`/repair loop, the same organic mechanism
`update-load-seeded-repair`'s capture also exercises) -- replayed here via
the driver's shared `approve_all`, robust to however many repair rounds the
real session actually took, rather than a fixed occurrence count.
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

# The real capture's own chat nudge (tests/release/capture.py's
# `capture_update_dynamic_expansion`), sent at the very first checkpoint --
# replaying the fixture requires sending back exactly what the live session
# received at that point, or the replay diverges.
_CHAT_NUDGE = (
    "before you approve -- given everything you've seen while working on "
    "this delta, please double check whether it also requires revisiting "
    "any other phase (system map, failure modes, metric coverage, or "
    "alert recommendations) beyond what triage originally named; if so, "
    "call run_control with a bare affected_verdict naming it now, before "
    "finishing this phase"
)


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


def test_e2e_update_dynamic_expansion_behind(tmp_path: Path) -> None:
    """triage names phases 2, 3, 4; a chat nudge at phase 2's own checkpoint
    prompts the model to flag phase 1 (behind) too, via a bare
    `affected_verdict`; all four phases -- 1, 2, 3, and 4 -- get their own
    ordinary checkpoint, and a genuine system-originated repair (unmapped
    failure modes, several real rounds) lands before the run completes."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    kvstore_fixtures.bootstrap_analyze_happy_path(blare_bin, repo_dir, xdg_state)

    second_sha = kvstore_fixtures.commit_dynamic_expansion_delta(repo_dir)
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-dynamic-expansion')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    # The chat nudge lands at the very first reply-pending prompt (phase 2's
    # own checkpoint -- the earliest phase triage named); every prompt after
    # that is approved, however many the real session's repair rounds took.
    output = process.read_until("$ approve", occurrence=1, timeout=30.0)
    assert "phase 2 " in output
    process.send_line(_CHAT_NUDGE)
    result = approve_all(process)

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output

    # Every phase's own checkpoint fired, including the dynamically-added
    # phase 1 (behind) -- confirming the expansion actually happened, not
    # just that triage's original three phases ran.
    for phase_header in ("phase 1 ", "phase 2 ", "phase 3 ", "phase 4 "):
        assert phase_header in result.output, f"{phase_header!r} never appeared"
    assert "amendment · invariant repair" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    blare_root_artifacts = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(blare_root_artifacts) == []
