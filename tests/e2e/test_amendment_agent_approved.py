"""e2e: an agent-proposed amendment, approved (R2; architecture.md's Amendment
mechanism; agent.md's provisional fixture list: "agent-proposed amendment,
approved").

Uses the amendment-agent-approved replay fixture: the happy path's phases 1-4,
then a chat exchange at phase 4's checkpoint that proposes an amendment to phase
1 (`amend_proposal`), resumed via `request_repair` since the proposing turn ends
without `amend_complete` (the model's own turn already ended before the
orchestrator can drive `request_repair` -- the resume path). The repair lands
during that call, the unit closes on approval, and phase 4's checkpoint then
re-presents fresh.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_REJECTABLE_AMENDMENT_PROMPT = "$ approve · reject · abort · anything else is chat"
_YAML = YAML(typ="safe")


def test_e2e_amendment_agent_proposed_approved(tmp_path: Path) -> None:
    """Chat at phase 4's checkpoint proposes an amendment to phase 1; its repair
    lands, the unit is re-presented once (naming the origin "proposed by agent"),
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
    for occurrence in (1, 2, 3):
        process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")

    # Phase 4's checkpoint: chat proposes the amendment.
    process.read_until(_CHECKPOINT_PROMPT, occurrence=4)
    process.send_line("can we revise the system map while we're here?")

    # The chat reply renders, then the amendment (rejectable -- agent-origin)
    # presents, naming its origin and phase 1's revised content.
    output = process.read_until(_REJECTABLE_AMENDMENT_PROMPT, occurrence=1)
    assert "amendment · proposed by agent" in output
    assert "phase 1" in output
    assert "the web frontend (revised)" in output
    process.send_line("approve")

    # Phase 4's checkpoint re-presents fresh after the unit closes.
    process.read_until(_CHECKPOINT_PROMPT, occurrence=5)
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    assert "analysis complete" in result.output

    system_map = _YAML.load((repo_dir / ".blare" / "system-map.yaml").read_bytes())
    [component] = system_map
    assert component["description"] == "the web frontend (revised)"
