"""Unit tests for blare.cli: T1.1's parse_args/main wiring and error/summary/notice
rendering, T2.3's checkpoint screen and the chat loop (`show_chat_reply`, kind-aware
for every `PromptKind`), and the full `RunSummary` rendering (entry counts, gap
counts); T2.4's `present_amendment` screen; T3.1's `present_no_impact` screen."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from blare.artifacts import GapSummary
from blare.cli import ParsedCommand, TerminalPresenter, main, parse_args
from blare.model import Phase, RunMode
from blare.orchestrator import (
    Abort,
    AmendmentOrigin,
    AmendmentPhaseSection,
    AmendmentView,
    Approve,
    Chat,
    CheckpointView,
    EntryChange,
    EntryCounts,
    NoImpactView,
    Presenter,
    PromptKind,
    Reject,
    RunSummary,
)


def test_contract_parse_args_analyze() -> None:
    """`analyze` parses to RunMode.ANALYZE."""
    assert parse_args(["analyze"]) == ParsedCommand(mode=RunMode.ANALYZE)


def test_contract_parse_args_update() -> None:
    """`update` parses to RunMode.UPDATE."""
    assert parse_args(["update"]) == ParsedCommand(mode=RunMode.UPDATE)


def test_contract_parse_args_unknown_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """An unrecognized subcommand exits 2 with usage text on stderr (argparse's own
    convention)."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["bogus"])

    assert exc_info.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_contract_parse_args_no_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """No subcommand at all exits 2 with usage text (a required subcommand is missing)."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args([])

    assert exc_info.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_contract_parse_args_version_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """--version prints the version and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--version"])

    assert exc_info.value.code == 0
    assert "blare" in capsys.readouterr().out


def test_contract_parse_args_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """--help prints usage and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])

    assert exc_info.value.code == 0
    assert "usage" in capsys.readouterr().out


def test_contract_parse_args_unattended_parses_on_both_subcommands_default_false() -> None:
    """`--unattended` (R26) parses on both `analyze` and `update`, defaulting
    false when omitted."""
    assert parse_args(["analyze"]) == ParsedCommand(mode=RunMode.ANALYZE, unattended=False)
    assert parse_args(["update"]) == ParsedCommand(mode=RunMode.UPDATE, unattended=False)
    assert parse_args(["analyze", "--unattended"]) == ParsedCommand(
        mode=RunMode.ANALYZE, unattended=True
    )
    assert parse_args(["update", "--unattended"]) == ParsedCommand(
        mode=RunMode.UPDATE, unattended=True
    )


def test_contract_main_wiring_passes_mode_cwd_and_a_terminal_presenter() -> None:
    """main() passes parse_args's mode, the invocation cwd, and a TerminalPresenter
    to the injected `run` callable."""
    received: dict[str, object] = {}

    def _recording_run(
        mode: RunMode, repo_path: Path, presenter: Presenter, *, unattended: bool = False
    ) -> int:
        received["mode"] = mode
        received["repo_path"] = repo_path
        received["presenter"] = presenter
        received["unattended"] = unattended
        return 0

    code = main(["analyze"], run=_recording_run)

    assert code == 0
    assert received["mode"] == RunMode.ANALYZE
    assert received["repo_path"] == Path.cwd()
    assert isinstance(received["presenter"], TerminalPresenter)
    assert received["unattended"] is False


def test_contract_main_wiring_passes_unattended_through_to_run_and_presenter() -> None:
    """main() passes `parsed.unattended` as `run`'s keyword-only `unattended`
    argument and constructs the `TerminalPresenter` with `unattended=True` too
    (R26) -- confirmed by behavior (the presenter never reads the real stdin
    it was constructed over), not a private attribute."""
    received: dict[str, object] = {}

    def _recording_run(
        mode: RunMode, repo_path: Path, presenter: Presenter, *, unattended: bool = False
    ) -> int:
        received["unattended"] = unattended
        received["presenter"] = presenter
        return 0

    code = main(["analyze", "--unattended"], run=_recording_run)

    assert code == 0
    assert received["unattended"] is True
    presenter = received["presenter"]
    assert isinstance(presenter, TerminalPresenter)
    reply = presenter.present_checkpoint(_fixed_view())
    assert reply == Approve()


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_contract_main_writes_bell_once_after_run_returns_when_unattended(
    exit_code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() writes exactly one ASCII BEL to stdout after `run()` returns
    whenever `parsed.unattended`, regardless of the exit code (R26) -- asserted
    across a success (0), an ordinary preflight refusal (1), the round-cap
    failure and any other run failure (2), and a user abort (3), one ending
    kind per parametrized case."""

    def _recording_run(
        mode: RunMode, repo_path: Path, presenter: Presenter, *, unattended: bool = False
    ) -> int:
        return exit_code

    code = main(["analyze", "--unattended"], run=_recording_run)

    assert code == exit_code
    assert capsys.readouterr().out == "\a"


def test_contract_main_writes_no_bell_when_unattended_never_passed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() writes no bell at all when `--unattended` was never passed."""

    def _recording_run(
        mode: RunMode, repo_path: Path, presenter: Presenter, *, unattended: bool = False
    ) -> int:
        return 0

    code = main(["analyze"], run=_recording_run)

    assert code == 0
    assert capsys.readouterr().out == ""


def test_contract_error_renders_cause_then_next_action_on_stderr() -> None:
    """error() renders the cause line, then "-> " + next_action, on stderr."""
    stdout, stderr = io.StringIO(), io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, stderr)

    presenter.error(cause="it broke", next_action="fix it")

    assert stderr.getvalue() == "it broke\n→ fix it\n"
    assert stdout.getvalue() == ""


def test_contract_error_renders_detail_beneath_cause() -> None:
    """error()'s optional detail renders beneath the cause and next action, on stderr."""
    stderr = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), io.StringIO(), stderr)

    presenter.error(cause="it broke", next_action="fix it", detail="traceback here")

    assert stderr.getvalue() == "it broke\n→ fix it\ntraceback here\n"


def test_contract_summary_renders_outcome_with_result_prefix() -> None:
    """summary() renders "-> outcome" on stdout; no transcript line when path is None."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.summary(RunSummary(outcome="no changes"))

    assert stdout.getvalue() == "→ no changes\n"


def test_contract_summary_renders_transcript_path_when_present() -> None:
    """summary() adds a transcript line when RunSummary carries a transcript_path."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.summary(RunSummary(outcome="no changes", transcript_path=Path("/tmp/t.jsonl")))

    assert stdout.getvalue() == "→ no changes\n→ transcript: /tmp/t.jsonl\n"


def test_contract_notice_renders_plain_line_without_prefix() -> None:
    """notice() renders one plain line on stdout, without the "-> " result prefix."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.notice("a stale lock was reclaimed")

    assert stdout.getvalue() == "a stale lock was reclaimed\n"


def test_contract_progress_renders_dot_prefix_label_elapsed_and_activity() -> None:
    """progress() (R25) renders "· label (Ns, activity)" on a TTY stream, byte-exact
    for a fixed set of arguments (single call): `\\r` + clear-to-end-of-line +
    content, no trailing newline (T4.6's in-place-update contract) -- distinct from
    both "→ " (results) and "$ " (prompts)."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("phase 3 — metric coverage", 12.0, "propose_edits")

    assert stdout.getvalue() == "\r\x1b[K· phase 3 — metric coverage (12s, propose_edits)"


def test_contract_progress_renders_waiting_when_last_activity_is_none() -> None:
    """progress() renders "waiting" in place of last_activity when it is None --
    no tool call has arrived yet (cli.md's Rendering rules) -- same TTY in-place
    byte-exact contract as above."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("triage", 3.0, None)

    assert stdout.getvalue() == "\r\x1b[K· triage (3s, waiting)"


def test_contract_is_interactive_reflects_stdin_isatty() -> None:
    """is_interactive() is stdin.isatty() (R22's criterion)."""

    class _NonTTYStream(io.StringIO):
        def isatty(self) -> bool:
            return False

    presenter = TerminalPresenter(_NonTTYStream(), io.StringIO(), io.StringIO())

    assert presenter.is_interactive() is False


class _BrokenPipeStream(io.StringIO):
    """A stream whose write always raises BrokenPipeError."""

    def write(self, s: str) -> int:
        raise BrokenPipeError


class _TTYStream(io.StringIO):
    """An in-memory stream that reports itself as a TTY -- T4.6's in-place
    progress redraw and severity coloring both branch on `isatty()`."""

    def isatty(self) -> bool:
        return True


class _BrokenPipeTTYStream(io.StringIO):
    """A TTY stream whose write always raises BrokenPipeError -- isolates a
    write failure on the path that actually attempts a write (T4.6: `progress`
    only writes immediately on a TTY; a non-TTY stream never attempts a write
    from a single call, so it can never observe this failure)."""

    def isatty(self) -> bool:
        return True

    def write(self, s: str) -> int:
        raise BrokenPipeError


class _NthWriteFailsStream(io.StringIO):
    """A TTY stream whose Nth `write()` call (1-indexed) raises BrokenPipeError;
    every other call behaves normally. Isolates a single write in a sequence --
    e.g. `_finalize_progress_line`'s own write -- so a test can assert its
    failure is swallowed without the surrounding writes also failing."""

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self._count = 0
        self._fail_at = fail_at

    def isatty(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._count += 1
        if self._count == self._fail_at:
            raise BrokenPipeError
        return super().write(s)


def test_failure_stdout_broken_pipe_in_summary_is_swallowed() -> None:
    """A BrokenPipeError inside summary() (a void method) is swallowed, no traceback."""
    presenter = TerminalPresenter(io.StringIO(), _BrokenPipeStream(), io.StringIO())

    presenter.summary(RunSummary(outcome="no changes"))  # must not raise


def test_failure_stderr_broken_pipe_in_error_is_swallowed() -> None:
    """A BrokenPipeError inside error() (a void method) is swallowed, no traceback."""
    presenter = TerminalPresenter(io.StringIO(), io.StringIO(), _BrokenPipeStream())

    presenter.error(cause="it broke", next_action="fix it")  # must not raise


def test_failure_stdout_broken_pipe_in_notice_is_swallowed() -> None:
    """A BrokenPipeError inside notice() (a void method) is swallowed, no traceback."""
    presenter = TerminalPresenter(io.StringIO(), _BrokenPipeStream(), io.StringIO())

    presenter.notice("a notice")  # must not raise


def test_failure_stdout_broken_pipe_in_progress_is_swallowed() -> None:
    """A BrokenPipeError inside progress() (R25, a void method like notice) is
    swallowed, no traceback, no effect on the run (cli.md's Error handling: the
    same void-class rule as notice). Uses a TTY stream because T4.6's progress()
    only attempts a write immediately on a TTY -- a non-TTY stream never writes
    from a single call, so it could never exercise this failure."""
    presenter = TerminalPresenter(io.StringIO(), _BrokenPipeTTYStream(), io.StringIO())

    presenter.progress("phase 1 — system map", 5.0, "Read")  # must not raise


# --- T2.3: checkpoint screen, chat loop, full summary content -----------------------


class _RaisingStream(io.StringIO):
    """A stream whose `readline` always raises the given exception -- for EOF,
    OSError, and KeyboardInterrupt distinctions at a reply-pending prompt."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    def readline(self, size: int = -1, /) -> str:  # type: ignore[override]
        raise self._exc


def _fixed_view() -> CheckpointView:
    """One fixed `CheckpointView` for byte-exact rendering assertions: one added
    system component, one updated failure mode (with a colorable severity field),
    one removed metric, and a gap summary."""
    return CheckpointView(
        phase=Phase.SYSTEM_MAP,
        gap_counts=GapSummary(alertable=2, metric_gap=1, excluded=0),
        added=(
            EntryChange(
                entry_type="system_components",
                id="sm-web",
                fields=(
                    ("name", "web"),
                    ("kind", "service"),
                    ("description", "the web frontend"),
                    ("depends_on", "(none)"),
                ),
            ),
        ),
        updated=(
            EntryChange(
                entry_type="failure_modes",
                id="fm-503",
                fields=(("title", "web returns 503"), ("severity", "critical")),
            ),
        ),
        removed=(
            EntryChange(
                entry_type="metrics",
                id="mx-old",
                fields=(("name", "old_metric_total"),),
            ),
        ),
    )


_FIXED_VIEW_LINES = (
    "phase 1 — system map",
    "",
    "added:",
    "  sm-web (system_components)",
    "    name: web",
    "    kind: service",
    "    description: the web frontend",
    "    depends_on: (none)",
    "",
    "updated:",
    "  fm-503 (failure_modes)",
    "    title: web returns 503",
    "    severity: critical",
    "",
    "removed:",
    "  mx-old (metrics)",
    "    name: old_metric_total",
    "",
    "2 alertable · 1 metric-gap · 0 excluded",
    "$ approve · abort · anything else is chat",
)


def test_contract_checkpoint_screen_renders_byte_exact() -> None:
    """Checkpoint screen: header, entry sections with content, gap summary, and the
    verb-naming prompt, asserted byte-exact for a fixed view (cli.md)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())

    reply = presenter.present_checkpoint(_fixed_view())

    assert reply == Approve()
    expected = "".join(line + "\n" for line in _FIXED_VIEW_LINES)
    assert stdout.getvalue() == expected


def test_contract_unattended_present_checkpoint_renders_view_skips_prompt_never_reads_stdin() -> (
    None
):
    """R26: `TerminalPresenter(unattended=True).present_checkpoint` renders the
    view content in full (byte-exact, matching the interactive rendering minus
    the reserved-word prompt line) but never prints that prompt and never reads
    stdin at all -- a stdin double that raises on any read confirms it is never
    touched -- returning `Approve()` immediately."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(
        _RaisingStream(AssertionError("stdin must never be read when unattended")),
        stdout,
        io.StringIO(),
        unattended=True,
    )

    reply = presenter.present_checkpoint(_fixed_view())

    assert reply == Approve()
    expected = "".join(line + "\n" for line in _FIXED_VIEW_LINES[:-1])  # no prompt line
    assert stdout.getvalue() == expected


def test_contract_checkpoint_reply_mapping() -> None:
    """Exact `approve`/`abort` act; anything else, including near-misses, is chat;
    empty line re-prompts; EOF is Abort (D9's exact-match contract)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    assert presenter.present_checkpoint(_fixed_view()) == Approve()

    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("abort\n"), stdout, io.StringIO())
    assert presenter.present_checkpoint(_fixed_view()) == Abort()

    stdout = io.StringIO()
    presenter = TerminalPresenter(
        io.StringIO("approve the second one\n"), stdout, io.StringIO()
    )
    assert presenter.present_checkpoint(_fixed_view()) == Chat("approve the second one")

    for near_miss in ("Approve", " approve "):
        stdout = io.StringIO()
        presenter = TerminalPresenter(io.StringIO(f"{near_miss}\n"), stdout, io.StringIO())
        assert presenter.present_checkpoint(_fixed_view()) == Chat(near_miss)

    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("\napprove\n"), stdout, io.StringIO())
    assert presenter.present_checkpoint(_fixed_view()) == Approve()

    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(""), stdout, io.StringIO())
    assert presenter.present_checkpoint(_fixed_view()) == Abort()


def test_contract_checkpoint_ctrl_c_is_abort_distinct_from_eof() -> None:
    """Ctrl-C (KeyboardInterrupt at the prompt) is Abort, via a distinct code path
    from EOF (both map to the same reply, but the failure-mode test plan calls out
    that they are handled by separate branches)."""
    presenter = TerminalPresenter(
        _RaisingStream(KeyboardInterrupt()), io.StringIO(), io.StringIO()
    )
    assert presenter.present_checkpoint(_fixed_view()) == Abort()


def test_failure_checkpoint_stdin_eof_mid_chat_is_abort() -> None:
    """stdin closing (EOF) mid-prompt returns Abort, no exception."""
    presenter = TerminalPresenter(_RaisingStream(EOFError()), io.StringIO(), io.StringIO())
    assert presenter.present_checkpoint(_fixed_view()) == Abort()


def test_failure_checkpoint_stdin_oserror_is_abort() -> None:
    """stdin raising OSError (EIO, terminal gone) mid-prompt returns Abort, no
    traceback."""
    presenter = TerminalPresenter(
        _RaisingStream(OSError("EIO")), io.StringIO(), io.StringIO()
    )
    assert presenter.present_checkpoint(_fixed_view()) == Abort()


def test_failure_checkpoint_stdout_broken_pipe_is_abort() -> None:
    """A BrokenPipeError rendering the checkpoint view (a reply-pending method)
    returns Abort rather than raising."""
    presenter = TerminalPresenter(io.StringIO("approve\n"), _BrokenPipeStream(), io.StringIO())
    assert presenter.present_checkpoint(_fixed_view()) == Abort()


def test_contract_show_chat_reply_prompt_none_renders_and_returns_none() -> None:
    """`show_chat_reply(prompt=None)` renders the reply, offers no prompt, and
    returns `None` -- the R18-redirect contract path."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    result = presenter.show_chat_reply("the no-impact conclusion was withdrawn", None)

    assert result is None
    assert stdout.getvalue() == "the no-impact conclusion was withdrawn\n"


def test_contract_show_chat_reply_renders_inline_and_reoffers_prompt() -> None:
    """A `Chat` reply followed by `show_chat_reply` renders the response inline
    (without redrawing the view) and re-offers the checkpoint prompt, returning the
    next reply."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())

    reply = presenter.show_chat_reply("noted, proceeding", PromptKind.CHECKPOINT)

    assert reply == Approve()
    assert stdout.getvalue() == (
        "noted, proceeding\n$ approve · abort · anything else is chat\n"
    )


def test_contract_show_chat_reply_kind_dependent_reject_mapping() -> None:
    """`reject` typed at a rejectable-amendment continuation returns `Reject`; at
    every other kind (checkpoint, no-impact, plain amendment) it is ordinary chat --
    the plain-amendment case backs the no-rejection rule for system-originated
    units. `approve`/`abort` map at every kind."""
    for kind in (
        PromptKind.CHECKPOINT,
        PromptKind.NO_IMPACT,
        PromptKind.AMENDMENT,
        PromptKind.REJECTABLE_AMENDMENT,
    ):
        presenter = TerminalPresenter(io.StringIO("approve\n"), io.StringIO(), io.StringIO())
        assert presenter.show_chat_reply("x", kind) == Approve()

        presenter = TerminalPresenter(io.StringIO("abort\n"), io.StringIO(), io.StringIO())
        assert presenter.show_chat_reply("x", kind) == Abort()

    presenter = TerminalPresenter(io.StringIO("reject\n"), io.StringIO(), io.StringIO())
    assert presenter.show_chat_reply("x", PromptKind.REJECTABLE_AMENDMENT) == Reject()

    for non_rejectable_kind in (
        PromptKind.CHECKPOINT,
        PromptKind.NO_IMPACT,
        PromptKind.AMENDMENT,
    ):
        presenter = TerminalPresenter(io.StringIO("reject\n"), io.StringIO(), io.StringIO())
        assert presenter.show_chat_reply("x", non_rejectable_kind) == Chat("reject")


def test_contract_rejectable_amendment_prompt_names_reject() -> None:
    """The rejectable-amendment continuation's prompt names `reject`; the plain
    (non-rejectable) amendment continuation's prompt does not."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.show_chat_reply("x", PromptKind.REJECTABLE_AMENDMENT)
    assert "reject" in stdout.getvalue()

    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.show_chat_reply("x", PromptKind.AMENDMENT)
    assert "reject" not in stdout.getvalue()


def _fixed_amendment_view() -> AmendmentView:
    """One fixed `AmendmentView` for byte-exact rendering: a two-phase unit (system
    map, alert recommendations), each with content, plus the gap summary."""
    return AmendmentView(
        origin=AmendmentOrigin.AGENT,
        sections=(
            AmendmentPhaseSection(
                phase=Phase.SYSTEM_MAP,
                updated=(
                    EntryChange(
                        entry_type="system_components",
                        id="sm-web",
                        fields=(("name", "web"), ("description", "revised")),
                    ),
                ),
            ),
            AmendmentPhaseSection(
                phase=Phase.ALERT_RECOMMENDATIONS,
                added=(
                    EntryChange(
                        entry_type="alert_recommendations",
                        id="ar-503",
                        fields=(("severity", "critical"),),
                    ),
                ),
            ),
        ),
        gap_counts=GapSummary(alertable=1, metric_gap=0, excluded=0),
    )


_FIXED_AMENDMENT_LINES = (
    "amendment · proposed by agent",
    "",
    "phase 1 — system map",
    "updated:",
    "  sm-web (system_components)",
    "    name: web",
    "    description: revised",
    "",
    "phase 4 — alert recommendations",
    "added:",
    "  ar-503 (alert_recommendations)",
    "    severity: critical",
    "",
    "1 alertable · 0 metric-gap · 0 excluded",
    "$ approve · reject · abort · anything else is chat",
)


def test_contract_amendment_screen_renders_byte_exact() -> None:
    """Amendment screen: origin line, one section per involved phase, prompt --
    asserted for a fixed two-phase unit (cli.md)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())

    reply = presenter.present_amendment(_fixed_amendment_view(), rejectable=True)

    assert reply == Approve()
    expected = "".join(line + "\n" for line in _FIXED_AMENDMENT_LINES)
    assert stdout.getvalue() == expected


def test_contract_unattended_present_amendment_renders_view_skips_prompt_never_reads_stdin() -> (
    None
):
    """R26: `TerminalPresenter(unattended=True).present_amendment` renders the
    view content in full but skips the prompt line (rejectable or not) and
    never reads stdin, returning `Approve()` immediately."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(
        _RaisingStream(AssertionError("stdin must never be read when unattended")),
        stdout,
        io.StringIO(),
        unattended=True,
    )

    reply = presenter.present_amendment(_fixed_amendment_view(), rejectable=True)

    assert reply == Approve()
    expected = "".join(line + "\n" for line in _FIXED_AMENDMENT_LINES[:-1])  # no prompt line
    assert stdout.getvalue() == expected


def test_contract_amendment_replies_rejectable_true_reject_maps() -> None:
    """`rejectable=True`: exact `reject` returns `Reject`, and the prompt names it."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("reject\n"), stdout, io.StringIO())

    reply = presenter.present_amendment(_fixed_amendment_view(), rejectable=True)

    assert reply == Reject()
    assert "reject" in stdout.getvalue()


def test_contract_amendment_replies_rejectable_false_reject_is_chat() -> None:
    """`rejectable=False`: the prompt offers no reject wording, and `reject` is
    ordinary chat (the no-rejection rule for system-originated units)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("reject\n"), stdout, io.StringIO())

    reply = presenter.present_amendment(_fixed_amendment_view(), rejectable=False)

    assert reply == Chat("reject")
    assert "reject ·" not in stdout.getvalue()


def test_contract_amendment_replies_approve_and_abort_map_either_way() -> None:
    """`approve`/`abort` map to Approve/Abort at both rejectable and non-rejectable
    amendment prompts."""
    for rejectable in (True, False):
        presenter = TerminalPresenter(io.StringIO("approve\n"), io.StringIO(), io.StringIO())
        assert presenter.present_amendment(_fixed_amendment_view(), rejectable) == Approve()

        presenter = TerminalPresenter(io.StringIO("abort\n"), io.StringIO(), io.StringIO())
        assert presenter.present_amendment(_fixed_amendment_view(), rejectable) == Abort()


def test_failure_present_amendment_stdout_broken_pipe_is_abort() -> None:
    """A BrokenPipeError rendering the amendment view returns Abort rather than
    raising (the same reply-pending stream-failure rule as present_checkpoint)."""
    presenter = TerminalPresenter(io.StringIO("approve\n"), _BrokenPipeStream(), io.StringIO())
    assert presenter.present_amendment(_fixed_amendment_view(), rejectable=True) == Abort()


def test_failure_present_amendment_stdin_eof_is_abort() -> None:
    """stdin EOF at the amendment prompt returns Abort, no exception."""
    presenter = TerminalPresenter(_RaisingStream(EOFError()), io.StringIO(), io.StringIO())
    assert presenter.present_amendment(_fixed_amendment_view(), rejectable=True) == Abort()


def test_failure_show_chat_reply_broken_pipe_prompting_is_abort() -> None:
    """A BrokenPipeError while rendering a prompting `show_chat_reply` call returns
    Abort, no traceback."""
    presenter = TerminalPresenter(
        io.StringIO("approve\n"), _BrokenPipeStream(), io.StringIO()
    )
    assert presenter.show_chat_reply("x", PromptKind.CHECKPOINT) == Abort()


def test_failure_show_chat_reply_prompt_none_broken_pipe_is_swallowed() -> None:
    """A BrokenPipeError rendering a `prompt=None` `show_chat_reply` call (void) is
    swallowed, returns `None`, no traceback (the void-class rule)."""
    presenter = TerminalPresenter(io.StringIO(), _BrokenPipeStream(), io.StringIO())
    assert presenter.show_chat_reply("x", None) is None


def test_contract_severity_colored_on_tty_bare_with_no_color_and_on_non_tty(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Severity words are colored on a TTY stdout, bare with `NO_COLOR` set, and bare
    on a non-TTY stdout -- byte-exact assertions (cli.md, brand §6: "color only for
    the severity word")."""
    monkeypatch.delenv("NO_COLOR", raising=False)

    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.present_checkpoint(_fixed_view())
    rendered = stdout.getvalue()
    assert "\x1b[38;2;255;90;31mcritical\x1b[0m" in rendered
    assert "    severity: critical\n" not in rendered  # the bare form is not present

    monkeypatch.setenv("NO_COLOR", "1")
    stdout_no_color = _TTYStream()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout_no_color, io.StringIO())
    presenter.present_checkpoint(_fixed_view())
    assert "    severity: critical\n" in stdout_no_color.getvalue()
    assert "\x1b[" not in stdout_no_color.getvalue()
    monkeypatch.delenv("NO_COLOR", raising=False)

    stdout_non_tty = io.StringIO()  # plain StringIO: isatty() is False
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout_non_tty, io.StringIO())
    presenter.present_checkpoint(_fixed_view())
    assert "    severity: critical\n" in stdout_non_tty.getvalue()
    assert "\x1b[" not in stdout_non_tty.getvalue()


def test_contract_summary_full_content_on_success() -> None:
    """`summary` renders outcome, the entry-count split, the gap-count split, and
    the transcript path, in that order, for a session-bearing success (R13)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.summary(
        RunSummary(
            outcome="analysis complete",
            transcript_path=Path("/tmp/t.jsonl"),
            gap_counts=GapSummary(alertable=2, metric_gap=1, excluded=1),
            entry_counts=EntryCounts(added=3, updated=1, removed=0),
        )
    )

    assert stdout.getvalue() == (
        "→ analysis complete\n"
        "→ 3 added · 1 updated · 0 removed\n"
        "→ 2 alertable · 1 metric-gap · 1 excluded\n"
        "→ transcript: /tmp/t.jsonl\n"
    )


def test_contract_summary_discarded_label_at_abort_and_failure() -> None:
    """At a non-writing ending, the entry-count split carries the "discarded" label
    and never reads as applied (cli.md)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.summary(
        RunSummary(
            outcome="aborted",
            transcript_path=Path("/tmp/t.jsonl"),
            gap_counts=GapSummary(alertable=0, metric_gap=0, excluded=0),
            entry_counts=EntryCounts(added=2, updated=0, removed=0),
            discarded=True,
        )
    )

    assert "→ discarded: 2 added · 0 updated · 0 removed\n" in stdout.getvalue()
    assert "→ 2 added" not in stdout.getvalue()


def test_contract_summary_sessionless_r7_style_has_no_entry_counts_or_transcript() -> None:
    """The sessionless R7-style summary (entry_counts=None) renders "no changes"
    with gap counts and no transcript line (cli.md)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.summary(
        RunSummary(
            outcome="up to date",
            gap_counts=GapSummary(alertable=1, metric_gap=0, excluded=0),
        )
    )

    assert stdout.getvalue() == "→ up to date\n→ 1 alertable · 0 metric-gap · 0 excluded\n"


def test_failure_show_chat_reply_kind_none_render_swallows_os_error() -> None:
    """An `OSError` writing a `prompt=None` render is swallowed (the void-class
    rule), same trigger class as the broken-pipe variant."""

    class _OSErrorStream(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError("disk full")

    presenter = TerminalPresenter(io.StringIO(), _OSErrorStream(), io.StringIO())
    assert presenter.show_chat_reply("x", None) is None


# ==== present_no_impact (T3.1) ======================================================


def _fixed_no_impact_view() -> NoImpactView:
    """One fixed `NoImpactView` for byte-exact rendering assertions."""
    return NoImpactView(
        delta_file_count=2,
        delta_files=("src/web.py", "src/worker.py"),
        conclusion="Both changes are comments-only; no artifact updates needed.",
    )


_FIXED_NO_IMPACT_LINES = (
    "no changes needed",
    "",
    "2 file(s) changed:",
    "  src/web.py",
    "  src/worker.py",
    "",
    "Both changes are comments-only; no artifact updates needed.",
    "$ approve · abort · anything else is chat",
)


def test_contract_no_impact_screen_renders_byte_exact() -> None:
    """No-impact screen: header, changed-file summary, conclusion text, checkpoint
    prompt -- asserted byte-exact for a fixed view (cli.md)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())

    reply = presenter.present_no_impact(_fixed_no_impact_view())

    assert reply == Approve()
    expected = "".join(line + "\n" for line in _FIXED_NO_IMPACT_LINES)
    assert stdout.getvalue() == expected


def test_contract_unattended_present_no_impact_renders_view_skips_prompt_never_reads_stdin() -> (
    None
):
    """R26: `TerminalPresenter(unattended=True).present_no_impact` renders the
    view content in full but skips the checkpoint prompt line and never reads
    stdin, returning `Approve()` immediately."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(
        _RaisingStream(AssertionError("stdin must never be read when unattended")),
        stdout,
        io.StringIO(),
        unattended=True,
    )

    reply = presenter.present_no_impact(_fixed_no_impact_view())

    assert reply == Approve()
    expected = "".join(line + "\n" for line in _FIXED_NO_IMPACT_LINES[:-1])  # no prompt line
    assert stdout.getvalue() == expected


def test_contract_no_impact_replies_per_checkpoint_convention() -> None:
    """Replies at the no-impact confirmation follow the same reserved-word
    convention as an ordinary checkpoint (cli.md: "replies per the checkpoint
    convention")."""
    presenter = TerminalPresenter(io.StringIO("abort\n"), io.StringIO(), io.StringIO())
    assert presenter.present_no_impact(_fixed_no_impact_view()) == Abort()

    presenter = TerminalPresenter(
        io.StringIO("what about worker.py?\n"), io.StringIO(), io.StringIO()
    )
    assert presenter.present_no_impact(_fixed_no_impact_view()) == Chat("what about worker.py?")


def test_failure_present_no_impact_stdout_broken_pipe_is_abort() -> None:
    """A `BrokenPipeError` rendering the no-impact view (a reply-pending method)
    returns `Abort` rather than raising."""
    presenter = TerminalPresenter(io.StringIO("approve\n"), _BrokenPipeStream(), io.StringIO())
    assert presenter.present_no_impact(_fixed_no_impact_view()) == Abort()


def test_failure_present_no_impact_stdin_eof_is_abort() -> None:
    """stdin EOF at the no-impact confirmation's prompt returns `Abort`."""
    presenter = TerminalPresenter(_RaisingStream(EOFError()), io.StringIO(), io.StringIO())
    assert presenter.present_no_impact(_fixed_no_impact_view()) == Abort()


# ==== progress consolidation (T4.6) =================================================


def _progress_content(label: str, elapsed_seconds: float, activity: str) -> str:
    """The bare rendered content of a progress line (no `\\r`/clear-to-end-of-line
    prefix, no trailing newline) -- shared by the consolidation tests below."""
    return f"· {label} ({int(elapsed_seconds)}s, {activity})"


def _in_place(content: str) -> str:
    """T4.6's in-place-update bytes for one progress write: `\\r` + clear-to-
    end-of-line + content, no trailing newline."""
    return f"\r\x1b[K{content}"


def test_contract_progress_consolidation_tty_same_key_updates_in_place() -> None:
    """T4.6, TTY stdout: two consecutive calls sharing the same `(label,
    last_activity)` key, `elapsed_seconds > 0` on both -- only elapsed time
    differs -- produce a single in-place update: two raw in-place writes back to
    back, with no finalizing `\\n` between them, asserted byte-exact."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("phase 1 — system map", 1.0, None)
    presenter.progress("phase 1 — system map", 2.0, None)

    expected = _in_place(_progress_content("phase 1 — system map", 1.0, "waiting")) + _in_place(
        _progress_content("phase 1 — system map", 2.0, "waiting")
    )
    assert stdout.getvalue() == expected


def test_contract_progress_consolidation_tty_activity_change_finalizes_then_starts_fresh() -> (
    None
):
    """T4.6, TTY stdout: a third call with a different `last_activity` first
    finalizes the prior line with a bare `\\n` (its already-rendered content
    unchanged), then writes the new key's content the same in-place way."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("phase 1 — system map", 1.0, None)
    presenter.progress("phase 1 — system map", 2.0, None)
    presenter.progress("phase 1 — system map", 3.0, "Read")

    expected = (
        _in_place(_progress_content("phase 1 — system map", 1.0, "waiting"))
        + _in_place(_progress_content("phase 1 — system map", 2.0, "waiting"))
        + "\n"
        + _in_place(_progress_content("phase 1 — system map", 3.0, "Read"))
    )
    assert stdout.getvalue() == expected


def test_contract_progress_consolidation_tty_label_change_finalizes_then_starts_fresh() -> None:
    """T4.6, TTY stdout: a call with a different `label` (a new phase) finalizes
    the prior line and starts fresh identically to an activity change."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("phase 1 — system map", 5.0, "Read")
    presenter.progress("phase 2 — failure modes", 0.0, None)

    expected = (
        _in_place(_progress_content("phase 1 — system map", 5.0, "Read"))
        + "\n"
        + _in_place(_progress_content("phase 2 — failure modes", 0.0, "waiting"))
    )
    assert stdout.getvalue() == expected


def test_contract_progress_consolidation_elapsed_zero_is_always_a_key_change() -> None:
    """T4.6: `elapsed_seconds == 0.0` is *always* a key change, even when
    `(label, last_activity)` is identical to the immediately preceding call --
    the regression test for two consecutive "repair"/None rounds (no tool call
    in either) that must not merge into one line with elapsed time running
    backwards."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("repair", 4.0, None)  # first round's last tick
    presenter.progress("repair", 0.0, None)  # second round's first tick

    expected = (
        _in_place(_progress_content("repair", 4.0, "waiting"))
        + "\n"
        + _in_place(_progress_content("repair", 0.0, "waiting"))
    )
    assert stdout.getvalue() == expected


def test_contract_progress_consolidation_non_tty_same_key_writes_nothing() -> None:
    """T4.6, non-TTY stdout: consecutive same-key calls (`elapsed_seconds > 0`)
    write nothing at all -- control sequences (and a live redraw) in a plain
    file are noise, not signal."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("phase 1 — system map", 1.0, None)
    presenter.progress("phase 1 — system map", 2.0, None)
    presenter.progress("phase 1 — system map", 3.0, None)

    assert stdout.getvalue() == ""


def test_contract_progress_consolidation_non_tty_key_change_commits_one_line_with_last_elapsed() -> (  # noqa: E501
    None
):
    """T4.6, non-TTY stdout: a key change (here, the `elapsed_seconds == 0.0`
    case) commits exactly one plain line (no `\\r` or escape sequences) holding
    the superseded key's *last-seen* elapsed time and activity, not its first."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("repair", 1.0, None)
    presenter.progress("repair", 2.0, None)  # last tick of the first "repair" round
    presenter.progress("repair", 0.0, None)  # second round's first tick -- a key change

    assert stdout.getvalue() == _progress_content("repair", 2.0, "waiting") + "\n"
    assert "\r" not in stdout.getvalue()
    assert "\x1b" not in stdout.getvalue()


def test_contract_progress_consolidation_non_tty_one_key_end_to_end_yields_one_line() -> None:
    """T4.6, non-TTY stdout: a run whose every progress call shares one key end
    to end still produces exactly one committed line, once something finalizes
    it -- here, the next stream-writing method (`notice`)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("triage", 1.0, "Read")
    presenter.progress("triage", 2.0, "Read")
    presenter.progress("triage", 3.0, "Read")
    presenter.notice("done")

    assert stdout.getvalue() == _progress_content("triage", 3.0, "Read") + "\ndone\n"


# --- Each of the seven stream-writing methods finalizes a pending line first --------


def test_contract_present_checkpoint_finalizes_pending_progress_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6: `present_checkpoint` finalizes any pending progress line (TTY: a
    bare `\\n`, since its content is already correctly displayed) before its own
    checkpoint content, never interleaved or missing the boundary."""
    monkeypatch.setenv("NO_COLOR", "1")  # isolates the finalize boundary from
    # severity coloring, which a TTY stdout would otherwise also trigger
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.present_checkpoint(_fixed_view())

    expected_checkpoint = "".join(line + "\n" for line in _FIXED_VIEW_LINES)
    assert stdout.getvalue() == progress_bytes + "\n" + expected_checkpoint


def test_contract_present_amendment_finalizes_pending_progress_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6: `present_amendment` finalizes a pending progress line before its
    own amendment content."""
    monkeypatch.setenv("NO_COLOR", "1")  # isolates the finalize boundary from
    # severity coloring, which a TTY stdout would otherwise also trigger
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.present_amendment(_fixed_amendment_view(), rejectable=True)

    expected_amendment = "".join(line + "\n" for line in _FIXED_AMENDMENT_LINES)
    assert stdout.getvalue() == progress_bytes + "\n" + expected_amendment


def test_contract_present_no_impact_finalizes_pending_progress_line() -> None:
    """T4.6: `present_no_impact` finalizes a pending progress line before its
    own no-impact content."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.present_no_impact(_fixed_no_impact_view())

    expected_no_impact = "".join(line + "\n" for line in _FIXED_NO_IMPACT_LINES)
    assert stdout.getvalue() == progress_bytes + "\n" + expected_no_impact


def test_contract_show_chat_reply_prompt_none_finalizes_pending_progress_line() -> None:
    """T4.6: `show_chat_reply(prompt=None)` finalizes a pending progress line
    before rendering its own reply text."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.show_chat_reply("the no-impact conclusion was withdrawn", None)

    assert stdout.getvalue() == (
        progress_bytes + "\nthe no-impact conclusion was withdrawn\n"
    )


def test_contract_show_chat_reply_prompting_finalizes_pending_progress_line() -> None:
    """T4.6: a prompting `show_chat_reply` call finalizes a pending progress
    line before rendering its own reply text and re-offered prompt."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.show_chat_reply("noted, proceeding", PromptKind.CHECKPOINT)

    assert stdout.getvalue() == (
        progress_bytes + "\nnoted, proceeding\n$ approve · abort · anything else is chat\n"
    )


def test_contract_notice_finalizes_pending_progress_line() -> None:
    """T4.6: `notice` finalizes a pending progress line before its own line."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.notice("a stale lock was reclaimed")

    assert stdout.getvalue() == progress_bytes + "\na stale lock was reclaimed\n"


def test_contract_error_finalizes_pending_progress_line_on_stdout() -> None:
    """T4.6: `error` finalizes a pending progress line -- on stdout, where the
    progress line lives -- before its own content, which renders on stderr as
    usual; folding the finalize step into the shared low-level write helpers is
    what makes this reach `error` even though `error`'s own writes target a
    different stream."""
    stdout = _TTYStream()
    stderr = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, stderr)
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.error(cause="it broke", next_action="fix it")

    assert stdout.getvalue() == progress_bytes + "\n"
    assert stderr.getvalue() == "it broke\n→ fix it\n"


def test_contract_summary_finalizes_pending_progress_line() -> None:
    """T4.6: `summary` finalizes a pending progress line before its own
    content."""
    stdout = _TTYStream()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())
    presenter.progress("triage", 3.0, None)
    progress_bytes = stdout.getvalue()

    presenter.summary(RunSummary(outcome="no changes"))

    assert stdout.getvalue() == progress_bytes + "\n→ no changes\n"


def test_failure_finalize_progress_line_broken_pipe_is_swallowed_and_does_not_affect_reply() -> (
    None
):
    """T4.6: a `BrokenPipeError` raised only by `_finalize_progress_line`'s own
    write, triggered from within a reply-pending method (`present_checkpoint`),
    is swallowed -- the method's own subsequent write still runs (proven by its
    content landing on the stream) and its own success (not the finalize's
    failure) is what determines the reply."""
    # Write #1 is progress()'s own in-place update (succeeds); write #2 is
    # present_checkpoint's finalize call (fails, swallowed); writes #3+ are
    # present_checkpoint's own content (succeed).
    stdout = _NthWriteFailsStream(fail_at=2)
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.progress("triage", 1.0, None)

    reply = presenter.present_checkpoint(_fixed_view())

    assert reply == Approve()
    assert "sm-web" in stdout.getvalue()  # the checkpoint's own content still wrote
