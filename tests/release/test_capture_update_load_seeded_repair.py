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

A previous real attempt against kvstore (pre-dating the bootstrap fix) found
the model organically resolving the seeded `fm-orphan-injected` violation
itself, via an agent-proposed `amend_proposal`/`propose_edits(remove)`,
before `_repair_residual_violations` (orchestrator.py) ever got a turn to
fire its own system-originated "invariant repair" -- the same class of
non-convergence documented for `amendment-system`'s first attempt and
`analyze-reanalysis-noop` (agent.md). Per the global rule against forcing a
scenario's shape, this only finalizes the capture if the real session's own
rendered terminal output actually shows the system-originated repair firing
(`"amendment · invariant repair"`, cli.py's origin line for
`AmendmentOrigin.SYSTEM`) -- a non-convergent run leaves the still-stale
fixture in place and fails loudly instead of silently overwriting it with
content that doesn't demonstrate this scenario's own mechanism.
"""

from __future__ import annotations

from pathlib import Path

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

_TARGET_NAME = "docs_update"


def test_live_capture_update_load_seeded_repair(tmp_path: Path) -> None:
    """`blare update` over a hand-seeded R4 violation completes; only
    finalizes the capture if the real session's rendered output actually
    shows the system-originated "invariant repair" firing -- a run where the
    model organically resolves the violation itself first (the same
    non-convergence class already documented for amendment-system's first
    attempt and analyze-reanalysis-noop) leaves the provisional/stale fixture
    in place and fails loudly instead."""
    cap = capture.capture_update_load_seeded_repair(tmp_path, _TARGET_NAME)

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    transcript = cap.live_transcript.read_text()
    assert "amendment · invariant repair" in transcript, (
        "the real run resolved the seeded violation organically (agent-proposed "
        "amendment) before the system-originated invariant repair ever fired -- "
        f"leaving the stale fixture in place; see {cap.live_transcript}"
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-load-seeded-repair")
