"""Entry point and terminal surface (architecture): no run logic of its own.

T1.1 scope: `parse_args`, `main`, and a `TerminalPresenter` that renders `error` and
`summary` for real (per `brand/design-language.md` §6) since those are what the
walking skeleton's two flows produce. Checkpoint/amendment/no-impact rendering and
the chat loop are stubbed (`NotImplementedError`) pending the phase engine
(T2.2-T3.1); their method signatures are implemented now purely so
`TerminalPresenter` type-checks against `orchestrator.Presenter` in full, per
`cli.md`'s approved interface.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from blare import orchestrator
from blare.model import RunMode
from blare.orchestrator import (
    AmendmentReply,
    AmendmentView,
    CheckpointReply,
    CheckpointView,
    NoImpactView,
    PromptKind,
    RunSummary,
)

__version__ = "0.1.0"

_RESULT_PREFIX = "→ "  # brand §6: "→" for results


@dataclass(frozen=True)
class ParsedCommand:
    """The result of parsing argv: which mode to run."""

    mode: RunMode


def parse_args(argv: list[str]) -> ParsedCommand:
    """Parse argv into a `ParsedCommand`. Raises `SystemExit` per argparse's own
    convention: 2 for unknown commands/stray flags (with usage on stderr), 0 for
    `--help`/`--version`.
    """
    parser = argparse.ArgumentParser(prog="blare")
    parser.add_argument("--version", action="version", version=f"blare {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("analyze")
    subparsers.add_parser("update")

    args = parser.parse_args(argv)
    mode = RunMode.ANALYZE if args.command == "analyze" else RunMode.UPDATE
    return ParsedCommand(mode=mode)


def main(argv: list[str], run: orchestrator.RunFn = orchestrator.run) -> int:
    """The console entry point: parse argv, wire a real presenter, run.

    Passes the invocation cwd as `run`'s `repo_path` (gitrepo discovers the root
    from it) and constructs the `TerminalPresenter` over the process's real
    stdin/stdout/stderr, per `cli.md`. Catches nothing itself: argparse's own
    `SystemExit` (usage errors, `--help`, `--version`) propagates untouched, and
    every other exit code is `run`'s to assign.
    """
    parsed = parse_args(argv)
    presenter = TerminalPresenter(sys.stdin, sys.stdout, sys.stderr)
    return run(parsed.mode, Path.cwd(), presenter)


class TerminalPresenter:
    """Renders the orchestrator's reports and forwards what the user types.

    Implements `orchestrator.Presenter` in full; only `error`, `summary`, `notice`,
    and `is_interactive` have real bodies in T1.1 — the rest are exercised once the
    phase engine exists to drive them.
    """

    def __init__(self, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr

    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
        raise NotImplementedError("checkpoint rendering lands in T2.3")

    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply:
        raise NotImplementedError("amendment rendering lands in T2.4")

    def present_no_impact(self, view: NoImpactView) -> CheckpointReply:
        raise NotImplementedError("no-impact rendering lands in T3.1")

    def show_chat_reply(
        self, text: str, prompt: PromptKind | None
    ) -> AmendmentReply | None:
        raise NotImplementedError("the chat loop lands in T2.3")

    def notice(self, text: str) -> None:
        self._write(self._stdout, text)

    def error(self, cause: str, next_action: str, detail: str | None = None) -> None:
        self._write(self._stderr, cause)
        self._write(self._stderr, f"{_RESULT_PREFIX}{next_action}")
        if detail is not None:
            self._write(self._stderr, detail)

    def summary(self, s: RunSummary) -> None:
        self._write(self._stdout, f"{_RESULT_PREFIX}{s.outcome}")
        if s.transcript_path is not None:
            self._write(self._stdout, f"{_RESULT_PREFIX}transcript: {s.transcript_path}")

    def is_interactive(self) -> bool:
        try:
            return self._stdin.isatty()
        except (ValueError, OSError):
            return False

    def _write(self, stream: TextIO, text: str) -> None:
        # Void methods swallow stream failures rather than raise (cli.md's
        # error-handling rule): the run's outcome is already decided, and a
        # render crash on a dead stream must not corrupt it.
        try:
            stream.write(text + "\n")
            stream.flush()
        except (BrokenPipeError, OSError):
            pass
