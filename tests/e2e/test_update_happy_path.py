"""e2e: `blare update` over a real single-commit delta (T3.1/T4.1).

T4.1: this fixture is a release-suite capture of the live Claude Agent SDK
against a real `~/external_git/miniflux_v2` delta (the single commit
`79d920bc`, a defensive fix to the `entry_tombstones` backfill migration in
`internal/database/migrations.go` skipping orphaned entries whose feed row is
gone). The real model's triage concluded `no_impact` for this delta: a narrow
migration-only correctness fix that adds no component, failure mode, metric,
or alert. `migrations_before.go`/`migrations_after.go` under
`tests/e2e/testdata/update_happy_path/` are byte-exact copies of the real
file's content at the delta's two endpoints (`cf5ae57d`, `79d920bc`) --
required so this synthetic repo's own `git diff` reproduces the real capture's
recorded `patch_text` byte-for-byte (blob hashes included, a pure function of
content per agent.md's Client seam notes), which is what the replaying
client's byte-exact outbound comparison needs to succeed at all.

Traces: R6, R9, R18 (no-impact confirmation, R7's diff-mode counterpart for a
delta that *was* analyzed and found to need nothing).
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
_MIGRATIONS_PATH = "internal/database/migrations.go"


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
        runfiles.Rlocation(f"blare/tests/e2e/testdata/update_happy_path/{name}")
    )
    assert path.exists()
    return path


def _write_valid_update_state(blare_root: Path, analyzed_sha: str) -> None:
    """A structurally and semantically valid `.blare/`: one excluded failure mode
    (needs no metrics/alerts to satisfy every R3-R5 invariant), so step 7's
    semantic check seeds nothing."""
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


def _commit_migrations_file(repo_dir: Path, testdata_name: str, message: str) -> str:
    """Write the real file's exact byte content (copied from the real capture's
    two endpoint commits) at the same relative path, then commit -- so this
    repo's own diff between the two commits is byte-identical to the real
    capture's recorded `patch_text`."""
    dest = repo_dir / _MIGRATIONS_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_testdata(testdata_name), dest)
    subprocess.run(["git", "add", _MIGRATIONS_PATH], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo_dir, check=True)
    return head_sha(repo_dir)


def test_e2e_update_happy_path_real_delta_concludes_no_impact(tmp_path: Path) -> None:
    """The real single-commit delta's triage concludes `no_impact`: the
    no-impact confirmation names the changed file, approving it is the run's
    only (final) confirmation, and only the recorded SHA advances -- every
    canonical entry file keeps its exact bytes."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"
    # Pin the abbreviated object-hash length `git diff`'s "index a..b" line uses:
    # git's default (core.abbrev=auto) picks the shortest unambiguous length for
    # the repo's *own* object count, which is 7 for this tiny synthetic repo but
    # was 8 for the real miniflux_v2 checkout the fixture was captured against --
    # pinning it to 8 here reproduces the real capture's recorded patch_text
    # byte-for-byte, which the replaying client's byte-exact comparison needs.
    subprocess.run(["git", "config", "core.abbrev", "8"], cwd=repo_dir, check=True)

    # The "before" endpoint (cf5ae57d) becomes the recorded analyzed_sha.
    first_sha = _commit_migrations_file(
        repo_dir, "migrations_before.go", "add migrations.go (pre-delta content)"
    )
    _write_valid_update_state(blare_root, first_sha)
    before = {
        name: (blare_root / name).read_bytes()
        for name in (
            "system-map.yaml",
            "failure-modes.yaml",
            "metrics.yaml",
            "metric-recommendations.yaml",
            "alert-recommendations.yaml",
            "coverage.yaml",
        )
    }
    before_config = (blare_root / "config.yaml").read_bytes()

    # The "after" endpoint (79d920bc): the real delta this scenario captures.
    second_sha = _commit_migrations_file(
        repo_dir,
        "migrations_after.go",
        "fix(database): skip orphaned entries in migration v127",
    )
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-happy-path')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "no changes needed" in output
    assert _MIGRATIONS_PATH in output
    assert "no new component, dependency, or data flow" in output
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    assert "0 added · 0 updated · 0 removed" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    for name, content in before.items():
        assert (blare_root / name).read_bytes() == content, f"{name} changed on a no-impact run"
    assert (blare_root / "config.yaml").read_bytes() == before_config
