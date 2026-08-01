"""live: capture the amendment-system fixture against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Distinct from update-load-seeded-repair (already captured for real): that
scenario's violation is caught by update mode's own preflight step 7, before
any phase runs. This one hand-injects its own, differently-named violation
(`fm-orphan-injected-gate` -- a distinct ID so this scenario's injection is
independently idempotent from update-load-seeded-repair's, which already
resolved its own `fm-orphan-injected` entry to `excluded` in this same real
`.blare/`) and runs `blare analyze` (a real re-analysis, R16): unless the
re-analysis happens to touch that entry on its own initiative, the violation
survives to the final approval gate, which opens a system-originated
amendment before confirmation -- distinct from update mode's proactive,
pre-phase repair. Tagged `live`, `exclusive` -- see
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

_GATE_ORPHAN_ID = "fm-orphan-injected-gate"


def test_live_capture_amendment_system(tmp_path: Path) -> None:
    """A hand-injected, still-unmapped failure mode survives a real
    re-analysis and is caught at the final approval gate as a
    system-originated amendment; the run completes and the resulting
    artifact set is internally consistent (the injected violation itself is
    resolved one way or another by the time the run ends)."""
    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    assert blare_root.is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    capture.inject_unmapped_failure_mode(
        blare_root, fm_id=_GATE_ORPHAN_ID, origin_note="amendment-system"
    )

    cap = capture.capture_amendment_system(tmp_path)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "amendment-system")

    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
