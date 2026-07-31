"""Low-level driving primitives shared by every release-suite scenario capture
(T4.1). Reuses the same PTY harness the e2e suite drives the installed `blare`
binary through (`tests/e2e/pty_harness.py`); the only difference from an e2e run is
the SDK seam value -- `record:<dir>` here instead of `replay:<dir>` -- which routes
through the live `claude_agent_sdk.ClaudeSDKClient` (T2.6) wrapped by the recorder
(T2.1), so every scenario module in this package produces a real fixture rather than
replaying one.

A real phase turn over a whole codebase can run for minutes; every wait here defaults
to a generous timeout rather than the e2e suite's few seconds. A live run's amendment
behaviour is not scriptable in advance either: the model may fold an organic
`amend_proposal` into any phase's own turn (observed for real capturing this task's
own fixtures), inserting a reply-pending prompt no fixed occurrence count anticipated.
Every "approve along the way" helper here therefore waits on the `"$ approve"` prefix
shared by every reply-pending prompt (ordinary checkpoint, amendment re-presentation,
no-impact confirmation -- cli.md) and approves whatever actually shows up, rather than
counting occurrences of one exact prompt string.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.pty_harness import PtyProcess

CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
REJECTABLE_AMENDMENT_PROMPT = "$ approve · reject · abort · anything else is chat"
PROMPT_PREFIX = "$ approve"

# A real phase turn over a whole codebase is minutes, not seconds (unlike the e2e
# suite's replayed, near-instant fixtures) -- generous by design.
DEFAULT_TIMEOUT = 1800.0


@dataclass(frozen=True)
class Capture:
    """One in-progress live capture: the running process, the directory
    `_RecordingSDKClient` (T2.6) is writing `scenario.jsonl` into as the session
    proceeds, and a plain-text `live_transcript` this driver appends the terminal's
    own rendered output to after every wait. Unlike the wire-level `scenario.jsonl`
    (which only gains a line when the model calls a tool or emits text),
    `live_transcript` is what lets a *separate* process check what is actually on
    screen while a capture is in progress -- a real phase turn can run silent for
    many minutes while the model uses its own native read tools, and the driving
    process's own PTY master fd cannot safely be shared with another process (a
    `/dev/ptmx`-derived fd re-opened via `/proc/<pid>/fd/<n>` allocates a *new*,
    unrelated pty pair rather than attaching to the existing one -- learned the hard
    way capturing this task's own fixtures)."""

    process: PtyProcess
    record_dir: Path
    live_transcript: Path


def start_recording(
    blare_bin: Path,
    args: list[str],
    repo_dir: Path,
    record_dir: Path,
    xdg_state: Path,
    home: Path | None = None,
) -> Capture:
    """Launch `blare` against `repo_dir` through a PTY (checkpoints are interactive by
    spec, R22) with `BLARE_SDK_FIXTURES=record:<record_dir>` -- the real client (T2.6),
    wrapped by the recorder (T2.1), writes the genuine session to
    `record_dir/scenario.jsonl` as it goes. `home` overrides `HOME` for the one scenario
    that needs a scratch, credential-less login state (R12's auth-required capture)."""
    record_dir.mkdir(parents=True, exist_ok=True)
    env = {"BLARE_SDK_FIXTURES": f"record:{record_dir}", "XDG_STATE_HOME": str(xdg_state)}
    if home is not None:
        env["HOME"] = str(home)
    process = PtyProcess([str(blare_bin), *args], cwd=repo_dir, env=env)
    live_transcript = record_dir / "live_terminal_output.txt"
    live_transcript.write_text("")
    return Capture(process=process, record_dir=record_dir, live_transcript=live_transcript)


def _append_live(cap: Capture, output: str) -> None:
    with cap.live_transcript.open("a", encoding="utf-8") as handle:
        handle.write(output)
        handle.write("\n----- driver checkpoint marker -----\n")


def _prompt_count(output: str) -> int:
    return output.count(PROMPT_PREFIX)


# A live gate-repair loop can genuinely stall: observed for real capturing this
# task's own fixtures, the model repeatedly re-edited an alert_recommendations
# entry's own linkage field without ever touching the failure mode's *coverage.yaml*
# entry (`alert_ids`), which is what `unmapped_failure_mode` actually checks -- the
# same violation kept reappearing identically. This is not scripting the model's
# conclusions; it is the same corrective nudge a real reviewing user would type at
# the same impasse, sent at most once per drive so a scripted capture cannot loop
# forever on a single ambiguity.
_STALL_HINT = (
    "the same violation keeps recurring -- a failure mode only counts as mapped "
    "once its own coverage.yaml entry's alert_ids field lists the covering alert's "
    "id (a separate propose_edits call with entry_type \"coverage\"), not just the "
    "alert_recommendations entry's own linkage field"
)


def _drive(
    cap: Capture,
    *,
    stop_marker: str | None,
    max_iterations: int,
    stall_after: int = 3,
    stall_hint: str = _STALL_HINT,
) -> str | None:
    """Shared loop behind `approve_until`/`approve_to_exit`: approve every
    reply-pending prompt, stopping at `stop_marker` (or at process exit when
    `stop_marker` is `None`). If the identical incremental content repeats
    `stall_after` times in a row -- a real, observed failure mode where a repair
    never resolves the violation it was meant to fix -- send `stall_hint` once
    instead of another blind "approve", then resume approving."""
    output = ""
    last_delta: str | None = None
    repeat = 0
    hinted = False
    for _ in range(max_iterations):
        try:
            new_output = cap.process.read_until(
                PROMPT_PREFIX, occurrence=_prompt_count(output) + 1, timeout=DEFAULT_TIMEOUT
            )
        except TimeoutError as exc:
            if stop_marker is None and "process exited: True" in str(exc):
                return None
            # An unexpected hang (not a clean exit): the caller is giving up on
            # this process without ever draining it via `finish()`, so nothing
            # else will kill it -- do that here rather than leaking a live
            # `blare` (and its own `claude` subprocess) past this point.
            cap.process.terminate()
            raise
        _append_live(cap, new_output)
        delta = new_output[len(output) :]
        output = new_output
        if stop_marker is not None and stop_marker in output:
            return output
        repeat = repeat + 1 if delta == last_delta else 0
        last_delta = delta
        if repeat >= stall_after and not hinted:
            cap.process.send_line(stall_hint)
            hinted = True
            repeat = 0
            continue
        cap.process.send_line("approve")
    # Exceeded max_iterations without resolving -- same "giving up, nobody else
    # will drain this" reasoning as the TimeoutError branch above.
    cap.process.terminate()
    raise RuntimeError(
        f"no resolution within {max_iterations} prompts (stop_marker={stop_marker!r}); "
        f"see {cap.live_transcript}"
    )


def approve_until(cap: Capture, stop_marker: str, *, max_iterations: int = 40) -> str:
    """Approve every reply-pending prompt until `stop_marker` first appears in the
    accumulated output, then return *without* replying to that one -- the caller
    decides what to send at the stop point (a chat interjection, a reject, or
    simply inspecting which phase/view it turned out to be)."""
    output = _drive(cap, stop_marker=stop_marker, max_iterations=max_iterations)
    assert output is not None
    return output


def approve_to_exit(cap: Capture, *, max_iterations: int = 60) -> None:
    """Approve every reply-pending prompt until the process exits -- for a scenario
    scripted as "approve everything", whatever real prompts a live run actually
    produces, including an unscripted organic amendment."""
    _drive(cap, stop_marker=None, max_iterations=max_iterations)


def chat_at_marker(cap: Capture, marker: str, text: str, *, max_iterations: int = 40) -> str:
    """Approve along until `marker` first appears (see `approve_until`), then send
    `text` as chat instead of approving. Returns the output at the point of the chat."""
    output = approve_until(cap, marker, max_iterations=max_iterations)
    cap.process.send_line(text)
    return output


def reply_at_marker(
    cap: Capture, marker: str, reply: str, *, max_iterations: int = 40
) -> str:
    """Approve along until `marker` first appears, then send `reply` verbatim (a
    reserved word like `reject`, or chat text) -- the general form `chat_at_marker`
    and an eventual reject/approve both reduce to."""
    output = approve_until(cap, marker, max_iterations=max_iterations)
    cap.process.send_line(reply)
    return output


def finish(cap: Capture, *, timeout: float = 300.0) -> tuple[int, str]:
    """Drain to process exit and append the tail to the live transcript."""
    result = cap.process.read_all_until_exit(timeout=timeout)
    _append_live(cap, result.output)
    return result.exit_code, result.output


def finalize_capture(record_dir: Path, dest_dir: Path) -> Path:
    """Copy `record_dir/scenario.jsonl` into `dest_dir` (a
    `tests/fixtures/claude-sdk/<scenario>/` directory), replacing whatever provisional
    fixture is there. Callers must read and scrub the recorded file (global recording
    rules) *before* calling this -- this function only moves already-reviewed bytes
    into place, it performs no scrubbing of its own."""
    src = record_dir / "scenario.jsonl"
    if not src.is_file():
        raise RuntimeError(f"no scenario.jsonl was recorded at {record_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "scenario.jsonl"
    shutil.copyfile(src, dest)
    return dest


__all__ = [
    "CHECKPOINT_PROMPT",
    "REJECTABLE_AMENDMENT_PROMPT",
    "PROMPT_PREFIX",
    "DEFAULT_TIMEOUT",
    "Capture",
    "start_recording",
    "approve_until",
    "approve_to_exit",
    "chat_at_marker",
    "reply_at_marker",
    "finish",
    "finalize_capture",
]
