"""live: capture the update-dynamic-expansion fixture (R18) against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session).
Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s docstring
for the shared re-run/scrub notes.

Whether the live model actually revises its own verdict mid-run (R18's dynamic
clause) is not scriptable -- this range was chosen for breadth (a real SQL
injection fix plus a metrics-endpoint security fix, spanning storage and
metrics/alerting concerns) to make an organic re-verdict plausible, but the
real capture's shape is whatever the model actually produces.
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

# A real six-commit range: a webauthn persistence fix, a dependency bump, a
# storage refactor, a real SQL-injection fix, two perf commits, and a
# metrics-endpoint timing-attack fix -- broad enough that the model may
# reasonably revise its own verdict more than once as it reads further.
_BASELINE_SHA = "059ec55f52eef2ef256b1a92137fadc959d83a3e"
_TARGET_SHA = "3747e686afd59d2064ebda85c05468de4befb7c3"


def test_live_capture_update_dynamic_expansion(tmp_path: Path) -> None:
    """`blare update` over a real, broad multi-commit delta completes; the
    resulting artifact set is internally consistent regardless of how many
    phases the live model's own verdicts ended up opening."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update(
        tmp_path, "update-dynamic-expansion", _BASELINE_SHA, _TARGET_SHA
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-dynamic-expansion")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
