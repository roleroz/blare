"""live: capture the update-dynamic-expansion fixture (R18) against the real
`~/external_git/miniflux_v2` checkout with the live Claude Agent SDK (T4.1's
continuation).

Requires a real prior analysis to already exist in `.blare/` (run
`test_capture_analyze_happy_path` first in the same release-suite session).
Tagged `live`, `exclusive` -- see `test_capture_analyze_happy_path`'s docstring
for the shared re-run/scrub notes.

Two good-faith attempts so far, neither converging to the described shape (a
revised `affected_verdict` opening a phase mid-run, no amendment):

1. A broad six-commit range (a real SQL-injection fix, a metrics-endpoint
   security fix, plus unrelated smaller commits), approving along blindly with
   no chat: spiraled into a 38-minute, 60-round repair loop without ever
   reaching a final confirmation. The real `.blare/` catalog had zero semantic
   violations afterward (checked offline), so the loop was the model's own
   edits repeatedly tripping the gate across that unusually rich delta, not a
   stuck or corrupted state -- abandoned rather than raising max_iterations
   and re-running an already-38-minute attempt.
2. A narrower five-commit range (a single "feed/entry language" feature) with
   `capture.capture_update_dynamic_expansion`'s chat nudge at the first
   checkpoint, asking the model to reconsider whether other phases need work:
   converged cleanly in under two minutes, but the model's triage had already
   concluded `no_impact`, and its response to the nudge was a genuine
   re-examination that explicitly *stood by* `no_impact` -- no `affected_verdict`
   call, no phase ever opened. A real, clean capture, just not this scenario's
   shape, so it was not finalized (the existing provisional fixture stands).

Per the global rule against forcing a scenario's shape, this finalizes the
capture only if the recording actually shows more than one distinct phase
opened (`phase_prompt` events for at least two different phase numbers) --
the signature of a genuine dynamic expansion -- and fails loudly naming the
scenario as unresolved otherwise, rather than silently overwriting the
provisional fixture with content that doesn't match.
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

# A real five-commit range: a single coherent feature (feed/entry language
# parsing and propagation across RSS/Atom/RDF/JSON Feed) -- attempt 2's range,
# chosen to avoid repeating attempt 1's repair-loop spiral while still being
# substantial enough that reconsidering other phases (the chat nudge) is
# plausible.
_BASELINE_SHA = "68e2655b773226aefdaccc73c0c6d152c98f3eec"
_TARGET_SHA = "5f710f916d4e6ee9fb0f308904eab9cd0f8d505f"


def _distinct_phases_opened(record_dir: Path) -> set[int]:
    phases: set[int] = set()
    for line in (record_dir / "scenario.jsonl").read_text().splitlines()[1:]:
        entry = json.loads(line)
        event = entry.get("event", {})
        if event.get("type") == "phase_prompt":
            phases.add(int(event["phase"]))
    return phases


def test_live_capture_update_dynamic_expansion(tmp_path: Path) -> None:
    """`blare update` over a real multi-commit delta, nudged at the first
    checkpoint to reconsider other phases, completes; only finalizes as the
    real fixture if more than one phase actually opened (a genuine dynamic
    expansion), leaving the provisional fixture in place and failing loudly
    otherwise."""
    assert mr.blare_root(mr.MINIFLUX_ROOT).is_dir(), (
        "no existing .blare/ state -- run test_capture_analyze_happy_path "
        "(or any real blare analyze) first in this release-suite session"
    )
    cap = capture.capture_update_dynamic_expansion(tmp_path, _BASELINE_SHA, _TARGET_SHA)

    blare_root = mr.blare_root(mr.MINIFLUX_ROOT)
    artifact_set = artifacts.load(blare_root, RunMode.UPDATE)
    assert artifacts.semantic_violations(artifact_set) == []

    phases_opened = _distinct_phases_opened(cap.record_dir)
    assert len(phases_opened) > 1, (
        "the real run did not dynamically open more than one phase (got "
        f"{phases_opened!r}) -- leaving the provisional fixture in place; "
        f"see {cap.live_transcript}"
    )
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "update-dynamic-expansion")

    state = _YAML.load((blare_root / "state.yaml").read_bytes())
    assert state["analyzed_sha"] == _TARGET_SHA
