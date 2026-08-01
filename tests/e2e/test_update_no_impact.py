"""e2e: `blare update`'s R18 no-impact flow (T3.1) -- triage concludes the
non-empty delta affects no artifacts; the run presents that conclusion for
confirmation; approval is the final confirmation for the run and changes
exactly the recorded SHA (and any derived-doc restoration) -- no entry file
changes.

T4.1: this fixture is a release-suite capture of the live Claude Agent SDK
against a real `~/external_git/miniflux_v2` delta (the single commit
`ff014256`, adding `t.Parallel()` to every function in
`internal/api/api_integration_test.go`) -- a genuine test-only change with no
production-code impact, which the real model correctly concluded needs no
artifact work. `tests/e2e/testdata/update_no_impact/` holds byte-exact copies
of the real file's content at the delta's two endpoints so this synthetic
repo's own `git diff` reproduces the real capture's recorded `patch_text`
byte-for-byte.

Traces: R18.
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
_INTEGRATION_TEST_PATH = "internal/api/api_integration_test.go"


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
    path = Path(runfiles.Rlocation(f"blare/tests/e2e/testdata/update_no_impact/{name}"))
    assert path.exists()
    return path


def _write_valid_update_state(blare_root: Path, analyzed_sha: str) -> None:
    """Same trivially-valid `.blare/` as the happy-path e2e test: one excluded
    failure mode, nothing for step 7's semantic check to seed."""
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


def _commit_integration_test_file(repo_dir: Path, testdata_name: str, message: str) -> str:
    """Write the real file's exact byte content (copied from the real capture's
    two endpoint commits) at the same relative path, then commit -- so this
    repo's own diff between the two commits is byte-identical to the real
    capture's recorded `patch_text`."""
    dest = repo_dir / _INTEGRATION_TEST_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_testdata(testdata_name), dest)
    subprocess.run(["git", "add", _INTEGRATION_TEST_PATH], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo_dir, check=True)
    return head_sha(repo_dir)


def test_e2e_update_no_impact_confirmed_changes_only_the_sha(tmp_path: Path) -> None:
    """A real test-only delta (t.Parallel() added throughout the API
    integration test suite): the agent concludes no_impact with an empty
    queue, the no-impact screen is presented, and approving it advances only
    the recorded SHA -- every canonical entry file keeps its exact bytes."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"
    # See test_update_happy_path.py's docstring: pins the abbreviated
    # object-hash length `git diff`'s "index a..b" line uses to match the real
    # miniflux_v2 checkout's (8 chars) rather than this tiny repo's default.
    subprocess.run(["git", "config", "core.abbrev", "8"], cwd=repo_dir, check=True)

    first_sha = _commit_integration_test_file(
        repo_dir, "api_integration_test_before.go", "add api_integration_test.go (pre-delta)"
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

    second_sha = _commit_integration_test_file(
        repo_dir,
        "api_integration_test_after.go",
        "test(api): run integration tests in parallel",
    )
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-no-impact')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "no changes needed" in output
    assert _INTEGRATION_TEST_PATH in output
    assert "Test scheduling changes have no observability footprint" in output
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
