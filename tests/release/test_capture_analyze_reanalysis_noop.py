"""live: capture the analyze-reanalysis-noop fixture (R16) against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

A first attempt (T4.1's first pass, no hint) found the model has no way to
know a prior analysis exists unless it checks `.blare/` on its own initiative
-- the phase prompts never mention it -- and produced a real but noisy
duplicate-then-reconcile run instead (kept as the real analyze-reanalysis-update
capture). This attempt uses a different strategy: an explicit chat hint at
phase 1's own checkpoint telling the model to check `.blare/` first and only
propose edits where its conclusions genuinely differ.

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session), and
runs with no code change since that prior analysis (whatever commit is
currently checked out is `blare`'s own real current HEAD). Tagged `live`,
`exclusive` -- see `test_capture_analyze_happy_path`'s docstring for the shared
re-run/scrub notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture


def test_live_capture_analyze_reanalysis_noop(tmp_path: Path) -> None:
    """`blare analyze` again with no code change and a chat hint to check
    `.blare/` first: only if the real run converges to a genuine zero-diff
    does this finalize it as the real fixture, superseding the still-
    provisional one -- a non-convergent run leaves the provisional fixture in
    place and fails loudly instead of silently overwriting it with noisy
    content (per the global rule against forcing a scenario's shape)."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_analyze_reanalysis_noop(tmp_path)

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []

    transcript = cap.live_transcript.read_text()
    assert "0 added · 0 updated · 0 removed" in transcript, (
        "the real re-analysis did not converge to a genuine zero-diff -- "
        f"leaving the provisional fixture in place; see {cap.live_transcript}"
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-reanalysis-noop")
