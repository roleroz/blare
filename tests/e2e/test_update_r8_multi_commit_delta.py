"""e2e: R8 -- diff mode handles a range spanning multiple commits as one delta,
not per-commit. Three real kvstore commits after the recorded SHA
(`kvstore_fixtures.commit_multi_commit_range`, R8's real capture) must reach
the agent as a single triage message naming all three changed files, and the
recorded SHA must land on the range's end commit, not the first.

Traces: R8.

Recaptured (2026-08-02, T4.1 continuation) against the fixed bootstrap
(decisions.md: "Bootstrap via replaying analyze-happy-path, not a fresh live
call"): the prior `.blare/` this test needs is built by replaying the
already-captured, real `analyze-happy-path` fixture
(`kvstore_fixtures.bootstrap_analyze_happy_path`), and `update-multi-commit`'s
own committed fixture is now a real capture taken against that same
bootstrapped state.

The real session's triage verdict names phases 2, 3, and 4; the checkpoint
sequence it actually produced is four reply-pending prompts, not three:
phase 2's own checkpoint, then an agent-originated amendment (mid-phase-3,
reclassifying two phase-2 entries the model decided were mis-scoped once it
saw phase 3's metric-coverage picture -- an organic amendment, the same
mechanism `capture_amendment_agent` scripts deliberately elsewhere), then
phase 3's own checkpoint, then phase 4's own checkpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e import kvstore_fixtures
from tests.e2e.pty_harness import PtyProcess

_PROMPT_PREFIX = "$ approve"
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


def _approve_and_read_next(process: PtyProcess, seen_output: str) -> str:
    """Send `approve` for whatever the caller already read, then block until the
    next reply-pending prompt (recomputing the occurrence count from
    `seen_output`, mirroring `pty_harness._drive`'s own reasoning: `read_until`'s
    `occurrence` counts from the start of the process's output, not this call's)."""
    process.send_line("approve")
    occurrence = seen_output.count(_PROMPT_PREFIX) + 1
    return process.read_until(_PROMPT_PREFIX, occurrence=occurrence, timeout=30.0)


def test_e2e_update_multi_commit_range_is_one_delta_with_end_sha_recorded(
    tmp_path: Path,
) -> None:
    """Three real commits after the recorded SHA (kvstore's storage.py fix,
    admin.py fix, and README update, R8's real capture) reach the agent as one
    triage message naming all three files -- not three runs, not three triage
    calls; the real session's own organic mid-run amendment is presented and
    approved alongside phases 2, 3, and 4's own checkpoints; and the recorded
    SHA lands on the range's end commit, not the first of the three."""
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
    # Occurrence 1: phase 2's own checkpoint (triage's named phases, in order).
    output = process.read_until(_PROMPT_PREFIX, occurrence=1, timeout=30.0)
    assert "phase 2 " in output

    # Occurrence 2: the organic, agent-originated amendment -- reclassifies two
    # phase-2 entries (fm-unescaped-delimiters, fm-wrong-value-returned) once
    # phase 3's metric-coverage analysis judged them mis-scoped. Rejectable,
    # agent origin.
    output = _approve_and_read_next(process, output)
    assert "amendment · proposed by agent" in output
    assert "reject" in output

    # Occurrence 3: phase 3's own checkpoint, resumed once the amendment lands.
    output = _approve_and_read_next(process, output)
    assert "phase 3 " in output

    # Occurrence 4: phase 4's own checkpoint.
    output = _approve_and_read_next(process, output)
    assert "phase 4 " in output

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

    # The real session updated both already-implemented metrics' descriptions
    # (to note the new malformed-line blind spot) but added no new metric --
    # this delta's only new metric_recommendation targets a defect that has no
    # implemented instrumentation of its own yet.
    metrics = {m["id"]: m for m in _load_yaml(blare_root / "metrics.yaml")}
    assert set(metrics) == {"mx-get-value-requests-total", "mx-default-process-collector"}
