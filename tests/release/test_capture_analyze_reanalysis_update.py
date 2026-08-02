"""live: capture the analyze-reanalysis-update fixture against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1).

Builds its own fresh kvstore repo, bootstraps its own real `blare analyze` at
`genesis`, then checks out `fix_evictor` (a real, single-file defensive fix --
`kvstore_repo.py`'s commit graph) before re-analyzing -- R16 re-analysis over
an existing state file, not a fresh R1 run. See
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

# kvstore_repo.build()'s named commit: a single-file defensive fix to
# evictor.py, ahead of genesis -- a genuine code delta for the re-analysis to
# react to (agent.md's provisional list: "one entry changed").
_NEW_SHA_NAME = "fix_evictor"


def test_live_capture_analyze_reanalysis_update(tmp_path: Path) -> None:
    """R16 re-analysis over the existing `.blare/` state completes and the
    resulting artifact set is internally consistent."""
    cap = capture.capture_analyze_reanalysis_update(tmp_path, _NEW_SHA_NAME)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-reanalysis-update")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
