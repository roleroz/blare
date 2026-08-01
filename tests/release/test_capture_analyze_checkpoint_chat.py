"""live: capture the analyze-checkpoint-chat fixture (R2) against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Runs `blare analyze` against whatever commit is currently checked out (a real
re-analysis, R16, since `.blare/` already exists from an earlier real
capture); chat right at phase 1's own checkpoint before anything else
happens. Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s
docstring for the shared re-run/scrub notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture


def test_live_capture_analyze_checkpoint_chat(tmp_path: Path) -> None:
    """A chat interjection at phase 1's checkpoint reaches the live model and
    the run completes, altering results per R2; the resulting artifact set is
    internally consistent."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_analyze_checkpoint_chat(tmp_path)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-checkpoint-chat")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
