"""The run lifecycle (architecture): the only module that coordinates the others.

T1.1 scope: the entry contract `run(mode, repo_path, presenter) -> int` wired
through two real steps — repo discovery (gitrepo) and the agent session's minimal
handshake (agent, via the fixture seam) — ending in a placeholder no-op summary.
This is deliberately not the nine-step preflight sequence, the phase engine,
amendments, the lock, or the write path: those are T2.2's ("orchestrator preflight")
and T2.3's ("analyze happy path") builds, per `engineering/modules/orchestrator.md`.

The `Presenter` protocol below mirrors `cli.md`'s `TerminalPresenter` interface in
full so `cli.TerminalPresenter` type-checks against it; only the methods this
skeleton's flow actually calls (`error`, `summary`) have callers today. The
view/reply types (`CheckpointView`, `AmendmentView`, `NoImpactView`,
`CheckpointReply`, `AmendmentReply`, `PromptKind`) are placeholders whose fields
land with the phase engine (T2.2/T2.3).
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from blare import agent, gitrepo
from blare.model import (
    BatchVerdict,
    BlareError,
    EditBatch,
    RunContext,
    RunControlCall,
    RunControlVerdict,
    RunMode,
)
from blare.stack import get_stack

__all__ = [
    "Abort",
    "AmendmentReply",
    "AmendmentView",
    "Approve",
    "Chat",
    "CheckpointReply",
    "CheckpointView",
    "NoImpactView",
    "Presenter",
    "PromptKind",
    "Reject",
    "RunFn",
    "RunSummary",
    "run",
]


@dataclass(frozen=True)
class Approve:
    """The user approved the current checkpoint/amendment."""


@dataclass(frozen=True)
class Abort:
    """The user aborted the run (R20: nothing is written)."""


@dataclass(frozen=True)
class Chat:
    """Free-form text the user typed instead of a reserved word."""

    text: str


@dataclass(frozen=True)
class Reject:
    """The user rejected an agent-proposed amendment (only ever returnable there)."""


CheckpointReply = Approve | Abort | Chat
AmendmentReply = Approve | Abort | Chat | Reject


class PromptKind(Enum):
    """Which prompt a `show_chat_reply` continuation is re-offering."""

    CHECKPOINT = "checkpoint"
    NO_IMPACT = "no_impact"
    AMENDMENT = "amendment"
    REJECTABLE_AMENDMENT = "rejectable_amendment"


@dataclass(frozen=True)
class CheckpointView:
    """A phase's results at its checkpoint. Fields land with T2.2/T2.3."""


@dataclass(frozen=True)
class AmendmentView:
    """An amendment unit's changed entries, grouped by phase. Fields land with T2.4."""


@dataclass(frozen=True)
class NoImpactView:
    """The R18 no-impact conclusion's delta summary. Fields land with T3.1."""


@dataclass(frozen=True)
class RunSummary:
    """What a run reports at its end (R13).

    T1.1 populates only `outcome` and `transcript_path`; the entry/gap count
    split lands with T2.3 once there is a candidate artifact set to summarize.
    """

    outcome: str
    transcript_path: Path | None = None


class Presenter(Protocol):
    """The terminal surface's contract, as `cli.md` documents it in full."""

    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply: ...

    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply: ...

    def present_no_impact(self, view: NoImpactView) -> CheckpointReply: ...

    def show_chat_reply(
        self, text: str, prompt: PromptKind | None
    ) -> AmendmentReply | None: ...

    def notice(self, text: str) -> None: ...

    def error(self, cause: str, next_action: str, detail: str | None = None) -> None: ...

    def summary(self, s: RunSummary) -> None: ...

    def is_interactive(self) -> bool: ...


RunFn = Callable[[RunMode, Path, Presenter], int]


class _PlaceholderTranscriptWriter:
    """T1.1/T2.1 placeholder `agent.TranscriptWriter`: a real one, rooted under
    `$XDG_STATE_HOME/blare/<repo-id>/transcripts/` via gitrepo's repo-id, is built at
    preflight step 9 by T2.2 (`orchestrator.md`). Until then the seam-through flow
    needs *something* real to hand `AgentSession`, so this writes JSONL to a plain
    temp file.
    """

    def __init__(self) -> None:
        fd, path = tempfile.mkstemp(prefix="blare-transcript-", suffix=".jsonl")
        os.close(fd)
        self._path = Path(path)

    def write_event(self, direction: str, event: dict[str, object]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"direction": direction, "event": event}) + "\n")

    @property
    def path(self) -> Path:
        return self._path


def _noop_sink(batch: EditBatch) -> BatchVerdict:
    """T1.1/T2.1 placeholder `agent.EditSink`: the real sink (phase-state rule plus
    artifacts' per-batch content check) lands with T2.2/T2.3; the seam-through flow
    never actually calls `run_phase`, so nothing ever proposes an edit through it."""
    return BatchVerdict(ok=True, message=None)


def _noop_control(call: RunControlCall) -> RunControlVerdict:
    """T1.1/T2.1 placeholder `agent.RunControlHandler`, see `_noop_sink`."""
    return RunControlVerdict(ok=True, message=None)


def _seam_through(mode: RunMode, repo_path: Path) -> RunSummary:
    """Repo discovery, then the agent session's minimal handshake.

    Raises `BlareError` (exit 1) for any refusal along this path. `mode` reaches
    `AgentSession.start` (its system prompt differs by mode) but the run otherwise
    does not yet diverge by mode — the phase engine and triage are T2.2 onward. The
    stack is hardcoded to the MVP default (`prometheus`) rather than resolved from
    `.blare/config.yaml`: config/stack resolution is `artifacts.load`/`empty_set`'s
    job (T1.4/T2.2), not yet wired here.
    """
    repo = gitrepo.GitRepo.discover(repo_path)

    client = agent.create_client()
    transcript = _PlaceholderTranscriptWriter()
    session = agent.AgentSession(
        client,
        sink=_noop_sink,
        control=_noop_control,
        stack=get_stack("prometheus"),
        transcript=transcript,
    )
    session.start(mode, RunContext(worktree_root=repo.worktree_root))
    session.close()

    return RunSummary(outcome="no changes", transcript_path=transcript.path)


def run(mode: RunMode, repo_path: Path, presenter: Presenter) -> int:
    """Blare's one entry contract: run a mode against a repo, render through presenter.

    Exit codes implemented so far: `0` success, `1` a `BlareError` refusal (every
    failure this skeleton can produce is a preflight-stage one: repo discovery or
    the agent auth handshake), `2` an unexpected exception. The full taxonomy
    (`2` for post-preflight run failures, `3` for user abort, SIGINT handling) is
    built out alongside the preflight sequence and phase engine in T2.2/T2.3.
    """
    try:
        summary = _seam_through(mode, repo_path)
    except BlareError as exc:
        presenter.error(cause=exc.cause, next_action=exc.next_action)
        return 1
    except Exception as exc:  # noqa: BLE001 - the architecture's non-module carve-out
        presenter.error(
            cause=f"unexpected error: {exc}",
            next_action="Re-run; if this persists, report the detail below.",
            detail=traceback.format_exc(),
        )
        return 2

    presenter.summary(summary)
    return 0
