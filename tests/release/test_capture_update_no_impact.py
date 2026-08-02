"""live: capture the update-no-impact fixture (R18) against a fresh `testdata/
kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

Builds its own fresh kvstore repo, bootstraps its own real `blare analyze` at
`genesis`, then checks out `test_only_change` (a real single-commit delta that
only adds a `tests/` directory, touching no production file -- `kvstore_repo.
py`'s commit graph) before running `blare update` -- a genuine non-empty delta
with no production-code impact, a plausible real no-impact conclusion (R18).
See `test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
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

_TARGET_NAME = "test_only_change"


def test_live_capture_update_no_impact(tmp_path: Path) -> None:
    """`blare update` over a real test-only delta completes; the recorded SHA
    advances to the real target commit, and the resulting artifact set is
    internally consistent."""
    cap = capture.capture_update(tmp_path, "update-no-impact", _TARGET_NAME)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-no-impact")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == cap.target_sha
