"""live: capture the update-load-seeded-repair fixture against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1).

Builds its own fresh kvstore repo, bootstraps its own real `blare analyze` at
`genesis`, hand-injects one unmapped failure mode into that real state
(`capture.inject_unmapped_failure_mode`, sanctioned by spec) to seed R18's
load-time semantic violation, then checks out `docs_update` before running
`blare update` over that delta -- its content doesn't matter, per
`capture.capture_update_load_seeded_repair`'s own docstring: the violation is
hand-seeded and the repair fires regardless of what triage concludes. See
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

_TARGET_NAME = "docs_update"


def test_live_capture_update_load_seeded_repair(tmp_path: Path) -> None:
    """`blare update` over a hand-seeded R4 violation completes via the
    proactive repair path, and the resulting artifact set is internally
    consistent (no residual semantic violations)."""
    cap = capture.capture_update_load_seeded_repair(tmp_path, _TARGET_NAME)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-load-seeded-repair")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []
