"""live: capture the update-load-seeded-repair fixture against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1).

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session);
this scenario hand-injects one unmapped failure mode into that real state
(`capture.inject_unmapped_failure_mode`, sanctioned by spec) to seed R18's
load-time semantic violation, then runs `blare update` over a small real commit
range. Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s
docstring for the shared re-run/scrub notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture

# A real one-commit range ahead of analyze-reanalysis-update's target, for the
# proactive repair's own delta (its content doesn't matter -- the violation is
# hand-seeded and the repair fires regardless of what triage concludes, R18).
_BASELINE_SHA = "4d84eee221d6ffeb62b4de83d7439a9f57935e43"
_TARGET_SHA = "92057dde56c9ad6c9ffe34524a1c0028d244d7ae"


def test_live_capture_update_load_seeded_repair(tmp_path: Path) -> None:
    """`blare update` over a hand-seeded R4 violation completes via the
    proactive repair path, and the resulting artifact set is internally
    consistent (no residual semantic violations)."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update_load_seeded_repair(tmp_path, _BASELINE_SHA, _TARGET_SHA)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-load-seeded-repair")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []
