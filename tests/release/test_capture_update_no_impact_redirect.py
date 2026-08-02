"""live: capture the update-no-impact-redirect fixture (R18) against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

Builds its own fresh kvstore repo, bootstraps its own real `blare analyze` at
`genesis`, then checks out `docs_update` (a real single-commit, docs-only
delta -- `kvstore_repo.py`'s commit graph adds a "Metrics" section to
README.md documenting the existing `kvstore_get_value_requests_total` counter)
before running `blare update` -- a genuine non-empty delta with no
production-code impact, a plausible real no-impact conclusion that this
scenario then redirects via chat.

A first attempt (against miniflux_v2) used an open-ended redirect ("does the
polling-frequency wording touch any failure mode here?"); the model did a
genuine re-investigation and explicitly stood by `no_impact` -- a real, clean
capture, but not this scenario's withdrawal shape, so it was not finalized.
This attempt's redirect is more directive (naming the specific failure mode to
add, the same way the amendment-scenario chat nudges already do), which is
what actually gives the model something concrete to accept or reject rather
than an open question it can correctly decline -- now pointed at the added
README section's own stated gap (it explicitly says the counter says nothing
about staleness, unbounded cache growth, or storage collisions).
"""

from __future__ import annotations

import json
from pathlib import Path

from ruamel.yaml import YAML

import blare.artifacts as artifacts
from blare.model import RunMode
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

_YAML = YAML(typ="safe")

_TARGET_NAME = "docs_update"
_REDIRECT_TEXT = (
    "wait -- the metrics section this commit adds even says the counter doesn't "
    "cover staleness, unbounded cache growth, or storage collisions. I think a "
    "coverage gap that's known and left undocumented is worth its own failure "
    "mode in phase 2 (an operator can't detect any of those bug classes from "
    "this metric alone) -- can you open phase 2 for this delta?"
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
    cap = capture.capture_update_no_impact_redirect(tmp_path, _TARGET_NAME, _REDIRECT_TEXT)

    blare_root = kvstore_repo.blare_root(cap.repo)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    phases_opened = _distinct_phases_opened(cap.record_dir)
    assert phases_opened, (
        "the real run never withdrew the no-impact conclusion (no phase "
        f"opened) -- leaving the provisional fixture in place; see {cap.live_transcript}"
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-no-impact-redirect")

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == cap.target_sha
