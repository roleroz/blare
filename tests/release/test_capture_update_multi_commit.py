"""live: capture the update-multi-commit fixture (R8) against the real
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

# A real three-commit range spanning three distinct concerns (ui perf, storage
# fix, rewrite-function fix) -- R8's "one delta, not per-commit" needs a
# multi-commit range to exercise at all.
_BASELINE_SHA = "8528e5e650b71537439c2f74fb35c3276d8978fd"
_TARGET_SHA = "cf5ae57d9a65b24394104fa12428a48ca5236a8d"


def test_live_capture_update_multi_commit(tmp_path: Path) -> None:
    """`blare update` over a real three-commit range completes as one delta
    (R8), the recorded SHA lands on the range's real end commit, and the
    resulting artifact set is internally consistent."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update(tmp_path, "update-multi-commit", _BASELINE_SHA, _TARGET_SHA)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-multi-commit")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
