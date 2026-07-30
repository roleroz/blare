"""The run lifecycle (architecture): the only module that coordinates the others.

T2.2 scope (`engineering/modules/orchestrator.md`): the full nine-step preflight
sequence, the lock, the run log, and the exit-code taxonomy. Steps 5-6 (update-only:
SHA ancestry, the R7 empty-delta short-circuit) are wired for real here but their e2e
coverage is T3.x's; step 7's semantic-violation seeding is computed here but nothing
yet consumes the resulting queue (T2.3/T2.4's phase engine); the analyze/update
happy-path phase engine, checkpoints, amendments, and the write path are NOT built
here -- after preflight completes, this module still ends in a placeholder no-op
summary (the same shape T1.1 established), superseded by T2.3.

The `Presenter` protocol below mirrors `cli.md`'s `TerminalPresenter` interface in
full so `cli.TerminalPresenter` type-checks against it; only the methods this
module's flow actually calls (`error`, `notice`, `summary`, `is_interactive`) have
callers today. The view/reply types (`CheckpointView`, `AmendmentView`,
`NoImpactView`, `CheckpointReply`, `AmendmentReply`, `PromptKind`) are placeholders
whose fields land with the phase engine (T2.3/T2.4).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from blare import agent, artifacts, gitrepo
from blare.model import (
    BatchVerdict,
    BlareError,
    EditBatch,
    RunContext,
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
    """A phase's results at its checkpoint. Fields land with T2.3."""


@dataclass(frozen=True)
class AmendmentView:
    """An amendment unit's changed entries, grouped by phase. Fields land with T2.4."""


@dataclass(frozen=True)
class NoImpactView:
    """The R18 no-impact conclusion's delta summary. Fields land with T3.1."""


@dataclass(frozen=True)
class RunSummary:
    """What a run reports at its end (R13).

    T2.2 populates `outcome`, `transcript_path`, and `gap_counts` -- the two
    sessionless/placeholder endings this task builds (R7's up-to-date exit, and the
    post-preflight placeholder) both have a real artifact set to count gaps over. The
    entry-count split (added/updated/removed, or "discarded" at a non-writing ending)
    has nothing to report until edits exist, so it lands with T2.3.
    """

    outcome: str
    transcript_path: Path | None = None
    gap_counts: artifacts.GapSummary | None = None


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


# --- Placeholder agent handlers (T2.2 never runs a phase, so neither is ever
# actually called; the real sink/control handlers -- phase-state rule, artifacts'
# content check -- land with T2.3's phase engine) -----------------------------------


def _noop_sink(batch: EditBatch) -> BatchVerdict:
    return BatchVerdict(ok=True, message=None)


def _noop_control(call: RunControlCall) -> RunControlVerdict:
    return RunControlVerdict(ok=True, message=None)


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


# --- The nine-step preflight sequence, plus the placeholder post-preflight ending --


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
    # transcript (R14).
    transcript = _RealTranscriptWriter(
        state_dir / _TRANSCRIPTS_DIRNAME / f"{run_id}.jsonl"
    )
    client = agent.create_client()
    session = agent.AgentSession(
        client,
        sink=_noop_sink,
        control=_noop_control,
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

    # --- Placeholder post-preflight ending (T1.1's pattern, extended with real gap
    # counts): the phase engine, checkpoints, and the write path are T2.3 onward.
    session.close()
    gaps = artifacts.gap_counts(artifact_set)
    presenter.summary(
        RunSummary(outcome="no changes", transcript_path=transcript.path, gap_counts=gaps)
    )
    return 0


def run(mode: RunMode, repo_path: Path, presenter: Presenter) -> int:
    """Blare's one entry contract: run a mode against a repo, render through presenter.

    Exit-code taxonomy (orchestrator.md), assigned by run stage, not by which module
    raised: `0` success (including R7 up-to-date); `1` refusal -- any `BlareError`
    raised before preflight completes (step 9's auth check succeeding); `2` -- an
    `BlareError` raised after preflight completes (none of T2.2's own code paths
    produce one, since the post-preflight placeholder cannot fail this way, but the
    stage-based check is written generally so T2.3's phase engine can raise into it
    without this function changing shape) or any unexpected (non-`BlareError`)
    exception, whatever stage it strikes (the architecture's non-module carve-out);
    `3` a user abort (SIGINT). Argparse usage errors are cli's own carve-out, not
    this function's concern.

    T2.2 does not yet build checkpoints, so a SIGINT here is never a mid-checkpoint
    abort (that variant, with discarded edit counts, needs a checkpoint to abort
    *at* -- T2.3's build). Two variants still apply, distinguished by whether a
    session had started (R14's own definition of "a session ran", step 9
    succeeding): pre-session, rendered as a single `aborted` notice with no summary
    and no error, since no artifacts or counts exist to summarize; and
    session-bearing (a SIGINT during the placeholder tail after step 9), which
    renders a summary naming the transcript path instead, per orchestrator.md:
    "Abort exits 3, writing nothing (R20), the summary still naming the transcript
    path (R14 -- a session ran)."
    """
    run_state = _RunState()
    try:
        return _execute(mode, repo_path, presenter, run_state)
    except KeyboardInterrupt:
        if run_state.transcript_path is not None:
            presenter.summary(
                RunSummary(outcome="aborted", transcript_path=run_state.transcript_path)
            )
        else:
            presenter.notice("aborted")
        return 3
    except BlareError as exc:
        presenter.error(cause=exc.cause, next_action=exc.next_action)
        stage = "failure" if run_state.preflight_complete else "refusal"
        _log(run_state, presenter, {"event": stage, "cause": exc.cause})
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
