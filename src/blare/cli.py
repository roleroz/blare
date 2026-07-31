"""Entry point and terminal surface (architecture): no run logic of its own.

T1.1 built `parse_args`, `main`, and `error`/`summary`/`notice`/`is_interactive`.
T2.3 built the rest of `cli.md`'s presenter contract: `present_checkpoint`, the
chat loop's `show_chat_reply` (kind-aware for every `PromptKind`, including the
amendment kinds -- it only needed the kind, never the amendment view itself), and
`summary`'s full R13 content (entry counts, gap counts). T2.4 built
`present_amendment`: the amendment screen (origin line, one section per involved
phase, the reply alphabet including `reject` when rejectable). T3.1 builds
`present_no_impact`: the R18 no-impact screen (header, changed-file summary, the
agent's conclusion text, then the ordinary checkpoint prompt). T4.3 builds
`progress` (R25): the periodic `· ` status line the orchestrator's ticker renders
while a driving call is in flight. T4.5 builds `--unattended` (R26): `parse_args`'
new flag, `TerminalPresenter`'s `unattended` constructor flag (every
reply-pending method renders its view then returns `Approve()` immediately,
never reading stdin), and `main`'s completion bell.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from blare import orchestrator
from blare.model import Phase, RunMode
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
_PROMPT_MARKER = "$ "  # brand §6: "$" for the prompt
_PROGRESS_PREFIX = "· "  # R25: distinct from both, since a progress line is neither
_WAITING_ACTIVITY = "waiting"  # cli.md: rendered when last_activity is None

# brand/design-language.md §2: --blare-alert (#FF5A1F) and --blare-alert-hi (#FFB020),
# as 24-bit ANSI SGR sequences -- the only two words this module ever colors (§6:
# "color only for the severity word").
_ANSI_RESET = "\x1b[0m"
_ANSI_ALERT = "\x1b[38;2;255;90;31m"
_ANSI_WARN = "\x1b[38;2;255;176;32m"

_PHASE_TITLES: dict[Phase, str] = {
    Phase.SYSTEM_MAP: "system map",
    Phase.FAILURE_MODES: "failure modes",
    Phase.METRIC_COVERAGE: "metric coverage",
    Phase.ALERT_RECOMMENDATIONS: "alert recommendations",
}

# cli.md: "a line that is exactly `approve` or exactly `abort`... the prompt names
# the two verbs"; the rejectable-amendment continuation additionally names `reject`
# (D9's third reserved word, only ever offered there).
_CHECKPOINT_PROMPT = f"{_PROMPT_MARKER}approve · abort · anything else is chat"
_REJECTABLE_AMENDMENT_PROMPT = f"{_PROMPT_MARKER}approve · reject · abort · anything else is chat"

# cli.md: "amendment · proposed by agent" / "amendment · invariant repair".
_AMENDMENT_ORIGIN_LINES: dict[orchestrator.AmendmentOrigin, str] = {
    orchestrator.AmendmentOrigin.AGENT: "amendment · proposed by agent",
    orchestrator.AmendmentOrigin.SYSTEM: "amendment · invariant repair",
}

# Which prompt text and whether `reject` is reserved, per `show_chat_reply`'s
# `PromptKind` (cli.md: reject is a reserved word only at the rejectable-amendment
# continuation; every other kind reuses the plain checkpoint wording).
_PROMPT_BY_KIND: dict[PromptKind, tuple[str, bool]] = {
    PromptKind.CHECKPOINT: (_CHECKPOINT_PROMPT, False),
    PromptKind.NO_IMPACT: (_CHECKPOINT_PROMPT, False),
    PromptKind.AMENDMENT: (_CHECKPOINT_PROMPT, False),
    PromptKind.REJECTABLE_AMENDMENT: (_REJECTABLE_AMENDMENT_PROMPT, True),
}

_SEVERITY_WORDS = ("critical", "warning")


class _GapCounts(Protocol):
    """Structural stand-in for `artifacts.GapSummary` (`CheckpointView.gap_counts`
    and `RunSummary.gap_counts`'s real type): this module renders the three counts
    by attribute without importing artifacts (architecture: cli -> orchestrator
    only). Read-only properties, not plain attributes, so a frozen dataclass
    (`GapSummary`'s real shape) structurally satisfies this protocol."""

    @property
    def alertable(self) -> int: ...
    @property
    def metric_gap(self) -> int: ...
    @property
    def excluded(self) -> int: ...


@dataclass(frozen=True)
class ParsedCommand:
    """The result of parsing argv: which mode to run."""

    mode: RunMode
    unattended: bool = False


def parse_args(argv: list[str]) -> ParsedCommand:
    """Parse argv into a `ParsedCommand`. Raises `SystemExit` per argparse's own
    convention: 2 for unknown commands/stray flags (with usage on stderr), 0 for
    `--help`/`--version`.
    """
    parser = argparse.ArgumentParser(prog="blare")
    parser.add_argument("--version", action="version", version=f"blare {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "update"):
        subparser = subparsers.add_parser(name)
        # R26: `--unattended` on both subcommands, false by default -- no other
        # subcommand accepts it (cli.md's Interface).
        subparser.add_argument("--unattended", action="store_true", default=False)

    args = parser.parse_args(argv)
    mode = RunMode.ANALYZE if args.command == "analyze" else RunMode.UPDATE
    return ParsedCommand(mode=mode, unattended=args.unattended)


def main(argv: list[str], run: orchestrator.RunFn = orchestrator.run) -> int:
    """The console entry point: parse argv, wire a real presenter, run.

    Passes the invocation cwd as `run`'s `repo_path` (gitrepo discovers the root
    from it) and constructs the `TerminalPresenter` over the process's real
    stdin/stdout/stderr, per `cli.md`. Catches nothing itself: argparse's own
    `SystemExit` (usage errors, `--help`, `--version`) propagates untouched, and
    every other exit code is `run`'s to assign.

    R26 (T4.5): `parsed.unattended` is passed both to the constructed
    `TerminalPresenter` and as `run`'s own keyword-only `unattended` argument.
    After `run` returns, whenever `--unattended` was given, this writes a single
    ASCII BEL byte (`\\a`) to stdout -- once, regardless of the exit code, so a
    user who stepped away is notified of *any* ending (success, the round-cap
    abort, an ordinary refusal, any other failure) without watching the screen.
    """
    parsed = parse_args(argv)
    presenter = TerminalPresenter(
        sys.stdin, sys.stdout, sys.stderr, unattended=parsed.unattended
    )
    exit_code = run(parsed.mode, Path.cwd(), presenter, unattended=parsed.unattended)
    if parsed.unattended:
        # Void, like every other terminal write in this module once the run's
        # outcome is already decided: a dead stdout here must not turn a
        # completed run into an unhandled traceback.
        with contextlib.suppress(BrokenPipeError, OSError):
            sys.stdout.write("\a")
            sys.stdout.flush()
    return exit_code


class TerminalPresenter:
    """Renders the orchestrator's reports and forwards what the user types.

    Implements `orchestrator.Presenter` in full.
    """

    def __init__(
        self, stdin: TextIO, stdout: TextIO, stderr: TextIO, *, unattended: bool = False
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._unattended = unattended

    # --- Checkpoint (T2.3) ----------------------------------------------------------

    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
        if self._unattended:
            # R26: the view still renders in full (unattended output is meant to
            # be reviewed later, e.g. redirected to a file), but the
            # reserved-word prompt line is never printed and stdin is never
            # read -- an immediate Approve(), the same reply an interactive
            # user's own typed "approve" would produce. A stream failure while
            # rendering is not mapped to Abort here (unlike the interactive
            # branch below): unattended never returns Abort from a reply-pending
            # method (cli.md's Error handling).
            self._write_lines(self._stdout, self._checkpoint_lines(view))
            return orchestrator.Approve()
        if not self._write_lines(self._stdout, self._checkpoint_lines(view)):
            # cli.md: a reply-pending method hit by a broken stream returns Abort --
            # the run cannot continue without a user, and R20 guarantees nothing is
            # written before final confirmation.
            return orchestrator.Abort()
        reply = self._prompt_and_read(_CHECKPOINT_PROMPT, rejectable=False)
        if reply is None:
            return orchestrator.Abort()
        assert not isinstance(reply, orchestrator.Reject), (
            "reject is never reserved at a plain checkpoint prompt"
        )
        return reply

    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply:
        if self._unattended:
            self._write_lines(self._stdout, self._amendment_lines(view))
            return orchestrator.Approve()
        if not self._write_lines(self._stdout, self._amendment_lines(view)):
            return orchestrator.Abort()
        prompt_text = _REJECTABLE_AMENDMENT_PROMPT if rejectable else _CHECKPOINT_PROMPT
        reply = self._prompt_and_read(prompt_text, rejectable)
        if reply is None:
            return orchestrator.Abort()
        return reply

    def present_no_impact(self, view: NoImpactView) -> CheckpointReply:
        if self._unattended:
            self._write_lines(self._stdout, self._no_impact_lines(view))
            return orchestrator.Approve()
        if not self._write_lines(self._stdout, self._no_impact_lines(view)):
            return orchestrator.Abort()
        reply = self._prompt_and_read(_CHECKPOINT_PROMPT, rejectable=False)
        if reply is None:
            return orchestrator.Abort()
        assert not isinstance(reply, orchestrator.Reject), (
            "reject is never reserved at the no-impact checkpoint prompt"
        )
        return reply

    def show_chat_reply(
        self, text: str, prompt: PromptKind | None
    ) -> AmendmentReply | None:
        if prompt is None:
            # Void per cli.md: a broken stream here is swallowed, None returned, and
            # the run proceeds -- the next reply-pending call converts a dead stream
            # to Abort instead.
            self._write_text(self._stdout, text)
            return None
        if not self._write_text(self._stdout, text):
            return orchestrator.Abort()
        prompt_text, rejectable = _PROMPT_BY_KIND[prompt]
        reply = self._prompt_and_read(prompt_text, rejectable)
        if reply is None:
            return orchestrator.Abort()
        return reply

    def progress(self, label: str, elapsed_seconds: float, last_activity: str | None) -> None:
        """R25: one status line while an agent-driving call is in flight, e.g.
        `· phase 3 — metric coverage (12s, propose_edits)` or `(12s, waiting)`
        before any tool call has arrived. Void like `notice`: swallows a stream
        failure and never raises -- the orchestrator's ticker calls this off the
        thread draining the turn, purely to inform, and no reply was ever
        expected here (cli.md's Error handling: "progress" is in the void class)."""
        activity = last_activity if last_activity is not None else _WAITING_ACTIVITY
        self._write(
            self._stdout,
            f"{_PROGRESS_PREFIX}{label} ({int(elapsed_seconds)}s, {activity})",
        )

    # --- Non-view methods -------------------------------------------------------------

    def notice(self, text: str) -> None:
        self._write(self._stdout, text)

    def error(self, cause: str, next_action: str, detail: str | None = None) -> None:
        self._write(self._stderr, cause)
        self._write(self._stderr, f"{_RESULT_PREFIX}{next_action}")
        if detail is not None:
            self._write(self._stderr, detail)

    def summary(self, s: RunSummary) -> None:
        self._write(self._stdout, f"{_RESULT_PREFIX}{s.outcome}")
        if s.entry_counts is not None:
            counts_text = (
                f"{s.entry_counts.added} added · {s.entry_counts.updated} updated · "
                f"{s.entry_counts.removed} removed"
            )
            label = "discarded: " if s.discarded else ""
            self._write(self._stdout, f"{_RESULT_PREFIX}{label}{counts_text}")
        if s.gap_counts is not None:
            self._write(
                self._stdout,
                f"{_RESULT_PREFIX}{s.gap_counts.alertable} alertable · "
                f"{s.gap_counts.metric_gap} metric-gap · {s.gap_counts.excluded} excluded",
            )
        if s.transcript_path is not None:
            self._write(self._stdout, f"{_RESULT_PREFIX}transcript: {s.transcript_path}")

    def is_interactive(self) -> bool:
        try:
            return self._stdin.isatty()
        except (ValueError, OSError):
            return False

    # --- Rendering helpers -------------------------------------------------------------

    def _checkpoint_lines(self, view: CheckpointView) -> list[str]:
        lines = [f"phase {int(view.phase)} — {_PHASE_TITLES[view.phase]}", ""]
        for label, changes in (
            ("added", view.added),
            ("updated", view.updated),
            ("removed", view.removed),
        ):
            if not changes:
                continue
            lines.append(f"{label}:")
            for change in changes:
                lines.append(f"  {change.id} ({change.entry_type})")
                for name, value in change.fields:
                    lines.append(f"    {self._render_field(name, value)}")
            lines.append("")
        lines.append(self._gap_line(view.gap_counts))
        return lines

    def _amendment_lines(self, view: AmendmentView) -> list[str]:
        lines = [_AMENDMENT_ORIGIN_LINES[view.origin], ""]
        for section in view.sections:
            lines.append(f"phase {int(section.phase)} — {_PHASE_TITLES[section.phase]}")
            for label, changes in (
                ("added", section.added),
                ("updated", section.updated),
                ("removed", section.removed),
            ):
                if not changes:
                    continue
                lines.append(f"{label}:")
                for change in changes:
                    lines.append(f"  {change.id} ({change.entry_type})")
                    for name, value in change.fields:
                        lines.append(f"    {self._render_field(name, value)}")
            lines.append("")
        lines.append(self._gap_line(view.gap_counts))
        return lines

    def _no_impact_lines(self, view: NoImpactView) -> list[str]:
        lines = ["no changes needed", "", f"{view.delta_file_count} file(s) changed:"]
        for path in view.delta_files:
            lines.append(f"  {path}")
        lines.append("")
        lines.append(view.conclusion)
        return lines

    def _render_field(self, name: str, value: str) -> str:
        if name == "severity" and value in _SEVERITY_WORDS:
            return f"{name}: {self._colorize(value)}"
        return f"{name}: {value}"

    def _gap_line(self, gaps: _GapCounts) -> str:
        return (
            f"{gaps.alertable} alertable · {gaps.metric_gap} metric-gap · "
            f"{gaps.excluded} excluded"
        )

    def _colorize(self, word: str) -> str:
        if not self._use_color():
            return word
        color = _ANSI_ALERT if word == "critical" else _ANSI_WARN
        return f"{color}{word}{_ANSI_RESET}"

    def _use_color(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        try:
            return self._stdout.isatty()
        except (ValueError, OSError):
            return False

    # --- Stream I/O --------------------------------------------------------------------

    def _write(self, stream: TextIO, text: str) -> None:
        # Void methods swallow stream failures rather than raise (cli.md's
        # error-handling rule): the run's outcome is already decided, and a
        # render crash on a dead stream must not corrupt it.
        try:
            stream.write(text + "\n")
            stream.flush()
        except (BrokenPipeError, OSError):
            pass

    def _write_lines(self, stream: TextIO, lines: list[str]) -> bool:
        """Like `_write`, but for a reply-pending render: reports success/failure
        instead of swallowing it, so the caller can map a broken stream to `Abort`."""
        try:
            for line in lines:
                stream.write(line + "\n")
            stream.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def _write_text(self, stream: TextIO, text: str) -> bool:
        try:
            stream.write(text + "\n")
            stream.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def _prompt_and_read(self, prompt_text: str, rejectable: bool) -> AmendmentReply | None:
        """Print `prompt_text`, read one line, and map it per D9's reserved-word
        convention -- `None` signals a broken stream (write or read), which every
        caller maps to `Abort`. Loops past a bare-newline empty line (cli.md:
        "empty line re-prompts"), re-printing the prompt each time."""
        while True:
            if not self._write_text(self._stdout, prompt_text):
                return None
            try:
                line = self._stdin.readline()
            except (EOFError, OSError, KeyboardInterrupt):
                return orchestrator.Abort()
            if line == "":
                return orchestrator.Abort()
            text = line.rstrip("\n")
            if text == "":
                continue
            if text == "approve":
                return orchestrator.Approve()
            if text == "abort":
                return orchestrator.Abort()
            if rejectable and text == "reject":
                return orchestrator.Reject()
            return orchestrator.Chat(text)
