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


def test_contract_main_wiring_passes_mode_cwd_and_a_terminal_presenter() -> None:
    """main() passes parse_args's mode, the invocation cwd, and a TerminalPresenter
    to the injected `run` callable."""
    received: dict[str, object] = {}

    def _recording_run(mode: RunMode, repo_path: Path, presenter: Presenter) -> int:
        received["mode"] = mode
        received["repo_path"] = repo_path
        received["presenter"] = presenter
        return 0

    code = main(["analyze"], run=_recording_run)

    assert code == 0
    assert received["mode"] == RunMode.ANALYZE
    assert received["repo_path"] == Path.cwd()
    assert isinstance(received["presenter"], TerminalPresenter)


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
    """progress() (R25) renders "· label (Ns, activity)", byte-exact for a fixed
    set of arguments -- distinct from both "→ " (results) and "$ " (prompts)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("phase 3 — metric coverage", 12.0, "propose_edits")

    assert stdout.getvalue() == "· phase 3 — metric coverage (12s, propose_edits)\n"


def test_contract_progress_renders_waiting_when_last_activity_is_none() -> None:
    """progress() renders "waiting" in place of last_activity when it is None --
    no tool call has arrived yet (cli.md's Rendering rules)."""
    stdout = io.StringIO()
    presenter = TerminalPresenter(io.StringIO(), stdout, io.StringIO())

    presenter.progress("triage", 3.0, None)

    assert stdout.getvalue() == "· triage (3s, waiting)\n"


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
    same void-class rule as notice)."""
    presenter = TerminalPresenter(io.StringIO(), _BrokenPipeStream(), io.StringIO())

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

    class _TTYStdout(io.StringIO):
        def isatty(self) -> bool:
            return True

    stdout = _TTYStdout()
    presenter = TerminalPresenter(io.StringIO("approve\n"), stdout, io.StringIO())
    presenter.present_checkpoint(_fixed_view())
    rendered = stdout.getvalue()
    assert "\x1b[38;2;255;90;31mcritical\x1b[0m" in rendered
    assert "    severity: critical\n" not in rendered  # the bare form is not present

    monkeypatch.setenv("NO_COLOR", "1")
    stdout_no_color = _TTYStdout()
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
