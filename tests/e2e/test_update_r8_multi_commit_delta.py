"""e2e: R8 -- diff mode handles a range spanning multiple commits as one delta,
not per-commit.

T4.1: this fixture is a release-suite capture of the live Claude Agent SDK
against a real `~/external_git/miniflux_v2` range spanning three real commits
(`8528e5e6..cf5ae57d`: a CSS `content-visibility` perf tweak, a storage-layer
`hide_globally` fix, and a content-rewrite DOM-order bug fix with its
regression test) touching four files total. The real triage message named all
four files and their combined patch text in one call -- not per-commit -- and
the real model concluded `no_impact`. `tests/e2e/testdata/update_multi_commit/`
holds byte-exact copies of each file's real content at the range's two
endpoints, required so this synthetic repo's own `git diff` reproduces the
real capture's recorded `patch_text` byte-for-byte (blob hashes included, per
agent.md's Client seam notes on content-addressed diffs).

Traces: R8.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import head_sha, init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_YAML = YAML(typ="safe")

# Real relative paths the recorded fixture's triage message names.
_REWRITE_FUNCS = "internal/reader/rewrite/content_rewrite_functions.go"
_REWRITE_TEST = "internal/reader/rewrite/content_rewrite_test.go"
_ENTRY = "internal/storage/entry.go"
_COMMON_CSS = "internal/ui/static/css/common.css"


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


def _testdata(name: str) -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(
        runfiles.Rlocation(f"blare/tests/e2e/testdata/update_multi_commit/{name}")
    )
    assert path.exists()
    return path


def _write_valid_update_state(blare_root: Path, analyzed_sha: str) -> None:
    """A structurally and semantically valid `.blare/`: one excluded failure mode
    needs no metrics/alerts to satisfy every R3-R5 invariant, so step 7's semantic
    check seeds nothing."""
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


def _write_all_before(repo_dir: Path) -> None:
    pairs = (
        (_REWRITE_FUNCS, "content_rewrite_functions_before.go"),
        (_REWRITE_TEST, "content_rewrite_test_before.go"),
        (_ENTRY, "entry_before.go"),
        (_COMMON_CSS, "common_before.css"),
    )
    for relative_path, testdata_name in pairs:
        dest = repo_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_testdata(testdata_name), dest)


def _commit(repo_dir: Path, relative_paths: list[str], message: str) -> str:
    subprocess.run(["git", "add", *relative_paths], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo_dir, check=True)
    return head_sha(repo_dir)


def test_e2e_update_multi_commit_range_is_one_delta_with_end_sha_recorded(
    tmp_path: Path,
) -> None:
    """A real three-commit range touching four files reaches the agent as one
    triage message naming all four -- not three runs, not three triage calls
    -- the real triage concluded `no_impact`, and the recorded SHA lands on
    the range's final commit."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"
    # See test_update_happy_path.py's docstring: pins the abbreviated
    # object-hash length `git diff`'s "index a..b" line uses to match the real
    # miniflux_v2 checkout's (8 chars) rather than this tiny repo's default.
    subprocess.run(["git", "config", "core.abbrev", "8"], cwd=repo_dir, check=True)

    _write_all_before(repo_dir)
    first_sha = _commit(
        repo_dir,
        [_REWRITE_FUNCS, _REWRITE_TEST, _ENTRY, _COMMON_CSS],
        "add pre-delta content",
    )
    _write_valid_update_state(blare_root, first_sha)

    # Commit 2 of the range: the CSS perf tweak and the storage fix (real
    # commits e7888e3d, 070bc9ef).
    shutil.copyfile(_testdata("common_after.css"), repo_dir / _COMMON_CSS)
    shutil.copyfile(_testdata("entry_after.go"), repo_dir / _ENTRY)
    _commit(
        repo_dir,
        [_COMMON_CSS, _ENTRY],
        "perf(ui): defer off-screen layout/paint; fix(storage): hide_globally",
    )

    # Commit 3 (the range's end, real commit cf5ae57d): the content-rewrite fix.
    shutil.copyfile(
        _testdata("content_rewrite_functions_after.go"), repo_dir / _REWRITE_FUNCS
    )
    shutil.copyfile(_testdata("content_rewrite_test_after.go"), repo_dir / _REWRITE_TEST)
    final_sha = _commit(
        repo_dir,
        [_REWRITE_FUNCS, _REWRITE_TEST],
        "fix(rewrite): keep content order when remove_tables unwraps tables",
    )
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
    assert "no changes needed" in output
    for path in (_REWRITE_FUNCS, _REWRITE_TEST, _ENTRY, _COMMON_CSS):
        assert path in output
    assert "MarkGloballyVisibleFeedsAsRead" in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    assert "0 added · 0 updated · 0 removed" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    # R6/R8: the recorded SHA is the range's end commit, captured at run start
    # -- not an intermediate commit, and the replayed fixture (which names all
    # four files across the whole range in one triage message) only matches at
    # all because gitrepo computed one delta over the whole range.
    assert state["analyzed_sha"] == final_sha
    assert state["analyzed_sha"] != first_sha
