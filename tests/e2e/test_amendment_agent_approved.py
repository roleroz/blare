"""e2e: an agent-proposed amendment, approved (R2; architecture.md's Amendment
mechanism; agent.md's provisional fixture list: "agent-proposed amendment,
approved").

Uses the real, live-captured amendment-agent-approved fixture (T4.1): a fresh
kvstore analyze; chat at phase 4's checkpoint proposes an amendment to phase 1;
the repair lands, the unit closes on approval, and phase 4's checkpoint
re-presents fresh. The real session also hit a genuine gate-repair stall along
the way (a real violation that kept recurring) resolved by the exact one-time
nudge `approve_all`/`approve_until` reproduce when they detect the same
repeating content (`pty_harness._drive`).

KNOWN ISSUE (T4.1, unresolved): this test is flaky against this specific
fixture -- it has been observed both passing and failing
(`FixtureMismatchError`, exit 2) across otherwise-identical re-runs with no
code change. The replayed fixture itself is a fixed, deterministic recording,
so the flakiness is in the PTY-driven timing of `_drive`'s stall-detection
here, not in the fixture: real (if fast, replayed) wall-clock variance between
runs appears to occasionally shift exactly when the repeat-counter reaches
`stall_after`, sending "approve" where the fixture expects the stall hint (or
vice versa) at a boundary case. Not resolved within T4.1's scope -- flagged
rather than quarantined so the failure stays visible; do not treat a green run
of this test alone as proof the flake is fixed.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess, approve_all, approve_until
from tests.e2e.repo_fixtures import init_repo

_REJECTABLE_AMENDMENT_MARKER = "amendment · proposed by agent"
_CHAT_TEXT = (
    "before we wrap up -- can you revise the system map now that we've seen the "
    "rest of the analysis?"
)
_YAML = YAML(typ="safe")


def test_e2e_amendment_agent_proposed_approved(tmp_path: Path) -> None:
    """Chat at phase 4's checkpoint proposes an amendment to phase 1; its repair
    lands, the unit is re-presented (naming the origin "proposed by agent"),
    approval re-freezes phase 1, and the run completes with the revision
    written."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation(
            "blare/tests/fixtures/claude-sdk/amendment-agent-approved/scenario.jsonl"
        )
    )
    assert blare_bin.exists()
    assert fixture_file.exists()

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)

    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}",
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
        },
    )
    # Phase 4's checkpoint: chat proposes the amendment.
    approve_until(process, "phase 4 —")
    process.send_line(_CHAT_TEXT)

    # The chat reply renders, then the amendment (rejectable -- agent-origin)
    # presents, naming its origin.
    output = approve_until(process, _REJECTABLE_AMENDMENT_MARKER)
    assert _REJECTABLE_AMENDMENT_MARKER in output
    assert "phase 1" in output
    process.send_line("approve")

    result = approve_all(process)

    assert result.exit_code == 0
    assert "analysis complete" in result.output

    system_map = _YAML.load((repo_dir / ".blare" / "system-map.yaml").read_bytes())
    assert {c["id"] for c in system_map} == {
        "sm-datastore-flatfile-storage",
        "sm-dep-filesystem",
        "sm-dep-prometheus-client",
        "sm-entrypoint-admin-update-value",
        "sm-entrypoint-get-value",
        "sm-job-data-sync",
        "sm-job-eviction-scheduler",
        "sm-job-evictor",
        "sm-service-cache",
    }
