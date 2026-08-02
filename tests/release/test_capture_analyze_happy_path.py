"""live: capture the analyze-happy-path fixture against a fresh `testdata/
kvstore` repo (`tests/release/kvstore_repo.py`) with the live Claude Agent SDK
(T4.1).

Tagged `live`: `bazel test --test_tag_filters=live //...` re-runs this against
the real subscription every time it is invoked (architecture.md, Test strategy:
"each run re-records the SDK fixtures it exercises") -- never part of the fast
or full suites, and never run automatically; a human runs the release command
deliberately. Every capture in this package now builds its own fresh kvstore
repo inside its own `tmp_path` and, where a prior analyzed state is needed,
bootstraps its own real `blare analyze` first -- there is no shared checkout
and no run-order requirement between scenarios any more (architecture.md, Test
strategy; decisions.md 2026-08-01).

Asserts contract shape only (artifacts validate, invariants hold), never exact
content -- the live model's real analysis is not deterministic run to run.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture


def test_live_capture_analyze_happy_path(tmp_path: Path) -> None:
    """A fresh `blare analyze` against a newly built kvstore repo completes,
    the resulting artifact set is internally consistent (loads cleanly, no
    residual semantic violations), and the fixture this run captured replaces
    the checked-in one (architecture.md: "each run re-records the SDK fixtures
    it exercises") -- read and scrub any new recording before committing it
    (global recording rules)."""
    cap = capture.capture_analyze_happy_path(tmp_path)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-happy-path")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
    gaps = artifacts.gap_counts(artifact_set)
    assert gaps.alertable + gaps.metric_gap + gaps.excluded > 0
