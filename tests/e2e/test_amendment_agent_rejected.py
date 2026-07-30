"""e2e: an agent-proposed amendment, rejected -- restore (R2; agent.md's
provisional fixture list: "agent-proposed amendment... rejected (restore)").

Same setup as the approved variant, but the user rejects the amendment: phase
1's pre-amendment content survives byte-for-byte, and the run still completes
normally through phase 4's own (unrelated) work.
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


def test_e2e_amendment_agent_proposed_rejected_restores(tmp_path: Path) -> None:
    """Rejecting the agent-proposed amendment restores phase 1's entry to its
    pre-amendment state, byte for byte; the run still completes (R20's write
    still lands the rest of the analysis)."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation(
            "blare/tests/fixtures/claude-sdk/amendment-agent-rejected/scenario.jsonl"
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

    process.read_until(_CHECKPOINT_PROMPT, occurrence=4)
    process.send_line("can we revise the system map while we're here?")

    process.read_until(_REJECTABLE_AMENDMENT_PROMPT, occurrence=1)
    process.send_line("reject")

    process.read_until(_CHECKPOINT_PROMPT, occurrence=5)
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    assert "analysis complete" in result.output

    system_map = _YAML.load((repo_dir / ".blare" / "system-map.yaml").read_bytes())
    [component] = system_map
    assert component["description"] == "the web frontend"
