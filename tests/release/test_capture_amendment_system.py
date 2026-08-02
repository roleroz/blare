"""live: capture the amendment-system fixture against a fresh `testdata/
kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

Distinct from update-load-seeded-repair: that scenario's violation is caught
by update mode's own preflight step 7, before any phase runs. This one hand-
injects its own unmapped failure mode into a freshly bootstrapped `.blare/`
(`capture.capture_amendment_system` builds its own kvstore repo, bootstraps a
real `blare analyze` at `genesis`, then injects) and runs `blare analyze`
again (a real re-analysis, R16): unless the re-analysis happens to touch that
entry on its own initiative, the violation survives to the final approval
gate, which opens a system-originated amendment before confirmation --
distinct from update mode's proactive, pre-phase repair. See
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture


def test_live_capture_amendment_system(tmp_path: Path) -> None:
    """A hand-injected, still-unmapped failure mode survives a real
    re-analysis and is caught at the final approval gate as a
    system-originated amendment; the run completes and the resulting
    artifact set is internally consistent (the injected violation itself is
    resolved one way or another by the time the run ends)."""
    cap = capture.capture_amendment_system(tmp_path)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "amendment-system")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
