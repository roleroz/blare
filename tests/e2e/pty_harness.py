"""A minimal PTY harness for driving the installed `blare` binary as a real user would.

Checkpoints are interactive by spec (R22): stdin must be a TTY or Blare refuses
before presenting any of them. A plain subprocess pipe makes stdin non-interactive,
so e2e tests drive `blare` through a real pseudo-terminal instead. T1.1's two
scenarios never reached a checkpoint prompt; T2.3 extends this file with
`read_until` (wait for a specific line of output -- a phase header or the
checkpoint prompt -- before sending the next scripted reply), which is what makes
a genuinely interactive multi-checkpoint scenario drivable.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PtyResult:
    """What a completed PTY-driven run produced."""

    exit_code: int
    output: str


class PtyProcess:
    """A subprocess whose stdin/stdout/stderr are connected to one pseudo-terminal."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        self._master_fd, slave_fd = os.openpty()
        self._process = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)
        self._output = bytearray()
        self._closed = False

    def send_line(self, line: str) -> None:
        """Write one line (with a trailing newline) to the process's stdin."""
        os.write(self._master_fd, (line + "\n").encode())

    def read_until(self, pattern: str, occurrence: int = 1, timeout: float = 10.0) -> str:
        """Block until `pattern` has appeared at least `occurrence` times in the
        output read so far (counting from the start of this process's output, so
        a pattern that repeats across checkpoints -- the prompt text itself -- can
        still be waited on precisely by occurrence), returning that output.

        Raises `TimeoutError` naming the output collected so far if `pattern`
        never appears (whether because `timeout` elapsed or the process exited
        first) -- this is a test-authoring aid, so a stuck scenario fails fast and
        legibly rather than hanging until pytest's own timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            text = self._output.decode(errors="replace")
            if text.count(pattern) >= occurrence:
                return text
            remaining = deadline - time.monotonic()
            process_done = self._process.poll() is not None
            if remaining <= 0 or process_done:
                raise TimeoutError(
                    f"pattern {pattern!r} (occurrence {occurrence}) did not appear "
                    f"(process exited: {process_done}); output so far:\n{text}"
                )
            ready, _, _ = select.select([self._master_fd], [], [], min(remaining, 0.2))
            if ready:
                try:
                    chunk = os.read(self._master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    self._output.extend(chunk)

    def read_all_until_exit(self, timeout: float = 10.0) -> PtyResult:
        """Drain output until the process exits, then return its exit code and output.

        Kills the process and returns exit code -1 if `timeout` elapses first.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._process.kill()
                # Suppressed, not propagated: a `wait()` that itself doesn't
                # reap within 5s (e.g. an uninterruptible child) must not skip
                # this method's own `_close()` below, which callers (T4.1's
                # `on_commit` context manager among them) rely on running
                # unconditionally once they've decided to stop waiting.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=5)
                break
            ready, _, _ = select.select([self._master_fd], [], [], min(remaining, 0.2))
            if ready:
                try:
                    chunk = os.read(self._master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    self._output.extend(chunk)
                    continue
            if self._process.poll() is not None:
                break

        exit_code = self._process.poll()
        self._close()
        return PtyResult(
            exit_code=exit_code if exit_code is not None else -1,
            output=self._output.decode(errors="replace"),
        )

    def terminate(self, timeout: float = 5.0) -> None:
        """Kill the process if it's still running and close the pty -- for a
        caller giving up on a stuck scenario without draining to exit (e.g. a
        driving loop's own iteration cap), so the child is never left running
        past the point its own harness stopped waiting on it."""
        if self._process.poll() is None:
            self._process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=timeout)
        self._close()

    def _close(self) -> None:
        if not self._closed:
            os.close(self._master_fd)
            self._closed = True


_PROMPT_PREFIX = "$ approve"

# cli.md's progress spinner redraws a `\x1b[K· <label> (Ns, <activity>)` line once
# per elapsed second, so the number of these lines between two otherwise-identical
# checkpoints varies with (replayed, but still real-clock-timed) response latency.
# Mirrors `tests/release/scenario_driver.py`'s own `_SPINNER_LINE`/stall-hint
# mechanism, duplicated here rather than imported: tests/release depends on
# tests/e2e (it reuses this module's `PtyProcess`), so the reverse import isn't
# available.
_SPINNER_LINE = re.compile(r"\x1b\[K·[^\n]*\(\d+s(?:, [^)]*)?\)\n?")

# amendment-agent-approved's real capture (T4.1) records a genuine live stall --
# a gate-repair loop that kept re-presenting the same violation -- and the exact
# nudge that broke it, sent once and never again. Replaying that fixture
# deterministically reproduces the same stall, so a driver that only ever sends
# "approve" diverges from the recording at the point the real session needed
# this hint; anything not exercising that fixture never triggers the repeat
# counter below and never sends it.
_STALL_HINT = (
    "the same violation keeps recurring -- a failure mode only counts as mapped "
    "once its own coverage.yaml entry's alert_ids field lists the covering alert's "
    "id (a separate propose_edits call with entry_type \"coverage\"), not just the "
    "alert_recommendations entry's own linkage field"
)


def _drive(
    process: PtyProcess,
    *,
    stop_marker: str | None,
    prompt_prefix: str,
    max_iterations: int,
    stall_after: int,
    stall_hint: str,
) -> str | None:
    """Shared loop behind `approve_until`/`approve_all`: approve every
    reply-pending prompt, stopping at `stop_marker` (or at process exit when
    `stop_marker` is `None`). If the identical incremental content (modulo the
    progress spinner's own timer noise) repeats `stall_after` times in a row,
    sends `stall_hint` once instead of another blind "approve" -- reproducing a
    real, recorded stall exactly once, the same way the live capture that stall
    came from was actually driven. Mirrors `tests/release/scenario_driver.py`'s
    own `_drive` exactly, including its reasoning for recomputing `occurrence`
    from the locally accumulated `output` on every iteration rather than
    incrementing a counter from 1: `PtyProcess.read_until`'s `occurrence` counts
    from the start of the *process's* output, not this call's, so a driving call
    that starts after another has already consumed some occurrences must ask for
    the next real one, not restart from 1."""
    output = ""
    last_delta: str | None = None
    repeat = 0
    hinted = False
    for _ in range(max_iterations):
        occurrence = output.count(prompt_prefix) + 1
        try:
            new_output = process.read_until(prompt_prefix, occurrence=occurrence, timeout=30.0)
        except TimeoutError as exc:
            if stop_marker is None and "process exited: True" in str(exc):
                return None
            raise
        delta = new_output[len(output) :]
        output = new_output
        if stop_marker is not None and stop_marker in output:
            return output
        normalized_delta = _SPINNER_LINE.sub("", delta)
        repeat = repeat + 1 if normalized_delta == last_delta else 0
        last_delta = normalized_delta
        if repeat >= stall_after and not hinted:
            process.send_line(stall_hint)
            hinted = True
            repeat = 0
            continue
        process.send_line("approve")
    raise RuntimeError(
        f"no resolution within {max_iterations} prompts (stop_marker={stop_marker!r})"
    )


def approve_all(
    process: PtyProcess,
    *,
    prompt_prefix: str = _PROMPT_PREFIX,
    max_iterations: int = 150,
    stall_after: int = 3,
    stall_hint: str = _STALL_HINT,
) -> PtyResult:
    """Approve every reply-pending prompt (an ordinary checkpoint, an amendment
    re-presentation, a no-impact confirmation -- cli.md, all sharing the `$ approve`
    prefix) until the process exits, mirroring a user who always types "approve".

    T4.1's real captures replay whatever real, organic checkpoint sequence the live
    model actually produced -- including spontaneous mid-run amendments a hand-
    authored provisional fixture never had -- so a fixed "N checkpoints, one per
    phase" occurrence count no longer holds for every scenario. This is
    `tests/release/scenario_driver.py`'s `approve_to_exit` for the e2e suite."""
    output = _drive(
        process,
        stop_marker=None,
        prompt_prefix=prompt_prefix,
        max_iterations=max_iterations,
        stall_after=stall_after,
        stall_hint=stall_hint,
    )
    assert output is None
    return process.read_all_until_exit()


def approve_until(
    process: PtyProcess,
    marker: str,
    *,
    prompt_prefix: str = _PROMPT_PREFIX,
    max_iterations: int = 100,
    stall_after: int = 3,
    stall_hint: str = _STALL_HINT,
) -> str:
    """Approve every reply-pending prompt until `marker` first appears in the
    accumulated output, then return *without* replying to that one -- the caller
    decides what to send at the stop point (a chat interjection, an abort, a
    reject, or simply inspecting which real checkpoint it turned out to be).

    Mirrors `tests/release/scenario_driver.py`'s own `approve_until`: a real
    capture's organic mid-run amendments mean a fixed "occurrence N is phase N"
    count no longer holds, so this waits for content instead (a phase header, an
    amendment's own re-presentation origin line -- cli.md), approving whatever
    real, unrelated checkpoints happen to come first -- including, potentially,
    the same real stall `approve_all` can hit, so this shares the same
    stall-hint mechanism rather than blindly approving through it."""
    output = _drive(
        process,
        stop_marker=marker,
        prompt_prefix=prompt_prefix,
        max_iterations=max_iterations,
        stall_after=stall_after,
        stall_hint=stall_hint,
    )
    assert output is not None
    return output


def run_blare(
    blare_bin: Path,
    args: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> PtyResult:
    """Run the installed `blare` binary against `cwd` through a PTY, to completion."""
    full_env: dict[str, str] = dict(os.environ)
    if env:
        full_env.update(env)
    process = PtyProcess([str(blare_bin), *args], cwd=cwd, env=full_env)
    return process.read_all_until_exit(timeout=timeout)


def run_blare_noninteractive(
    blare_bin: Path,
    args: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> PtyResult:
    """Run `blare` via a plain (non-PTY) subprocess, so stdin is not a TTY.

    Used by every refusal e2e test whose refusal fires before R22's TTY check (step
    8) -- there is no checkpoint to drive, so a real terminal buys nothing and would
    only mask a bug that made the run reach further than expected. The dedicated
    R22 e2e test uses this deliberately, to prove the refusal itself.
    """
    full_env: dict[str, str] = dict(os.environ)
    if env:
        full_env.update(env)
    result = subprocess.run(
        [str(blare_bin), *args],
        cwd=str(cwd),
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return PtyResult(exit_code=result.returncode, output=result.stdout)


__all__ = [
    "PtyProcess",
    "PtyResult",
    "approve_all",
    "approve_until",
    "run_blare",
    "run_blare_noninteractive",
]
