"""The run lifecycle (architecture): the only module that coordinates the others.

T2.2 scope (`engineering/modules/orchestrator.md`): the full nine-step preflight
sequence, the lock, the run log, and the exit-code taxonomy. Steps 5-6 (update-only:
SHA ancestry, the R7 empty-delta short-circuit) are wired for real here; T3.1 supplies
their e2e coverage alongside the rest of update mode.

T2.3 scope: the analyze-mode phase engine -- four phases in order, each opening a
phase, running it via `AgentSession.run_phase`, presenting a `CheckpointView` and
looping chat to a terminal reply -- the final approval gate
(`artifacts.semantic_violations`), and the write path (the R20 re-check, then the
three write primitives in order, state last).

T2.4 scope: the amendment mechanism -- unit mechanics (agent-origin via
`amend_proposal`/`amend_complete`, system-origin from a failed approval gate), the
frozen-only cascade (`artifacts.referencing_phases` union `semantic_violations`'s
repair phases, restricted to frozen phases), the closure loop (`_advance_unit`,
looping `AgentSession.request_repair` calls until a recompute adds nothing), one
re-presentation per closure (`_present_amendment_once`), and outcome notification
(`AgentSession.notify_amendment_outcome`). The final approval gate opens a
system-originated unit on a semantic violation instead of raising.

T3.1 scope (this task): diff mode's post-preflight flow -- `AgentSession.triage`
consumes the effective delta and answers with an `affected_verdict` (seeding the
queue) or a `no_impact` conclusion (`_handle_affected_verdict`/`_handle_no_impact`);
`_drain_phase_queue` runs exactly the queue's phases, in phase order, reusing
`_run_checkpoint` and the amendment machinery rather than a parallel
implementation; `_run_no_impact_checkpoint` presents the R18 no-impact
confirmation; `_finalize_and_write` (the approval gate plus the write path,
factored out of what was previously only the analyze tail) drains the queue
again at the top of its own gate loop -- so a phase a gate-opened system unit
leaves `open` (joined from `unvisited`) still gets its ordinary checkpoint
before the write -- and is shared by both modes' final confirmation, which is
what makes R18's SHA-only advance the *same*
write path over an unchanged candidate rather than a separate one.

T3.2 scope (this task): R18's dynamic clauses in full, plus R15's refusal
e2e coverage (the code itself already existed from T2.2). Dynamic phase-queue
expansion -- a revised `affected_verdict` opening a phase ahead of or behind
the run's position, mid-phase or mid-chat -- needed no new mechanism: it was
already correct via the same `_handle_affected_verdict`/`_drain_phase_queue`
plumbing T3.1 built (the queue is re-read from `phase_status` at the top of
every drain iteration), so this task's contribution there is test coverage,
not code. Two things genuinely needed building: `_repair_residual_violations`
(called right after triage) proactively opens a system-originated unit for
whatever semantic violations the candidate still carries -- recomputed fresh
rather than reusing step 7's snapshot, since a triage-time agent amendment may
already have fixed it -- because `request_repair` is the only channel that
can ever tell the model about a load-seeded violation (agent.md: "loaded-state
violations do not travel in RunContext"). And `_run_no_impact_checkpoint` now
implements R18's full redirect: a mid-chat run-control call that opens a phase
or a unit withdraws the no-impact conclusion via `prompt=None` (mooting the
in-progress reply rather than re-offering a now-stale conclusion), resolves
any opened unit, and either lets the caller's queue drain take over (something
durably opened) or re-presents the conclusion fresh (a redirecting unit was
rejected, restoring exactly the state the conclusion was based on).

The `Presenter` protocol below mirrors `cli.md`'s `TerminalPresenter` interface in
full so `cli.TerminalPresenter` type-checks against it.
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
    EditOp,
    Phase,
    RunContext,
    RunControlAction,
    RunControlCall,
    RunControlVerdict,
    RunMode,
    Violation,
)

__all__ = [
    "Abort",
    "AmendmentOrigin",
    "AmendmentPhaseSection",
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


class AmendmentOrigin(Enum):
    """Which of the amendment mechanism's two origins produced a unit (architecture:
    "one mechanism, two origins")."""

    AGENT = "agent"
    SYSTEM = "system"


@dataclass(frozen=True)
class AmendmentPhaseSection:
    """One involved phase's changed entries within an amendment unit (T2.4) -- the
    same `EntryChange` shape `CheckpointView` uses, diffed against the unit's own
    pre-amendment baseline rather than a phase's checkpoint-opening baseline."""

    phase: Phase
    added: tuple[EntryChange, ...] = ()
    updated: tuple[EntryChange, ...] = ()
    removed: tuple[EntryChange, ...] = ()


@dataclass(frozen=True)
class AmendmentView:
    """An amendment unit's changed entries, grouped by phase (T2.4; architecture:
    "the unit's changed entries grouped per involved phase, plus origin"). Always
    reflects the *current* changed set: re-presented once per closure, so a
    re-presentation following further chat-driven repairs carries a fresh view."""

    origin: AmendmentOrigin
    sections: tuple[AmendmentPhaseSection, ...]
    gap_counts: artifacts.GapSummary


@dataclass(frozen=True)
class NoImpactView:
    """The R18 no-impact conclusion's delta summary and the agent's conclusion
    text (T3.1; cli.md: "the delta summary from NoImpactView (changed-file count
    and list), the agent's conclusion text")."""

    delta_file_count: int
    delta_files: tuple[str, ...]
    conclusion: str


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

# A run-log sink bound to this run's `_RunState`/`presenter` (orchestrator.md,
# Failure visibility: the run log records "amendment units, gate results"
# alongside preflight/phase events) -- threaded into the amendment machinery
# below so a unit opening, a cascade join, a unit's closure, and a gate check
# are all as visible in the run log as an ordinary phase transition.
_Log = Callable[[dict[str, object]], None]


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


# --- Phase engine: the pending edit set, the sink/control handlers, and
# checkpoint-view rendering support ---------------------------------------------------


class _PhaseStatus(Enum):
    """A phase's state (architecture: "Phase states"): `unvisited -> open -> frozen`
    in the ordinary run, or re-opened from either `frozen` or `unvisited` by the
    amendment mechanism (T2.4), which is the only path that can move a phase
    backwards (frozen -> open) or open one out of order (unvisited -> open ahead of
    the run's position)."""

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


@dataclass
class _AmendmentUnit:
    """One open amendment unit (T2.4; orchestrator.md, Amendments): tracks origin,
    every phase it has opened (mapped to that phase's status *before* the unit
    opened it -- frozen or unvisited, the restore/re-freeze target), the ids
    changed by repairs landing in the unit's own phases (the reference half of the
    blast-radius recompute), and the phases already told about via
    `AgentSession.request_repair` (`announced`) so a later call only ever reports
    the delta.

    `baseline` is the *whole* candidate captured once, at unit inception, before
    any phase was opened -- sufficient as the byte-for-byte restore/checkpoint
    baseline for *every* phase the unit ever comes to include: a phase can only
    join the unit from frozen or unvisited, both of which are untouchable by the
    sink until they join, so none of them could have changed before `baseline` was
    captured (see the design note this task's report carries in full).

    `dirty` is set whenever something happens that a recompute might care about
    (a batch lands in one of the unit's phases, or a phase joins) and consumed by
    `_advance_unit`'s recompute step -- this is deliberately independent of
    `amend_complete_received`: the latter only ever matters once, to detect
    whether the *very first* driving turn already completed the repair itself
    (agent.md's same-turn case) before this unit has ever been announced via
    `request_repair`; every later recompute (including ones `_present_amendment_once`
    triggers from re-presentation chat, which carries no `amend_complete` contract
    at all) is driven by `dirty`, not by the completion signal.
    """

    origin: AmendmentOrigin
    baseline: artifacts.ArtifactSet
    opened_from: dict[Phase, _PhaseStatus] = dataclasses.field(default_factory=dict)
    changed_ids: set[str] = dataclasses.field(default_factory=set)
    announced: set[Phase] = dataclasses.field(default_factory=set)
    amend_complete_received: bool = False
    dirty: bool = False
    # The violations that justified opening each not-yet-announced phase (empty
    # for a phase opened by agent_proposal, or by cascade through pure reference
    # invalidation) -- looked up, never recomputed, when a phase is finally
    # announced, so a cascade round's violations survive regardless of unit
    # origin (architecture.md: "violations carried when the join came from the
    # invariant half, empty for pure reference invalidation" -- a rule about
    # *how the phase joined*, not about who owns the unit).
    pending_violations: dict[Phase, tuple[Violation, ...]] = dataclasses.field(
        default_factory=dict
    )


@dataclass
class _UnitHolder:
    """The run's at-most-one open amendment unit, mutated by replacement (mirrors
    `_CandidateHolder`) -- shared by the sink, the control handler, and the phase
    engine's own unit-resolution code."""

    current: _AmendmentUnit | None = None


@dataclass
class _NoImpactHolder:
    """T3.1: the run's standing `no_impact` conclusion, if the control handler has
    accepted one (`orchestrator.md`'s "No-impact flow (R18)"). `None` until
    accepted; read by the update-mode driver right after `AgentSession.triage`
    returns to decide whether to present the no-impact confirmation or run the
    phase engine over the (then necessarily non-empty) queue."""

    conclusion: str | None = None


def _mark_phase_open(
    phase: Phase,
    phase_status: dict[Phase, _PhaseStatus],
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
) -> None:
    """Transition `phase` out of `unvisited`/`frozen` into `open`, capturing its
    checkpoint-diff baseline the *first* time it ever leaves `unvisited` -- whether
    that happens via the ordinary phase loop or the amendment mechanism naming it
    ahead of the run's position. Re-opening an already-visited (frozen) phase never
    touches `phase_baselines`: that phase's one ordinary `CheckpointView` already
    fired and will not fire again: the amendment's own `AmendmentView` is what shows
    its changes from here on."""
    if phase not in phase_baselines:
        phase_baselines[phase] = holder.current
    phase_status[phase] = _PhaseStatus.OPEN


def _batch_touched_ids(batch: EditBatch) -> set[str]:
    """The id(s) one accepted `EditBatch` added, updated, or removed -- the
    reference half of an amendment's blast-radius recompute needs exactly these,
    not a before/after diff (`_phase_diff` computes content for display; this is
    cheaper and is all `referencing_phases` needs)."""
    ids: set[str] = set()
    for edit in batch.edits:
        if edit.entry_type == "coverage":
            payload = edit.payload_or_id
            if isinstance(payload, dict):
                fm_id = payload.get("failure_mode_id")
                if isinstance(fm_id, str):
                    ids.add(fm_id)
            continue
        if edit.op is EditOp.REMOVE:
            target = edit.payload_or_id
            if isinstance(target, str):
                ids.add(target)
        else:
            payload = edit.payload_or_id
            if isinstance(payload, dict):
                id_ = payload.get("id")
                if isinstance(id_, str):
                    ids.add(id_)
    return ids


def _make_sink(
    holder: _CandidateHolder,
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
) -> agent.EditSink:
    """The edit sink (architecture: Edit-proposal protocol): enforces the phase-state
    rule (this module's own), then artifacts' per-batch content check; an accepted
    batch's candidate replaces the holder's current set. When an amendment unit is
    open and the batch targets one of *its* phases, the touched ids join the unit's
    `changed_ids` -- the reference half of the next recompute (T2.4)."""

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
        unit = unit_holder.current
        if unit is not None and batch.phase in unit.opened_from:
            unit.changed_ids.update(_batch_touched_ids(batch))
            unit.dirty = True
        return verdict

    return sink


def _handle_amend_proposal(
    payload: Mapping[str, object],
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    log: _Log,
) -> RunControlVerdict:
    """`amend_proposal`: opens every named phase not already open, starting a unit
    if none is open yet or joining the open one (architecture: "join-over-reject
    precedence" -- an already-open named phase is simply a no-op within it).
    Rejected as a verdict when *every* named phase is already open (orchestrator.md:
    "those phases are open -- just edit them")."""
    phases_raw = payload.get("phases")
    if not isinstance(phases_raw, list) or not phases_raw:
        return RunControlVerdict(
            ok=False, message="amend_proposal requires a non-empty 'phases' list"
        )
    try:
        phases = [Phase(int(p)) for p in phases_raw]
    except (ValueError, TypeError):
        return RunControlVerdict(
            ok=False, message=f"amend_proposal names invalid phase(s): {phases_raw!r}"
        )
    non_open = [
        p for p in phases if phase_status.get(p, _PhaseStatus.UNVISITED) is not _PhaseStatus.OPEN
    ]
    if not non_open:
        return RunControlVerdict(
            ok=False,
            message=(
                "named phase(s) are already open -- edit them directly instead of "
                "proposing an amendment"
            ),
        )
    unit = unit_holder.current
    starting_unit = unit is None
    if unit is None:
        unit = _AmendmentUnit(origin=AmendmentOrigin.AGENT, baseline=holder.current)
        unit_holder.current = unit
    joined: list[Phase] = []
    for p in phases:
        if phase_status.get(p, _PhaseStatus.UNVISITED) is not _PhaseStatus.OPEN:
            unit.opened_from[p] = phase_status.get(p, _PhaseStatus.UNVISITED)
            _mark_phase_open(p, phase_status, phase_baselines, holder)
            joined.append(p)
    log(
        {
            "event": "amendment_unit_opened" if starting_unit else "amendment_phase_joined",
            "origin": unit.origin.value,
            "phases": [int(p) for p in joined],
        }
    )
    return RunControlVerdict(ok=True, message="phase(s) opened for the amendment")


def _handle_amend_complete(unit_holder: _UnitHolder) -> RunControlVerdict:
    """`amend_complete`: rejected as a verdict when no unit is open; otherwise marks
    the unit's current repair round complete, unblocking whichever
    `AgentSession.request_repair` call is awaiting it and letting the phase
    engine's closure loop (`_advance_unit`) run the next recompute."""
    unit = unit_holder.current
    if unit is None:
        return RunControlVerdict(ok=False, message="amend_complete with no amendment unit open")
    unit.amend_complete_received = True
    return RunControlVerdict(ok=True, message="amend_complete acknowledged")


def _handle_affected_verdict(
    payload: Mapping[str, object],
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    log: _Log,
) -> RunControlVerdict:
    """`affected_verdict` (update mode; R18): seeds the queue with every named
    phase that is still unvisited -- architecture.md's "a run-control verdict
    marking an unvisited phase affected" -- via the same `_mark_phase_open` the
    ordinary phase loop and the amendment mechanism both use, so a triage-seeded
    phase's checkpoint baseline is captured exactly like an amendment-opened
    ahead phase's. Total per orchestrator.md's run-control rules: a named phase
    already open is a no-op acknowledgment; one already frozen is rejected,
    directing the agent to `amend_proposal` instead; naming an unvisited phase
    while a unit is open is rejected too (close the unit first -- unit tracking
    stays free of concurrent non-unit openings)."""
    phases_raw = payload.get("phases")
    if not isinstance(phases_raw, list) or not phases_raw:
        return RunControlVerdict(
            ok=False, message="affected_verdict requires a non-empty 'phases' list"
        )
    try:
        phases = [Phase(int(p)) for p in phases_raw]
    except (ValueError, TypeError):
        return RunControlVerdict(
            ok=False, message=f"affected_verdict names invalid phase(s): {phases_raw!r}"
        )
    frozen_named = [
        p for p in phases if phase_status.get(p, _PhaseStatus.UNVISITED) is _PhaseStatus.FROZEN
    ]
    if frozen_named:
        names = ", ".join(str(int(p)) for p in sorted(frozen_named, key=int))
        return RunControlVerdict(
            ok=False,
            message=(
                f"phase(s) {names} are already frozen -- use amend_proposal to "
                "reopen them instead of affected_verdict"
            ),
        )
    unvisited_named = [
        p for p in phases if phase_status.get(p, _PhaseStatus.UNVISITED) is _PhaseStatus.UNVISITED
    ]
    if unit_holder.current is not None and unvisited_named:
        return RunControlVerdict(
            ok=False,
            message=(
                "an amendment unit is open -- call amend_complete to close it "
                "before revising the affected-phase verdict"
            ),
        )
    for p in unvisited_named:
        _mark_phase_open(p, phase_status, phase_baselines, holder)
    log(
        {
            "event": "affected_verdict",
            "phases": [int(p) for p in phases],
            "opened": [int(p) for p in unvisited_named],
        }
    )
    return RunControlVerdict(ok=True, message="phase(s) noted as affected")


def _handle_no_impact(
    payload: Mapping[str, object],
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    load_seeded_violations: list[Violation],
    no_impact_holder: _NoImpactHolder,
    log: _Log,
) -> RunControlVerdict:
    """`no_impact` (update mode; R18): accepted -- recording the conclusion for
    the R18 confirmation the update driver presents once `triage` returns -- only
    when no unit is open and the affected-phase queue is empty, where "empty"
    counts both a triage-opened phase *and* a load-time semantic-violation seed
    from preflight step 7 (orchestrator.md: "a no_impact conclusion with a
    non-empty queue... is rejected back to the agent... the seeded phases still
    need work"). This check only needs the raw step-7 snapshot to make the
    queue non-empty; actually opening and repairing a load-seeded violation's
    phase is `_repair_residual_violations` (T3.2), called by the update driver
    right after `triage()` returns, before this handler could even see another
    `no_impact` attempt."""
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        return RunControlVerdict(
            ok=False, message="no_impact requires a non-empty 'reasoning' string"
        )
    if unit_holder.current is not None:
        return RunControlVerdict(
            ok=False,
            message="an amendment unit is open -- call amend_complete to close it first",
        )
    queue_nonempty = any(status is _PhaseStatus.OPEN for status in phase_status.values())
    if queue_nonempty or load_seeded_violations:
        return RunControlVerdict(
            ok=False,
            message=(
                "phase(s) are already queued and still need work -- address them "
                "(or call affected_verdict) instead of concluding no impact"
            ),
        )
    no_impact_holder.conclusion = reasoning
    log({"event": "no_impact_accepted"})
    return RunControlVerdict(ok=True, message="no-impact conclusion recorded")


def _make_control_handler(
    mode: RunMode,
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    log: _Log,
    no_impact_holder: _NoImpactHolder,
    load_seeded_violations: list[Violation],
) -> agent.RunControlHandler:
    """The run-control handler (architecture: Run-control channel). `amend_proposal`
    and `amend_complete` are real (T2.4); `affected_verdict`/`no_impact` are real in
    update mode (T3.1) and stay rejected in analyze mode (diff-mode-only, R18) per
    the architecture's "Run-control handling is total" rule: never a raise, always
    a verdict the model can act on."""

    def control(call: RunControlCall) -> RunControlVerdict:
        if call.action is RunControlAction.AMEND_PROPOSAL:
            return _handle_amend_proposal(
                call.payload, phase_status, unit_holder, phase_baselines, holder, log
            )
        if call.action is RunControlAction.AMEND_COMPLETE:
            return _handle_amend_complete(unit_holder)
        if call.action is RunControlAction.AFFECTED_VERDICT:
            if mode is RunMode.ANALYZE:
                return RunControlVerdict(
                    ok=False,
                    message=(
                        "affected_verdict is a diff-mode verdict (R18); this is a "
                        "full analysis run -- work through all four phases instead"
                    ),
                )
            return _handle_affected_verdict(
                call.payload, phase_status, unit_holder, phase_baselines, holder, log
            )
        if call.action is RunControlAction.NO_IMPACT:
            if mode is RunMode.ANALYZE:
                return RunControlVerdict(
                    ok=False,
                    message=(
                        "no_impact is a diff-mode verdict (R18); this is a full "
                        "analysis run -- work through all four phases instead"
                    ),
                )
            return _handle_no_impact(
                call.payload,
                phase_status,
                unit_holder,
                load_seeded_violations,
                no_impact_holder,
                log,
            )
        return RunControlVerdict(  # pragma: no cover - every action handled above
            ok=False, message=f"{call.action.value} is not a recognized run-control action"
        )

    return control


def _advance_unit(
    unit_holder: _UnitHolder,
    phase_status: dict[Phase, _PhaseStatus],
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    session: agent.AgentSession,
    log: _Log,
) -> None:
    """Drive the open unit to a state ready for presentation (orchestrator.md,
    Amendments). Two independent conditions can each require another round:

    - `pending` -- phases opened but never yet reported to the model via
      `request_repair`. Non-empty exactly after a phase joins (initial open or
      cascade), each carrying whatever violations justified its inclusion (see
      `pending_violations` on `_AmendmentUnit`; empty for an agent proposal or a
      pure-reference cascade join). The one exception is the unit's very first
      round when the proposing turn already called `amend_complete` itself
      before this ever ran (a legal same-turn propose-repair-complete sequence,
      agent.md): the model already knows, having proposed it, so no
      announcement is sent, only recorded as such. Announcing calls
      `request_repair`, which itself guarantees `amend_complete` arrives before
      returning (or raises) -- nothing here needs its own reminder/retry logic.
    - `dirty` -- something changed since the last recompute (a batch landed in
      one of the unit's phases, tracked independently of `amend_complete`
      because re-presentation chat, which can also land batches, carries no
      such completion contract at all). Consumed by the recompute: the union of
      `artifacts.referencing_phases` over the unit's changed ids and the repair
      phases of `artifacts.semantic_violations`, restricted to *frozen* phases,
      opens any newly implicated phase (storing the violations that justified
      it, if any) -- which then becomes `pending` again, looping back to
      announce it.

    Returns once neither condition holds -- the unit is ready for presentation.
    A no-op if already ready (safe to call unconditionally after any driving
    call or chat exchange, including a re-presentation chat that changed
    nothing)."""
    unit = unit_holder.current
    assert unit is not None
    while True:
        pending = tuple(sorted(set(unit.opened_from) - unit.announced, key=int))
        if pending:
            same_turn_complete = not unit.announced and unit.amend_complete_received
            unit.announced.update(pending)
            if not same_turn_complete:
                violations = [v for p in pending for v in unit.pending_violations.get(p, ())]
                session.request_repair(list(pending), violations)
            continue
        if not unit.dirty:
            return
        unit.dirty = False
        violations = artifacts.semantic_violations(holder.current)
        ref_phases = artifacts.referencing_phases(holder.current, unit.changed_ids)
        violation_phases = {v.phase for v in violations}
        candidate_new = (ref_phases | violation_phases) - set(unit.opened_from)
        new_phases = sorted(
            (p for p in candidate_new if phase_status[p] is _PhaseStatus.FROZEN), key=int
        )
        if not new_phases:
            return
        for p in new_phases:
            unit.opened_from[p] = phase_status[p]
            unit.pending_violations[p] = tuple(v for v in violations if v.phase is p)
            _mark_phase_open(p, phase_status, phase_baselines, holder)
        log(
            {
                "event": "amendment_cascade_joined",
                "phases": [int(p) for p in new_phases],
                "violation_count": sum(len(unit.pending_violations[p]) for p in new_phases),
            }
        )
        # loop: the phases just opened are now `pending` and get announced at
        # the top, carrying the violations just stored for them.


def _build_amendment_view(unit: _AmendmentUnit, current: artifacts.ArtifactSet) -> AmendmentView:
    """The unit's current changed set, grouped per involved phase (T2.4), diffed
    against the unit's own pre-amendment baseline via the same `_phase_diff` a
    `CheckpointView` uses."""
    sections = tuple(
        AmendmentPhaseSection(
            phase=phase,
            added=added,
            updated=updated,
            removed=removed,
        )
        for phase in sorted(unit.opened_from, key=int)
        for added, updated, removed in (_phase_diff(phase, unit.baseline, current),)
    )
    return AmendmentView(
        origin=unit.origin, sections=sections, gap_counts=artifacts.gap_counts(current)
    )


def _restore_from_baseline(
    current: artifacts.ArtifactSet,
    baseline: artifacts.ArtifactSet,
    opened_from: Mapping[Phase, _PhaseStatus],
) -> artifacts.ArtifactSet:
    """Byte-for-byte restore of every phase in `opened_from`'s owned entries back to
    `baseline`, leaving every other phase's entries untouched (a rejected unit's
    restore, T2.4). Coverage -- mechanical, spanning phases 3-4 by side -- is
    reconciled afterward: its keys follow the (possibly-restored) failure-mode set,
    and each side reverts to `baseline` only when its owning phase is in
    `opened_from`, the other side kept at its current value."""
    system_components = (
        dict(baseline.system_components)
        if Phase.SYSTEM_MAP in opened_from
        else current.system_components
    )
    failure_modes = (
        dict(baseline.failure_modes)
        if Phase.FAILURE_MODES in opened_from
        else current.failure_modes
    )
    restore_metric_side = Phase.METRIC_COVERAGE in opened_from
    restore_alert_side = Phase.ALERT_RECOMMENDATIONS in opened_from
    metrics = dict(baseline.metrics) if restore_metric_side else current.metrics
    metric_recommendations = (
        dict(baseline.metric_recommendations)
        if restore_metric_side
        else current.metric_recommendations
    )
    alert_recommendations = (
        dict(baseline.alert_recommendations)
        if restore_alert_side
        else current.alert_recommendations
    )
    new_coverage: dict[str, artifacts.CoverageEntry] = {}
    for fm_id in failure_modes:
        base_entry = baseline.coverage.get(fm_id)
        cur_entry = current.coverage.get(fm_id)
        # A failure mode restored back into existence (removed by the unit, now
        # reappearing because FAILURE_MODES is in opened_from) has no
        # `cur_entry` at all -- mechanical coverage completeness deleted it
        # alongside the removal. There is nothing "current" to preserve for
        # either side in that case regardless of which phases are in
        # opened_from: both sides must come from baseline, the only place its
        # pre-amendment coverage still exists, or this restore would silently
        # drop it instead of reviving it byte-for-byte.
        if cur_entry is None:
            metric_ids, metric_rec_ids = (
                (base_entry.detecting_metric_ids, base_entry.metric_recommendation_ids)
                if base_entry is not None
                else ((), ())
            )
            alert_ids = base_entry.alert_ids if base_entry is not None else ()
        else:
            if restore_metric_side:
                metric_ids, metric_rec_ids = (
                    (base_entry.detecting_metric_ids, base_entry.metric_recommendation_ids)
                    if base_entry is not None
                    else ((), ())
                )
            else:
                metric_ids, metric_rec_ids = (
                    cur_entry.detecting_metric_ids,
                    cur_entry.metric_recommendation_ids,
                )
            if restore_alert_side:
                alert_ids = base_entry.alert_ids if base_entry is not None else ()
            else:
                alert_ids = cur_entry.alert_ids
        new_coverage[fm_id] = artifacts.CoverageEntry(
            failure_mode_id=fm_id,
            detecting_metric_ids=metric_ids,
            metric_recommendation_ids=metric_rec_ids,
            alert_ids=alert_ids,
        )
    return dataclasses.replace(
        current,
        system_components=system_components,
        failure_modes=failure_modes,
        metrics=metrics,
        metric_recommendations=metric_recommendations,
        alert_recommendations=alert_recommendations,
        coverage=new_coverage,
    )


def _close_unit(
    unit_holder: _UnitHolder,
    phase_status: dict[Phase, _PhaseStatus],
    holder: _CandidateHolder,
    session: agent.AgentSession,
    log: _Log,
    *,
    approved: bool,
) -> None:
    """Close the open unit (T2.4): approval re-freezes exactly the phases that were
    frozen when the unit opened (a phase opened from unvisited stays open, taking
    its ordinary checkpoint later -- opening a phase for a repair never substitutes
    for running it); rejection restores every phase's pre-amendment state and phase
    status. Either way, `AgentSession.notify_amendment_outcome` closes the loop on
    the model's side."""
    unit = unit_holder.current
    assert unit is not None
    restored: list[Phase] = []
    if approved:
        for p, prior in unit.opened_from.items():
            if prior is _PhaseStatus.FROZEN:
                phase_status[p] = _PhaseStatus.FROZEN
    else:
        holder.current = _restore_from_baseline(holder.current, unit.baseline, unit.opened_from)
        for p, prior in unit.opened_from.items():
            phase_status[p] = prior
            restored.append(p)
    unit_holder.current = None
    log(
        {
            "event": "amendment_unit_closed",
            "origin": unit.origin.value,
            "approved": approved,
            "phases": [int(p) for p in sorted(unit.opened_from, key=int)],
            "restored_phases": sorted(int(p) for p in restored),
        }
    )
    session.notify_amendment_outcome(approved=approved, restored_phases=sorted(restored, key=int))


def _present_amendment_once(
    unit_holder: _UnitHolder,
    phase_status: dict[Phase, _PhaseStatus],
    holder: _CandidateHolder,
    session: agent.AgentSession,
    presenter: Presenter,
    log: _Log,
) -> None:
    """Present the (closure-ready) unit once and drive its reply to a terminal
    outcome: Approve/Reject close it (`_close_unit`); Abort unwinds via
    `_AbortRun`; Chat routes through `session.chat`, and if that turn actually
    changed anything (a batch landed in a unit phase, or a further phase joined),
    returns *without* closing so the caller's closure loop
    (`_resolve_unit_to_presentation`) can recompute and re-present a fresh view --
    "once" means once per closure, never a mid-chat redraw of the same view."""
    unit = unit_holder.current
    assert unit is not None
    rejectable = unit.origin is AmendmentOrigin.AGENT
    view = _build_amendment_view(unit, holder.current)
    reply: AmendmentReply = presenter.present_amendment(view, rejectable)
    while True:
        if isinstance(reply, Approve):
            _close_unit(unit_holder, phase_status, holder, session, log, approved=True)
            return
        if isinstance(reply, Abort):
            raise _AbortRun
        if isinstance(reply, Reject):
            if not rejectable:
                raise AssertionError(
                    "Reject returned for a non-rejectable (system-originated) "
                    "amendment prompt -- a protocol violation"
                )
            _close_unit(unit_holder, phase_status, holder, session, log, approved=False)
            return
        if isinstance(reply, Chat):
            before_phases = set(unit.opened_from)
            chat_reply_text = session.chat(reply.text)
            kind = PromptKind.REJECTABLE_AMENDMENT if rejectable else PromptKind.AMENDMENT
            next_reply = presenter.show_chat_reply(chat_reply_text, kind)
            assert next_reply is not None, (
                "an amendment prompt was given (not None); show_chat_reply must "
                "re-offer it and return the next reply"
            )
            if unit.dirty or set(unit.opened_from) != before_phases:
                # A batch landed in a unit phase, or a fresh amend_proposal
                # joined another one (architecture.md: "accepted batches and
                # joining proposals return the unit to the closure loop") --
                # hand back to `_resolve_unit_to_presentation`, which
                # re-advances and re-presents a fresh view; a plain
                # conversational reply with neither falls through to just
                # re-offering this same prompt below.
                return
            reply = next_reply
            continue
        raise AssertionError(f"unexpected amendment reply {reply!r}")  # pragma: no cover


def _resolve_unit_to_presentation(
    unit_holder: _UnitHolder,
    phase_status: dict[Phase, _PhaseStatus],
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    session: agent.AgentSession,
    presenter: Presenter,
    log: _Log,
) -> None:
    """Drive any open unit through the closure loop and its (possibly repeated)
    re-presentation until it closes (T2.4; orchestrator.md: "An open unit defers
    everything downstream of it"). A no-op if no unit is open."""
    while unit_holder.current is not None:
        _advance_unit(unit_holder, phase_status, phase_baselines, holder, session, log)
        _present_amendment_once(unit_holder, phase_status, holder, session, presenter, log)


def _open_system_unit(
    violations: list[Violation],
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    log: _Log,
) -> None:
    """Open a system-originated unit naming every violation's repair phase
    (architecture: Amendment mechanism). Two call sites (T3.2): a failed
    approval gate (`_finalize_and_write`), and update mode's own post-triage
    check for load-seeded violations (`_repair_residual_violations`) -- both
    just hand this whatever `artifacts.semantic_violations` currently reports,
    so this function itself does not care which. In analyze mode the gate call
    only ever fires once every phase has already run (the gate's own
    precondition), so every named phase is frozen, never unvisited. In update
    mode this is no longer guaranteed: a repair phase can still be `unvisited`
    (nothing has named it yet) or already `open` (a triage verdict opened it
    for an unrelated reason, but the model still needs telling about the
    violation -- request_repair is the only channel that can, since
    load-seeded violations never travel in RunContext). Whichever caller,
    `_mark_phase_open` is a no-op for a phase already open, and `_close_unit`'s
    approval branch only ever re-freezes a phase whose *recorded* prior status
    was frozen -- recording `open` as the prior status here is therefore
    harmless: approval leaves it open, exactly as it already was. The queue
    re-read at the top of `_drain_phase_queue`'s own loop is what gives every
    phase this unit opens (from unvisited or already-open alike) its own
    ordinary checkpoint before the write, never only the amendment's."""
    assert unit_holder.current is None
    unit = _AmendmentUnit(origin=AmendmentOrigin.SYSTEM, baseline=holder.current)
    for p in sorted({v.phase for v in violations}, key=int):
        unit.opened_from[p] = phase_status[p]
        unit.pending_violations[p] = tuple(v for v in violations if v.phase is p)
        _mark_phase_open(p, phase_status, phase_baselines, holder)
    unit_holder.current = unit
    log(
        {
            "event": "amendment_unit_opened",
            "origin": AmendmentOrigin.SYSTEM.value,
            "phases": sorted(int(p) for p in unit.opened_from),
            "violation_count": len(violations),
        }
    )


def _repair_residual_violations(
    holder: _CandidateHolder,
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    session: agent.AgentSession,
    presenter: Presenter,
    log: _Log,
) -> None:
    """T3.2: update mode's own post-triage check for R18's load-seeded violation
    repairs (agent.md: "in update mode the orchestrator calls it [request_repair]
    right after triage seeds the queue, naming the repair phases and
    violations"). Recomputes `artifacts.semantic_violations` fresh against the
    *current* candidate rather than reusing step 7's snapshot: a triage-time
    agent amendment (resolved by the caller just before this runs) may already
    have fixed what step 7 found, and reusing the stale list would re-request a
    repair that no longer exists. A no-op when nothing is currently violating.

    Assumes `unit_holder.current is None` (the caller resolves any unit triage
    itself opened before calling this -- `_open_system_unit`'s own precondition).
    Whatever residual violations remain get one system-originated unit, exactly
    like a failed approval gate's own repair (`_finalize_and_write`); any
    violation this doesn't catch (e.g. one newly introduced by a later phase's
    own work) is still caught by that gate at the end, so nothing is silently
    lost."""
    violations = artifacts.semantic_violations(holder.current)
    if not violations:
        return
    log(
        {
            "event": "load_seeded_violation_repair",
            "violation_count": len(violations),
            "kinds": [v.kind.value for v in violations],
        }
    )
    _open_system_unit(violations, phase_status, unit_holder, phase_baselines, holder, log)
    _resolve_unit_to_presentation(
        unit_holder, phase_status, phase_baselines, holder, session, presenter, log
    )


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


class _AbortRun(Exception):
    """Internal control-flow signal only: any `Abort` reply during the phase
    engine or amendment handling unwinds here, so the analyze block has exactly
    one abort-summary/exit path regardless of where the abort happened (an
    ordinary checkpoint, an amendment re-presentation, or a system amendment's).
    Never escapes `_execute`."""


def _run_checkpoint(
    session: agent.AgentSession,
    presenter: Presenter,
    build_view: Callable[[], CheckpointView],
    unit_holder: _UnitHolder,
    phase_status: dict[Phase, _PhaseStatus],
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    log: _Log,
) -> None:
    """Present one checkpoint and drive its chat loop to approval (architecture:
    "Checkpoint loop"); raises `_AbortRun` on abort. An `amend_proposal` arising
    during chat defers the checkpoint (orchestrator.md): the chat reply still
    renders (via `prompt=None`, mooting the in-progress prompt), the unit is
    resolved to closure and re-presentation, and the checkpoint re-presents fresh
    afterward via `build_view` -- so a mid-run amendment's repairs (if any touched
    this phase's own entries) are visible in the re-presented view too."""
    view = build_view()
    reply: CheckpointReply | AmendmentReply | None = presenter.present_checkpoint(view)
    while True:
        if isinstance(reply, Approve):
            return
        if isinstance(reply, Abort):
            raise _AbortRun
        if isinstance(reply, Chat):
            chat_reply_text = session.chat(reply.text)
            if unit_holder.current is not None:
                presenter.show_chat_reply(chat_reply_text, None)
                _resolve_unit_to_presentation(
                    unit_holder, phase_status, phase_baselines, holder, session, presenter, log
                )
                view = build_view()
                reply = presenter.present_checkpoint(view)
                continue
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


def _run_no_impact_checkpoint(
    session: agent.AgentSession,
    presenter: Presenter,
    view: NoImpactView,
    unit_holder: _UnitHolder,
    phase_status: dict[Phase, _PhaseStatus],
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    holder: _CandidateHolder,
    log: _Log,
) -> None:
    """Present the R18 no-impact conclusion as a checkpoint -- approve/abort/chat,
    the same reply convention `_run_checkpoint` drives (cli.md: "replies per the
    checkpoint convention"); raises `_AbortRun` on abort.

    T3.2's redirect: a run-control call during chat that opens a phase (a
    revised `affected_verdict`) or a unit (`amend_proposal`) withdraws the
    conclusion the same way (orchestrator.md, "No-impact flow (R18)"): the
    in-progress reply is rendered via `prompt=None` -- mooting this round
    rather than re-offering the now-stale conclusion -- any opened unit is
    driven to closure right here (mirroring `_run_checkpoint`'s own
    `unit_holder.current is not None` handling), and then:

    - if anything is durably open afterward (a phase left open by a bare
      `affected_verdict`, or one the unit's own approval left open -- "opening
      a phase for a repair never substitutes for running it"), the conclusion
      never returns: this function returns having withdrawn it, and the
      caller's unconditional `_drain_phase_queue` call runs the newly opened
      work through its own ordinary checkpoint(s);
    - if a unit opened and was *rejected*, with nothing else left open, the
      restore returned to exactly the state the conclusion was based on --
      the no-impact confirmation is re-presented fresh ("rejection of the
      withdrawal is what puts the conclusion back on the table").

    Plain chat with neither effect just re-offers this same prompt, as before
    -- the ordinary (non-redirect) path is unchanged from T3.1."""
    reply: CheckpointReply | AmendmentReply | None = presenter.present_no_impact(view)
    while True:
        if isinstance(reply, Approve):
            return
        if isinstance(reply, Abort):
            raise _AbortRun
        if isinstance(reply, Chat):
            chat_reply_text = session.chat(reply.text)
            redirect = unit_holder.current is not None or any(
                status is _PhaseStatus.OPEN for status in phase_status.values()
            )
            if redirect:
                presenter.show_chat_reply(chat_reply_text, None)
                if unit_holder.current is not None:
                    _resolve_unit_to_presentation(
                        unit_holder, phase_status, phase_baselines, holder, session,
                        presenter, log,
                    )
                if any(status is _PhaseStatus.OPEN for status in phase_status.values()):
                    return
                # The unit opened and was rejected, restoring the pre-unit
                # state -- nothing else is open, so the conclusion still
                # stands: re-present it fresh (R18).
                reply = presenter.present_no_impact(view)
                continue
            reply = presenter.show_chat_reply(chat_reply_text, PromptKind.NO_IMPACT)
            assert reply is not None, (
                "a no-impact prompt was given (not None); show_chat_reply must "
                "re-offer it and return the next reply"
            )
            assert not isinstance(reply, Reject), (
                "Reject is never returnable at the no-impact confirmation (cli.md)"
            )
            continue
        raise AssertionError(f"unexpected no-impact reply {reply!r}")  # pragma: no cover


def _drain_phase_queue(
    session: agent.AgentSession,
    presenter: Presenter,
    holder: _CandidateHolder,
    phase_status: dict[Phase, _PhaseStatus],
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    unit_holder: _UnitHolder,
    log: _Log,
) -> None:
    """Run exactly the queue's phases, in phase order (T3.1; architecture: update
    mode's phase engine over the R18-seeded queue) -- reusing the analyze phase
    engine's own machinery (`_run_checkpoint`, the "resume any open unit before
    proceeding" rule) rather than a parallel implementation. The queue is
    re-read from `phase_status` at the top of every iteration rather than
    snapshotted once after triage: a phase opened *mid-run* -- an ahead phase
    named by an agent-proposed amendment, or (T3.2) a revised `affected_verdict`
    naming a phase ahead of or behind the run's current position -- must still
    get its own ordinary checkpoint once the phases ahead of it have been
    processed, "opening a phase for a repair never substitutes for running it"
    (orchestrator.md) -- analyze mode gets this for free from its fixed
    four-phase loop; update mode's queue is not fixed, so this loop recomputes
    it instead. This re-read is the entire mechanism R18's dynamic-expansion
    clause needs: `_handle_affected_verdict` already opens whatever phase a
    revised verdict names (T3.1), so a later iteration here simply finds it in
    the recomputed queue, whichever position it falls at. Returns once no phase
    is left `open` -- a no-op when
    called with an already-empty queue, which is what lets `_finalize_and_write`
    call this unconditionally from both modes (a no-op in analyze mode, where
    the queue is always already empty by the time the gate runs)."""
    while True:
        queue = sorted((p for p in Phase if phase_status[p] is _PhaseStatus.OPEN), key=int)
        if not queue:
            return
        phase = queue[0]
        session.run_phase(phase)
        log({"event": "phase_run", "phase": int(phase)})

        if unit_holder.current is not None:
            # orchestrator.md, Approval gate: any driving call returning with a
            # unit open resumes it immediately before anything else proceeds.
            _resolve_unit_to_presentation(
                unit_holder, phase_status, phase_baselines, holder, session, presenter, log
            )

        def _build_checkpoint_view(phase: Phase = phase) -> CheckpointView:
            added, updated, removed = _phase_diff(phase, phase_baselines[phase], holder.current)
            return CheckpointView(
                phase=phase,
                gap_counts=artifacts.gap_counts(holder.current),
                added=added,
                updated=updated,
                removed=removed,
            )

        _run_checkpoint(
            session, presenter, _build_checkpoint_view, unit_holder, phase_status,
            phase_baselines, holder, log,
        )
        phase_status[phase] = _PhaseStatus.FROZEN
        log({"event": "phase_frozen", "phase": int(phase)})


def _finalize_and_write(
    holder: _CandidateHolder,
    phase_status: dict[Phase, _PhaseStatus],
    unit_holder: _UnitHolder,
    phase_baselines: dict[Phase, artifacts.ArtifactSet],
    session: agent.AgentSession,
    presenter: Presenter,
    log: _Log,
    repo: gitrepo.GitRepo,
    end_sha: str,
    blare_root: Path,
) -> None:
    """Final confirmation (architecture: "the checkpoint approval at which the
    phase queue is empty and the semantic check passes") and the write path
    (orchestrator.md, Write path): the approval gate looped until *both* the
    phase queue is empty *and* the semantic check passes -- a failure opens a
    system-originated amendment unit, which can itself leave a phase `open`
    (one it joined from `unvisited`, per `_open_system_unit`'s note); draining
    the queue again at the top of every iteration is what gives that phase its
    own ordinary checkpoint before the write, rather than only the amendment's
    -- then the write-time re-check and the three write primitives in order,
    state last. Shared by analyze's phase-4 checkpoint approval and update
    mode's own final checkpoint (whichever queued phase's, or the no-impact
    confirmation's): R18's SHA-only advance is this exact path run over an
    unchanged candidate, not a separate code path. In analyze mode the queue
    is always already empty here (its fixed four-phase loop guarantees it), so
    draining it is a no-op there."""
    while True:
        _drain_phase_queue(
            session, presenter, holder, phase_status, phase_baselines, unit_holder, log
        )
        violations = artifacts.semantic_violations(holder.current)
        if not violations:
            break
        log(
            {
                "event": "gate_failed",
                "violation_count": len(violations),
                "kinds": [v.kind.value for v in violations],
            }
        )
        _open_system_unit(violations, phase_status, unit_holder, phase_baselines, holder, log)
        _resolve_unit_to_presentation(
            unit_holder, phase_status, phase_baselines, holder, session, presenter, log
        )
    log({"event": "gate_passed"})

    if not repo.tree_matches(end_sha, ".blare"):
        raise WriteTimeRecheckError(
            cause=(
                "the working tree outside .blare/ changed since this run started; "
                "what was analyzed no longer matches the repository"
            ),
            next_action="Re-run blare against the current commit.",
        )
    if not artifacts.raw_bytes_match(blare_root, holder.current):
        raise WriteTimeRecheckError(
            cause="the canonical YAML under .blare/ changed since this run loaded it",
            next_action="Re-run blare; do not hand-edit .blare/ during a run.",
        )

    def _do_write() -> None:
        report = artifacts.write_entries_and_config(blare_root, holder.current)
        log(
            {
                "event": "write_report",
                "primitive": "write_entries_and_config",
                "written": [str(p) for p in report.written],
                "skipped": [str(p) for p in report.skipped],
            }
        )
        report = artifacts.write_docs(blare_root, holder.current)
        log(
            {
                "event": "write_report",
                "primitive": "write_docs",
                "written": [str(p) for p in report.written],
                "skipped": [str(p) for p in report.skipped],
            }
        )
        report = artifacts.write_state(blare_root, holder.current, end_sha)
        log(
            {
                "event": "write_report",
                "primitive": "write_state",
                "written": [str(p) for p in report.written],
                "skipped": [str(p) for p in report.skipped],
            }
        )

    sigint_deferred = _write_with_sigint_masked(_do_write)
    if sigint_deferred:
        log({"event": "sigint_deferred_during_write"})


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


# --- The nine-step preflight sequence, then either mode's post-preflight flow:
# update mode's triage-driven phase engine (T3.1), or analyze mode's fixed
# four-phase engine (T2.3/T2.4); see the mode check below -----------------------


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
    # queue (R18). Computed here so the ordering rule "(7,8) semantic seeds never
    # terminate the run" holds structurally. In update mode this snapshot also
    # feeds `_handle_no_impact`'s queue-emptiness check below (T3.1): a
    # no_impact conclusion is rejected while it is non-empty. Actually opening
    # and driving repairs for these violations happens later, right after
    # `triage()` returns, via `_repair_residual_violations` (T3.2) -- which
    # recomputes the check fresh rather than reusing this snapshot, since a
    # triage-time agent amendment may already have fixed what's found here.
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
    # Per-phase checkpoint-diff baselines (T2.4): populated the first time a phase
    # ever leaves `unvisited`, whether via the ordinary loop below or the amendment
    # mechanism naming it ahead of the run's position -- see `_mark_phase_open`.
    phase_baselines: dict[Phase, artifacts.ArtifactSet] = {}
    unit_holder = _UnitHolder()
    sink = _make_sink(holder, phase_status, unit_holder)
    # T3.1: the standing no_impact conclusion (if any) the control handler
    # accepts, and step 7's semantic-violation seeds -- both feed the update
    # driver's no-impact decision below (`violations` is step 7's own list,
    # already computed above and untouched since; a later local reassignment of
    # that name inside the analyze/update gate loops rebinds it, never mutates
    # this list).
    no_impact_holder = _NoImpactHolder()

    def _log_event(event: dict[str, object]) -> None:
        _log(run_state, presenter, event)

    control = _make_control_handler(
        mode, phase_status, unit_holder, phase_baselines, holder, _log_event,
        no_impact_holder, violations,
    )

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

    if mode is RunMode.UPDATE:
        # --- T3.1: diff mode's post-preflight flow. AgentSession.triage seeds the
        # queue (an affected_verdict) or concludes no_impact; the phase engine
        # then runs exactly the seeded queue's phases, in phase order -- the same
        # amendment/checkpoint machinery T2.3/T2.4 built for analyze mode, reused
        # rather than reimplemented (`_drain_phase_queue`,
        # `_run_checkpoint`). Final confirmation is either the last queued
        # phase's ordinary checkpoint or the no-impact confirmation's approval
        # (R18's SHA-only advance) -- both funnel into the same
        # `_finalize_and_write` the analyze tail below uses. Wrapped exactly like
        # analyze's own try/except/finally (see that block's comment for the
        # abort/close rationale).
        try:
            session.triage()
            _log(run_state, presenter, {"event": "triage_complete"})

            if unit_holder.current is not None:
                # agent.md lists `triage` among the driving calls whose return
                # with a unit still open must resume it immediately
                # (orchestrator.md, Approval gate).
                _resolve_unit_to_presentation(
                    unit_holder, phase_status, phase_baselines, holder, session, presenter,
                    _log_event,
                )

            # T3.2: R18's load-seeded violation repairs -- request_repair is the
            # only channel that can ever tell the model about a violation
            # already present in the loaded state (agent.md: "loaded-state
            # violations do not travel in RunContext"), so the orchestrator
            # proactively opens a system-originated unit for whatever is still
            # violating right after triage, before deciding what to do about a
            # no_impact conclusion or draining the queue.
            _repair_residual_violations(
                holder, phase_status, unit_holder, phase_baselines, session, presenter,
                _log_event,
            )

            # T3.2: a same-turn sequence within triage() itself -- an accepted
            # no_impact followed by an amend_proposal before the turn ends --
            # is nothing `_handle_amend_proposal` forbids, and the unit just
            # resolved above (or a residual-violation unit just above that)
            # can leave a phase durably open even though `no_impact_holder`
            # still holds the earlier conclusion. Presenting that conclusion
            # now would offer it for approval as if it were still current; the
            # "queue empty" precondition R18's no-impact confirmation assumes
            # (orchestrator.md, "No-impact flow") already failed by the time
            # we get here, so it is withdrawn the same way a mid-chat redirect
            # withdraws it -- never presented at all, and the caller's
            # unconditional `_drain_phase_queue` below is what runs the
            # opened phase's own ordinary checkpoint instead.
            queue_already_open = any(
                status is _PhaseStatus.OPEN for status in phase_status.values()
            )
            if no_impact_holder.conclusion is not None and not queue_already_open:
                view = NoImpactView(
                    delta_file_count=len(delta_files),
                    delta_files=delta_files,
                    conclusion=no_impact_holder.conclusion,
                )
                # T3.2: `_run_no_impact_checkpoint` itself moots the prompt and
                # resolves any unit/phase a mid-chat redirect opens (R18),
                # re-presenting the conclusion fresh if a redirecting unit is
                # rejected -- nothing further to do here once it returns.
                _run_no_impact_checkpoint(
                    session, presenter, view, unit_holder, phase_status, phase_baselines,
                    holder, _log_event,
                )
            # `_drain_phase_queue` is a no-op over an empty queue, so this
            # unconditional call covers both the no_impact-withdrawn path (a
            # phase is now open) and the ordinary affected_verdict path.
            _drain_phase_queue(
                session, presenter, holder, phase_status, phase_baselines, unit_holder,
                _log_event,
            )

            _finalize_and_write(
                holder, phase_status, unit_holder, phase_baselines, session, presenter,
                _log_event, repo, end_sha, blare_root,
            )
        except _AbortRun:
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
        finally:
            session.close()

        counts = _overall_counts(artifact_set, holder.current)
        presenter.summary(
            RunSummary(
                outcome="update complete",
                transcript_path=transcript.path,
                gap_counts=artifacts.gap_counts(holder.current),
                entry_counts=counts,
            )
        )
        return 0

    # --- The analyze phase engine: four phases in order, a checkpoint after each,
    # the amendment mechanism (T2.4) interleaved wherever it arises, the final
    # approval gate (looping system-originated amendments until it passes), then
    # the write path. Wrapped in try/except/finally: `_AbortRun` (any Abort reply,
    # ordinary checkpoint or amendment) renders the one abort summary and exits 3;
    # `session.close()` runs on every exit from here on -- approval, abort, or any
    # other exception (WriteTimeRecheckError, a WriteError from a write primitive,
    # or an AgentSessionError from the session itself). `close` is idempotent and
    # safe after any error (agent.md), which is what makes an unconditional
    # `finally` here correct rather than merely convenient.
    try:
        for phase in _ANALYZE_PHASES:
            _mark_phase_open(phase, phase_status, phase_baselines, holder)
            session.run_phase(phase)
            _log(run_state, presenter, {"event": "phase_run", "phase": int(phase)})

            if unit_holder.current is not None:
                # orchestrator.md, Approval gate: "an amendment unit open when
                # run_phase returns... the orchestrator immediately resumes the
                # unit via request_repair... and only then presents the pending
                # checkpoint."
                _resolve_unit_to_presentation(
                    unit_holder, phase_status, phase_baselines, holder, session, presenter,
                    _log_event,
                )

            def _build_checkpoint_view(phase: Phase = phase) -> CheckpointView:
                # `phase: Phase = phase` binds the enclosing loop iteration's
                # value at definition time (not by reference) -- safe either way,
                # since `_run_checkpoint` only ever calls this within the same
                # iteration that defines it, but this also satisfies the linter's
                # general loop-closure check.
                added, updated, removed = _phase_diff(phase, phase_baselines[phase], holder.current)
                return CheckpointView(
                    phase=phase,
                    gap_counts=artifacts.gap_counts(holder.current),
                    added=added,
                    updated=updated,
                    removed=removed,
                )

            _run_checkpoint(
                session, presenter, _build_checkpoint_view, unit_holder, phase_status,
                phase_baselines, holder, _log_event,
            )
            phase_status[phase] = _PhaseStatus.FROZEN
            _log(run_state, presenter, {"event": "phase_frozen", "phase": int(phase)})

        # Approval gate, write-time re-check, and the three write primitives in
        # order, state last (orchestrator.md, Approval gate / Write path) --
        # shared with update mode's own final checkpoint via `_finalize_and_write`
        # (T3.1) rather than a duplicated implementation.
        _finalize_and_write(
            holder, phase_status, unit_holder, phase_baselines, session, presenter,
            _log_event, repo, end_sha, blare_root,
        )
    except _AbortRun:
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
    `WriteTimeRecheckError`, an `AgentSessionError` mid-phase, a `WriteError` from a
    write primitive, or a protocol-violation exception such as an unexpected
    `Reject` all land here) or any unexpected (non-`BlareError`) exception,
    whatever stage it strikes (the architecture's non-module carve-out); `3` a user
    abort (SIGINT, or an `Abort` reply at a checkpoint or amendment presentation --
    the phase engine's own `_AbortRun` handler returns 3 directly for the latter,
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
