"""live: capture the amendment-agent-approved fixture against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

Builds its own fresh kvstore repo and runs a fresh `blare analyze` at
`genesis`; chat at phase 4's checkpoint prompts an agent-proposed amendment,
approved at its re-presentation. See `test_capture_analyze_happy_path`'s
docstring for the shared re-run/scrub notes.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture


def test_live_capture_amendment_agent_approved(tmp_path: Path) -> None:
    """A chat-prompted agent-proposed amendment, approved at its
    re-presentation: the run completes and the resulting artifact set is
    internally consistent."""
    cap = capture.capture_amendment_agent(tmp_path, "amendment-agent-approved", approve=True)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "amendment-agent-approved")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.ANALYZE)
    assert artifacts.semantic_violations(artifact_set) == []
