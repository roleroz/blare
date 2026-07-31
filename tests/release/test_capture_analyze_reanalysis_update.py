"""live: capture the analyze-reanalysis-update fixture against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1).

Requires a real prior analysis to already exist in `.blare/` -- run
`test_capture_analyze_happy_path` (or any real `blare analyze`) first in the
same release-suite session; this scenario is R16 re-analysis over an existing
state file, not a fresh R1 run. Tagged `live`, `exclusive` (shares mutable state
with the other live targets against the same checkout) -- see
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub notes.
"""

from __future__ import annotations

from pathlib import Path

from blare import artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture

# Real commits ahead of analyze-happy-path's base -- a genuine code delta for
# the re-analysis to react to (agent.md's provisional list: "one entry changed").
_NEW_SHA = "aa509b880242b1410baea7b22d27e096e8e90597"


def test_live_capture_analyze_reanalysis_update(tmp_path: Path) -> None:
    """R16 re-analysis over the existing `.blare/` state completes and the
    resulting artifact set is internally consistent."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_analyze_reanalysis_update(tmp_path, _NEW_SHA)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-reanalysis-update")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
