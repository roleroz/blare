"""live: capture the update-no-impact-redirect fixture (R18) against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session).
Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s docstring
for the shared re-run/scrub notes.

A first attempt used an open-ended redirect ("does the polling-frequency
wording touch any failure mode here?"); the model did a genuine
re-investigation and explicitly stood by `no_impact` -- a real, clean capture,
but not this scenario's withdrawal shape, so it was not finalized. This
attempt's redirect is more directive (naming the specific failure mode to add,
the same way the amendment-scenario chat nudges already do), which is what
actually gives the model something concrete to accept or reject rather than
an open question it can correctly decline.
"""

from __future__ import annotations

import json
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
_REDIRECT_TEXT = (
    "wait -- if POLLING_FREQUENCY's unit was never documented before this commit, "
    "an operator could genuinely have configured it believing it meant seconds "
    "rather than minutes, causing far slower feed refreshes than intended. I think "
    "that's worth its own failure mode in phase 2 (operator misconfiguration due to "
    "the previously ambiguous unit) -- can you open phase 2 for this delta?"
)


def _distinct_phases_opened(record_dir: Path) -> set[int]:
    phases: set[int] = set()
    for line in (record_dir / "scenario.jsonl").read_text().splitlines()[1:]:
        entry = json.loads(line)
        event = entry.get("event", {})
        if event.get("type") == "phase_prompt":
            phases.add(int(event["phase"]))
    return phases


def test_live_capture_update_no_impact_redirect(tmp_path: Path) -> None:
    """`blare update` over a real docs-only delta reaches the no-impact
    confirmation; a directive chat redirect at that confirmation withdraws it
    and opens at least one ordinary phase checkpoint instead -- only finalizes
    if a phase actually opened (the withdrawal's signature), leaving the
    provisional fixture in place and failing loudly otherwise."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update_no_impact_redirect(
        tmp_path, _BASELINE_SHA, _TARGET_SHA, _REDIRECT_TEXT
    )

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    phases_opened = _distinct_phases_opened(cap.record_dir)
    assert phases_opened, (
        "the real run never withdrew the no-impact conclusion (no phase "
        f"opened) -- leaving the provisional fixture in place; see {cap.live_transcript}"
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-no-impact-redirect")

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
