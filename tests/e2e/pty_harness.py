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

import os
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

    def _close(self) -> None:
        if not self._closed:
            os.close(self._master_fd)
            self._closed = True


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


__all__ = ["PtyProcess", "PtyResult", "run_blare", "run_blare_noninteractive"]
