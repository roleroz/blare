"""e2e: R8 -- diff mode handles a range spanning multiple commits as one delta,
not per-commit. Three real kvstore commits after the recorded SHA
(`kvstore_fixtures.commit_multi_commit_range`, R8's real capture) must reach
the agent as a single triage message naming all three changed files, and the
recorded SHA must land on the range's end commit, not the first.

Traces: R8.

Mechanism fixed (2026-08-02, decisions.md: "Bootstrap via replaying
analyze-happy-path, not a fresh live call"): the prior `.blare/` this test
needs is now built by replaying the already-captured, real `analyze-happy-path`
fixture (`kvstore_fixtures.bootstrap_analyze_happy_path`) rather than seeding a
hand-authored, minimal `.blare/`. Recapture pending (separate follow-up, not
this task): `update-multi-commit`'s own committed fixture was captured against
the *old*, non-deterministic live-bootstrap model, so its recorded edits still
reference IDs from that discarded session -- this test is expected to keep
failing, now for that one, cleanly-isolated reason, until `update-multi-commit`
is recaptured against the fixed bootstrap.
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


def test_e2e_update_multi_commit_range_is_one_delta_with_end_sha_recorded(
    tmp_path: Path,
) -> None:
    """Three real commits after the recorded SHA (kvstore's storage.py fix,
    admin.py fix, and README update, R8's real capture) reach the agent as one
    triage message naming all three files -- not three runs, not three triage
    calls -- and the recorded SHA lands on the final commit."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    first_sha = kvstore_fixtures.build_genesis(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    kvstore_fixtures.bootstrap_analyze_happy_path(blare_bin, repo_dir, xdg_state)

    final_sha = kvstore_fixtures.commit_multi_commit_range(repo_dir)
    assert final_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-multi-commit')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "phase 3 " in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    # R6/R8: the recorded SHA is the range's end commit, captured at run start --
    # not the first of the three new commits, and the replayed fixture (which
    # names all three changed files in one triage message) only matches at all
    # because gitrepo computed one delta over the whole range.
    assert state["analyzed_sha"] == final_sha
    assert state["analyzed_sha"] != first_sha

    metrics = {m["id"]: m for m in _load_yaml(blare_root / "metrics.yaml")}
    assert set(metrics) == {"mx-multi"}
