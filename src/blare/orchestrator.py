"""The run lifecycle (architecture): the only module that coordinates the others.

T2.2 scope (`engineering/modules/orchestrator.md`): the full nine-step preflight
sequence, the lock, the run log, and the exit-code taxonomy. Steps 5-6 (update-only:
SHA ancestry, the R7 empty-delta short-circuit) are wired for real here but their e2e
coverage is T3.x's.

T2.3 scope (this task): the analyze-mode phase engine -- four phases in order, each
opening a phase, running it via `AgentSession.run_phase`, presenting a
`CheckpointView` and looping chat to a terminal reply -- the final approval gate
(`artifacts.semantic_violations`), and the write path (the R20 re-check, then the
three write primitives in order, state last). Diff mode's post-preflight flow
(triage, the phase engine over the R18-seeded queue) is unchanged from T2.2's
placeholder tail -- that is T3.x's build. The amendment mechanism (unit tracking,
cascade, system-originated amendments' repair loop) is T2.4's build: a semantic
violation at the final gate here raises `SemanticGateFailureError` rather than
opening a repair unit, per this task's explicit scope boundary.

The `Presenter` protocol below mirrors `cli.md`'s `TerminalPresenter` interface in
full so `cli.TerminalPresenter` type-checks against it. `present_amendment` and
`present_no_impact` have no caller yet (T2.4/T3.1's views); every other method,
`CheckpointView` included, is exercised by the analyze phase engine.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import os
import signal
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from blare import agent, artifacts, gitrepo
from blare.model import (
    BatchVerdict,
    BlareError,
    EditBatch,
    Phase,
    RunContext,
    RunControlAction,
    RunControlCall,
    RunControlVerdict,
    RunMode,
)

__all__ = [
    "Abort",
    "AmendmentReply",
    "AmendmentView",
    "Approve",
    "Chat",
    "CheckpointReply",
    "CheckpointView",
    "DirtyWorkingTreeError",
    "EntryChange",
    "EntryCounts",
    "LockHeldError",
    "NoImpactView",
    "NonAncestorSHAError",
    "NonInteractiveError",
    "Presenter",
    "PromptKind",
    "Reject",
    "RunFn",
    "RunSummary",
    "SemanticGateFailureError",
    "StateDirectoryError",
    "WriteTimeRecheckError",
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
class EntryChange:
    """One entry's content for checkpoint/amendment display (T2.3).

    `fields` is an ordered `(field_name, rendered_value)` sequence built generically
    from the entry's dataclass fields (`dataclasses.fields`, skipping the id field) so
    this module never hardcodes per-entry-type formatting and cli never needs to import
    artifacts' entry dataclasses to render them (architecture: cli -> orchestrator
    only) -- it just prints the pairs it is handed.
    """

    entry_type: str
    id: str
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CheckpointView:
    """A phase's results at its checkpoint (T2.3): the entries that phase's edits
    added/updated/removed since the phase opened, with their content, and the gap
    summary over the whole candidate set (architecture: "phase, entries
    added/updated/removed with their content, gap summary")."""

    phase: Phase
    gap_counts: artifacts.GapSummary
    added: tuple[EntryChange, ...] = ()
    updated: tuple[EntryChange, ...] = ()
    removed: tuple[EntryChange, ...] = ()


@dataclass(frozen=True)
class AmendmentView:
    """An amendment unit's changed entries, grouped by phase. Fields land with T2.4."""


@dataclass(frozen=True)
class NoImpactView:
    """The R18 no-impact conclusion's delta summary. Fields land with T3.1."""


@dataclass(frozen=True)
class EntryCounts:
    """R13's exact entry-count split for a `RunSummary` (T2.3)."""

    added: int
    updated: int
    removed: int


@dataclass(frozen=True)
class RunSummary:
    """What a run reports at its end (R13).

    `entry_counts` is `None` only for the sessionless R7 up-to-date ending (cli.md:
    "the sessionless R7-style summary renders 'no changes' with gap counts and no
    transcript line"); every session-bearing ending (success, abort, failure) carries
    real counts, even if all zero. `discarded` is true at a non-writing ending (abort,
    or a post-preflight failure before the write completed) -- R20 guarantees nothing
    landed, so the counts describe what was discarded, not applied (cli.md's rendering
    rule).
    """

    outcome: str
    transcript_path: Path | None = None
    gap_counts: artifacts.GapSummary | None = None
    entry_counts: EntryCounts | None = None
    discarded: bool = False


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


# --- Preflight-owned errors (orchestrator.md: steps 2, 3, 5, 8) ---------------------


class StateDirectoryError(BlareError):
    """Step 2: `$XDG_STATE_HOME/blare/<repo-id>/` (or the run log under it) could not
    be created or written to. Names the path; next action is to make it writable or
    redirect `XDG_STATE_HOME`."""


class DirtyWorkingTreeError(BlareError):
    """Step 2: the working tree outside `.blare/` differs from HEAD (R11's third
    clause) -- modified/deleted tracked files or untracked files, listed verbatim.
    Git-ignored files and changes confined to `.blare/` never trigger this
    (`gitrepo.dirty_paths_outside` already excludes both)."""


class LockHeldError(BlareError):
    """Step 3: another Blare invocation already holds the per-repo lock (R21). Names
    the owning PID; a dead owner is reclaimed instead of raising this."""


class NonAncestorSHAError(BlareError):
    """Step 5 (update only): the recorded `analyzed_sha` does not resolve to a commit
    in this repository, or is not an ancestor of the current commit (R15). Names both
    recovery options: re-run full analysis, or hand-edit the state file."""


class NonInteractiveError(BlareError):
    """Step 8: stdin is not a TTY, so checkpoints cannot be presented (R22). Fires
    only once checkpoints would actually be needed -- after the R7 short-circuit and
    any earlier refusal, per the preflight's fail-fast ordering."""


# --- Post-preflight errors (orchestrator.md: Approval gate, Write path) -------------


class SemanticGateFailureError(BlareError):
    """The final approval gate (orchestrator.md, Approval gate) found semantic-
    invariant violations in the candidate set. Per architecture.md's Amendment
    mechanism this should raise a system-originated amendment for the user to steer
    (via chat) or abort -- building that repair loop is T2.4's task, out of this
    task's scope. Rather than silently writing an invalid set or inventing amendment
    machinery, this reports the violations found and fails the run (R20: nothing is
    written before final confirmation, and this is never reached)."""


class WriteTimeRecheckError(BlareError):
    """R20's write-time re-check (orchestrator.md, Write path): the working tree
    outside `.blare/` no longer matches the commit captured at run start, or the
    canonical YAML no longer matches what this run loaded -- the repository changed
    mid-run. Aborts before writing anything."""


_R15_NEXT_ACTION = (
    "Re-run `blare analyze` to start a fresh analysis (R16), or hand-edit "
    "analyzed_sha in .blare/state.yaml to a real ancestor of the current commit."
)


# --- State directory, run log, transcript (architecture: Transcripts and lock) -----

_BLARE_STATE_SUBDIR = "blare"
_RUN_LOG_DIRNAME = "runs"
_TRANSCRIPTS_DIRNAME = "transcripts"
_LOCK_FILENAME = "lock"


def _xdg_state_home() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    if value:
        return Path(value)
    return Path.home() / ".local" / "state"


def _state_dir(repo_id: str) -> Path:
    """`$XDG_STATE_HOME/blare/<repo-id>/` -- the root for this repo's lock,
    transcripts, and run logs (architecture: Transcripts and lock)."""
    return _xdg_state_home() / _BLARE_STATE_SUBDIR / repo_id


def _mint_run_id() -> str:
    """One value shared by the run log's and this run's transcript's file names
    (orchestrator.md, Failure visibility: "the whole coordination behind 'named by
    the same timestamp'"). Leads with a microsecond-resolution UTC timestamp for
    humans skimming the directory; a PID and a short random suffix are appended so
    two runs starting in the same process (as happens in-process in tests) or in the
    same microsecond never collide -- the timestamp is what the docs name, the
    suffix is this module's own uniqueness guarantee.
    """
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _ensure_state_dir(state_dir: Path) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StateDirectoryError(
            cause=f"cannot create the state directory {state_dir}: {exc}",
            next_action=(
                f"Ensure {state_dir.parent} is writable, or set XDG_STATE_HOME to a "
                "writable location, then re-run blare."
            ),
        ) from exc


class _RunLog:
    """JSONL run log, one file per run (architecture: Failure visibility). Opens the
    file to append one line per event rather than holding a persistent handle open
    (matching this module's transcript-writer style), so a crash mid-run never loses
    a partially buffered line."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, event: dict[str, object]) -> None:
        line: dict[str, object] = {
            "time": _dt.datetime.now(_dt.UTC).isoformat(),
            **event,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")


def _start_run_log(state_dir: Path, run_id: str) -> _RunLog:
    """Step 2's "run-log start": create the run log file and write its first event,
    proving the location is writable now rather than at the first later write."""
    path = state_dir / _RUN_LOG_DIRNAME / f"{run_id}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        run_log = _RunLog(path)
        run_log.record({"event": "run_started"})
    except OSError as exc:
        raise StateDirectoryError(
            cause=f"cannot start the run log at {path}: {exc}",
            next_action=(
                f"Ensure {path.parent} is writable, or set XDG_STATE_HOME to a "
                "writable location, then re-run blare."
            ),
        ) from exc
    return run_log


class _RealTranscriptWriter:
    """The orchestrator-constructed `agent.TranscriptWriter` (architecture:
    transcripts live under `$XDG_STATE_HOME/blare/<repo-id>/transcripts/`, named by
    the run's minted id -- the same one the run log uses). Opens the file per event,
    same rationale as `_RunLog`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write_event(self, direction: str, event: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"direction": direction, "event": event}) + "\n")

    @property
    def path(self) -> Path:
        return self._path


# --- Lock (orchestrator.md, Lock) ---------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """The lock's liveness check (Linux-only per spec): a module-level function so
    tests can monkeypatch it (`orchestrator.md`: "an injected callable for
    testability") without threading an extra parameter through `run()`'s fixed
    entry contract."""
    return Path(f"/proc/{pid}").exists()


def _read_lock_owner(path: Path) -> int | None:
    """The owning PID recorded in a lock file, or `None` if it cannot be determined
    (missing, unreadable, or malformed) -- treated as stale rather than trusted."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    return pid if isinstance(pid, int) else None


@dataclass
class _LockHandle:
    """A held lock; `release` is idempotent and safe to call more than once (the
    `finally` in `run()` calls it exactly once, but idempotence costs nothing)."""

    path: Path

    def release(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def _write_lock_file(path: Path) -> None:
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "started_at": _dt.datetime.now(_dt.UTC).isoformat(),
        }
    ).encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


# Bounds the reclaim-and-retry loop below: a losing race against another
# invocation reclaiming the very same stale lock (unlinking, then losing the
# re-create to the other side) must fall back to a proper R21 refusal rather than
# an unbounded retry or an uncaught `FileExistsError` (exit 2 instead of 1).
_LOCK_ACQUIRE_ATTEMPTS = 5


def _acquire_lock(lock_path: Path, notice: Callable[[str], None]) -> _LockHandle:
    """`$XDG_STATE_HOME/blare/<repo-id>/lock`, PID-based, created with `O_EXCL`
    (R21). A held lock whose owner is alive raises `LockHeldError` naming the PID; a
    dead owner's lock is reclaimed (unlinked, with a notice) and re-created. Retries
    the reclaim a bounded number of times: reclaiming is itself a race between
    however many invocations found the same stale lock, so losing the re-create to
    one of them must re-evaluate (the winner is a live owner now) rather than crash
    or spin forever."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    already_noticed = False
    last_owner_pid: int | None = None
    for _attempt in range(_LOCK_ACQUIRE_ATTEMPTS):
        try:
            _write_lock_file(lock_path)
            return _LockHandle(path=lock_path)
        except FileExistsError:
            owner_pid = _read_lock_owner(lock_path)
            last_owner_pid = owner_pid
            if owner_pid is not None and _pid_alive(owner_pid):
                raise LockHeldError(
                    cause=(
                        f"another blare run (pid {owner_pid}) is already active "
                        "against this repository"
                    ),
                    next_action="Wait for it to finish, or stop it if it is stuck.",
                ) from None
            if not already_noticed:
                notice(
                    f"reclaiming a stale lock at {lock_path} "
                    f"(owning process {owner_pid} is no longer running)"
                )
                already_noticed = True
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()
    raise LockHeldError(
        cause=(
            f"could not acquire the lock at {lock_path} after repeated contention "
            f"(last seen owned by pid {last_owner_pid})"
        ),
        next_action="Wait for the other blare run to finish, then retry.",
    )


# --- Artifact dispatch (orchestrator.md, step 4) ------------------------------------


def _dispatch_artifacts(blare_root: Path, mode: RunMode) -> artifacts.ArtifactSet:
    """`state_exists` decides the branch: analyze without state initializes fresh
    (R1's inverse refusal via `init_inspection`, then `empty_set`); update without
    state refuses (R17, via `load`'s own `StateMissingError`); otherwise both modes
    load and validate the existing set (R19/R23/R24)."""
    if artifacts.state_exists(blare_root):
        return artifacts.load(blare_root, mode)
    if mode is RunMode.ANALYZE:
        artifacts.init_inspection(blare_root)
        return artifacts.empty_set(blare_root)
    return artifacts.load(blare_root, mode)


# --- Phase engine (T2.3): the pending edit set, the sink/control handlers, and
# checkpoint-view rendering support ---------------------------------------------------


class _PhaseStatus(Enum):
    """A phase's state (architecture: "Phase states"). T2.3's analyze engine only
    ever drives phases forward in order (unvisited -> open -> frozen); the amendment
    mechanism that can re-open a frozen phase is T2.4's build."""

    UNVISITED = "unvisited"
    OPEN = "open"
    FROZEN = "frozen"


@dataclass
class _CandidateHolder:
    """The run's one pending candidate `ArtifactSet`, mutated (by replacement -- the
    field is reassigned, `apply` itself is pure) as accepted edit batches land. A
    plain mutable holder rather than a nonlocal variable because the sink closure and
    the phase loop both need to read and update the same cell."""

    current: artifacts.ArtifactSet


def _make_sink(
    holder: _CandidateHolder, phase_status: dict[Phase, _PhaseStatus]
) -> agent.EditSink:
    """The edit sink (architecture: Edit-proposal protocol): enforces the phase-state
    rule (this module's own), then artifacts' per-batch content check; an accepted
    batch's candidate replaces the holder's current set."""

    def sink(batch: EditBatch) -> BatchVerdict:
        status = phase_status.get(batch.phase, _PhaseStatus.UNVISITED)
        if status is not _PhaseStatus.OPEN:
            return BatchVerdict(
                ok=False,
                message=(
                    f"phase {batch.phase.value} is {status.value}, not open -- edits "
                    "are only accepted for the currently open phase"
                ),
            )
        verdict = artifacts.batch_check(holder.current, batch)
        if not verdict.ok:
            return verdict
        holder.current = artifacts.apply(holder.current, batch)
        return verdict

    return sink


def _make_control_handler(mode: RunMode) -> agent.RunControlHandler:
    """The run-control handler (architecture: Run-control channel). T2.3's analyze
    engine has no triage/no-impact verdicts to accept (diff-mode-only, R18) and no
    amendment mechanism yet (T2.4) -- every call is rejected with a verdict naming
    why, per the architecture's "Run-control handling is total" rule: never a raise,
    always a verdict the model can act on."""

    def control(call: RunControlCall) -> RunControlVerdict:
        if mode is RunMode.ANALYZE and call.action in (
            RunControlAction.AFFECTED_VERDICT,
            RunControlAction.NO_IMPACT,
        ):
            return RunControlVerdict(
                ok=False,
                message=(
                    f"{call.action.value} is a diff-mode verdict (R18); this is a "
                    "full analysis run -- work through all four phases instead"
                ),
            )
        return RunControlVerdict(
            ok=False,
            message=(
                f"{call.action.value} is not supported in this build (the amendment "
                "mechanism lands in a later task) -- continue proposing edits, or "
                "state your conclusion in free text at the checkpoint"
            ),
        )

    return control


# Which entry-based `ArtifactSet` fields each phase owns, for checkpoint-view diffing
# (architecture: "Each artifact belongs to the phase that produces it"). The coverage
# mapping spans phases 3-4 by side (metric side / alert side) rather than by whole
# entries, so it is deliberately not attributed to a single phase here and is not
# shown in per-phase checkpoint sections -- its effect is already visible through the
# gap-count summary every checkpoint carries.
_PHASE_ENTRY_TYPES: dict[Phase, tuple[str, ...]] = {
    Phase.SYSTEM_MAP: ("system_components",),
    Phase.FAILURE_MODES: ("failure_modes",),
    Phase.METRIC_COVERAGE: ("metrics", "metric_recommendations"),
    Phase.ALERT_RECOMMENDATIONS: ("alert_recommendations",),
}

# Every entry-based field on `ArtifactSet`, for the run-level entry-count summary
# (R13) -- unlike `_PHASE_ENTRY_TYPES` above, this includes "coverage" since the
# overall count legitimately covers every entry Blare writes, mechanical or not.
_ALL_ENTRY_TYPES: tuple[str, ...] = (
    "system_components",
    "failure_modes",
    "metrics",
    "metric_recommendations",
    "alert_recommendations",
    "coverage",
)


def _format_field_value(value: object) -> str:
    """Render one entry field's value as display text -- generic over every entry
    type's field shapes (str, bool, tuple of ids, or a str->str mapping), so this
    module never special-cases a specific entry dataclass."""
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value) if value else "(none)"
    if isinstance(value, Mapping):
        rendered = ", ".join(f"{key}={val}" for key, val in value.items())
        return rendered if rendered else "(none)"
    return str(value)


def _entry_change(entry_type: str, entry_id: str, entry: object) -> EntryChange:
    """Build one `EntryChange` from an artifacts entry dataclass instance, via
    `dataclasses.fields` rather than hardcoding each entry type's field names (so
    cli, which renders these generically, never needs to import artifacts' entry
    types -- architecture: cli -> orchestrator only)."""
    rendered_fields: list[tuple[str, str]] = []
    for entry_field in dataclasses.fields(entry):  # type: ignore[arg-type]
        if entry_field.name in ("id", "failure_mode_id"):
            continue
        rendered_fields.append(
            (entry_field.name, _format_field_value(getattr(entry, entry_field.name)))
        )
    return EntryChange(entry_type=entry_type, id=entry_id, fields=tuple(rendered_fields))


def _phase_diff(
    phase: Phase, baseline: artifacts.ArtifactSet, current: artifacts.ArtifactSet
) -> tuple[tuple[EntryChange, ...], tuple[EntryChange, ...], tuple[EntryChange, ...]]:
    """Added/updated/removed entries for `phase`'s owned entry type(s), comparing the
    candidate as it stood when the phase opened (`baseline`) to `current` -- the
    content a `CheckpointView` reports for that phase."""
    added: list[EntryChange] = []
    updated: list[EntryChange] = []
    removed: list[EntryChange] = []
    for entry_type in _PHASE_ENTRY_TYPES[phase]:
        before: dict[str, object] = getattr(baseline, entry_type)
        after: dict[str, object] = getattr(current, entry_type)
        for entry_id in sorted(set(after) - set(before)):
            added.append(_entry_change(entry_type, entry_id, after[entry_id]))
        for entry_id in sorted(set(after) & set(before)):
            if after[entry_id] != before[entry_id]:
                updated.append(_entry_change(entry_type, entry_id, after[entry_id]))
        for entry_id in sorted(set(before) - set(after)):
            removed.append(_entry_change(entry_type, entry_id, before[entry_id]))
    return tuple(added), tuple(updated), tuple(removed)


def _overall_counts(
    initial: artifacts.ArtifactSet, final: artifacts.ArtifactSet
) -> EntryCounts:
    """The whole-run entry-count split (R13) across every entry-based field, comparing
    the set the run started from to its current candidate -- used for both the
    write-path success summary and a discarded-edits summary at abort/failure."""
    added = updated = removed = 0
    for entry_type in _ALL_ENTRY_TYPES:
        before: dict[str, object] = getattr(initial, entry_type)
        after: dict[str, object] = getattr(final, entry_type)
        added += len(set(after) - set(before))
        removed += len(set(before) - set(after))
        updated += sum(
            1 for entry_id in set(after) & set(before) if after[entry_id] != before[entry_id]
        )
    return EntryCounts(added=added, updated=updated, removed=removed)


def _run_checkpoint(
    session: agent.AgentSession, presenter: Presenter, view: CheckpointView
) -> bool:
    """Present one checkpoint and drive its chat loop to a terminal reply (architecture:
    "Checkpoint loop"). Returns `True` on approval, `False` on abort. The view is
    presented exactly once; a `Chat` reply routes through `session.chat` and
    `presenter.show_chat_reply`, which itself reads and returns the next reply -- the
    view is never redrawn (cli.md)."""
    reply: CheckpointReply | AmendmentReply | None = presenter.present_checkpoint(view)
    while True:
        if isinstance(reply, Approve):
            return True
        if isinstance(reply, Abort):
            return False
        if isinstance(reply, Chat):
            chat_reply_text = session.chat(reply.text)
            reply = presenter.show_chat_reply(chat_reply_text, PromptKind.CHECKPOINT)
            assert reply is not None, (
                "a checkpoint prompt was given (not None); show_chat_reply must "
                "re-offer it and return the next reply"
            )
            assert not isinstance(reply, Reject), (
                "Reject is only returnable at a rejectable-amendment continuation, "
                "never at a plain checkpoint (cli.md)"
            )
            continue
        raise AssertionError(f"unexpected checkpoint reply {reply!r}")  # pragma: no cover


# The four full-analysis phases, in run order (spec, Scope).
_ANALYZE_PHASES: tuple[Phase, ...] = (
    Phase.SYSTEM_MAP,
    Phase.FAILURE_MODES,
    Phase.METRIC_COVERAGE,
    Phase.ALERT_RECOMMENDATIONS,
)


def _write_with_sigint_masked(write: Callable[[], None]) -> bool:
    """Run `write` (the three write primitives, in order) with SIGINT masked
    (orchestrator.md, Write path: "SIGINT is masked from final confirmation until the
    write completes"). Returns whether a SIGINT arrived during the window -- the
    caller logs it, but the run is reported as what it is: completed, never aborted,
    since honoring the signal here would contradict a write that already succeeded."""
    deferred = False

    def _defer(signum: int, frame: object) -> None:
        nonlocal deferred
        deferred = True

    previous_handler = signal.signal(signal.SIGINT, _defer)
    try:
        write()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    return deferred


# --- Run state (tracked across the try/except in `run()` for cleanup and the
# stage-based exit-code rule) --------------------------------------------------------


@dataclass
class _RunState:
    run_log: _RunLog | None = None
    lock: _LockHandle | None = None
    preflight_complete: bool = False
    run_log_broken: bool = False
    # Set once `AgentSession.start` returns successfully (step 9) -- "a session
    # ran" per R14's own definition. Distinct from `preflight_complete` (an
    # exit-code concern): this one exists so a SIGINT reaching this function after
    # a session started renders the session-bearing abort summary (transcript path
    # included) rather than the pre-session `aborted` notice, per orchestrator.md's
    # Error handling section.
    transcript_path: Path | None = None
    # Set once the phase engine's pending edit set exists (T2.3): lets `run()`'s
    # exception handler render a post-preflight failure's summary (discarded counts,
    # gap counts) even though the failure unwound through an exception rather than a
    # normal return (orchestrator.md, Error handling: "Every exit-2 session-bearing
    # ending then renders the summary").
    holder: _CandidateHolder | None = None
    initial_set: artifacts.ArtifactSet | None = None


def _log(run_state: _RunState, presenter: Presenter, event: dict[str, object]) -> None:
    """Best-effort run-log write (orchestrator.md, Failure visibility): a write
    failure after step 2 never fails the run, degrading to one presenter notice;
    further events are silently dropped rather than repeating the notice."""
    if run_state.run_log is None or run_state.run_log_broken:
        return
    try:
        run_state.run_log.record(event)
    except OSError as exc:
        run_state.run_log_broken = True
        presenter.notice(f"could not write the run log at {run_state.run_log.path}: {exc}")


# --- The nine-step preflight sequence, then the analyze phase engine and write
# path (update mode still ends in T2.2's placeholder tail; see the mode check
# below) ------------------------------------------------------------------------


def _execute(
    mode: RunMode, repo_path: Path, presenter: Presenter, run_state: _RunState
) -> int:
    # Step 1: repo discovery; no-commits check (R11). No repo-id exists yet, so a
    # failure here has no run log -- its diagnosis is the R13 message alone.
    repo = gitrepo.GitRepo.discover(repo_path)
    end_sha = repo.head_sha()

    # Step 2: state-directory creation and run-log start; then the dirty-tree check
    # (R11's third clause).
    repo_id = repo.repo_id()
    state_dir = _state_dir(repo_id)
    _ensure_state_dir(state_dir)
    run_id = _mint_run_id()
    run_log = _start_run_log(state_dir, run_id)
    run_state.run_log = run_log
    _log(run_state, presenter, {"event": "preflight_step", "step": 2, "detail": "state_dir_ready"})

    dirty = repo.dirty_paths_outside(".blare")
    if dirty:
        raise DirtyWorkingTreeError(
            cause=(
                "the working tree has changes outside .blare/: " + ", ".join(sorted(dirty))
            ),
            next_action="Commit or stash these changes, then re-run blare.",
        )
    _log(run_state, presenter, {"event": "preflight_step", "step": 2, "detail": "clean_tree"})

    # Step 3: lock acquisition (R21). The lock file only -- its directory already
    # exists from step 2.
    lock = _acquire_lock(state_dir / _LOCK_FILENAME, notice=presenter.notice)
    run_state.lock = lock
    _log(run_state, presenter, {"event": "preflight_step", "step": 3, "detail": "lock_acquired"})

    # Step 4: artifact dispatch (R1/R17/R19/R23/R24). Config and stack resolution
    # happen inside artifacts on every branch; the orchestrator never touches the
    # stack module and hands `artifact_set.stack` to the agent session as a value.
    blare_root = repo.worktree_root / ".blare"
    artifact_set = _dispatch_artifacts(blare_root, mode)
    _log(run_state, presenter, {"event": "preflight_step", "step": 4, "detail": "artifacts_loaded"})

    delta_files: tuple[str, ...] = ()
    if mode is RunMode.UPDATE:
        # Step 5 (update only): recorded SHA resolves and is an ancestor (R15).
        analyzed_sha = artifact_set.analyzed_sha
        if analyzed_sha is None:
            # Unreachable in practice: `load` guarantees a non-empty analyzed_sha
            # (R19's "state missing its SHA" fails structurally first). Guarded
            # here only so mypy can narrow the type below without an assert.
            raise NonAncestorSHAError(
                cause="the loaded state records no analyzed SHA",
                next_action=_R15_NEXT_ACTION,
            )
        if not repo.resolves(analyzed_sha):
            raise NonAncestorSHAError(
                cause=(
                    f"the recorded analyzed SHA {analyzed_sha!r} does not resolve to "
                    "a commit in this repository"
                ),
                next_action=_R15_NEXT_ACTION,
            )
        if not repo.is_ancestor(analyzed_sha, end_sha):
            raise NonAncestorSHAError(
                cause=(
                    f"the recorded analyzed SHA {analyzed_sha!r} is not an ancestor "
                    f"of the current commit {end_sha!r}"
                ),
                next_action=_R15_NEXT_ACTION,
            )
        _log(
            run_state,
            presenter,
            {"event": "preflight_step", "step": 5, "detail": "sha_is_ancestor"},
        )

        # Step 6 (update only): empty effective delta -> up-to-date summary, exit 0
        # (R7; no session, no login, no transcript). Detecting this is a pure git
        # operation and never invokes the agent, so it precedes the semantic check.
        delta = repo.effective_delta(analyzed_sha, end_sha, ".blare")
        if delta.is_empty:
            gaps = artifacts.gap_counts(artifact_set)
            _log(
                run_state,
                presenter,
                {
                    "event": "up_to_date",
                    "gap_counts": {
                        "alertable": gaps.alertable,
                        "metric_gap": gaps.metric_gap,
                        "excluded": gaps.excluded,
                    },
                },
            )
            presenter.summary(RunSummary(outcome="up to date", gap_counts=gaps))
            return 0
        delta_files = tuple(f.path for f in delta.files)
        _log(
            run_state,
            presenter,
            {
                "event": "preflight_step",
                "step": 6,
                "detail": f"delta of {len(delta_files)} file(s)",
            },
        )

    # Step 7: semantic check on the loaded set -> violations seed the affected-phase
    # queue (R18). T2.2 computes this (so the ordering rule "(7,8) semantic seeds
    # never terminate the run" holds structurally) but nothing yet consumes the
    # resulting queue -- the phase engine that would is T2.3/T2.4's build.
    violations = artifacts.semantic_violations(artifact_set)
    _log(
        run_state,
        presenter,
        {"event": "preflight_step", "step": 7, "violation_count": len(violations)},
    )

    # Step 8: TTY check, only reached once checkpoints would actually be presented
    # (R22).
    if not presenter.is_interactive():
        raise NonInteractiveError(
            cause="stdin is not a TTY; checkpoints cannot be presented",
            next_action="Run blare from an interactive terminal.",
        )
    _log(run_state, presenter, {"event": "preflight_step", "step": 8, "detail": "stdin_is_tty"})

    # Step 9: auth preflight via AgentSession.start (R12). This is also where the
    # transcript is first created -- runs ending before this point write no
    # transcript (R14). The pending edit set (holder/phase_status) and the real
    # sink/control handlers are built here, before the session, since the session
    # needs them at construction time regardless of mode; only ANALYZE mode's phase
    # engine (below) actually drives phases through them in this task.
    holder = _CandidateHolder(artifact_set)
    run_state.holder = holder
    run_state.initial_set = artifact_set
    phase_status: dict[Phase, _PhaseStatus] = dict.fromkeys(Phase, _PhaseStatus.UNVISITED)
    sink = _make_sink(holder, phase_status)
    control = _make_control_handler(mode)

    transcript = _RealTranscriptWriter(
        state_dir / _TRANSCRIPTS_DIRNAME / f"{run_id}.jsonl"
    )
    client = agent.create_client()
    session = agent.AgentSession(
        client,
        sink=sink,
        control=control,
        stack=artifact_set.stack,
        transcript=transcript,
    )
    session.start(
        mode,
        RunContext(
            worktree_root=repo.worktree_root,
            delta_files=delta_files,
            # The effective delta's patch text (agent.md's RunContext.patch_text) has
            # no producer yet: gitrepo.md's interface exposes name-status only, not
            # full diff text. T2.2 leaves this empty rather than shelling out to git
            # itself (forbidden -- "no other module invokes git") or inventing an
            # undocumented gitrepo method; the real triage flow that consumes this
            # (agent.md) is T3.1's build, which is where this gap needs closing.
            patch_text="",
        ),
    )
    run_state.preflight_complete = True
    run_state.transcript_path = transcript.path
    _log(run_state, presenter, {"event": "preflight_step", "step": 9, "detail": "auth_ready"})

    if mode is not RunMode.ANALYZE:
        # Diff mode's post-preflight flow (triage, the phase engine over the seeded
        # queue) is T3.x's build; this placeholder tail is unchanged from T2.2.
        session.close()
        gaps = artifacts.gap_counts(artifact_set)
        presenter.summary(
            RunSummary(outcome="no changes", transcript_path=transcript.path, gap_counts=gaps)
        )
        return 0

    # --- The analyze phase engine (T2.3): four phases in order, a checkpoint after
    # each, the final approval gate, then the write path. Wrapped in try/finally so
    # `session.close()` runs on every exit from here on -- approval, abort, or any
    # exception (SemanticGateFailureError, WriteTimeRecheckError, a WriteError from
    # a write primitive, or an AgentSessionError from the session itself) -- not
    # only the approval and abort paths that used to close it explicitly. `close`
    # is idempotent and safe after any error (agent.md), which is what makes an
    # unconditional `finally` here correct rather than merely convenient.
    try:
        for phase in _ANALYZE_PHASES:
            phase_status[phase] = _PhaseStatus.OPEN
            baseline = holder.current
            session.run_phase(phase)
            _log(run_state, presenter, {"event": "phase_run", "phase": int(phase)})

            added, updated, removed = _phase_diff(phase, baseline, holder.current)
            view = CheckpointView(
                phase=phase,
                gap_counts=artifacts.gap_counts(holder.current),
                added=added,
                updated=updated,
                removed=removed,
            )
            approved = _run_checkpoint(session, presenter, view)
            if not approved:
                counts = _overall_counts(artifact_set, holder.current)
                presenter.summary(
                    RunSummary(
                        outcome="aborted",
                        transcript_path=transcript.path,
                        gap_counts=artifacts.gap_counts(holder.current),
                        entry_counts=counts,
                        discarded=True,
                    )
                )
                return 3
            phase_status[phase] = _PhaseStatus.FROZEN
            _log(run_state, presenter, {"event": "phase_frozen", "phase": int(phase)})

        # Approval gate (orchestrator.md, Approval gate): the queue is empty and no
        # amendment unit is open (T2.3 never opens one), so this approval is final
        # confirmation, gated on the semantic check.
        violations = artifacts.semantic_violations(holder.current)
        if violations:
            described = "; ".join(
                f"{violation.kind.value} ({', '.join(violation.entry_ids)})"
                for violation in violations
            )
            raise SemanticGateFailureError(
                cause=(
                    "the final approval gate found semantic-invariant violations that "
                    f"the amendment mechanism would ordinarily repair: {described}"
                ),
                next_action=(
                    "This build does not yet implement automatic repair (a later task); "
                    "adjust the run's guidance and re-run blare analyze."
                ),
            )
        _log(run_state, presenter, {"event": "gate_passed"})

        # Write path (R20): the write-time re-check, then the three write
        # primitives in order, state last.
        if not repo.tree_matches(end_sha, ".blare"):
            raise WriteTimeRecheckError(
                cause=(
                    "the working tree outside .blare/ changed since this run started; "
                    "what was analyzed no longer matches the repository"
                ),
                next_action="Re-run blare analyze against the current commit.",
            )
        if not artifacts.raw_bytes_match(blare_root, holder.current):
            raise WriteTimeRecheckError(
                cause="the canonical YAML under .blare/ changed since this run loaded it",
                next_action="Re-run blare analyze; do not hand-edit .blare/ during a run.",
            )

        def _do_write() -> None:
            report = artifacts.write_entries_and_config(blare_root, holder.current)
            _log(
                run_state,
                presenter,
                {
                    "event": "write_report",
                    "primitive": "write_entries_and_config",
                    "written": [str(p) for p in report.written],
                    "skipped": [str(p) for p in report.skipped],
                },
            )
            report = artifacts.write_docs(blare_root, holder.current)
            _log(
                run_state,
                presenter,
                {
                    "event": "write_report",
                    "primitive": "write_docs",
                    "written": [str(p) for p in report.written],
                    "skipped": [str(p) for p in report.skipped],
                },
            )
            report = artifacts.write_state(blare_root, holder.current, end_sha)
            _log(
                run_state,
                presenter,
                {
                    "event": "write_report",
                    "primitive": "write_state",
                    "written": [str(p) for p in report.written],
                    "skipped": [str(p) for p in report.skipped],
                },
            )

        sigint_deferred = _write_with_sigint_masked(_do_write)
        if sigint_deferred:
            _log(run_state, presenter, {"event": "sigint_deferred_during_write"})
    finally:
        session.close()

    counts = _overall_counts(artifact_set, holder.current)
    presenter.summary(
        RunSummary(
            outcome="analysis complete",
            transcript_path=transcript.path,
            gap_counts=artifacts.gap_counts(holder.current),
            entry_counts=counts,
        )
    )
    return 0


@dataclass(frozen=True)
class _DiscardedSummaryFields:
    """The three `RunSummary` fields `_discarded_summary_fields` computes, bundled so
    its caller can splice them into a `RunSummary(...)` call without an untyped
    `**dict` (which mypy --strict cannot check against the dataclass's field types)."""

    gap_counts: artifacts.GapSummary | None
    entry_counts: EntryCounts | None
    discarded: bool


def _discarded_summary_fields(run_state: _RunState) -> _DiscardedSummaryFields:
    """`gap_counts`/`entry_counts`/`discarded=True` for a non-writing ending's summary
    (abort or a post-preflight failure), when the phase engine had already built a
    pending candidate to count -- absent otherwise (a pre-session ending has no
    candidate and renders no summary at all, per the caller). Computing the counts
    is best-effort: this runs from inside an exception handler already reporting a
    failure or an abort (a `KeyboardInterrupt` included -- another one arriving while
    merely computing diagnostic counts, e.g. a second signal, is exactly the kind of
    second fault this must survive), and it must never itself crash that reporting
    path or replace the original event with an unrelated one; it degrades to a bare
    `discarded` summary instead. Catching `BaseException` rather than `Exception` is
    deliberate here for that reason, not an oversight.
    """
    if run_state.holder is None or run_state.initial_set is None:
        return _DiscardedSummaryFields(gap_counts=None, entry_counts=None, discarded=False)
    try:
        return _DiscardedSummaryFields(
            gap_counts=artifacts.gap_counts(run_state.holder.current),
            entry_counts=_overall_counts(run_state.initial_set, run_state.holder.current),
            discarded=True,
        )
    except BaseException:  # noqa: BLE001 - best-effort diagnostic counts, never fatal here
        return _DiscardedSummaryFields(gap_counts=None, entry_counts=None, discarded=True)


def run(mode: RunMode, repo_path: Path, presenter: Presenter) -> int:
    """Blare's one entry contract: run a mode against a repo, render through presenter.

    Exit-code taxonomy (orchestrator.md), assigned by run stage, not by which module
    raised: `0` success (including R7 up-to-date); `1` refusal -- any `BlareError`
    raised before preflight completes (step 9's auth check succeeding); `2` -- a
    `BlareError` raised after preflight completes (the analyze phase engine's own
    `SemanticGateFailureError`/`WriteTimeRecheckError`, an `AgentSessionError` mid-
    phase, or a `WriteError` from a write primitive all land here) or any
    unexpected (non-`BlareError`) exception, whatever stage it strikes (the
    architecture's non-module carve-out); `3` a user abort (SIGINT, or an `Abort`
    reply at a checkpoint -- the phase engine returns 3 directly for the latter,
    never via this exception handler). Argparse usage errors are cli's own
    carve-out, not this function's concern.

    A SIGINT reaching this handler is distinguished by whether a session had
    started (R14's own definition of "a session ran", step 9 succeeding):
    pre-session, rendered as a single `aborted` notice with no summary and no
    error, since no artifacts or counts exist to summarize; and session-bearing
    (during preflight's step 9 auth call, or during the analyze phase engine
    outside the write path's masked window -- see `_write_with_sigint_masked`),
    which renders a summary naming the transcript path and, once the phase engine
    has built a pending candidate, real discarded entry/gap counts (via
    `_discarded_summary_fields`), per orchestrator.md: "Abort exits 3, writing
    nothing (R20), the summary still naming the transcript path (R14 -- a session
    ran)." A checkpoint reply of `Abort` is a distinct path from a SIGINT here: it
    is handled inline in the phase engine (never raised as `KeyboardInterrupt`),
    which is why this handler's own summary-building can only ever see the
    SIGINT variant.
    """
    run_state = _RunState()
    try:
        return _execute(mode, repo_path, presenter, run_state)
    except KeyboardInterrupt:
        if run_state.transcript_path is not None:
            fields = _discarded_summary_fields(run_state)
            presenter.summary(
                RunSummary(
                    outcome="aborted",
                    transcript_path=run_state.transcript_path,
                    gap_counts=fields.gap_counts,
                    entry_counts=fields.entry_counts,
                    discarded=fields.discarded,
                )
            )
        else:
            presenter.notice("aborted")
        return 3
    except BlareError as exc:
        presenter.error(cause=exc.cause, next_action=exc.next_action)
        stage = "failure" if run_state.preflight_complete else "refusal"
        _log(run_state, presenter, {"event": stage, "cause": exc.cause})
        if run_state.preflight_complete:
            # orchestrator.md, Error handling: "Every exit-2 session-bearing ending
            # then renders the summary -- outcome failed, discarded counts,
            # transcript path".
            fields = _discarded_summary_fields(run_state)
            presenter.summary(
                RunSummary(
                    outcome="failed",
                    transcript_path=run_state.transcript_path,
                    gap_counts=fields.gap_counts,
                    entry_counts=fields.entry_counts,
                    discarded=fields.discarded,
                )
            )
        return 2 if run_state.preflight_complete else 1
    except Exception as exc:  # noqa: BLE001 - the architecture's non-module carve-out
        detail = traceback.format_exc()
        cause = f"unexpected error: {exc}"
        next_action = "Re-run; if this persists, report the detail below."
        if run_state.run_log is not None:
            # The run log exists (step 2 completed): the traceback is preserved
            # there instead of duplicated on stderr.
            _log(run_state, presenter, {"event": "unexpected_exception", "traceback": detail})
            presenter.error(cause=cause, next_action=next_action)
        else:
            # Before the run log exists (step 1, or step 2's own failure): print the
            # traceback beneath the rendered cause instead.
            presenter.error(cause=cause, next_action=next_action, detail=detail)
        return 2
    finally:
        if run_state.lock is not None:
            run_state.lock.release()
