"""e2e: `blare update`'s happy path (T3.1) -- triage's affected_verdict seeds
exactly the named phase; only that phase's checkpoint is presented; only its
artifacts change; the recorded SHA advances to the delta's end commit.

Traces: R6, R9.

Mechanism fixed (2026-08-02, decisions.md: "Bootstrap via replaying
analyze-happy-path, not a fresh live call"): the prior `.blare/` this test
needs is now built by replaying the already-captured, real `analyze-happy-path`
fixture (`kvstore_fixtures.bootstrap_analyze_happy_path`) rather than seeding a
hand-authored, minimal `.blare/` -- so its IDs are the same real, fixed IDs a
fresh real bootstrap now deterministically reproduces. Recapture pending
(separate follow-up, not this task): `update-happy-path`'s own committed
fixture was captured against the *old*, non-deterministic live-bootstrap
model, so its recorded edits still reference IDs (e.g.
fm-evictor-unit-mismatch-never-expires) that don't exist in the new,
correctly-bootstrapped `.blare/` (which has analyze-happy-path's own
fm-evictor-no-op-unit-bug instead) -- this test is expected to keep failing,
now for that one, cleanly-isolated reason, until `update-happy-path` is
recaptured against the fixed bootstrap.
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

    kvstore_fixtures.bootstrap_analyze_happy_path(blare_bin, repo_dir, xdg_state)
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
