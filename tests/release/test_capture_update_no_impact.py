"""live: capture the update-no-impact fixture (R18) against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session).
Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s docstring
for the shared re-run/scrub notes.
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

# A real single-commit, test-only range (internal/api/api_integration_test.go
# only) -- a genuine non-empty delta with no production-code impact, a
# plausible real no-impact conclusion (R18).
_BASELINE_SHA = "79d920bc1ac900834322fcfe13b6e228228c2fe0"
_TARGET_SHA = "ff01425686117039f1618d958888b0aa21581324"


def test_live_capture_update_no_impact(tmp_path: Path) -> None:
    """`blare update` over a real test-only delta completes; the recorded SHA
    advances to the real target commit, and the resulting artifact set is
    internally consistent."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update(tmp_path, "update-no-impact", _BASELINE_SHA, _TARGET_SHA)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-no-impact")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
