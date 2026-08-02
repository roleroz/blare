"""live: capture the analyze-checkpoint-chat fixture (R2) against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

Builds its own fresh kvstore repo and runs a fresh `blare analyze` at
`genesis` (no bootstrap needed -- this run itself establishes `.blare/`);
chat right at phase 1's own checkpoint before anything else happens. See
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture


def test_live_capture_analyze_checkpoint_chat(tmp_path: Path) -> None:
    """A chat interjection at phase 1's checkpoint reaches the live model and
    the run completes, altering results per R2; the resulting artifact set is
    internally consistent."""
    cap = capture.capture_analyze_checkpoint_chat(tmp_path)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "analyze-checkpoint-chat")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
