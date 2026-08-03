"""e2e: `blare update`'s happy path (T3.1) -- triage's affected_verdict seeds
exactly the named phases; only those phases' checkpoints are presented; only
their artifacts change; the recorded SHA advances to the delta's end commit.

Traces: R6, R9.

Recaptured (2026-08-02, T4.1 continuation) against the fixed bootstrap
(decisions.md: "Bootstrap via replaying analyze-happy-path, not a fresh live
call"): the prior `.blare/` this test needs is built by replaying the
already-captured, real `analyze-happy-path` fixture
(`kvstore_fixtures.bootstrap_analyze_happy_path`), and `update-happy-path`'s
own committed fixture is now a real capture taken against that same
bootstrapped state, so its recorded edits reference `analyze-happy-path`'s
own real IDs (e.g. `fm-evictor-no-op-unit-bug`) instead of a discarded live
bootstrap session's transient ones.

The real session's triage verdict names phases 2, 3, and 4 (not just one
phase, despite this test's name -- the fix retires a failure mode phase 2
had documented as an active bug, which ripples into phase 3's metric
recommendations and phase 4's alerts); phase 1 (system map) is the only
phase left unnamed, so `system-map.yaml` is the only canonical file R9
guarantees stays byte-for-byte untouched. `metrics.yaml` also happens to
stay untouched by this particular real session (the fix touches no
metric-emitting code), asserted here as an observed fact of the fixture,
not a general R9 guarantee.
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


def test_e2e_update_happy_path_only_affected_phases_pause_and_change(
    tmp_path: Path,
) -> None:
    """A real, live-captured `blare update` over kvstore's real fix_evictor delta
    (T4.1): every checkpoint the real session actually presented is approved
    (`approve_all`); phase 1's file (unnamed by the verdict) is byte-for-byte
    untouched (R9); the named phases' files do change; and the recorded SHA
    advances to the delta's real commit."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    kvstore_fixtures.bootstrap_analyze_happy_path(blare_bin, repo_dir, xdg_state)
    unaffected_before = {
        name: (blare_root / name).read_bytes() for name in ("system-map.yaml", "metrics.yaml")
    }
    affected_before = {
        name: (blare_root / name).read_bytes()
        for name in (
            "failure-modes.yaml",
            "metric-recommendations.yaml",
            "alert-recommendations.yaml",
            "coverage.yaml",
        )
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

    # R9: phase 1, not named by the verdict, is byte-for-byte untouched.
    # metrics.yaml (phase 3, named) happens to be untouched too, since this
    # particular real session's fix touches no metric-emitting code.
    for name, content in unaffected_before.items():
        assert (blare_root / name).read_bytes() == content, f"{name} changed unexpectedly"

    # The named phases' own files do change -- the fix retires a
    # now-fixed failure mode and ripples into its metric/alert coverage.
    for name, content in affected_before.items():
        assert (blare_root / name).read_bytes() != content, f"{name} did not change as expected"
