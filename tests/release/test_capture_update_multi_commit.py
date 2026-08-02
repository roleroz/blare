"""live: capture the update-multi-commit fixture (R8) against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

Builds its own fresh kvstore repo, bootstraps its own real `blare analyze` at
`genesis`, then checks out `multi_commit_range_end` (a real three-commit range
spanning three distinct concerns: the storage JSON-lines fix, the admin
stale-cache fix, and the README metrics doc update -- `kvstore_repo.py`'s
commit graph) before running `blare update` -- R8's "one delta, not per-commit"
needs a multi-commit range to exercise at all. See
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

_YAML = YAML(typ="safe")

_TARGET_NAME = "multi_commit_range_end"


def test_live_capture_update_multi_commit(tmp_path: Path) -> None:
    """`blare update` over a real three-commit range completes as one delta
    (R8), the recorded SHA lands on the range's real end commit, and the
    resulting artifact set is internally consistent."""
    cap = capture.capture_update(tmp_path, "update-multi-commit", _TARGET_NAME)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-multi-commit")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == cap.target_sha
