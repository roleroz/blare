"""e2e: R25 -- progress feedback during a slow phase (T4.3).

Discovered via live user testing: a real `blare analyze` run gave zero
terminal output while a phase was computing, leaving no way to tell whether
the process was working or hung, or which phase was active, across a phase
that ran for nearly two hours. This drives the same scenario at e2e scale
against the progress-feedback replay fixture: phase 1's turn is scripted with
several tool calls (two simulated filesystem-read "activity" events, each
carrying a real `delay_before`, then the ordinary propose_edits round trip)
before its `turn_end`, so the phase genuinely takes real wall-clock time and
the orchestrator's real-clock progress ticker has something to tick against.
"""

from __future__ import annotations

import re
from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
# cli.md's Rendering rules: "· phase 3 — metric coverage (12s, propose_edits)".
_PROGRESS_LINE = re.compile(r"^· phase 1 — system map \(\d+s, (waiting|Read|Grep)\)$", re.MULTILINE)


def test_e2e_progress_lines_appear_before_the_checkpoint_during_a_slow_phase(
    tmp_path: Path,
) -> None:
    """Progress lines (R25) appear on the PTY, naming phase 1 and updating
    last_activity as the scripted tool calls arrive, all before phase 1's own
    checkpoint renders; the run then proceeds normally to completion."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/progress-feedback/scenario.jsonl")
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
    # Block until phase 1's checkpoint prompt appears -- the scripted delay
    # (2 * 1.2s) comfortably exceeds the orchestrator's ~1s tick interval, so
    # real progress lines accumulate in the output collected on the way here.
    # The PTY reports CRLF line endings; normalize before matching line anchors.
    output_before_checkpoint = process.read_until(
        _CHECKPOINT_PROMPT, occurrence=1, timeout=15.0
    ).replace("\r\n", "\n")

    progress_lines = _PROGRESS_LINE.findall(output_before_checkpoint)
    assert progress_lines, (
        f"expected at least one '· phase 1 — system map (...)' progress line "
        f"before the checkpoint prompt; output was:\n{output_before_checkpoint}"
    )
    # last_activity updates: at least one tick shows a real tool name, not only
    # "waiting" (R25: "the name of the most recent tool call the model made").
    assert "Read" in progress_lines or "Grep" in progress_lines
    # The progress lines appear strictly before the checkpoint prompt itself,
    # never after or interleaved with the checkpoint's own rendered content
    # (the phase header line names the phase, distinct from a progress line).
    checkpoint_prompt_pos = output_before_checkpoint.index(_CHECKPOINT_PROMPT)
    last_progress_match = list(re.finditer(_PROGRESS_LINE, output_before_checkpoint))[-1]
    assert last_progress_match.end() <= checkpoint_prompt_pos

    # The run proceeds normally: approve all four checkpoints to completion.
    for occurrence in (1, 2, 3, 4):
        if occurrence > 1:
            process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    assert (repo_dir / ".blare" / "state.yaml").is_file()
