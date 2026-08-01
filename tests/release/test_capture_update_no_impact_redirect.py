"""live: capture the update-no-impact-redirect fixture (R18) against the real
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

# A real single-commit, docs-only range (miniflux.1, the man page, only) -- a
# genuine non-empty delta with no production-code impact, a plausible real
# no-impact conclusion that this scenario then redirects via chat.
_BASELINE_SHA = "9a774083ab4f61f7d604fb950bdd6aff029824a1"
_TARGET_SHA = "2717336d2c6519e649f52d958a4fbc84aaed5e8e"
_REDIRECT_TEXT = "actually, does the polling-frequency wording touch any failure mode here?"


def test_live_capture_update_no_impact_redirect(tmp_path: Path) -> None:
    """`blare update` over a real docs-only delta reaches the no-impact
    confirmation; a chat redirect at that confirmation withdraws it and opens
    at least one ordinary phase checkpoint instead; the run completes and the
    resulting artifact set is internally consistent."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update_no_impact_redirect(
        tmp_path, _BASELINE_SHA, _TARGET_SHA, _REDIRECT_TEXT
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-no-impact-redirect")

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
