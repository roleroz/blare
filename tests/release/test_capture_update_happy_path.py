"""live: capture the update-happy-path fixture against a fresh `testdata/
kvstore` repo with the live Claude Agent SDK (T4.1's continuation) --
unblocked by T4.4's real `patch_text` wiring.

Builds its own fresh kvstore repo, bootstraps its own real `blare analyze` at
`genesis`, then checks out `fix_evictor` (a real, single-file defensive fix --
`kvstore_repo.py`'s commit graph) before running `blare update` -- one clear
concern, a plausible single-phase-affected delta. See
`test_capture_analyze_happy_path`'s docstring for the shared re-run/scrub
notes.

Asserts contract shape only (artifacts validate, invariants hold, the recorded
SHA advances to the delta's real end commit), never exact content -- the live
model's real analysis of a real diff is not deterministic run to run.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

_YAML = YAML(typ="safe")

_TARGET_NAME = "fix_evictor"


def test_live_capture_update_happy_path(tmp_path: Path) -> None:
    """`blare update` over a real single-commit delta completes, the recorded
    SHA advances to the real target commit, and the resulting artifact set is
    internally consistent (no residual semantic violations)."""
    cap = capture.capture_update(tmp_path, "update-happy-path", _TARGET_NAME)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-happy-path")

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == cap.target_sha
