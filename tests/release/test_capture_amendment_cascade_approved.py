"""live: capture the amendment-cascade-approved fixture against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Runs `blare analyze` (a real re-analysis, R16); chat at phase 4's checkpoint
asks for a failure-mode rename that cascades (via `referencing_phases`) into
phase 3's metric coverage -- an amendment unit spanning multiple phases,
approved as one unit. Tagged `live`, `exclusive` -- see
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture


def test_live_capture_amendment_cascade_approved(tmp_path: Path) -> None:
    """A chat-prompted rename cascades across phases as one amendment unit,
    approved: the run completes and the resulting artifact set is internally
    consistent."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_amendment_cascade(tmp_path, "amendment-cascade-approved", approve=True)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "amendment-cascade-approved")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
