"""live: capture the analyze-happy-path fixture against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1).

Tagged `live`: `bazel test --test_tag_filters=live //...` re-runs this against
the real subscription every time it is invoked (architecture.md, Test strategy:
"each run re-records the SDK fixtures it exercises") -- never part of the fast
or full suites, and never run automatically; a human runs the release command
deliberately.

Asserts contract shape only (artifacts validate, invariants hold), never exact
content -- the live model's real analysis of miniflux_v2 is not deterministic
run to run (architecture.md, Test strategy: Release bullet).
"""

from __future__ import annotations

from pathlib import Path

from blare import artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture

# The real miniflux_v2 commit this scenario analyzes fresh (architecture.md,
# Constraints: "The test codebase is ~/external_git/miniflux_v2").
_BASE_SHA = "8528e5e650b71537439c2f74fb35c3276d8978fd"


def test_live_capture_analyze_happy_path(tmp_path: Path) -> None:
    """A fresh `blare analyze` against the real checkout completes, the
    resulting artifact set is internally consistent (loads cleanly, no
    residual semantic violations), and the fixture this run captured replaces
    the checked-in one (architecture.md: "each run re-records the SDK fixtures
    it exercises") -- read and scrub any new recording before committing it
    (global recording rules)."""
    cap = capture.capture_analyze_happy_path(tmp_path, _BASE_SHA)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-happy-path")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
    gaps = artifacts.gap_counts(artifact_set)
    assert gaps.alertable + gaps.metric_gap + gaps.excluded > 0
