"""e2e: R25 -- progress feedback during a slow phase (T4.3), consolidated (T4.6).

Discovered via live user testing: a real `blare analyze` run gave zero
terminal output while a phase was computing, leaving no way to tell whether
the process was working or hung, or which phase was active, across a phase
that ran for nearly two hours. This drives the same scenario at e2e scale
against the progress-feedback replay fixture: phase 1's turn is scripted with
several tool calls (two simulated filesystem-read "activity" events, each
carrying a real `delay_before`, then the ordinary propose_edits round trip)
before its `turn_end`, so the phase genuinely takes real wall-clock time and
the orchestrator's real-clock progress ticker has something to tick against.

A second live-testing pass (2026-07-31, T4.6) found the *original* form of
this feedback noisy: one line appended per ~1s tick, dozens of near-identical
lines during a slow phase. `TerminalPresenter.progress` now consolidates
same-key ticks into a single in-place-updating line per state, committing a
small, fixed number of *permanent* lines -- one per distinct state the run
actually passed through, each holding that state's final elapsed time --
rather than one per tick. This test now asserts that consolidated shape.
"""

from __future__ import annotations

import re
from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
# cli.md's Rendering rules: "· phase 3 — metric coverage (12s, propose_edits)".
# T4.6: a same-key tick is only ever written in place (`\r` + clear-to-end-of-line,
# no trailing newline -- optional here since a superseded tick is immediately
# overwritten by the next `\r`-prefixed write with no newline in between, so it
# never satisfies this pattern); a *permanent*, finalized line is always
# followed by a real `\n` -- either `_finalize_progress_line`'s own bare `\n`
# (a key change) or the checkpoint's own trailing newline convention (the run's
# last tick, finalized once the checkpoint itself starts rendering). Requiring
# the literal trailing `\n` is what distinguishes a committed, permanent line
# from an in-place-only redraw that never became one.
_PROGRESS_LINE = re.compile(
    r"(?:\r\x1b\[K)?· phase 1 — system map \((\d+)s, (waiting|Read|Grep)\)\n"
)


def test_e2e_progress_lines_appear_before_the_checkpoint_during_a_slow_phase(
    tmp_path: Path,
) -> None:
    """Progress lines (R25) appear on the PTY, naming phase 1 and updating
    last_activity as the scripted tool calls arrive, all before phase 1's own
    checkpoint renders, consolidated (T4.6) into a small, fixed number of
    permanent lines -- one per state reached, not one per tick -- each holding
    that state's final elapsed time; the run then proceeds normally to
    completion."""
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
    # the "waiting" and "Read" states are each ticked at least once on the way
    # here. The PTY reports CRLF line endings for the plain `\n` every other
    # write in this module uses; normalize those before matching, but leave
    # the raw `\r`/`\x1b[K` bytes T4.6 writes for the in-place redraw alone --
    # they are not touched by the PTY's NL-to-CRLF output translation, and
    # `_PROGRESS_LINE` above accounts for them directly.
    output_before_checkpoint = process.read_until(
        _CHECKPOINT_PROMPT, occurrence=1, timeout=15.0
    ).replace("\r\n", "\n")

    permanent_lines = list(_PROGRESS_LINE.finditer(output_before_checkpoint))
    activities_seen = [match.group(2) for match in permanent_lines]
    assert activities_seen, (
        f"expected at least one permanent '· phase 1 — system map (...)' progress "
        f"line before the checkpoint prompt; output was:\n{output_before_checkpoint}"
    )
    # "waiting" is always the first state, and is always superseded once the
    # scripted "Read" activity arrives (well within the call's ~2.4s scripted
    # duration) -- becoming a permanent line via that key change.
    assert activities_seen[0] == "waiting"
    assert "Read" in activities_seen
    # "Grep" arrives only ~1.2s before the phase's own turn ends, leaving little
    # room for a tick to land while it is the current activity -- timing-
    # dependent, exactly like the pre-T4.6 version of this test's own "Read" or
    # "Grep" allowance. When it is reached, it is the last state (nothing comes
    # after it in the fixture); when it is not, "Read" is the last state
    # finalized directly by the checkpoint's own finalize step.
    assert activities_seen[-1] == ("Grep" if "Grep" in activities_seen else "Read")
    # Consolidation (T4.6): a small, fixed number of permanent lines -- one per
    # state actually reached (at most "waiting", "Read", "Grep") -- never one
    # per tick, which the pre-T4.6 behavior produced dozens of for this same
    # scripted delay.
    assert len(activities_seen) <= 3
    # Each state is consolidated into exactly one permanent line, never
    # repeated -- the whole point of T4.6 over the old per-tick behavior.
    assert len(activities_seen) == len(set(activities_seen))
    # Elapsed time only ever increases across the states reached -- the T4.6
    # collision rule (`elapsed_seconds == 0.0` is always a key change) exists
    # precisely so elapsed time can never run backwards between two driving
    # calls; here it is a sanity check within this one call's own states.
    elapsed_seen = [int(match.group(1)) for match in permanent_lines]
    assert elapsed_seen == sorted(elapsed_seen)

    # The progress lines appear strictly before the checkpoint prompt itself,
    # never after or interleaved with the checkpoint's own rendered content
    # (the phase header line names the phase, distinct from a progress line).
    checkpoint_prompt_pos = output_before_checkpoint.index(_CHECKPOINT_PROMPT)
    assert permanent_lines[-1].end() <= checkpoint_prompt_pos

    # The run proceeds normally: approve all four checkpoints to completion.
    for occurrence in (1, 2, 3, 4):
        if occurrence > 1:
            process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    assert "analysis complete" in result.output
    assert (repo_dir / ".blare" / "state.yaml").is_file()
