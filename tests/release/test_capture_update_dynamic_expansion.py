"""live: capture the update-dynamic-expansion fixture (R18) against a fresh
`testdata/kvstore` repo with the live Claude Agent SDK (T4.1's continuation).

The target changed to kvstore (2026-08-01, decisions.md); this is a fresh
attempt against `dynamic_expansion_delta` -- kvstore_repo.py's commit bundling
the storage-collision fix and the admin stale-cache fix into one commit,
spanning two distinct failure domains, chosen as this scenario's new candidate
because a single commit touching two unrelated concerns is a plausible trigger
for the model to reconsider whether other phases need work.

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
from tests.release import capture, kvstore_repo
from tests.release.scenario_driver import finalize_capture

_YAML = YAML(typ="safe")

_TARGET_NAME = "dynamic_expansion_delta"


def _distinct_phases_opened(record_dir: Path) -> set[int]:
    phases: set[int] = set()
    for line in (record_dir / "scenario.jsonl").read_text().splitlines()[1:]:
        entry = json.loads(line)
        event = entry.get("event", {})
        if event.get("type") == "phase_prompt":
            phases.add(int(event["phase"]))
    return phases


def test_live_capture_update_dynamic_expansion(tmp_path: Path) -> None:
    """`blare update` over a real multi-domain delta, nudged at the first
    checkpoint to reconsider other phases, completes; only finalizes as the
    real fixture if more than one phase actually opened (a genuine dynamic
    expansion), leaving the provisional fixture in place and failing loudly
    otherwise."""
    cap = capture.capture_update_dynamic_expansion(tmp_path, _TARGET_NAME)

    blare_root = kvstore_repo.blare_root(cap.repo)
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
    assert state["analyzed_sha"] == cap.target_sha
