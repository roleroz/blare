"""Unit tests for blare.cli (T1.1 subset: parse_args, main wiring, error/summary/notice
rendering). Checkpoint/amendment/chat rendering lands with T2.2-T3.1."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from blare.cli import ParsedCommand, TerminalPresenter, main, parse_args
from blare.model import RunMode
from blare.orchestrator import Presenter, RunSummary


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
