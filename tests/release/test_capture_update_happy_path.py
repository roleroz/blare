"""live: capture the update-happy-path fixture against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation) -- unblocked by T4.4's real `patch_text` wiring.

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session).
Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s docstring
for the shared re-run/scrub notes.

Asserts contract shape only (artifacts validate, invariants hold, the recorded
SHA advances to the delta's real end commit), never exact content -- the live
model's real analysis of a real diff is not deterministic run to run.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture
from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import finalize_capture

_YAML = YAML(typ="safe")

# A real single-commit range: a database-migration orphan-entry fix
# (internal/database/migrations.go only) -- one clear concern, a plausible
# single-phase-affected delta.
_BASELINE_SHA = "cf5ae57d9a65b24394104fa12428a48ca5236a8d"
_TARGET_SHA = "79d920bc1ac900834322fcfe13b6e228228c2fe0"


def test_live_capture_update_happy_path(tmp_path: Path) -> None:
    """`blare update` over a real single-commit delta completes, the recorded
    SHA advances to the real target commit, and the resulting artifact set is
    internally consistent (no residual semantic violations)."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update(tmp_path, "update-happy-path", _BASELINE_SHA, _TARGET_SHA)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-happy-path")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
