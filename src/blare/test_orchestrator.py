"""Unit tests for blare.orchestrator: T2.2's nine-step preflight sequence, the lock,
the run log, and the exit-code taxonomy; T2.3's phase engine, checkpoints, the
approval gate, and the write path; T2.4's amendment mechanism (unit mechanics, the
frozen-only cascade, system amendments, the closure loop, outcome notification).

Fakes per orchestrator.md's test plan: `FakeSDKClient` (a scripted `agent.SDKClient`
stand-in, used only by tests that exercise the real `agent.AgentSession`'s own auth
handshake), `FakeAgentSession` (a scripted `agent.AgentSession` stand-in -- phase edit
batches, chat replies, request_repair/notify_amendment_outcome -- for every test that
needs the phase engine to actually run), and `FakePresenter` (scripted replies,
records every `CheckpointView`/`AmendmentView` presented).
gitrepo and artifacts are real, exercised over temporary git repositories -- matching
the design doc's "gitrepo and artifacts are real, over temp repos".

The diff-mode phase engine and `triage` (T3.x) are out of this file's scope.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from blare import agent, artifacts, gitrepo, orchestrator
from blare.model import (
    BatchVerdict,
    Edit,
    EditBatch,
    EditOp,
    Phase,
    RunContext,
    RunControlAction,
    RunControlCall,
    RunControlVerdict,
    RunMode,
    Violation,
    ViolationKind,
)
from blare.orchestrator import (
    AmendmentOrigin,
    AmendmentReply,
    AmendmentView,
    CheckpointReply,
    CheckpointView,
    DirtyWorkingTreeError,
    LockHeldError,
    NoImpactView,
    NonAncestorSHAError,
    NonInteractiveError,
    PromptKind,
    RunSummary,
    StateDirectoryError,
)


@dataclass
class FakePresenter:
    """Records what the orchestrator reports; the unit-level stand-in for a TTY.

    `checkpoint_replies` scripts `present_checkpoint`'s replies in order (defaulting
    to auto-approve once exhausted, which is what every preflight-focused test
    wants); `chat_reply_script` does the same for `show_chat_reply`.
    """

    interactive: bool = True
    notices: list[str] = field(default_factory=list)
    errors: list[tuple[str, str, str | None]] = field(default_factory=list)
    summaries: list[RunSummary] = field(default_factory=list)
    checkpoint_views: list[CheckpointView] = field(default_factory=list)
    checkpoint_replies: list[CheckpointReply] = field(default_factory=list)
    chat_reply_script: list[AmendmentReply | None] = field(default_factory=list)
    chat_reply_calls: list[tuple[str, PromptKind | None]] = field(default_factory=list)
    amendment_views: list[AmendmentView] = field(default_factory=list)
    amendment_rejectable_seen: list[bool] = field(default_factory=list)
    amendment_replies: list[AmendmentReply] = field(default_factory=list)
    no_impact_views: list[NoImpactView] = field(default_factory=list)
    no_impact_replies: list[CheckpointReply] = field(default_factory=list)
    # R25 (T4.3): every `progress()` call, in order, and a single ordered event
    # log spanning progress *and* every presentation/reply method -- unlike the
    # per-kind lists above, this is what lets a test assert relative order (a
    # progress tick happened before the checkpoint it precedes, none after).
    progress_calls: list[tuple[str, float, str | None]] = field(default_factory=list)
    event_order: list[str] = field(default_factory=list, init=False)

    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
        self.checkpoint_views.append(view)
        self.event_order.append("checkpoint")
        if self.checkpoint_replies:
            return self.checkpoint_replies.pop(0)
        return orchestrator.Approve()

    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply:
        self.amendment_views.append(view)
        self.amendment_rejectable_seen.append(rejectable)
        self.event_order.append("amendment")
        if self.amendment_replies:
            return self.amendment_replies.pop(0)
        return orchestrator.Approve()

    def present_no_impact(self, view: NoImpactView) -> CheckpointReply:
        self.no_impact_views.append(view)
        self.event_order.append("no_impact")
        if self.no_impact_replies:
            return self.no_impact_replies.pop(0)
        return orchestrator.Approve()

    def show_chat_reply(
        self, text: str, prompt: PromptKind | None
    ) -> AmendmentReply | None:
        self.chat_reply_calls.append((text, prompt))
        self.event_order.append("chat_reply")
        if self.chat_reply_script:
            return self.chat_reply_script.pop(0)
        return orchestrator.Approve()

    def progress(self, label: str, elapsed_seconds: float, last_activity: str | None) -> None:
        self.progress_calls.append((label, elapsed_seconds, last_activity))
        self.event_order.append("progress")

    def notice(self, text: str) -> None:
        self.notices.append(text)

    def error(self, cause: str, next_action: str, detail: str | None = None) -> None:
        self.errors.append((cause, next_action, detail))

    def summary(self, s: RunSummary) -> None:
        self.summaries.append(s)

    def is_interactive(self) -> bool:
        return self.interactive


@dataclass
class FakeSDKClient:
    """A scripted SDKClient stand-in (agent.md's replay client itself is exercised in
    test_agent.py). `ready` controls the handshake's auth outcome (R12)."""

    ready: bool = True

    def handshake(self) -> agent.HandshakeResult:
        return agent.HandshakeResult(ready=self.ready)

    def configure_worktree_root(self, root: Path) -> None:
        pass

    def configure_session(
        self,
        mode: RunMode,
        system_prompt: str,
        tools: tuple[agent.ToolDefinition, ...],
        disallowed_tools: tuple[str, ...],
    ) -> None:
        pass

    def send(self, event: dict[str, object]) -> None:
        raise NotImplementedError("these tests never run a phase")

    def receive(self) -> dict[str, object]:
        raise NotImplementedError("these tests never run a phase")

    def close(self) -> None:
        pass


@dataclass
class FakeAgentSession:
    """A scripted `agent.AgentSession` stand-in (orchestrator.md's test plan): drives
    the *real* injected `sink`/`control` handlers (so the phase-state rule and
    artifacts' content check are genuinely exercised) against scripted edit batches
    and chat replies, rather than replaying real SDK wire events -- that full-stack
    exercise is e2e's job. Trivial (no edits, no chat) by default, which is what
    every preflight-focused test needs to reach a real completed run.

    Constructed with the same keyword arguments `orchestrator._execute` passes to
    `agent.AgentSession` (`sink`, `control`, `stack`, `transcript`), plus `client`
    positionally, so a factory built from this class can replace `agent.AgentSession`
    via `monkeypatch.setattr` transparently.
    """

    client: object
    sink: agent.EditSink
    control: agent.RunControlHandler
    stack: object
    transcript: object
    edits_by_phase: dict[Phase, list[EditBatch]] = field(default_factory=dict)
    chat_script: list[str] = field(default_factory=list)
    # Scripted edit batches / run_control calls to apply, via the real sink/control
    # handlers, when `chat` is called with exactly this text -- what lets a test
    # simulate the model proposing/completing an amendment, or landing a repair
    # batch, during checkpoint or amendment re-presentation chat.
    chat_edits_by_text: dict[str, list[EditBatch]] = field(default_factory=dict)
    chat_run_control_by_text: dict[str, list[RunControlCall]] = field(default_factory=dict)
    # Scripted `run_control` calls to issue via the real control handler during a
    # given phase's turn (orchestrator.md's test plan wants run-control totality
    # exercised, even though analyze mode rejects every one of them today).
    run_control_calls_by_phase: dict[Phase, list[RunControlCall]] = field(
        default_factory=dict
    )
    # An exception to raise from `run_phase` for the named phase, simulating the
    # agent session dying mid-phase (orchestrator.md's failure-mode test plan:
    # "agent: AgentSessionError mid-phase").
    raise_in_phase: dict[Phase, Exception] = field(default_factory=dict)
    # Repair batches to apply (through the real sink) when `request_repair` is
    # called naming exactly this sorted phase tuple -- keyed the same way
    # `edits_by_phase` keys phase turns, so a test scripts "what the model does
    # when asked to repair phases X" once, however many times the closure loop
    # calls request_repair for that exact frontier.
    repair_edits_by_phases: dict[tuple[Phase, ...], list[EditBatch]] = field(
        default_factory=dict
    )
    # Extra run_control calls (e.g. a fresh amend_proposal joining more phases) to
    # issue, via the real control handler, during a given request_repair call --
    # keyed like repair_edits_by_phases, applied before the scripted amend_complete.
    repair_run_control_by_phases: dict[tuple[Phase, ...], list[RunControlCall]] = field(
        default_factory=dict
    )
    # When True, a request_repair call does not itself acknowledge amend_complete
    # -- the test drives it explicitly (via `control`) to exercise the reminder /
    # resume path at the orchestrator level. Default False: every scripted fake
    # acknowledges completion immediately, since the resume-retry mechanics are
    # agent.py's own unit-level concern (test_agent.py), not this module's.
    withhold_amend_complete: bool = False
    # Scripted run_control calls to issue (via the real control handler) when
    # `triage` is called (T3.1) -- models the agent's triage turn(s) without
    # re-simulating real turn boundaries (like every other `*_calls_by_*` field
    # here, this fake applies them all in one shot rather than one per drained
    # turn -- the turn-by-turn reminder/raise mechanics are agent.py's own unit
    # concern, test_agent.py).
    triage_run_control_calls: list[RunControlCall] = field(default_factory=list)
    # R25 (T4.3): mirrors the real AgentSession.__init__'s on_activity parameter,
    # so a factory built from this class can replace agent.AgentSession
    # transparently even now that the real one takes it too.
    on_activity: Callable[[str], None] | None = None
    # Which driving call (by its `call_order` tag prefix, e.g. "run_phase",
    # "triage", "chat", "request_repair", "notify_amendment_outcome") should run
    # `activity_script` -- (real, tiny sleep_seconds, activity_name) pairs fired
    # in order via `on_activity` -- before returning. Lets a test make exactly
    # one driving call "slow" (a few tens of milliseconds) so the orchestrator's
    # progress ticker, with its interval shrunk via monkeypatching
    # `orchestrator._PROGRESS_TICK_INTERVAL_SECONDS`, genuinely ticks more than
    # once while it is in flight, without faking real time itself.
    slow_call: str | None = None
    activity_script: list[tuple[float, str]] = field(default_factory=list)
    started_with: tuple[RunMode, RunContext] | None = field(default=None, init=False)
    ran_phases: list[Phase] = field(default_factory=list, init=False)
    triage_called: bool = field(default=False, init=False)
    chat_calls: list[str] = field(default_factory=list, init=False)
    rejected_batches: list[BatchVerdict] = field(default_factory=list, init=False)
    run_control_verdicts: list[RunControlVerdict] = field(default_factory=list, init=False)
    request_repair_calls: list[tuple[tuple[Phase, ...], tuple[Violation, ...]]] = field(
        default_factory=list, init=False
    )
    notify_outcomes: list[tuple[bool, tuple[Phase, ...]]] = field(
        default_factory=list, init=False
    )
    closed: bool = field(default=False, init=False)
    # A single ordered log across every driving call this fake receives (T3.2):
    # most scenarios only need per-kind lists (`ran_phases`,
    # `request_repair_calls`, ...), but a scenario distinguishing *when* one
    # driving call happens relative to another -- e.g. whether a proactive
    # repair reaches the model before or after an unrelated phase's own turn
    # -- needs their relative order, which no per-kind list captures on its
    # own.
    call_order: list[str] = field(default_factory=list, init=False)

    def start(self, mode: RunMode, context: RunContext) -> None:
        # Mirrors the real `AgentSession.start`'s auth check (agent.md, R12) so tests
        # that need an auth failure to fire *after* the phase-engine wiring exists
        # still see it -- every other construction-time behavior (system prompt,
        # tool registration) is real `AgentSession`'s own concern, not re-tested here.
        result = self.client.handshake()  # type: ignore[attr-defined]
        if not result.ready:
            raise agent.AuthRequiredError(
                cause="no Claude Code subscription login available",
                next_action="Run `claude` and log in, then re-run blare.",
            )
        self.started_with = (mode, context)
        self._write_transcript("outbound", {"type": "session_init", "mode": mode.value})

    def _run_activity_script(self, tag: str) -> None:
        """R25 (T4.3): if `tag` matches the scripted `slow_call`, sleep and fire
        `on_activity` for each scripted (sleep_seconds, name) pair in order."""
        if self.slow_call != tag:
            return
        for sleep_seconds, name in self.activity_script:
            time.sleep(sleep_seconds)
            if self.on_activity is not None:
                self.on_activity(name)

    def run_phase(self, phase: Phase) -> None:
        self.ran_phases.append(phase)
        self.call_order.append(f"run_phase:{int(phase)}")
        self._write_transcript("outbound", {"type": "phase_prompt", "phase": int(phase)})
        # Tagged per phase number so a test can make exactly one phase "slow"
        # (e.g. "run_phase:1") without every other phase paying the same delay.
        self._run_activity_script(f"run_phase:{int(phase)}")
        if phase in self.raise_in_phase:
            raise self.raise_in_phase[phase]
        for batch in self.edits_by_phase.get(phase, []):
            verdict = self.sink(batch)
            if not verdict.ok:
                self.rejected_batches.append(verdict)
        for call in self.run_control_calls_by_phase.get(phase, []):
            self.run_control_verdicts.append(self.control(call))
        self._write_transcript("inbound", {"type": "turn_end"})

    def chat(self, text: str) -> str:
        self.chat_calls.append(text)
        self.call_order.append("chat")
        self._write_transcript("outbound", {"type": "chat", "text": text})
        self._run_activity_script("chat")
        for batch in self.chat_edits_by_text.get(text, []):
            verdict = self.sink(batch)
            if not verdict.ok:
                self.rejected_batches.append(verdict)
        for call in self.chat_run_control_by_text.get(text, []):
            self.run_control_verdicts.append(self.control(call))
        reply = self.chat_script.pop(0) if self.chat_script else ""
        self._write_transcript("inbound", {"type": "text", "text": reply})
        return reply

    def _write_transcript(self, direction: str, event: dict[str, object]) -> None:
        self.transcript.write_event(direction, event)  # type: ignore[attr-defined]

    def close(self) -> None:
        self.closed = True

    @property
    def transcript_path(self) -> Path:
        return self.transcript.path  # type: ignore[attr-defined,no-any-return]

    def triage(self) -> None:
        self.triage_called = True
        self.call_order.append("triage")
        self._write_transcript("outbound", {"type": "triage"})
        self._run_activity_script("triage")
        for call in self.triage_run_control_calls:
            self.run_control_verdicts.append(self.control(call))
        self._write_transcript("inbound", {"type": "turn_end"})

    def request_repair(self, phases: list[Phase], violations: list[Violation]) -> None:
        key = tuple(sorted(phases, key=int))
        self.request_repair_calls.append((key, tuple(violations)))
        self.call_order.append("request_repair:" + ",".join(str(int(p)) for p in key))
        self._write_transcript(
            "outbound", {"type": "request_repair", "phases": [int(p) for p in key]}
        )
        self._run_activity_script("request_repair")
        for batch in self.repair_edits_by_phases.get(key, []):
            verdict = self.sink(batch)
            if not verdict.ok:
                self.rejected_batches.append(verdict)
        for call in self.repair_run_control_by_phases.get(key, []):
            self.run_control_verdicts.append(self.control(call))
        if not self.withhold_amend_complete:
            self.control(RunControlCall(action=RunControlAction.AMEND_COMPLETE, payload={}))
        self._write_transcript("inbound", {"type": "turn_end"})

    def notify_amendment_outcome(
        self, approved: bool, restored_phases: list[Phase]
    ) -> None:
        self.notify_outcomes.append((approved, tuple(restored_phases)))
        self._write_transcript(
            "outbound", {"type": "amendment_outcome", "approved": approved}
        )
        self._run_activity_script("notify_amendment_outcome")


def _ready_session(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool = True,
    edits_by_phase: dict[Phase, list[EditBatch]] | None = None,
    chat_script: list[str] | None = None,
    run_control_calls_by_phase: dict[Phase, list[RunControlCall]] | None = None,
    raise_in_phase: dict[Phase, Exception] | None = None,
    chat_edits_by_text: dict[str, list[EditBatch]] | None = None,
    chat_run_control_by_text: dict[str, list[RunControlCall]] | None = None,
    repair_edits_by_phases: dict[tuple[Phase, ...], list[EditBatch]] | None = None,
    repair_run_control_by_phases: dict[tuple[Phase, ...], list[RunControlCall]] | None = None,
    withhold_amend_complete: bool = False,
    triage_run_control_calls: list[RunControlCall] | None = None,
    slow_call: str | None = None,
    activity_script: list[tuple[float, str]] | None = None,
) -> list[FakeAgentSession]:
    """Patch `agent.create_client` and `agent.AgentSession` so the phase engine runs
    against a scripted `FakeAgentSession` instead of real SDK wire replay -- what
    every test that needs preflight to reach a completed run (rather than merely
    step 9's auth success) wants. Returns the list `FakeAgentSession` instances get
    appended to as they are constructed (one per `orchestrator.run()` call), so a
    test driving multiple runs can inspect each one afterward.

    `slow_call`/`activity_script` (R25, T4.3) forward to `FakeAgentSession` so a
    test can make exactly one driving call sleep briefly while firing scripted
    `on_activity` names -- see `FakeAgentSession._run_activity_script`.
    """
    monkeypatch.setattr(agent, "create_client", lambda: FakeSDKClient(ready=ready))
    scripted_edits = edits_by_phase or {}
    scripted_chat = list(chat_script or [])
    scripted_run_control = run_control_calls_by_phase or {}
    scripted_raises = raise_in_phase or {}
    scripted_chat_edits = chat_edits_by_text or {}
    scripted_chat_run_control = chat_run_control_by_text or {}
    scripted_repair_edits = repair_edits_by_phases or {}
    scripted_repair_run_control = repair_run_control_by_phases or {}
    scripted_triage_run_control = list(triage_run_control_calls or [])
    scripted_activity_script = list(activity_script or [])
    sessions: list[FakeAgentSession] = []

    def _factory(
        client: object,
        sink: agent.EditSink,
        control: agent.RunControlHandler,
        stack: object,
        transcript: object,
        on_activity: Callable[[str], None] | None = None,
    ) -> FakeAgentSession:
        session = FakeAgentSession(
            client=client,
            sink=sink,
            control=control,
            stack=stack,
            transcript=transcript,
            edits_by_phase=scripted_edits,
            chat_script=list(scripted_chat),
            run_control_calls_by_phase=scripted_run_control,
            raise_in_phase=scripted_raises,
            chat_edits_by_text=scripted_chat_edits,
            chat_run_control_by_text=scripted_chat_run_control,
            repair_edits_by_phases=scripted_repair_edits,
            repair_run_control_by_phases=scripted_repair_run_control,
            withhold_amend_complete=withhold_amend_complete,
            triage_run_control_calls=list(scripted_triage_run_control),
            on_activity=on_activity,
            slow_call=slow_call,
            activity_script=list(scripted_activity_script),
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(agent, "AgentSession", _factory)
    return sessions


# --- Repo-building helpers -----------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_no_commits(path: Path) -> None:
    _run_git(["init", "--quiet"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)


def _commit_all(path: Path, message: str) -> None:
    _run_git(["add", "-A"], path)
    _run_git(["commit", "--quiet", "-m", message], path)


def _init_repo(path: Path) -> None:
    """A repo with one commit -- the minimum R11 (both clauses) allows through."""
    _init_repo_no_commits(path)
    (path / "README.md").write_text("test repo\n")
    _commit_all(path, "initial commit")


def _blare_root(repo: Path) -> Path:
    return repo / ".blare"


def _write_yaml_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_minimal_analyzed_state(
    repo: Path, analyzed_sha: str, schema_version: int = 1
) -> None:
    """A minimal, structurally valid `.blare/` with every entry file empty."""
    root = _blare_root(repo)
    _write_yaml_file(
        root / "state.yaml",
        f'analyzed_sha: "{analyzed_sha}"\nschema_version: {schema_version}\n',
    )
    for name in (
        "system-map.yaml",
        "failure-modes.yaml",
        "metrics.yaml",
        "metric-recommendations.yaml",
        "alert-recommendations.yaml",
        "coverage.yaml",
    ):
        _write_yaml_file(root / name, "[]\n")


def _write_default_config(repo: Path) -> None:
    """`blare update` requires a config file (R23); write one for tests that only
    care about a later preflight step."""
    _write_yaml_file(_blare_root(repo) / "config.yaml", "stack: prometheus\n")


def _write_valid_update_state(repo: Path, analyzed_sha: str) -> None:
    """A structurally *and* semantically valid `.blare/` for update-mode tests: one
    excluded failure mode (trivially satisfies every R3-R5 invariant with no
    metrics/alerts needed), plus a default config -- what every update happy-path
    test starts from so step 7's semantic check seeds nothing (T3.1)."""
    _write_minimal_analyzed_state(repo, analyzed_sha)
    root = _blare_root(repo)
    _write_yaml_file(
        root / "failure-modes.yaml",
        "- id: fm-timeout\n"
        "  title: upstream timeout\n"
        "  description: a call to an upstream service times out\n"
        "  severity: warning\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: excluded\n"
        "  exclusion_reason: not independently detectable\n",
    )
    _write_yaml_file(
        root / "coverage.yaml",
        "- failure_mode_id: fm-timeout\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids: []\n",
    )
    _write_default_config(repo)


def _write_update_state_with_semantic_violation(repo: Path, analyzed_sha: str) -> None:
    """A structurally valid but semantically *violating* `.blare/`: one non-excluded
    failure mode with no alert coverage -- step 7's semantic check seeds its repair
    phase (`ViolationKind.UNMAPPED_FAILURE_MODE` repairs in phase 4, per
    `model._REPAIR_PHASE`) -- for testing that R18's no_impact rejection accounts
    for load-seeded violations, not just triage-opened phases (T3.1)."""
    _write_minimal_analyzed_state(repo, analyzed_sha)
    root = _blare_root(repo)
    _write_yaml_file(
        root / "failure-modes.yaml",
        "- id: fm-orphan\n"
        "  title: an unmapped failure\n"
        "  description: has a coverage status but no alert coverage\n"
        "  severity: warning\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: alertable\n",
    )
    _write_yaml_file(
        root / "coverage.yaml",
        "- failure_mode_id: fm-orphan\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids: []\n",
    )
    _write_default_config(repo)


def _isolate_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `XDG_STATE_HOME` at a private tmp directory so lock/run-log/transcript
    files never touch the real user's state directory during tests."""
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return state_home


def _ready_client(monkeypatch: pytest.MonkeyPatch, ready: bool = True) -> None:
    monkeypatch.setattr(agent, "create_client", lambda: FakeSDKClient(ready=ready))


def repo_head(repo: Path) -> str:
    return gitrepo.GitRepo.discover(repo).head_sha()


def commit_file_update(repo: Path, relative_path: str, content: str) -> str:
    """Write `content` to `relative_path` (outside `.blare/`) and commit it --
    T3.1's update-mode tests use this to give a run a genuine non-empty effective
    delta between the recorded `analyzed_sha` and HEAD."""
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _run_git(["add", relative_path], repo)
    _run_git(["commit", "--quiet", "-m", f"update {relative_path}"], repo)
    return repo_head(repo)


# --- Happy path: analyze, fresh repo -------------------------------------------------


def test_contract_analyze_fresh_repo_reaches_session_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean repo with no `.blare/` state: preflight completes, all four phases run
    (trivially, no edits scripted) and reach final confirmation, the write path
    completes, and the summary carries real (zero) entry and gap counts (T2.3)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert presenter.errors == []
    assert len(presenter.summaries) == 1
    summary = presenter.summaries[0]
    assert summary.outcome == "analysis complete"
    assert summary.transcript_path is not None
    assert summary.transcript_path.is_file()
    assert summary.gap_counts == artifacts.GapSummary(alertable=0, metric_gap=0, excluded=0)
    assert summary.entry_counts == orchestrator.EntryCounts(added=0, updated=0, removed=0)
    assert not summary.discarded
    assert (_blare_root(repo) / "state.yaml").is_file()
    assert (_blare_root(repo) / "config.yaml").is_file()
    # Every checkpoint was presented and auto-approved, in phase order.
    assert [view.phase for view in presenter.checkpoint_views] == list(Phase)


def test_contract_analyze_over_existing_state_reaches_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Analyze with an existing, valid state file loads it (rather than refusing
    per R1) and proceeds through preflight and the (trivial, no-op) phase engine to
    a completed run -- the mode-dispatch half of R16, not R16 itself: ID/byte
    stability of edits against existing entries is T2.5's e2e scope."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=repo_head(repo))
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert presenter.errors == []
    assert len(presenter.summaries) == 1


# --- R11: outside a repo; no commits -------------------------------------------------


def test_contract_r11_refuses_outside_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a git repository, run() exits 1 and renders the refusal (R11)."""
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, tmp_path, presenter)

    assert code == 1
    assert len(presenter.errors) == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "not inside a git repository" in cause
    assert next_action != ""
    assert presenter.summaries == []


def test_contract_r11_refuses_no_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository with no commits yet exits 1 (R11's second clause) -- new in
    T2.2: T1.1's flow never called `head_sha`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_no_commits(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    assert len(presenter.errors) == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "no commits" in cause
    assert next_action != ""


def test_contract_r11_refuses_dirty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A modified tracked file outside `.blare/` refuses at step 2 (R11's third
    clause), naming the file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("changed\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "README.md" in cause


def test_contract_r11_dirty_confined_to_blare_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change confined to `.blare/` (an untracked file there) never triggers the
    dirty-tree refusal (R11: "Differences confined to .blare/ never block")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".blare").mkdir()
    (repo / ".blare" / "scratch.txt").write_text("stray\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0


# --- Preflight ordering: adjacent pairs ---------------------------------------------


def test_contract_ordering_no_commits_before_dirty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(1,2): a repo with no commits AND an untracked file (which would also be
    "dirty") reports the no-commits refusal, since step 1 precedes step 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_no_commits(repo)
    (repo / "untracked.txt").write_text("x\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "no commits" in cause


def test_contract_ordering_dirty_tree_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(2,3): a dirty tree AND a held lock reports the dirty-tree refusal, since
    step 2 precedes step 3."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("changed\n")
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    (lock_dir / "lock").write_text(json.dumps({"pid": os.getpid(), "started_at": "x"}))

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "README.md" in cause


def test_contract_ordering_lock_before_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(3,4): a held lock AND structurally invalid artifacts reports the lock
    refusal, since step 3 precedes step 4."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_yaml_file(_blare_root(repo) / "state.yaml", "not: [valid\n")
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    (lock_dir / "lock").write_text(json.dumps({"pid": os.getpid(), "started_at": "x"}))

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert str(os.getpid()) in cause


def test_contract_ordering_structural_validation_before_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4,5): structurally invalid artifacts AND a non-ancestor recorded SHA
    reports the structural-validation refusal, since step 4's `load()` must
    return successfully before step 5 can even run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha="0" * 40)
    _write_default_config(repo)
    # An invalid severity enum: a structural (R19) failure.
    _write_yaml_file(
        _blare_root(repo) / "failure-modes.yaml",
        (
            "- id: fm-1\n"
            "  title: t\n"
            "  description: d\n"
            "  severity: not-a-real-severity\n"
            "  user_visible: false\n"
            "  caused_by: []\n"
            "  coverage_status: alertable\n"
        ),
    )
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "failure-modes.yaml" in cause
    assert "0" * 40 not in cause


def test_contract_r15_refuses_sha_that_resolves_but_is_not_an_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R15's second clause: a recorded SHA that resolves to a real commit, but one
    that is not an ancestor of the current commit (e.g. a diverged branch), refuses
    -- distinct from the "does not resolve at all" clause covered elsewhere."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    original_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _run_git(["checkout", "--orphan", "unrelated"], repo)
    (repo / "OTHER.md").write_text("an unrelated history\n")
    _commit_all(repo, "unrelated commit")
    unrelated_sha = repo_head(repo)
    _run_git(["checkout", original_branch], repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=unrelated_sha)
    _write_default_config(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 1
    cause, next_action, _detail = presenter.errors[0]
    assert unrelated_sha in cause
    assert "not an ancestor" in cause
    assert next_action != ""


def test_contract_ordering_non_ancestor_sha_refuses_even_with_empty_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(5,6): a non-ancestor recorded SHA refuses even though the delta from that
    (bogus) SHA to HEAD would otherwise be computed -- R15 precedes R7."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha="0" * 40)
    _write_default_config(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "0" * 40 in cause
    assert "blare analyze" in next_action or "ancestor" in next_action


def test_contract_ordering_empty_delta_skips_semantic_seeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(6,7): an empty effective delta exits 0 with no semantic-violation seeding
    and no session, even when the loaded set already violates the invariants (R7
    precedence over R18 seeding)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    head = repo_head(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=head)
    _write_default_config(repo)
    # An unmapped, non-excluded failure mode: a semantic violation (R4) that would
    # seed the affected-phase queue if step 7 ran.
    _write_yaml_file(
        _blare_root(repo) / "failure-modes.yaml",
        (
            "- id: fm-1\n"
            "  title: t\n"
            "  description: d\n"
            "  severity: warning\n"
            "  user_visible: false\n"
            "  caused_by: []\n"
            "  coverage_status: alertable\n"
        ),
    )
    _write_yaml_file(
        _blare_root(repo) / "coverage.yaml",
        (
            "- failure_mode_id: fm-1\n"
            "  detecting_metric_ids: []\n"
            "  metric_recommendation_ids: []\n"
            "  alert_ids: []\n"
        ),
    )
    _isolate_state_home(monkeypatch, tmp_path)

    called = {"create_client": False}

    def _fail_if_called() -> agent.SDKClient:
        called["create_client"] = True
        raise AssertionError("no session may be created on the R7 short-circuit path")

    monkeypatch.setattr(agent, "create_client", _fail_if_called)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    assert called["create_client"] is False
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "up to date"
    assert presenter.summaries[0].transcript_path is None


def test_contract_ordering_semantic_seeds_do_not_block_but_non_tty_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(7,8): a seeded queue (semantic violations present) never terminates the
    run by itself; with non-interactive stdin, R22 is what fires."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=repo_head(repo))
    _write_yaml_file(
        _blare_root(repo) / "failure-modes.yaml",
        (
            "- id: fm-1\n"
            "  title: t\n"
            "  description: d\n"
            "  severity: warning\n"
            "  user_visible: false\n"
            "  caused_by: []\n"
            "  coverage_status: alertable\n"
        ),
    )
    _write_yaml_file(
        _blare_root(repo) / "coverage.yaml",
        (
            "- failure_mode_id: fm-1\n"
            "  detecting_metric_ids: []\n"
            "  metric_recommendation_ids: []\n"
            "  alert_ids: []\n"
        ),
    )
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter(interactive=False)

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "TTY" in cause


# --- R12: auth failure ---------------------------------------------------------------


def test_contract_r12_refuses_when_auth_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handshake that is not ready (no login) exits 1, naming the login step
    (R12)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch, ready=False)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    _cause, next_action, _detail = presenter.errors[0]
    assert "claude" in next_action


# --- R17: update without state -------------------------------------------------------


def test_contract_r17_refuses_update_without_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`blare update` in a repo without a state file exits 1 naming `blare
    analyze` (R17)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "state.yaml" in cause
    assert "blare analyze" in next_action


# --- R19: structural validation -------------------------------------------------------


def test_contract_r19_refuses_on_malformed_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed YAML in a canonical file exits 1 naming the file and problem
    (R19)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha="deadbeef")
    _write_yaml_file(_blare_root(repo) / "system-map.yaml", "not: [valid: yaml\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "system-map.yaml" in cause


# --- R1 inverse: orphaned canonical files, no state file -----------------------------


def test_contract_r1_inverse_refuses_on_orphaned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Analyze with no state file, but canonical entry files already on disk, exits
    1 naming them and touches nothing (R1's inverse refusal)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_yaml_file(_blare_root(repo) / "failure-modes.yaml", "[]\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "failure-modes.yaml" in cause
    assert not (_blare_root(repo) / "state.yaml").exists()


# --- R23: unsupported / missing config ------------------------------------------------


def test_contract_r23_refuses_unsupported_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing config naming an unsupported stack exits 1 naming the file and
    the supported values (R23)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=repo_head(repo))
    _write_yaml_file(_blare_root(repo) / "config.yaml", "stack: bogus-stack\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "bogus-stack" in cause
    assert "prometheus" in cause


def test_contract_r23_refuses_unsupported_stack_in_update_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R23's unsupported-stack refusal fires the same way in `blare update` as in
    `blare analyze` -- the orchestrator.md test plan names "each mode" explicitly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=repo_head(repo))
    _write_yaml_file(_blare_root(repo) / "config.yaml", "stack: bogus-stack\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "bogus-stack" in cause
    assert "prometheus" in cause


def test_contract_r23_refuses_missing_config_at_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing config at `blare update` time is the same error as an unsupported
    one (R23); at analyze it would default instead."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=repo_head(repo))
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "config.yaml" in cause


# --- R24: schema-version mismatch -----------------------------------------------------


def test_contract_r24_refuses_on_schema_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded schema version that does not match the running Blare's exits 1
    naming both versions (R24)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_yaml_file(
        _blare_root(repo) / "state.yaml",
        "analyzed_sha: deadbeef\nschema_version: 999\n",
    )
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "999" in cause
    assert str(artifacts.CURRENT_SCHEMA_VERSION) in cause


# --- R22: non-interactive ------------------------------------------------------------


def test_contract_r22_refuses_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY stdin before any session refuses (R22), needing no login."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)

    def _fail_if_called() -> agent.SDKClient:
        raise AssertionError("no login should be attempted before the TTY check")

    monkeypatch.setattr(agent, "create_client", _fail_if_called)
    presenter = FakePresenter(interactive=False)

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "TTY" in cause


# --- R21: lock ------------------------------------------------------------------------


def test_contract_r21_refuses_when_lock_held_by_live_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock held by a live PID exits 1 naming the PID (R21)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    (lock_dir / "lock").write_text(json.dumps({"pid": 999999999, "started_at": "x"}))
    monkeypatch.setattr(orchestrator, "_pid_alive", lambda pid: True)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "999999999" in cause


def test_contract_r21_reclaims_stale_lock_with_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock whose owning process is dead is reclaimed automatically, with a
    notice, and the run proceeds (R21)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    (lock_dir / "lock").write_text(json.dumps({"pid": 12345, "started_at": "x"}))
    monkeypatch.setattr(orchestrator, "_pid_alive", lambda pid: False)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert any("stale" in n and "12345" in n for n in presenter.notices)


def test_contract_lock_acquire_gives_up_after_repeated_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent race reclaiming the same stale lock -- every re-create attempt
    loses to a concurrent winner -- falls back to a proper `LockHeldError` (exit 1,
    naming the last-seen PID) after a bounded number of retries, rather than an
    unbounded retry loop or an unhandled `FileExistsError` (which would surface as
    an unexpected exception, exit 2, instead of R21's own refusal)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / "lock"
    lock_path.write_text(json.dumps({"pid": 424242, "started_at": "x"}))
    monkeypatch.setattr(orchestrator, "_pid_alive", lambda pid: False)

    def _always_lose_the_race(path: Path) -> None:
        # Simulates another invocation's reclaim always winning the re-create
        # between this process's unlink and its own write.
        lock_path.write_text(json.dumps({"pid": 424242, "started_at": "x"}))
        raise FileExistsError

    monkeypatch.setattr(orchestrator, "_write_lock_file", _always_lose_the_race)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "424242" in cause
    assert next_action != ""
    # Exactly one stale-reclaim notice, not one per retry.
    assert sum(1 for n in presenter.notices if "stale" in n) == 1


def test_contract_lock_released_on_every_exit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successive runs after a success, an abort, and a refusal each acquire
    cleanly with no stale-lock notice -- the lock is released in a `finally` on
    every exit path once acquired."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)

    presenter1 = FakePresenter()
    assert orchestrator.run(RunMode.ANALYZE, repo, presenter1) == 0
    assert not any("stale" in n for n in presenter1.notices)

    # SIGINT injected mid-preflight: an abort.
    original_dirty_paths_outside = gitrepo.GitRepo.dirty_paths_outside

    def _raise_sigint(*_a: object, **_k: object) -> list[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(gitrepo.GitRepo, "dirty_paths_outside", _raise_sigint)
    presenter2 = FakePresenter()
    assert orchestrator.run(RunMode.ANALYZE, repo, presenter2) == 3
    monkeypatch.setattr(gitrepo.GitRepo, "dirty_paths_outside", original_dirty_paths_outside)

    # A refusal (dirty tree).
    (repo / "README.md").write_text("changed\n")
    presenter3 = FakePresenter()
    assert orchestrator.run(RunMode.ANALYZE, repo, presenter3) == 1
    assert not any("stale" in n for n in presenter3.notices)

    # Clean again: one more success, still no stale notice.
    _run_git(["checkout", "--", "README.md"], repo)
    presenter4 = FakePresenter()
    assert orchestrator.run(RunMode.ANALYZE, repo, presenter4) == 0
    assert not any("stale" in n for n in presenter4.notices)


# --- SIGINT / exit-code taxonomy ------------------------------------------------------


def test_contract_sigint_during_preflight_exits_3_with_aborted_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGINT during preflight (T2.2 never reaches a checkpoint) exits 3 with a
    single `aborted` notice -- no summary, no error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)

    def _raise_sigint(*_a: object, **_k: object) -> artifacts.ArtifactSet:
        raise KeyboardInterrupt

    monkeypatch.setattr(artifacts, "empty_set", _raise_sigint)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 3
    assert presenter.notices == ["aborted"]
    assert presenter.errors == []
    assert presenter.summaries == []


def test_contract_sigint_after_session_started_renders_summary_with_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGINT after the session has started (post step 9, during the phase engine)
    is a session-bearing abort: it renders a summary naming the transcript path
    rather than the pre-session `aborted` notice (orchestrator.md, Error handling:
    "the summary still naming the transcript path (R14 -- a session ran)"). The
    gap-counts computation raising a second `KeyboardInterrupt` while the abort
    summary is itself being built must not crash the run a second time (it degrades
    to a bare "discarded" summary instead, per `_discarded_summary_fields`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)

    def _raise_sigint(*_a: object, **_k: object) -> artifacts.GapSummary:
        raise KeyboardInterrupt

    monkeypatch.setattr(artifacts, "gap_counts", _raise_sigint)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 3
    assert presenter.notices == []
    assert presenter.errors == []
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "aborted"
    assert presenter.summaries[0].transcript_path is not None
    assert presenter.summaries[0].discarded


def test_contract_unexpected_exception_at_step1_exits_2_with_stderr_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected (non-BlareError) exception at step 1 -- before the run log
    exists -- exits 2 with the traceback rendered beneath the cause."""

    def _raise(*_args: object, **_kwargs: object) -> gitrepo.GitRepo:
        raise RuntimeError("boom")

    monkeypatch.setattr(gitrepo.GitRepo, "discover", classmethod(lambda cls, *a, **k: _raise()))
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, tmp_path, presenter)

    assert code == 2
    assert len(presenter.errors) == 1
    cause, _next_action, detail = presenter.errors[0]
    assert "boom" in cause
    assert detail is not None
    assert "RuntimeError" in detail


def test_contract_unexpected_exception_after_run_log_exists_logs_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception raised after the run log exists (step 2 onward)
    exits 2, with the traceback preserved in the run log rather than duplicated on
    stderr."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> artifacts.ArtifactSet:
        raise RuntimeError("mid-run boom")

    monkeypatch.setattr(artifacts, "empty_set", _raise)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2
    cause, _next_action, detail = presenter.errors[0]
    assert "mid-run boom" in cause
    assert detail is None

    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    run_log_dir = state_home / "blare" / repo_id / "runs"
    [log_path] = list(run_log_dir.glob("*.jsonl"))
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(
        entry.get("event") == "unexpected_exception"
        and "RuntimeError" in entry.get("traceback", "")
        for entry in lines
    )


# --- Run log --------------------------------------------------------------------------


def test_contract_run_log_records_preflight_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run log (JSONL) records preflight-step outcomes, named by the same id
    used for the transcript."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)
    assert code == 0

    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    run_log_dir = state_home / "blare" / repo_id / "runs"
    [log_path] = list(run_log_dir.glob("*.jsonl"))
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    steps = [entry.get("step") for entry in lines if entry.get("event") == "preflight_step"]
    assert steps == [2, 2, 3, 4, 7, 8, 9]

    transcript_dir = state_home / "blare" / repo_id / "transcripts"
    [transcript_path] = list(transcript_dir.glob("*.jsonl"))
    assert presenter.summaries[0].transcript_path == transcript_path
    # The run log and transcript share the run's minted id (same file stem).
    assert log_path.stem == transcript_path.stem


def test_failure_run_log_write_degrades_to_notice_and_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run-log write failure after step 2 never fails the run (orchestrator.md,
    Failure visibility): it degrades to one presenter notice naming the path, and
    the run reaches its normal outcome."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)

    original_start_run_log = orchestrator._start_run_log

    def _start_then_make_readonly(state_dir: Path, run_id: str) -> orchestrator._RunLog:
        run_log = original_start_run_log(state_dir, run_id)
        run_log.path.chmod(0o444)
        return run_log

    monkeypatch.setattr(orchestrator, "_start_run_log", _start_then_make_readonly)
    presenter = FakePresenter()

    try:
        code = orchestrator.run(RunMode.ANALYZE, repo, presenter)
    finally:
        # Restore write permission so pytest's tmp_path cleanup can remove it.
        repo_id = gitrepo.GitRepo.discover(repo).repo_id()
        state_home = Path(os.environ["XDG_STATE_HOME"])
        run_log_dir = state_home / "blare" / repo_id / "runs"
        for path in run_log_dir.glob("*.jsonl"):
            path.chmod(0o644)

    assert code == 0
    assert any("could not write the run log" in n for n in presenter.notices)
    assert len(presenter.summaries) == 1


def test_contract_contending_invocations_write_distinct_run_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A losing invocation (lock contention) still writes its own run log (step 2
    precedes step 3), and it never collides with a later winner's."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    (lock_dir / "lock").write_text(json.dumps({"pid": 999999999, "started_at": "x"}))
    monkeypatch.setattr(orchestrator, "_pid_alive", lambda pid: True)

    loser_presenter = FakePresenter()
    assert orchestrator.run(RunMode.ANALYZE, repo, loser_presenter) == 1

    (lock_dir / "lock").unlink()
    winner_presenter = FakePresenter()
    assert orchestrator.run(RunMode.ANALYZE, repo, winner_presenter) == 0

    run_log_dir = lock_dir / "runs"
    logs = sorted(run_log_dir.glob("*.jsonl"))
    assert len(logs) == 2
    assert logs[0] != logs[1]


# --- T2.3: the analyze phase engine, checkpoints, the approval gate, the write path --


def _happy_path_edits() -> dict[Phase, list[EditBatch]]:
    """A small, internally consistent artifact set spanning every T2.3 e2e criterion
    in one script: a system map (R1), a failure-mode chain where a user-visible
    failure is caused by a non-user-visible one (R3), an excluded failure mode and
    an alertable one with a metric detecting it (R4, R5), and an alert recommendation
    whose severity matches its failure mode and whose coverage linkage agrees."""
    return {
        Phase.SYSTEM_MAP: [
            EditBatch(
                phase=Phase.SYSTEM_MAP,
                edits=(
                    Edit(
                        EditOp.ADD,
                        "system_components",
                        {
                            "id": "sm-web",
                            "name": "web",
                            "kind": "service",
                            "description": "the web frontend",
                            "depends_on": [],
                        },
                    ),
                ),
            )
        ],
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(
                    Edit(
                        EditOp.ADD,
                        "failure_modes",
                        {
                            "id": "fm-timeout",
                            "title": "upstream timeout",
                            "description": "a call to an upstream service times out",
                            "severity": "warning",
                            "user_visible": False,
                            "caused_by": [],
                            "coverage_status": "excluded",
                            "exclusion_reason": "not independently detectable",
                        },
                    ),
                    Edit(
                        EditOp.ADD,
                        "failure_modes",
                        {
                            "id": "fm-503",
                            "title": "web returns 503",
                            "description": "the web frontend serves 503s to users",
                            "severity": "critical",
                            "user_visible": True,
                            "caused_by": ["fm-timeout"],
                            "coverage_status": "alertable",
                        },
                    ),
                ),
            )
        ],
        Phase.METRIC_COVERAGE: [
            EditBatch(
                phase=Phase.METRIC_COVERAGE,
                edits=(
                    Edit(
                        EditOp.ADD,
                        "metrics",
                        {
                            "id": "mx-errors",
                            "name": "http_requests_total",
                            "type": "counter",
                            "labels": ["status"],
                            "emitted_at": ["web/handler.go:10"],
                            "description": "request count by status",
                        },
                    ),
                    Edit(
                        EditOp.UPDATE,
                        "coverage",
                        {"failure_mode_id": "fm-503", "detecting_metric_ids": ["mx-errors"]},
                    ),
                ),
            )
        ],
        Phase.ALERT_RECOMMENDATIONS: [
            EditBatch(
                phase=Phase.ALERT_RECOMMENDATIONS,
                edits=(
                    Edit(
                        EditOp.ADD,
                        "alert_recommendations",
                        {
                            "id": "ar-503",
                            "name": "High503Rate",
                            "expr": "up == 0",
                            "for_duration": "5m",
                            "severity": "critical",
                            "failure_mode_ids": ["fm-503"],
                            "annotations": {"summary": "s", "description": "d"},
                        },
                    ),
                    Edit(
                        EditOp.UPDATE,
                        "coverage",
                        {"failure_mode_id": "fm-503", "alert_ids": ["ar-503"]},
                    ),
                ),
            )
        ],
    }


def test_contract_analyze_happy_path_writes_full_artifact_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full analyze happy path: four checkpoints, all approved; the write path
    writes the whole artifact set to disk (entries, derived docs, default config,
    state last); exit 0; summary counts and gap counts are real."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert sessions[0].closed
    summary = presenter.summaries[0]
    assert summary.outcome == "analysis complete"
    assert not summary.discarded
    # 1 system component + 2 failure modes + 1 metric + 1 alert + 2 mechanically
    # created coverage entries (one per failure mode) = 7 entries added overall.
    assert summary.entry_counts == orchestrator.EntryCounts(added=7, updated=0, removed=0)
    assert summary.gap_counts == artifacts.GapSummary(alertable=1, metric_gap=0, excluded=1)

    root = _blare_root(repo)
    loaded = artifacts.load(root, RunMode.ANALYZE)
    assert set(loaded.system_components) == {"sm-web"}
    assert set(loaded.failure_modes) == {"fm-timeout", "fm-503"}
    assert loaded.failure_modes["fm-503"].caused_by == ("fm-timeout",)
    assert set(loaded.metrics) == {"mx-errors"}
    assert set(loaded.alert_recommendations) == {"ar-503"}
    assert loaded.coverage["fm-503"].detecting_metric_ids == ("mx-errors",)
    assert loaded.coverage["fm-503"].alert_ids == ("ar-503",)
    assert artifacts.semantic_violations(loaded) == []
    assert (root / "config.yaml").is_file()
    for doc_name in (
        "system-map.md",
        "failure-modes.md",
        "metrics.md",
        "metric-recommendations.md",
        "alert-recommendations.md",
        "coverage.md",
    ):
        doc_path = root / "docs" / doc_name
        assert doc_path.is_file()
        assert doc_path.read_bytes().startswith(artifacts.GENERATED_DOC_HEADER.encode())


def test_contract_checkpoint_chat_routes_to_session_and_represents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: chat at a checkpoint routes the text through `AgentSession.chat`, the
    reply renders via `show_chat_reply` (re-offering the same checkpoint prompt,
    never redrawing the view), and approval after the chat exchange proceeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(monkeypatch, chat_script=["noted, nothing further to add"])
    presenter = FakePresenter()
    # Phase 1's checkpoint: chat first, then approve; phases 2-4 auto-approve.
    presenter.checkpoint_replies = [orchestrator.Chat("what about auth?")]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert sessions[0].chat_calls == ["what about auth?"]
    assert presenter.chat_reply_calls == [
        ("noted, nothing further to add", PromptKind.CHECKPOINT)
    ]
    # Exactly one CheckpointView was presented for phase 1 (the view is not redrawn
    # after chat -- cli.md); the run still reached all four phases.
    assert [view.phase for view in presenter.checkpoint_views] == list(Phase)


def test_contract_sink_rejects_batch_for_frozen_and_unvisited_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit sink enforces the phase-state rule (architecture: Edit-proposal
    protocol): a batch tagged for an already-frozen phase, or for a phase not yet
    open, is rejected -- never silently applied or crashing the run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    stray_batch_for_frozen_phase = EditBatch(
        phase=Phase.SYSTEM_MAP,
        edits=(
            Edit(
                EditOp.ADD,
                "system_components",
                {
                    "id": "sm-late",
                    "name": "late",
                    "kind": "service",
                    "description": "proposed after phase 1 already froze",
                    "depends_on": [],
                },
            ),
        ),
    )
    stray_batch_for_unvisited_phase = EditBatch(
        phase=Phase.ALERT_RECOMMENDATIONS,
        edits=(
            Edit(
                EditOp.ADD,
                "alert_recommendations",
                {
                    "id": "ar-early",
                    "name": "TooEarly",
                    "expr": "up == 0",
                    "for_duration": "5m",
                    "severity": "warning",
                    "failure_mode_ids": [],
                    "annotations": {"summary": "s", "description": "d"},
                },
            ),
        ),
    )
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase={
            # Phase 2 tries both a frozen-phase batch (phase 1, already frozen by
            # the time phase 2 runs) and an unvisited-phase batch (phase 4, not
            # reached yet).
            Phase.FAILURE_MODES: [stray_batch_for_frozen_phase, stray_batch_for_unvisited_phase],
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert len(session.rejected_batches) == 2
    assert all(not verdict.ok for verdict in session.rejected_batches)
    assert "not open" in (session.rejected_batches[0].message or "")
    # Neither stray batch actually landed anywhere.
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.system_components == {}
    assert loaded.alert_recommendations == {}


def test_contract_abort_at_checkpoint_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R20: aborting at any checkpoint exits 3, writes nothing under `.blare/`, and
    the summary names the transcript path with a discarded entry-count split."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    presenter = FakePresenter()
    # Abort at phase 2's checkpoint (after phase 1 already froze with a real edit).
    presenter.checkpoint_replies = [orchestrator.Approve(), orchestrator.Abort()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 3
    assert sessions[0].closed
    assert not (_blare_root(repo) / "state.yaml").exists()
    assert not (_blare_root(repo) / "system-map.yaml").exists()
    assert len(presenter.summaries) == 1
    summary = presenter.summaries[0]
    assert summary.outcome == "aborted"
    assert summary.discarded
    assert summary.transcript_path is not None
    # Phases 1 and 2's edits (sm-web; fm-timeout, fm-503, plus their 2 mechanically
    # created coverage entries) were proposed and accepted into the pending
    # candidate before the abort at phase 2's checkpoint discarded all of it.
    assert summary.entry_counts == orchestrator.EntryCounts(added=5, updated=0, removed=0)
    # Only the first two phases ran before the abort.
    assert [view.phase for view in presenter.checkpoint_views] == [
        Phase.SYSTEM_MAP,
        Phase.FAILURE_MODES,
    ]


def test_contract_gate_failure_opens_system_amendment_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2.3's scoped boundary is gone (T2.4 supersedes it, per architecture.md's
    Amendment mechanism): a candidate that fails the final semantic gate (an
    unmapped, non-excluded failure mode) opens a system-originated amendment
    instead of failing the run outright. Once the repair lands (via chat) and is
    approved, the run reaches final confirmation and writes the repaired set --
    nothing is written before that (R20)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits_by_phase = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(
                    Edit(
                        EditOp.ADD,
                        "failure_modes",
                        {
                            "id": "fm-unmapped",
                            "title": "never gets an alert",
                            "description": "left unmapped on purpose for this test",
                            "severity": "warning",
                            "user_visible": False,
                            "caused_by": [],
                            "coverage_status": "alertable",
                        },
                    ),
                ),
            )
        ],
    }
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits_by_phase,
        chat_script=["fixed"],
        chat_run_control_by_text={
            "fix it": [RunControlCall(RunControlAction.AMEND_COMPLETE, {})]
        },
        chat_edits_by_text={
            "fix it": [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-unmapped", ["fm-unmapped"]),
                        _coverage_alert_edit("fm-unmapped", ["ar-unmapped"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.amendment_replies = [orchestrator.Chat("fix it"), orchestrator.Approve()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert len(presenter.errors) == 0
    assert len(presenter.amendment_views) == 2
    assert presenter.amendment_views[0].origin is AmendmentOrigin.SYSTEM
    assert presenter.amendment_rejectable_seen == [False, False]
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert set(loaded.alert_recommendations) == {"ar-unmapped"}
    assert artifacts.semantic_violations(loaded) == []
    assert sessions[0].closed


def test_contract_sigint_during_write_completes_and_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """orchestrator.md, Write path: SIGINT is masked during the write -- a real
    signal arriving between primitives is deferred, the write completes, and the
    run is reported as what it is: completed (exit 0), not aborted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch)
    presenter = FakePresenter()

    original_write_docs = artifacts.write_docs

    def _send_sigint_then_write(root: Path, s: artifacts.ArtifactSet) -> artifacts.WriteReport:
        os.kill(os.getpid(), 2)  # signal.SIGINT, injected mid-write
        return original_write_docs(root, s)

    monkeypatch.setattr(artifacts, "write_docs", _send_sigint_then_write)

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "analysis complete"
    assert (_blare_root(repo) / "state.yaml").is_file()


def test_contract_derived_doc_restored_at_final_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R10: a derived doc hand-edited *during a checkpoint pause of the same run* --
    the single-run construction that dodges R1's inverse refusal, since nothing was
    at that path when preflight's `init_inspection` ran -- is restored to the
    canonical form of the final candidate at final confirmation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    doc_path = _blare_root(repo) / "docs" / "system-map.md"

    class _EditDuringPhase1(FakePresenter):
        def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
            if view.phase is Phase.SYSTEM_MAP:
                doc_path.parent.mkdir(parents=True, exist_ok=True)
                doc_path.write_text(
                    artifacts.GENERATED_DOC_HEADER + "\nhand-edited during the pause\n"
                )
            return super().present_checkpoint(view)

    presenter = _EditDuringPhase1()
    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    restored = doc_path.read_bytes()
    assert restored.startswith(artifacts.GENERATED_DOC_HEADER.encode())
    assert b"hand-edited during the pause" not in restored
    assert b"sm-web" in restored


def test_contract_derived_doc_not_restored_on_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abort variant of the same setup (R10, R20): nothing is written, so a
    derived doc hand-edited mid-run survives exactly as edited."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    doc_path = _blare_root(repo) / "docs" / "system-map.md"
    edited_content = artifacts.GENERATED_DOC_HEADER + "\nhand-edited during the pause\n"

    class _EditThenAbort(FakePresenter):
        def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
            if view.phase is Phase.SYSTEM_MAP:
                doc_path.parent.mkdir(parents=True, exist_ok=True)
                doc_path.write_text(edited_content)
                return orchestrator.Abort()
            return super().present_checkpoint(view)

    presenter = _EditThenAbort()
    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 3
    assert doc_path.read_text() == edited_content
    assert not (_blare_root(repo) / "state.yaml").exists()


# --- T2.4: amendments (unit mechanics, the frozen-only cascade, system amendments,
# the closure loop, outcome notification) --------------------------------------------


def _fm_edit(
    fm_id: str,
    *,
    title: str = "a failure mode",
    severity: str = "warning",
    user_visible: bool = False,
    caused_by: list[str] | None = None,
    coverage_status: str = "alertable",
    exclusion_reason: str | None = None,
) -> Edit:
    payload: dict[str, object] = {
        "id": fm_id,
        "title": title,
        "description": "d",
        "severity": severity,
        "user_visible": user_visible,
        "caused_by": caused_by or [],
        "coverage_status": coverage_status,
    }
    if exclusion_reason is not None:
        payload["exclusion_reason"] = exclusion_reason
    return Edit(EditOp.ADD, "failure_modes", payload)


def _fm_update_edit(
    fm_id: str,
    *,
    title: str = "a failure mode",
    severity: str = "warning",
    user_visible: bool = False,
    caused_by: list[str] | None = None,
    coverage_status: str = "alertable",
    exclusion_reason: str | None = None,
) -> Edit:
    payload: dict[str, object] = {
        "id": fm_id,
        "title": title,
        "description": "d",
        "severity": severity,
        "user_visible": user_visible,
        "caused_by": caused_by or [],
        "coverage_status": coverage_status,
    }
    if exclusion_reason is not None:
        payload["exclusion_reason"] = exclusion_reason
    return Edit(EditOp.UPDATE, "failure_modes", payload)


def _alert_edit(alert_id: str, fm_ids: list[str], severity: str = "critical") -> Edit:
    return Edit(
        EditOp.ADD,
        "alert_recommendations",
        {
            "id": alert_id,
            "name": alert_id,
            "expr": "up == 0",
            "for_duration": "5m",
            "severity": severity,
            "failure_mode_ids": fm_ids,
            "annotations": {"summary": "s", "description": "d"},
        },
    )


def _coverage_alert_edit(fm_id: str, alert_ids: list[str]) -> Edit:
    return Edit(EditOp.UPDATE, "coverage", {"failure_mode_id": fm_id, "alert_ids": alert_ids})


def _coverage_metric_edit(fm_id: str, detecting_metric_ids: list[str]) -> Edit:
    return Edit(
        EditOp.UPDATE,
        "coverage",
        {"failure_mode_id": fm_id, "detecting_metric_ids": detecting_metric_ids},
    )


def test_contract_agent_amendment_single_phase_no_cascade_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent-proposed amendment naming one frozen phase, whose repair touches
    nothing any other phase references: no cascade, one re-presentation, approval
    re-freezes exactly that phase. Proposed during the (frozen) phase-4 checkpoint's
    chat, resumed via `request_repair` since the proposing turn ends without
    `amend_complete` (agent.md's resume path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_edits_by_text={"please amend phase 1": []},
        chat_run_control_by_text={
            "please amend phase 1": [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ]
        },
        repair_edits_by_phases={
            (Phase.SYSTEM_MAP,): [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web",
                                "kind": "service",
                                "description": "revised during the amendment",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    # Phase 4's checkpoint: chat first (proposes the amendment), then approve.
    presenter.checkpoint_replies = [
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Chat("please amend phase 1"),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.request_repair_calls == [((Phase.SYSTEM_MAP,), ())]
    assert session.notify_outcomes == [(True, ())]
    assert len(presenter.amendment_views) == 1
    view = presenter.amendment_views[0]
    assert view.origin is AmendmentOrigin.AGENT
    assert presenter.amendment_rejectable_seen == [True]
    assert [s.phase for s in view.sections] == [Phase.SYSTEM_MAP]
    [section] = view.sections
    assert [c.id for c in section.updated] == ["sm-web"]

    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.system_components["sm-web"].description == "revised during the amendment"


def test_contract_amendment_cascade_reference_and_invariant_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cascade pulls in a frozen phase via a semantic-violation repair phase
    (the invariant half: the unit's own origin, an `EMPTY_LINKAGE_METRIC_RECOMMENDATION`
    violation opening metric coverage) and a *different* frozen phase via
    `referencing_phases` (the reference half: repairing the linkage also touches
    fm-A, which ar-A references, cascading into alert recommendations -- reachable
    here only because this is a *system*-originated unit: by the time the gate
    runs, every phase has already frozen, unlike any agent-proposed amendment,
    which can only ever arise before phase 4 itself has frozen). The unit is
    re-presented once; approval re-freezes exactly the phases frozen when it
    opened."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(_fm_edit("fm-a", severity="critical", coverage_status="alertable"),),
            )
        ],
        Phase.METRIC_COVERAGE: [
            EditBatch(
                phase=Phase.METRIC_COVERAGE,
                edits=(
                    Edit(
                        EditOp.ADD,
                        "metrics",
                        {
                            "id": "mx-1",
                            "name": "some_metric_total",
                            "type": "counter",
                            "labels": [],
                            "emitted_at": ["a.go:1"],
                            "description": "d",
                        },
                    ),
                    # A metric recommendation with no failure modes named yet --
                    # the EMPTY_LINKAGE_METRIC_RECOMMENDATION violation the gate
                    # will find, deliberately left broken for this test.
                    Edit(
                        EditOp.ADD,
                        "metric_recommendations",
                        {
                            "id": "mr-bad",
                            "kind": "new",
                            "failure_mode_ids": [],
                            "rationale": "r",
                            "details": "d",
                        },
                    ),
                ),
            )
        ],
        Phase.ALERT_RECOMMENDATIONS: [
            EditBatch(
                phase=Phase.ALERT_RECOMMENDATIONS,
                edits=(_alert_edit("ar-a", ["fm-a"]), _coverage_alert_edit("fm-a", ["ar-a"])),
            )
        ],
    }
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        repair_edits_by_phases={
            (Phase.METRIC_COVERAGE,): [
                EditBatch(
                    phase=Phase.METRIC_COVERAGE,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "metric_recommendations",
                            {
                                "id": "mr-bad",
                                "kind": "new",
                                "failure_mode_ids": ["fm-a"],
                                "rationale": "r",
                                "details": "d",
                            },
                        ),
                        _coverage_metric_edit("fm-a", []),
                    ),
                )
            ],
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # Round 1: the system unit's own opening announcement, carrying the invariant
    # violation that triggered it. Round 2: a pure-reference cascade (no new
    # violation drove it) into alert recommendations, via ar-a referencing fm-a.
    assert len(session.request_repair_calls) == 2
    first_phases, first_violations = session.request_repair_calls[0]
    assert first_phases == (Phase.METRIC_COVERAGE,)
    assert {v.kind for v in first_violations} == {ViolationKind.EMPTY_LINKAGE_METRIC_RECOMMENDATION}
    second_phases, second_violations = session.request_repair_calls[1]
    assert second_phases == (Phase.ALERT_RECOMMENDATIONS,)
    assert second_violations == ()
    assert len(presenter.amendment_views) == 1  # re-presented once
    assert presenter.amendment_views[0].origin is AmendmentOrigin.SYSTEM
    assert {s.phase for s in presenter.amendment_views[0].sections} == {
        Phase.METRIC_COVERAGE,
        Phase.ALERT_RECOMMENDATIONS,
    }
    assert session.notify_outcomes == [(True, ())]

    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.metric_recommendations["mr-bad"].failure_mode_ids == ("fm-a",)
    assert artifacts.semantic_violations(loaded) == []


def test_contract_agent_amendment_cascades_into_already_frozen_phase_via_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An *agent*-proposed amendment can cascade into an already-frozen phase
    too, not only a system-originated one (the prior cascade test's origin):
    proposed during phase 4's own checkpoint chat (phase 3 already frozen),
    renaming fm-503 -- which mr-x references -- pulls metric coverage in via
    pure `referencing_phases`, no violation involved, logged as a genuine
    cascade join in the run log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    edits[Phase.METRIC_COVERAGE] = [
        *edits[Phase.METRIC_COVERAGE],
        EditBatch(
            phase=Phase.METRIC_COVERAGE,
            edits=(
                Edit(
                    EditOp.ADD,
                    "metric_recommendations",
                    {
                        "id": "mr-x",
                        "kind": "new",
                        "failure_mode_ids": ["fm-503"],
                        "rationale": "r",
                        "details": "d",
                    },
                ),
            ),
        ),
    ]
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_run_control_by_text={
            "please rename fm-503": [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [2]})
            ]
        },
        repair_edits_by_phases={
            (Phase.FAILURE_MODES,): [
                EditBatch(
                    phase=Phase.FAILURE_MODES,
                    edits=(
                        _fm_update_edit(
                            "fm-503",
                            title="web returns 503 (renamed)",
                            severity="critical",
                            user_visible=True,
                            caused_by=["fm-timeout"],
                            coverage_status="alertable",
                        ),
                    ),
                )
            ],
        },
    )
    presenter = FakePresenter()
    presenter.checkpoint_replies = [
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Chat("please rename fm-503"),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.request_repair_calls[0] == ((Phase.FAILURE_MODES,), ())
    second_phases, second_violations = session.request_repair_calls[1]
    assert second_phases == (Phase.METRIC_COVERAGE,)
    assert second_violations == ()  # pure reference, no invariant involved
    assert len(session.request_repair_calls) == 2
    assert len(presenter.amendment_views) == 1
    assert presenter.amendment_views[0].origin is AmendmentOrigin.AGENT
    assert {s.phase for s in presenter.amendment_views[0].sections} == {
        Phase.FAILURE_MODES,
        Phase.METRIC_COVERAGE,
    }
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.failure_modes["fm-503"].title == "web returns 503 (renamed)"

    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    run_log_dir = state_home / "blare" / repo_id / "runs"
    [log_path] = list(run_log_dir.glob("*.jsonl"))
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    cascade_events = [e for e in lines if e["event"] == "amendment_cascade_joined"]
    assert len(cascade_events) == 1
    assert cascade_events[0]["phases"] == [3]
    assert cascade_events[0]["violation_count"] == 0


def test_contract_amendment_names_unvisited_ahead_phase_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An amendment naming an unvisited (ahead) phase opens it; approval leaves it
    open rather than freezing it (it was never frozen to begin with) -- it takes its
    ordinary checkpoint when the queue reaches it, and its unit repairs are present
    as pending edits there (opening a phase for a repair never substitutes for
    running it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(
                    _fm_edit("fm-timeout", coverage_status="excluded", exclusion_reason="r"),
                    _fm_edit(
                        "fm-503",
                        severity="critical",
                        user_visible=True,
                        caused_by=["fm-timeout"],
                        coverage_status="alertable",
                    ),
                ),
            )
        ],
    }
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        run_control_calls_by_phase={
            Phase.FAILURE_MODES: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [4]})
            ]
        },
        repair_edits_by_phases={
            (Phase.ALERT_RECOMMENDATIONS,): [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-early", ["fm-503"]),
                        _coverage_alert_edit("fm-503", ["ar-early"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.request_repair_calls == [((Phase.ALERT_RECOMMENDATIONS,), ())]
    assert session.notify_outcomes == [(True, ())]
    # The ordinary phase-4 checkpoint still fired, showing the amendment's repairs
    # as its own content -- its baseline was captured when the amendment opened it,
    # not "nothing changed" (opening a phase never substitutes for running it).
    phase4_views = [v for v in presenter.checkpoint_views if v.phase is Phase.ALERT_RECOMMENDATIONS]
    assert len(phase4_views) == 1
    assert [c.id for c in phase4_views[0].added] == ["ar-early"]

    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert set(loaded.alert_recommendations) == {"ar-early"}
    assert loaded.coverage["fm-503"].alert_ids == ("ar-early",)


def test_contract_amendment_names_unvisited_ahead_phase_rejected_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting an amendment that opened an unvisited phase returns it to
    unvisited, its repairs discarded; the phase's own later checkpoint runs
    normally and the final artifact set reflects only the ordinary work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(
                    _fm_edit("fm-timeout", coverage_status="excluded", exclusion_reason="r"),
                    _fm_edit(
                        "fm-503",
                        severity="critical",
                        user_visible=True,
                        caused_by=["fm-timeout"],
                        coverage_status="alertable",
                    ),
                ),
            )
        ],
        Phase.ALERT_RECOMMENDATIONS: [
            EditBatch(
                phase=Phase.ALERT_RECOMMENDATIONS,
                edits=(
                    _alert_edit("ar-final", ["fm-503"]),
                    _coverage_alert_edit("fm-503", ["ar-final"]),
                ),
            )
        ],
    }
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        run_control_calls_by_phase={
            Phase.FAILURE_MODES: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [4]})
            ]
        },
        repair_edits_by_phases={
            (Phase.ALERT_RECOMMENDATIONS,): [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-early", ["fm-503"]),
                        _coverage_alert_edit("fm-503", ["ar-early"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.amendment_replies = [orchestrator.Reject()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.notify_outcomes == [(False, (Phase.ALERT_RECOMMENDATIONS,))]
    phase4_views = [v for v in presenter.checkpoint_views if v.phase is Phase.ALERT_RECOMMENDATIONS]
    assert len(phase4_views) == 1
    assert [c.id for c in phase4_views[0].added] == ["ar-final"]

    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert set(loaded.alert_recommendations) == {"ar-final"}
    assert loaded.coverage["fm-503"].alert_ids == ("ar-final",)


def test_contract_run_control_totality_amend_proposal_and_amend_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run-control totality for the amendment actions: a proposal adding a
    non-open phase mid-unit joins the unit (join-over-reject precedence); a
    proposal naming only already-open phases is rejected as a verdict; an
    amend_complete with no unit open is rejected as a verdict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        run_control_calls_by_phase={
            Phase.FAILURE_MODES: [
                # No unit open yet: rejected as a verdict.
                RunControlCall(RunControlAction.AMEND_COMPLETE, {}),
                # Opens a unit on phase 1.
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]}),
                # Joins phase 3 (non-open) -- phase 1 (already open, part of the
                # unit) is a no-op within it.
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1, 3]}),
                # Names only already-open phases (1 and 3, both open now): rejected.
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]}),
            ],
        },
        # Both opened phases must be resolved before the run can finish: repair
        # them trivially and close the unit without residual violations.
        repair_edits_by_phases={
            (Phase.SYSTEM_MAP, Phase.METRIC_COVERAGE): [],
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    verdicts = session.run_control_verdicts
    assert len(verdicts) == 4
    assert verdicts[0].ok is False  # amend_complete, no unit open
    assert verdicts[1].ok is True  # amend_proposal opens phase 1
    assert verdicts[2].ok is True  # amend_proposal joins phase 3 (phase 1 a no-op)
    assert verdicts[3].ok is False  # amend_proposal naming only already-open phases


def test_contract_amendment_open_when_run_phase_returns_defers_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An amendment unit open when `run_phase` returns defers that phase's
    checkpoint: the unit is resumed via `request_repair` to closure and
    re-presentation, and only then does the checkpoint present -- recorded here by
    checking the amendment view is presented before the phase's own checkpoint
    view (the gate never fires while a unit is open, so the run only reaches exit 0
    if the deferred checkpoint eventually did present and get approved)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    # The amendment fires during phase 2's own turn (phase 1 is frozen by then).
    _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        run_control_calls_by_phase={
            Phase.FAILURE_MODES: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ]
        },
        repair_edits_by_phases={
            (Phase.SYSTEM_MAP,): [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web",
                                "kind": "service",
                                "description": "repaired mid-phase-2",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert len(presenter.amendment_views) == 1
    # Phase 2's own checkpoint view (added failure modes) still shows up, and
    # every phase's checkpoint fired in order despite the mid-run amendment.
    assert [v.phase for v in presenter.checkpoint_views] == list(Phase)
    fm_view = next(v for v in presenter.checkpoint_views if v.phase is Phase.FAILURE_MODES)
    assert {c.id for c in fm_view.added} == {"fm-timeout", "fm-503"}
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.system_components["sm-web"].description == "repaired mid-phase-2"


def test_contract_amend_proposal_during_ordinary_checkpoint_chat_defers_and_represents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `amend_proposal` arising during an ordinary checkpoint's chat defers that
    checkpoint: the chat reply still renders (via `prompt=None`, mooting the
    in-progress prompt), the unit runs to closure, and the checkpoint re-presents
    fresh afterward."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_script=["noted, opening an amendment"],
        chat_run_control_by_text={
            "what about sm-web?": [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ]
        },
        repair_edits_by_phases={
            (Phase.SYSTEM_MAP,): [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web",
                                "kind": "service",
                                "description": "revised via checkpoint chat",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    # Phase 1's own checkpoint: chat first (opens an amendment on the very phase
    # under checkpoint... no: use phase 2's checkpoint chatting about phase 1,
    # which is frozen by then).
    presenter.checkpoint_replies = [
        orchestrator.Approve(),
        orchestrator.Chat("what about sm-web?"),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.chat_calls == ["what about sm-web?"]
    # The chat reply rendered with prompt=None (mooted) rather than re-offering the
    # checkpoint prompt directly.
    assert ("noted, opening an amendment", None) in presenter.chat_reply_calls
    assert len(presenter.amendment_views) == 1
    # Phase 2's checkpoint re-presented fresh after the unit closed: two views for
    # phase 2 would indicate a redraw bug, but cli.md's presenter is stateless per
    # call, so what matters is a *second* present_checkpoint call happened for
    # phase 2 after the amendment resolved -- i.e. the run reached phase 2's
    # approval and continued, which only exit 0 for the full run confirms here.
    assert [v.phase for v in presenter.checkpoint_views] == [
        Phase.SYSTEM_MAP,
        Phase.FAILURE_MODES,
        Phase.FAILURE_MODES,
        Phase.METRIC_COVERAGE,
        Phase.ALERT_RECOMMENDATIONS,
    ]
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.system_components["sm-web"].description == "revised via checkpoint chat"


def test_contract_amendment_representation_chat_lands_batch_returns_to_closure_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chat during an amendment's re-presentation that lands a batch (and signals
    completion) returns the unit to the closure loop: recompute runs, and the next
    `AmendmentView` shows the updated set -- two distinct views, not a redraw of the
    same one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_run_control_by_text={
            "please open phase 1": [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ],
            "also fix the name": [RunControlCall(RunControlAction.AMEND_COMPLETE, {})],
        },
        chat_edits_by_text={
            "also fix the name": [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web-2",
                                "kind": "service",
                                "description": "the web frontend",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.checkpoint_replies = [
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Chat("please open phase 1"),
    ]
    presenter.amendment_replies = [
        orchestrator.Chat("also fix the name"),
        orchestrator.Approve(),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert len(presenter.amendment_views) == 2
    first, second = presenter.amendment_views
    assert first != second
    [first_section] = first.sections
    assert not first_section.added and not first_section.updated and not first_section.removed
    [second_section] = second.sections
    assert second_section.updated and second_section.updated[0].id == "sm-web"
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.system_components["sm-web"].name == "web-2"


def test_contract_system_amendment_no_reject_chat_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A semantic violation at the approval gate opens a system-originated
    amendment: no reject is offered, and chat that repairs the violation converges
    (the gate re-fires and passes once the residual repair lands)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(
                    _fm_edit(
                        "fm-orphan",
                        severity="critical",
                        user_visible=True,
                        coverage_status="alertable",
                    ),
                ),
            )
        ],
        # Phase 4 runs and freezes without ever mapping fm-orphan to an alert.
    }
    _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_script=["fixed"],
        chat_run_control_by_text={
            "let me fix that": [RunControlCall(RunControlAction.AMEND_COMPLETE, {})]
        },
        chat_edits_by_text={
            "let me fix that": [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-orphan", ["fm-orphan"]),
                        _coverage_alert_edit("fm-orphan", ["ar-orphan"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.amendment_replies = [orchestrator.Chat("let me fix that"), orchestrator.Approve()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert presenter.amendment_rejectable_seen == [False, False]
    assert presenter.amendment_views[0].origin is AmendmentOrigin.SYSTEM
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert artifacts.semantic_violations(loaded) == []


def test_contract_system_amendment_abort_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abort is always available at a system-originated amendment's
    re-presentation (R2), even though reject is not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(_fm_edit("fm-orphan", severity="critical", coverage_status="alertable"),),
            )
        ],
    }
    _ready_session(monkeypatch, edits_by_phase=edits)
    presenter = FakePresenter()
    presenter.amendment_replies = [orchestrator.Abort()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 3
    assert not (_blare_root(repo) / "state.yaml").exists()


def test_contract_gate_loop_residual_violation_raises_second_system_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A system-originated unit approved with a residual violation elsewhere
    re-fails the gate and raises a second unit; the write is reached only after a
    passing check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(
                    _fm_edit("fm-a", severity="critical", coverage_status="alertable"),
                    _fm_edit("fm-b", severity="critical", coverage_status="alertable"),
                ),
            )
        ],
    }
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_script=["fixed fm-a", "fixed fm-b"],
        chat_run_control_by_text={
            "fix fm-a": [RunControlCall(RunControlAction.AMEND_COMPLETE, {})],
            "fix fm-b": [RunControlCall(RunControlAction.AMEND_COMPLETE, {})],
        },
        chat_edits_by_text={
            "fix fm-a": [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(_alert_edit("ar-a", ["fm-a"]), _coverage_alert_edit("fm-a", ["ar-a"])),
                )
            ],
            "fix fm-b": [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(_alert_edit("ar-b", ["fm-b"]), _coverage_alert_edit("fm-b", ["ar-b"])),
                )
            ],
        },
    )
    presenter = FakePresenter()
    presenter.amendment_replies = [
        orchestrator.Chat("fix fm-a"),
        orchestrator.Approve(),
        orchestrator.Chat("fix fm-b"),
        orchestrator.Approve(),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    # Approving the first unit right after fixing only fm-a (fm-b's violation
    # still stands) closes it prematurely -- the gate re-fires and opens a
    # *second*, separate system unit for the residual violation, never reaching
    # the write until that one closes too: four presentations across two units
    # (open -> Chat fixes fm-a -> Approve closes early; re-open -> Chat fixes
    # fm-b -> Approve closes for good), all system-originated.
    assert len(presenter.amendment_views) == 4
    assert all(v.origin is AmendmentOrigin.SYSTEM for v in presenter.amendment_views)
    assert sessions[0].notify_outcomes == [(True, ()), (True, ())]
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert set(loaded.alert_recommendations) == {"ar-a", "ar-b"}
    assert artifacts.semantic_violations(loaded) == []


def test_failure_reject_at_non_rejectable_amendment_is_protocol_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `FakePresenter` returning `Reject` at a system-originated (non-rejectable)
    unit's re-presentation is a protocol violation, handled as an unexpected
    exception (exit 2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(_fm_edit("fm-orphan", severity="critical", coverage_status="alertable"),),
            )
        ],
    }
    _ready_session(monkeypatch, edits_by_phase=edits)
    presenter = FakePresenter()
    presenter.amendment_replies = [orchestrator.Reject()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2


def test_contract_amendment_representation_chat_lands_batch_without_amend_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-presentation chat that lands a batch *without* the model also calling
    amend_complete in that same chat turn (legal: request_repair's completion
    contract only ever applies to the announcing round, never to re-presentation
    chat) still returns the unit to the closure loop and converges -- a
    regression test for a first-review-round bug where `_advance_unit` asserted
    a phase must always be pending whenever `amend_complete_received` was False,
    which does not hold once a unit has already been through one closure round."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_run_control_by_text={
            "please open phase 1": [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ],
            # No run_control call here -- landing this batch does not also
            # signal amend_complete, unlike every other amendment test.
        },
        chat_edits_by_text={
            "one more tweak, no need to confirm": [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web",
                                "kind": "service",
                                "description": "tweaked without amend_complete",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.checkpoint_replies = [
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Chat("please open phase 1"),
    ]
    presenter.amendment_replies = [
        orchestrator.Chat("one more tweak, no need to confirm"),
        orchestrator.Approve(),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert len(presenter.amendment_views) == 2
    second_view = presenter.amendment_views[1]
    [section] = second_view.sections
    assert section.updated and section.updated[0].id == "sm-web"
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert loaded.system_components["sm-web"].description == "tweaked without amend_complete"
    assert sessions[0].notify_outcomes == [(True, ())]


def test_contract_restore_from_baseline_revives_removed_failure_mode_coverage(
    tmp_path: Path,
) -> None:
    """`_restore_from_baseline` restores a failure mode's coverage byte-for-byte
    even when it was *removed* by the amendment and the unit never opened
    metric coverage or alert recommendations at all -- a regression test for a
    first-review-round bug where the removed entry's absence from `current`
    (mechanical coverage completeness deletes it alongside the failure mode)
    made the restore fall back to an empty entry instead of reviving the
    baseline's."""
    root = tmp_path / ".blare"
    root.mkdir()
    base = artifacts.empty_set(root)
    baseline = artifacts.apply(
        base,
        EditBatch(
            phase=Phase.FAILURE_MODES,
            edits=(_fm_edit("fm-b", coverage_status="excluded", exclusion_reason="r"),),
        ),
    )
    baseline = artifacts.apply(
        baseline,
        EditBatch(
            phase=Phase.METRIC_COVERAGE,
            edits=(_coverage_metric_edit("fm-b", ["mx-1"]),),
        ),
    )
    current = artifacts.apply(
        baseline,
        EditBatch(phase=Phase.FAILURE_MODES, edits=(Edit(EditOp.REMOVE, "failure_modes", "fm-b"),)),
    )
    assert "fm-b" not in current.coverage  # mechanical completeness removed it

    restored = orchestrator._restore_from_baseline(
        current, baseline, {Phase.FAILURE_MODES: orchestrator._PhaseStatus.FROZEN}
    )

    assert "fm-b" in restored.failure_modes
    assert restored.coverage["fm-b"].detecting_metric_ids == ("mx-1",)


def test_contract_amendment_naming_unvisited_repair_phase_does_not_expand_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A violation whose repair phase is unvisited does not expand the unit
    (orchestrator.md's Test plan, distinct from the frozen-vs-currently-open
    exclusion other tests cover): the amendment fires during phase 2's own
    turn, well before phase 4 (alert recommendations) has ever run, so a
    naturally-occurring UNMAPPED_FAILURE_MODE violation there (fm-x has no
    alert yet) must not pull phase 4 into the unit -- it stays unvisited and
    only opens later, on its own ordinary turn."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(_fm_edit("fm-x", severity="critical", coverage_status="alertable"),),
            )
        ],
        Phase.ALERT_RECOMMENDATIONS: [
            EditBatch(
                phase=Phase.ALERT_RECOMMENDATIONS,
                edits=(_alert_edit("ar-x", ["fm-x"]), _coverage_alert_edit("fm-x", ["ar-x"])),
            )
        ],
    }
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        run_control_calls_by_phase={
            Phase.FAILURE_MODES: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ]
        },
        repair_edits_by_phases={
            (Phase.SYSTEM_MAP,): [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.ADD,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web",
                                "kind": "service",
                                "description": "added during the amendment",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # fm-x is unmapped (no alert yet) at the moment of the recompute -- a real
    # violation whose repair phase is ALERT_RECOMMENDATIONS, still unvisited at
    # that point, so it must not appear anywhere in what got announced/joined.
    assert session.request_repair_calls == [((Phase.SYSTEM_MAP,), ())]
    assert len(presenter.amendment_views) == 1
    assert {s.phase for s in presenter.amendment_views[0].sections} == {Phase.SYSTEM_MAP}
    # Phase 4 still took its own, ordinary checkpoint later (never substituted).
    assert [v.phase for v in presenter.checkpoint_views] == list(Phase)
    loaded = artifacts.load(_blare_root(repo), RunMode.ANALYZE)
    assert artifacts.semantic_violations(loaded) == []


def test_contract_run_log_records_amendment_units_and_gate_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run log records amendment units and gate results alongside the
    ordinary preflight/phase events (orchestrator.md, Failure visibility and
    Test plan: "amendment units, gate results... all present"): a unit
    opening, a unit closing, and a failed-then-passed gate. The cascade-join
    event (`amendment_cascade_joined`) is asserted separately, in
    `test_contract_agent_amendment_cascades_into_already_frozen_phase_via_reference`,
    whose scenario actually produces one -- this one's single-phase violation
    never cascades."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    edits: dict[Phase, list[EditBatch]] = {
        Phase.FAILURE_MODES: [
            EditBatch(
                phase=Phase.FAILURE_MODES,
                edits=(_fm_edit("fm-orphan", severity="critical", coverage_status="alertable"),),
            )
        ],
    }
    _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_script=["fixed"],
        chat_run_control_by_text={
            "let me fix that": [RunControlCall(RunControlAction.AMEND_COMPLETE, {})]
        },
        chat_edits_by_text={
            "let me fix that": [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-orphan", ["fm-orphan"]),
                        _coverage_alert_edit("fm-orphan", ["ar-orphan"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.amendment_replies = [orchestrator.Chat("let me fix that"), orchestrator.Approve()]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    run_log_dir = state_home / "blare" / repo_id / "runs"
    [log_path] = list(run_log_dir.glob("*.jsonl"))
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    events = [entry["event"] for entry in lines]
    assert "gate_failed" in events
    assert "amendment_unit_opened" in events
    assert "amendment_unit_closed" in events
    opened = next(e for e in lines if e["event"] == "amendment_unit_opened")
    assert opened["origin"] == "system"
    assert opened["phases"] == [4]
    closed = next(e for e in lines if e["event"] == "amendment_unit_closed")
    assert closed["approved"] is True
    assert "gate_passed" in events


# --- Failure-mode tests ---------------------------------------------------------------


def test_failure_state_dir_unwritable_exits_1_naming_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`$XDG_STATE_HOME` pointing at an unwritable location exits 1 at step 2,
    naming the path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    readonly_home = tmp_path / "readonly-state"
    readonly_home.mkdir(mode=0o500)
    monkeypatch.setenv("XDG_STATE_HOME", str(readonly_home))
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    try:
        code = orchestrator.run(RunMode.ANALYZE, repo, presenter)
        assert code == 1
        cause, _next_action, _detail = presenter.errors[0]
        assert str(readonly_home) in cause
    finally:
        readonly_home.chmod(0o700)


def test_failure_gitrepo_command_error_during_preflight_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `GitCommandError` during preflight exits 1, carrying git's stderr."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)

    def _raise(*_a: object, **_k: object) -> list[str]:
        raise gitrepo.GitCommandError(cause="git exploded", next_action="investigate")

    monkeypatch.setattr(gitrepo.GitRepo, "dirty_paths_outside", _raise)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "git exploded" in cause


def test_failure_artifacts_structural_error_exits_1_naming_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifacts structural error (unreadable/invalid file) exits 1 naming the
    file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha="deadbeef")
    (_blare_root(repo) / "metrics.yaml").write_text("- id: not-an-mx-prefix\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1
    cause, _next_action, _detail = presenter.errors[0]
    assert "metrics.yaml" in cause


def test_failure_agent_auth_required_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AgentSession.start` raising `AuthRequiredError` (R12) exits 1."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch, ready=False)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 1


def test_failure_agent_session_error_mid_phase_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AgentSessionError` raised mid-phase (the session dying) exits 2, `.blare/`
    is untouched, and the transcript path is still printed (orchestrator.md's
    failure-mode test plan: "agent: AgentSessionError mid-phase")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        raise_in_phase={
            Phase.FAILURE_MODES: agent.AgentSessionError(
                cause="phase 2: transport error", next_action="Re-run blare."
            )
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2
    cause, _next_action, _detail = presenter.errors[0]
    assert "transport error" in cause
    assert not (_blare_root(repo) / "state.yaml").exists()
    assert sessions[0].closed
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "failed"
    assert presenter.summaries[0].discarded
    assert presenter.summaries[0].transcript_path is not None


def test_failure_write_primitive_failure_mid_write_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write primitive raising partway through the write path (artifacts.md:
    `WriteError` naming the failing file) exits 2, leaves `state.yaml` untouched on
    disk (it is written last), and the reports already logged show what landed
    before the failure (orchestrator.md's failure-mode test plan: "artifacts: ...
    write failure mid-write (injected) -> exit 2, state file untouched on disk,
    report shows what landed")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    presenter = FakePresenter()

    def _raise(root: Path, s: artifacts.ArtifactSet) -> artifacts.WriteReport:
        raise artifacts.WriteError(
            cause=f"{root / 'docs'} could not be written (disk full)",
            next_action="Check disk space, then re-run blare.",
        )

    monkeypatch.setattr(artifacts, "write_docs", _raise)

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2
    cause, _next_action, _detail = presenter.errors[0]
    assert "could not be written" in cause
    assert not (_blare_root(repo) / "state.yaml").exists()
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "failed"
    assert presenter.summaries[0].discarded
    assert sessions[0].closed

    repo_id = gitrepo.GitRepo.discover(repo).repo_id()
    run_log_dir = state_home / "blare" / repo_id / "runs"
    [log_path] = list(run_log_dir.glob("*.jsonl"))
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    write_reports = [entry for entry in lines if entry.get("event") == "write_report"]
    # write_entries_and_config (the primitive before the one that raised) already
    # logged its report -- "the reports collected so far" the design doc promises.
    assert any(r["primitive"] == "write_entries_and_config" for r in write_reports)
    assert not any(r["primitive"] == "write_state" for r in write_reports)


def test_contract_write_recheck_aborts_on_mid_run_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R20's write-time re-check: a commit landing on the repo after the run
    started (before final confirmation) aborts the write (exit 2), and nothing
    under `.blare/` is created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)

    class _CommitDuringPhase1(FakePresenter):
        def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
            if view.phase is Phase.SYSTEM_MAP:
                (repo / "README.md").write_text("changed after the run started\n")
                _commit_all(repo, "a commit landing mid-run")
            return super().present_checkpoint(view)

    sessions = _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    presenter = _CommitDuringPhase1()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2
    cause, _next_action, _detail = presenter.errors[0]
    assert "changed since this run started" in cause
    assert not (_blare_root(repo)).exists()
    assert sessions[0].closed


def test_contract_write_recheck_aborts_on_mid_run_canonical_yaml_hand_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R20's write-time re-check: a hand edit to canonical YAML (not a derived doc)
    landing mid-run aborts the write (exit 2), distinct from a derived-doc edit
    (R10), which never aborts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    canonical_path = _blare_root(repo) / "system-map.yaml"

    class _HandEditCanonicalDuringPhase1(FakePresenter):
        def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
            if view.phase is Phase.SYSTEM_MAP:
                # A fresh run's baseline has nothing at this path yet
                # (`init_inspection` verified that); a file appearing here mid-run
                # is exactly what `raw_bytes_match` must catch.
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_text("- id: sm-hand-edited\n")
            return super().present_checkpoint(view)

    sessions = _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    presenter = _HandEditCanonicalDuringPhase1()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2
    cause, _next_action, _detail = presenter.errors[0]
    assert "canonical YAML" in cause
    assert not (_blare_root(repo) / "state.yaml").exists()
    assert sessions[0].closed


def test_contract_run_control_calls_reach_the_real_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run-control handling is total (architecture): every action reaches the real
    handler and gets a verdict, never a raise. `affected_verdict`/`no_impact` stay
    rejected in analyze mode (diff-mode-only, R18); `amend_proposal`/
    `amend_complete` are real (T2.4) -- proposing and completing an amendment for
    an unvisited phase, all within one turn, is accepted both times and needs no
    `request_repair` round-trip (the model already knows, having done it itself)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    calls = [
        RunControlCall(action=RunControlAction.AFFECTED_VERDICT, payload={"phases": [1]}),
        RunControlCall(action=RunControlAction.NO_IMPACT, payload={}),
        RunControlCall(action=RunControlAction.AMEND_PROPOSAL, payload={"phases": [4]}),
        RunControlCall(action=RunControlAction.AMEND_COMPLETE, payload={}),
    ]
    sessions = _ready_session(
        monkeypatch, run_control_calls_by_phase={Phase.SYSTEM_MAP: calls}
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    verdicts = sessions[0].run_control_verdicts
    assert len(verdicts) == 4
    assert verdicts[0].ok is False
    assert verdicts[1].ok is False
    assert verdicts[2].ok is True
    assert verdicts[3].ok is True
    assert "diff-mode verdict" in (verdicts[0].message or "")
    assert "diff-mode verdict" in (verdicts[1].message or "")
    # The amendment closed within the same turn (no request_repair round-trip):
    # phase 4 was opened from unvisited, so it stays open, taking its ordinary
    # checkpoint later.
    assert sessions[0].request_repair_calls == []
    assert len(presenter.amendment_views) == 1


def test_contract_checkpoint_view_carries_real_entry_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `CheckpointView` the phase engine builds from real `ArtifactSet` entries
    (via `_phase_diff`/`_entry_change`) carries the actual added entry's id, type,
    and fields -- not just a count -- for phase 1 and phase 2's chain (R3)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(monkeypatch, edits_by_phase=_happy_path_edits())
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    phase1_view, phase2_view = presenter.checkpoint_views[0], presenter.checkpoint_views[1]
    assert len(phase1_view.added) == 1
    added_component = phase1_view.added[0]
    assert added_component.entry_type == "system_components"
    assert added_component.id == "sm-web"
    assert ("name", "web") in added_component.fields
    assert ("kind", "service") in added_component.fields

    added_failure_modes = {change.id: change for change in phase2_view.added}
    assert set(added_failure_modes) == {"fm-timeout", "fm-503"}
    assert ("caused_by", "fm-timeout") in added_failure_modes["fm-503"].fields
    assert ("severity", "critical") in added_failure_modes["fm-503"].fields
    assert not phase1_view.updated
    assert not phase1_view.removed


# --- Type re-exports sanity (nothing here calls these directly, but they must be
# importable per the module's __all__) -------------------------------------------------


def test_contract_error_types_are_blare_errors() -> None:
    """Every preflight-owned error type derives from the system's one error shape."""
    from blare.model import BlareError

    for exc_type in (
        StateDirectoryError,
        DirtyWorkingTreeError,
        LockHeldError,
        NonAncestorSHAError,
        NonInteractiveError,
    ):
        assert issubclass(exc_type, BlareError)


# ==== T3.1: update core -- triage, verdict seeding, no-impact flow, SHA-only
# advance =============================================================================


def test_contract_update_triage_affected_verdict_seeds_named_phases_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """triage's affected_verdict seeds exactly the named phase(s); unaffected
    phases never pause (R18): only phase 3's checkpoint is presented, and only
    phase 3's own artifacts change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    before = {
        name: (_blare_root(repo) / name).read_bytes()
        for name in ("system-map.yaml", "failure-modes.yaml", "coverage.yaml")
    }
    second_sha = commit_file_update(repo, "src/metrics.py", "# add a metric\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
        ],
        edits_by_phase={
            Phase.METRIC_COVERAGE: [
                EditBatch(
                    phase=Phase.METRIC_COVERAGE,
                    edits=(
                        Edit(
                            EditOp.ADD,
                            "metrics",
                            {
                                "id": "mx-new",
                                "name": "http_requests_total",
                                "type": "counter",
                                "labels": [],
                                "emitted_at": ["src/metrics.py:1"],
                                "description": "d",
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.triage_called
    assert session.ran_phases == [Phase.METRIC_COVERAGE]
    assert [v.phase for v in presenter.checkpoint_views] == [Phase.METRIC_COVERAGE]
    assert not presenter.no_impact_views

    root = _blare_root(repo)
    loaded = artifacts.load(root, RunMode.UPDATE)
    assert loaded.analyzed_sha == second_sha
    assert set(loaded.metrics) == {"mx-new"}
    # Unaffected phases' files are untouched (R9): system map and failure modes
    # keep their exact bytes; coverage keeps its mechanical shape unchanged too.
    assert (root / "system-map.yaml").read_bytes() == before["system-map.yaml"]
    assert (root / "failure-modes.yaml").read_bytes() == before["failure-modes.yaml"]
    assert (root / "coverage.yaml").read_bytes() == before["coverage.yaml"]


def test_contract_update_no_impact_empty_queue_confirms_sha_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no_impact conclusion with an empty queue is presented for confirmation;
    approval is the final confirmation for the run and changes exactly the state
    SHA (plus any derived-doc restoration) -- no entry file changes (R18)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    before = {
        name: (_blare_root(repo) / name).read_bytes()
        for name in (
            "system-map.yaml",
            "failure-modes.yaml",
            "metrics.yaml",
            "metric-recommendations.yaml",
            "alert-recommendations.yaml",
            "coverage.yaml",
        )
    }
    before_config = (_blare_root(repo) / "config.yaml").read_bytes()
    second_sha = commit_file_update(repo, "README.md", "unrelated doc change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "docs-only change"})
        ],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.triage_called
    assert session.ran_phases == []
    assert not presenter.checkpoint_views
    assert len(presenter.no_impact_views) == 1
    view = presenter.no_impact_views[0]
    assert view.conclusion == "docs-only change"
    assert view.delta_file_count == 1
    assert view.delta_files == ("README.md",)

    root = _blare_root(repo)
    loaded = artifacts.load(root, RunMode.UPDATE)
    assert loaded.analyzed_sha == second_sha
    for name, content in before.items():
        assert (root / name).read_bytes() == content, f"{name} changed on a no-impact run"
    assert (root / "config.yaml").read_bytes() == before_config
    summary = presenter.summaries[0]
    assert summary.entry_counts == orchestrator.EntryCounts(added=0, updated=0, removed=0)


def test_contract_update_no_impact_rejected_when_triage_seeded_phase_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no_impact conclusion is rejected as a verdict while the queue is non-empty
    from the agent's own affected_verdict earlier in the same triage turn (R18:
    "the seeded phases still need work")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]}),
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing else needed"}),
        ],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert [v.ok for v in session.run_control_verdicts] == [True, False]
    assert "still need" in (session.run_control_verdicts[1].message or "")
    assert not presenter.no_impact_views
    assert session.ran_phases == [Phase.METRIC_COVERAGE]


def test_contract_update_no_impact_rejected_when_load_seeded_violation_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no_impact conclusion is rejected when step 7's semantic check already
    seeded a repair phase from a load-time invariant violation (R18) -- even
    though triage itself opened nothing yet. The model then retries with an
    affected_verdict naming the repair phase, which the run completes normally."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_update_state_with_semantic_violation(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing to do"}),
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [4]}),
        ],
        edits_by_phase={
            Phase.ALERT_RECOMMENDATIONS: [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-orphan", ["fm-orphan"], severity="warning"),
                        _coverage_alert_edit("fm-orphan", ["ar-orphan"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert [v.ok for v in session.run_control_verdicts] == [False, True]
    assert not presenter.no_impact_views
    assert session.ran_phases == [Phase.ALERT_RECOMMENDATIONS]
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert artifacts.semantic_violations(loaded) == []


def test_contract_update_load_seeded_violation_repaired_proactively_after_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3.2: a load-seeded violation whose repair phase the model never names
    (it settles on some other affected phase instead) is repaired proactively
    -- `_repair_residual_violations` opens a system-originated unit for it and
    calls `request_repair` right after `triage()` returns, before the queue is
    ever drained -- rather than surviving all the way to `_finalize_and_write`'s
    own gate. The unit's approval leaves the (previously `unvisited`) repair
    phase open, and the ordinary queue drain still gives it its own checkpoint
    afterward: "opening a phase for a repair never substitutes for running
    it" holds for this proactive path too, not only for the gate's."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_update_state_with_semantic_violation(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing to do"}),
            # Settles on phase 2 -- not the violation's own repair phase (4) --
            # so the violation is still present when triage() returns and
            # `_repair_residual_violations` finds it.
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [2]}),
        ],
        repair_edits_by_phases={
            (Phase.ALERT_RECOMMENDATIONS,): [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-orphan", ["fm-orphan"], severity="warning"),
                        _coverage_alert_edit("fm-orphan", ["ar-orphan"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # request_repair fired proactively, naming the violation, before either
    # phase's own ordinary checkpoint ran -- this is what distinguishes the
    # proactive path from the pre-T3.2 gate-only fallback, which would only
    # ever call request_repair *after* every named phase had already run.
    assert len(session.request_repair_calls) == 1
    repaired_phases, repaired_violations = session.request_repair_calls[0]
    assert repaired_phases == (Phase.ALERT_RECOMMENDATIONS,)
    assert [v.kind for v in repaired_violations] == [ViolationKind.UNMAPPED_FAILURE_MODE]
    assert session.call_order == [
        "triage",
        "request_repair:4",
        "run_phase:2",
        "run_phase:4",
    ]
    # Phase 2 ran normally first (settled by the verdict); phase 4 ran
    # afterward too, as its own ordinary checkpoint -- the repair itself
    # landed via request_repair, not via run_phase.
    assert session.ran_phases == [Phase.FAILURE_MODES, Phase.ALERT_RECOMMENDATIONS]
    assert [v.phase for v in presenter.checkpoint_views] == [
        Phase.FAILURE_MODES,
        Phase.ALERT_RECOMMENDATIONS,
    ]
    # The proactive repair's own amendment was presented (system origin,
    # non-rejectable) in addition to phase 4's own ordinary checkpoint above.
    assert len(presenter.amendment_views) == 1
    assert presenter.amendment_views[0].origin is orchestrator.AmendmentOrigin.SYSTEM
    assert session.notify_outcomes == [(True, ())]
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert artifacts.semantic_violations(loaded) == []
    assert set(loaded.alert_recommendations) == {"ar-orphan"}


def test_contract_update_gate_still_catches_violation_introduced_after_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3.2: `_repair_residual_violations` only ever sees what's wrong right
    after `triage()` returns -- a violation a later phase's *own* edits
    introduce (nothing was wrong in the loaded state, so the proactive check
    is a no-op here) is still caught, exactly as before T3.2, by
    `_finalize_and_write`'s own gate once the queue empties: the proactive
    check is an addition, not a replacement for that fallback."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [2]})
        ],
        edits_by_phase={
            # Phase 2's own turn introduces a fresh, unmapped failure mode --
            # nothing was wrong until this lands, so only the gate (not the
            # proactive post-triage check) can ever catch it.
            Phase.FAILURE_MODES: [
                EditBatch(phase=Phase.FAILURE_MODES, edits=(_fm_edit("fm-fresh"),))
            ]
        },
        repair_edits_by_phases={
            (Phase.ALERT_RECOMMENDATIONS,): [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-fresh", ["fm-fresh"], severity="warning"),
                        _coverage_alert_edit("fm-fresh", ["ar-fresh"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # Exactly one request_repair call: the gate's own, once phase 2's edit
    # made the candidate violate R4 -- nothing to repair right after triage.
    assert len(session.request_repair_calls) == 1
    repaired_phases, repaired_violations = session.request_repair_calls[0]
    assert repaired_phases == (Phase.ALERT_RECOMMENDATIONS,)
    assert [v.kind for v in repaired_violations] == [ViolationKind.UNMAPPED_FAILURE_MODE]
    assert session.ran_phases == [Phase.FAILURE_MODES, Phase.ALERT_RECOMMENDATIONS]
    # Phase 4 opened only via the gate's system unit (prior: unvisited), so it
    # still gets its own ordinary checkpoint once the gate's loop drains the
    # queue again, in addition to phase 2's.
    assert [v.phase for v in presenter.checkpoint_views] == [
        Phase.FAILURE_MODES,
        Phase.ALERT_RECOMMENDATIONS,
    ]
    assert len(presenter.amendment_views) == 1
    assert presenter.amendment_views[0].origin is orchestrator.AmendmentOrigin.SYSTEM
    assert session.notify_outcomes == [(True, ())]
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert artifacts.semantic_violations(loaded) == []
    assert set(loaded.alert_recommendations) == {"ar-fresh"}


def test_contract_update_affected_verdict_naming_open_phase_is_noop_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An affected_verdict naming an already-open phase is acknowledged as a
    no-op (architecture.md's run-control totality) -- it neither duplicates the
    phase in the queue nor causes a second checkpoint."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
        ],
        run_control_calls_by_phase={
            Phase.METRIC_COVERAGE: [
                RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert session.run_control_verdicts[-1].ok is True
    assert session.ran_phases == [Phase.METRIC_COVERAGE]
    assert [v.phase for v in presenter.checkpoint_views] == [Phase.METRIC_COVERAGE]


def test_contract_update_affected_verdict_naming_frozen_phase_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An affected_verdict naming an already-frozen phase is rejected, directing
    the agent to amend_proposal instead (orchestrator.md, Amendments)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3, 4]})
        ],
        run_control_calls_by_phase={
            Phase.ALERT_RECOMMENDATIONS: [
                RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    rejected = session.run_control_verdicts[-1]
    assert rejected.ok is False
    assert "amend_proposal" in (rejected.message or "")
    assert session.ran_phases == [Phase.METRIC_COVERAGE, Phase.ALERT_RECOMMENDATIONS]


def test_contract_update_amendment_ahead_phase_gets_ordinary_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase engine's queue is genuinely dynamic in update mode too: an
    amendment proposed mid-run naming an unvisited phase not in triage's original
    queue still gets its own ordinary checkpoint once the queue reaches it (reusing
    T2.4's amendment machinery rather than a parallel implementation)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        # Triage names only phase 2; phase 4 is opened later, mid-run, by an
        # agent-proposed amendment -- it was never in the original queue.
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [2]})
        ],
        edits_by_phase={
            Phase.FAILURE_MODES: [
                EditBatch(
                    phase=Phase.FAILURE_MODES,
                    edits=(
                        _fm_edit(
                            "fm-503",
                            severity="critical",
                            user_visible=True,
                            coverage_status="alertable",
                        ),
                    ),
                )
            ],
        },
        run_control_calls_by_phase={
            Phase.FAILURE_MODES: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [4]})
            ]
        },
        repair_edits_by_phases={
            (Phase.ALERT_RECOMMENDATIONS,): [
                EditBatch(
                    phase=Phase.ALERT_RECOMMENDATIONS,
                    edits=(
                        _alert_edit("ar-503", ["fm-503"]),
                        _coverage_alert_edit("fm-503", ["ar-503"]),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # Phase 4 was never named by triage, yet its ordinary checkpoint still fires
    # once the (dynamically recomputed) queue reaches it -- "opening a phase for a
    # repair never substitutes for running it" holds in update mode too.
    assert session.ran_phases == [Phase.FAILURE_MODES, Phase.ALERT_RECOMMENDATIONS]
    assert [v.phase for v in presenter.checkpoint_views] == [
        Phase.FAILURE_MODES,
        Phase.ALERT_RECOMMENDATIONS,
    ]
    assert session.notify_outcomes == [(True, ())]
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert set(loaded.alert_recommendations) == {"ar-503"}


def test_contract_update_r6_r8_r9_multi_commit_delta_only_affected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6/R8/R9: a delta spanning multiple commits is handled as one delta (not
    per-commit); only the triage-affected phase's artifacts change; the recorded
    SHA is the delta's end commit captured at run start."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    before = {
        name: (_blare_root(repo) / name).read_bytes()
        for name in ("system-map.yaml", "metrics.yaml", "coverage.yaml")
    }
    commit_file_update(repo, "src/a.py", "# change a\n")
    final_sha = commit_file_update(repo, "src/b.py", "# change b\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [2]})
        ],
        edits_by_phase={
            Phase.FAILURE_MODES: [
                EditBatch(
                    phase=Phase.FAILURE_MODES,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "failure_modes",
                            {
                                "id": "fm-timeout",
                                "title": "upstream timeout (revised)",
                                "description": "a call to an upstream service times out",
                                "severity": "warning",
                                "user_visible": False,
                                "caused_by": [],
                                "coverage_status": "excluded",
                                "exclusion_reason": "not independently detectable",
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # R8: gitrepo computed one delta over both new commits -- triage saw one
    # message naming both changed files, not two separate triage calls.
    assert session.started_with is not None
    _, context = session.started_with
    assert set(context.delta_files) == {"src/a.py", "src/b.py"}

    root = _blare_root(repo)
    loaded = artifacts.load(root, RunMode.UPDATE)
    # R6: the recorded SHA is the delta's end commit captured at run start.
    assert loaded.analyzed_sha == final_sha
    assert loaded.failure_modes["fm-timeout"].title == "upstream timeout (revised)"
    # R9: every other phase's artifacts are untouched.
    for name, content in before.items():
        assert (root / name).read_bytes() == content, f"{name} changed unexpectedly"


def test_contract_update_t4_4_patch_text_flows_into_run_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T4.4: for a non-empty delta, RunContext.patch_text carries gitrepo.patch_text's
    return value for the captured (analyzed_sha, end_sha, '.blare') range -- asserted
    via the fake GitRepo's recorded arguments, not by re-deriving the diff text here,
    alongside the existing context.delta_files assertion."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    final_sha = commit_file_update(repo, "src/a.py", "# change a\n")
    _isolate_state_home(monkeypatch, tmp_path)

    recorded_args: list[tuple[str, str, str]] = []
    sentinel_patch_text = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+# change a\n"

    def _fake_patch_text(
        self: gitrepo.GitRepo, base_sha: str, end_sha: str, exclude: str
    ) -> str:
        recorded_args.append((base_sha, end_sha, exclude))
        return sentinel_patch_text

    monkeypatch.setattr(gitrepo.GitRepo, "patch_text", _fake_patch_text)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [2]})
        ],
        edits_by_phase={
            Phase.FAILURE_MODES: [
                EditBatch(
                    phase=Phase.FAILURE_MODES,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "failure_modes",
                            {
                                "id": "fm-timeout",
                                "title": "upstream timeout (revised)",
                                "description": "a call to an upstream service times out",
                                "severity": "warning",
                                "user_visible": False,
                                "caused_by": [],
                                "coverage_status": "excluded",
                                "exclusion_reason": "not independently detectable",
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    assert recorded_args == [(first_sha, final_sha, ".blare")]
    session = sessions[0]
    assert session.started_with is not None
    _, context = session.started_with
    assert set(context.delta_files) == {"src/a.py"}
    assert context.patch_text == sentinel_patch_text


def test_contract_update_r7_empty_delta_still_exits_0_with_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7's own e2e proof at the unit level: an empty effective delta (same commit
    as recorded) exits 0, produces zero diff, and never constructs a session --
    `agent.create_client`/`agent.AgentSession` are never touched (T2.2's
    already-written short-circuit; T3.1 supplies the missing proof)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    before = (_blare_root(repo) / "failure-modes.yaml").read_bytes()
    _isolate_state_home(monkeypatch, tmp_path)

    def _boom() -> None:
        raise AssertionError("agent.create_client must never be called on the R7 path")

    monkeypatch.setattr(agent, "create_client", _boom)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    summary = presenter.summaries[0]
    assert summary.outcome == "up to date"
    assert summary.transcript_path is None
    assert (_blare_root(repo) / "failure-modes.yaml").read_bytes() == before
    state = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert state.analyzed_sha == first_sha


def test_contract_update_no_impact_rejected_when_unit_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no_impact conclusion is rejected as a verdict while an amendment unit is
    open (orchestrator.md: "a no_impact verdict arriving while any unit is open
    is rejected as a verdict... close the unit first")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [2]}),
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing else"}),
        ],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert [v.ok for v in session.run_control_verdicts] == [True, False]
    assert "close it" in (session.run_control_verdicts[1].message or "")
    assert not presenter.no_impact_views


def test_contract_update_no_impact_withdrawn_by_same_turn_amend_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3.2: the opposite order from the test above -- an accepted `no_impact`
    followed, in the *same* triage turn, by an `amend_proposal` (nothing in
    `_handle_amend_proposal` forbids this sequence). The unit resolved right
    after `triage()` returns leaves its named phase durably open, so the
    now-stale conclusion must never be presented for approval: this is R18's
    withdrawal applied at the same point a mid-chat redirect would apply it,
    just triggered before the no-impact screen is ever shown at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing obvious"}),
            RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [3]}),
        ],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert [v.ok for v in session.run_control_verdicts] == [True, True]
    # The conclusion was withdrawn -- never presented at all.
    assert not presenter.no_impact_views
    # Phase 3 was opened from unvisited by the amendment: it stays open and
    # takes its own ordinary checkpoint (opening a phase for a repair never
    # substitutes for running it).
    assert session.ran_phases == [Phase.METRIC_COVERAGE]
    assert [v.phase for v in presenter.checkpoint_views] == [Phase.METRIC_COVERAGE]
    assert session.notify_outcomes == [(True, ())]


def test_contract_update_affected_verdict_unvisited_phase_rejected_when_unit_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An affected_verdict naming an unvisited phase is rejected as a verdict
    while an amendment unit is open (orchestrator.md: "while a unit is open...
    an affected_verdict naming an unvisited phase are both rejected as
    verdicts... close the unit first")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [2]}),
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]}),
        ],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert [v.ok for v in session.run_control_verdicts] == [True, False]
    assert "close it" in (session.run_control_verdicts[1].message or "")
    # Phase 3 was never opened -- the rejected verdict named it, nothing else did.
    assert Phase.METRIC_COVERAGE not in session.ran_phases


def test_contract_update_no_impact_chat_reopening_a_phase_still_gets_its_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a run_control call issued mid-chat during the no-impact
    confirmation -- before approval -- must not be silently skipped. A phase
    opened that way still gets its own ordinary checkpoint before anything is
    written; approving the (now stale) no-impact prompt must never bypass that
    review (R2, R18)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    reconsider_text = "actually, does this affect metric coverage?"
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing obvious"})
        ],
        chat_run_control_by_text={
            reconsider_text: [
                RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
            ]
        },
        edits_by_phase={
            Phase.METRIC_COVERAGE: [
                EditBatch(
                    phase=Phase.METRIC_COVERAGE,
                    edits=(
                        Edit(
                            EditOp.ADD,
                            "metrics",
                            {
                                "id": "mx-late",
                                "name": "late_metric_total",
                                "type": "counter",
                                "labels": [],
                                "emitted_at": ["src/x.py:1"],
                                "description": "d",
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.no_impact_replies = [orchestrator.Chat(reconsider_text)]
    presenter.chat_reply_script = [orchestrator.Approve()]

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # The phase opened mid-chat still got its own real turn and checkpoint --
    # never silently written without review.
    assert session.ran_phases == [Phase.METRIC_COVERAGE]
    assert [v.phase for v in presenter.checkpoint_views] == [Phase.METRIC_COVERAGE]
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert set(loaded.metrics) == {"mx-late"}


def test_contract_update_no_impact_chat_opening_amendment_still_gets_presented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an amend_proposal (plus its repair edit) issued mid-chat
    during the no-impact confirmation still gets its amendment re-presented,
    and the phase it opened (from unvisited) still gets its own ordinary
    checkpoint afterward -- neither is written without going through its own
    presentation, whatever the no-impact prompt's own reply was (R2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    reconsider_text = "let's double check phase 3"
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing obvious"})
        ],
        chat_run_control_by_text={
            reconsider_text: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [3]})
            ]
        },
        repair_edits_by_phases={
            (Phase.METRIC_COVERAGE,): [
                EditBatch(
                    phase=Phase.METRIC_COVERAGE,
                    edits=(
                        Edit(
                            EditOp.ADD,
                            "metrics",
                            {
                                "id": "mx-sneaky",
                                "name": "sneaky_metric_total",
                                "type": "counter",
                                "labels": [],
                                "emitted_at": ["src/x.py:1"],
                                "description": "d",
                            },
                        ),
                    ),
                )
            ]
        },
    )
    presenter = FakePresenter()
    presenter.no_impact_replies = [orchestrator.Chat(reconsider_text)]
    presenter.chat_reply_script = [orchestrator.Approve()]

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # The amendment was genuinely presented and approved -- never silently
    # written alongside the no-impact approval.
    assert len(presenter.amendment_views) == 1
    assert session.notify_outcomes == [(True, ())]
    # Phase 3 was opened from unvisited by the amendment: it stays open and
    # takes its own ordinary checkpoint afterward (opening a phase for a repair
    # never substitutes for running it).
    assert session.ran_phases == [Phase.METRIC_COVERAGE]
    assert [v.phase for v in presenter.checkpoint_views] == [Phase.METRIC_COVERAGE]
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert set(loaded.metrics) == {"mx-sneaky"}


# --- T3.2: dynamic phase-queue expansion via a revised (bare) affected_verdict,
# ahead of and behind the run's current position -----------------------------


def test_contract_update_dynamic_expansion_ahead_and_behind_via_revised_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R18's dynamic-expansion clause: triage names only phase 3; mid-phase-3,
    the model revises its verdict twice more -- once naming phase 2 (behind the
    run's current position) and once naming phase 4 (ahead) -- via a bare
    `affected_verdict`, no amendment involved. Both still get their own
    ordinary checkpoint, in phase order, once phase 3's is done: this needed no
    new mechanism (`_handle_affected_verdict` already opens whatever phase it
    names; `_drain_phase_queue` already re-reads `phase_status` fresh every
    iteration), so this test is this task's whole contribution to that clause."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
        ],
        run_control_calls_by_phase={
            Phase.METRIC_COVERAGE: [
                RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [2]}),
                RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [4]}),
            ]
        },
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert [v.ok for v in session.run_control_verdicts] == [True, True, True]
    # Phase 3 (triage's own) ran first; the behind phase (2) and the ahead
    # phase (4) both ran afterward, in phase order -- neither substituted for
    # the other, and neither was silently skipped.
    assert session.ran_phases == [
        Phase.METRIC_COVERAGE,
        Phase.FAILURE_MODES,
        Phase.ALERT_RECOMMENDATIONS,
    ]
    assert [v.phase for v in presenter.checkpoint_views] == [
        Phase.METRIC_COVERAGE,
        Phase.FAILURE_MODES,
        Phase.ALERT_RECOMMENDATIONS,
    ]


# --- T3.2: the R18 redirect at the no-impact confirmation --------------------


def test_contract_update_no_impact_redirect_rejected_unit_represents_conclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R18's redirect, the rejection half (orchestrator.md: "on rejection the
    restore covers the whole pre-unit state, the withdrawn conclusion
    included, and the no-impact confirmation is re-presented"): chat during
    the no-impact confirmation proposes an amendment (mooting the conclusion),
    the unit is rejected, nothing else is left open, and the *same* no-impact
    conclusion is presented again -- approving that second presentation is
    what finally proceeds, exactly the confirmation the agent's original
    conclusion was based on."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    reconsider_text = "wait, maybe phase 3 needs a look"
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing obvious"})
        ],
        chat_run_control_by_text={
            reconsider_text: [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [3]})
            ]
        },
    )
    presenter = FakePresenter()
    presenter.no_impact_replies = [orchestrator.Chat(reconsider_text)]
    # The amendment presentation (rejectable, agent-origin) is rejected; the
    # no-impact confirmation that follows is then approved for real.
    presenter.amendment_replies = [orchestrator.Reject()]
    presenter.no_impact_replies.append(orchestrator.Approve())

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    # The prompt was mooted (prompt=None), not re-offered with the stale
    # conclusion -- this is what distinguishes a redirect from an ordinary
    # chat re-offer.
    assert any(prompt is None for _text, prompt in presenter.chat_reply_calls)
    assert len(presenter.amendment_views) == 1
    assert session.notify_outcomes == [(False, (Phase.METRIC_COVERAGE,))]
    # The no-impact screen was presented twice: the original, then the fresh
    # re-presentation after the rejected redirect.
    assert len(presenter.no_impact_views) == 2
    # Nothing ever opened for real: phase 3 was restored to unvisited, so no
    # phase ever ran, and only the SHA/derived docs would change at write time.
    assert session.ran_phases == []
    assert presenter.checkpoint_views == []
    loaded = artifacts.load(_blare_root(repo), RunMode.UPDATE)
    assert loaded.analyzed_sha == repo_head(repo)


def test_contract_update_no_impact_redirect_bare_verdict_withdraws_for_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R18's redirect via a bare `affected_verdict` (no amendment unit at all):
    once a phase is durably open there is no "reject" to fall back to, so the
    conclusion is withdrawn permanently -- the no-impact screen is presented
    exactly once, never again, and the newly opened phase gets its own
    ordinary checkpoint."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    reconsider_text = "actually, metric coverage needs a look"
    sessions = _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing obvious"})
        ],
        chat_run_control_by_text={
            reconsider_text: [
                RunControlCall(RunControlAction.AFFECTED_VERDICT, {"phases": [3]})
            ]
        },
    )
    presenter = FakePresenter()
    presenter.no_impact_replies = [orchestrator.Chat(reconsider_text)]

    code = orchestrator.run(RunMode.UPDATE, repo, presenter)

    assert code == 0
    session = sessions[0]
    assert any(prompt is None for _text, prompt in presenter.chat_reply_calls)
    assert len(presenter.no_impact_views) == 1
    assert session.ran_phases == [Phase.METRIC_COVERAGE]
    assert [v.phase for v in presenter.checkpoint_views] == [Phase.METRIC_COVERAGE]


# ==== T4.3: progress ticker (R25) ===================================================
#
# `FakePresenter.progress`/`event_order` and `FakeAgentSession.slow_call`
# /`activity_script`/`on_activity` (defined above) are this section's own fakes,
# per orchestrator.md's Test plan. `_PROGRESS_TICK_INTERVAL_SECONDS` is
# monkeypatched down to a few tens of milliseconds so a driving call held open
# for a handful of scripted, tiny real sleeps still ticks more than once for
# real without costing seconds of test time; `_sequential_clock` supplies the
# module's own documented seam (`Callable[[], float]`) so every *reported*
# elapsed-time value is exact and independent of real timing precision -- only
# how many ticks occur (never what they report) depends on real scheduling.


def _sequential_clock(step: float = 5.0) -> Callable[[], float]:
    """A deterministic elapsed-time source: returns 0.0, step, 2*step, ... on
    successive calls, decoupled from real wall-clock time -- the module's own
    test seam for the ticker's clock (orchestrator.md's Test plan)."""
    values = iter(float(n) * step for n in range(100_000))

    def _clock() -> float:
        return next(values)

    return _clock


def test_contract_activity_cell_set_get_reset() -> None:
    """The activity cell starts at None, reflects the most recent `set`, and
    `reset` clears it back to None -- the primitive `AgentSession`'s
    `on_activity` callback and the ticker share across threads."""
    cell = orchestrator._ActivityCell()
    assert cell.get() is None
    cell.set("propose_edits")
    assert cell.get() == "propose_edits"
    cell.set("Read")
    assert cell.get() == "Read"
    cell.reset()
    assert cell.get() is None


def test_contract_progress_ticker_first_tick_is_always_zero_and_none() -> None:
    """The very first tick is always exactly elapsed=0.0/last_activity=None --
    hardcoded, never read from the clock or the activity cell, so it can never
    race whatever the driving call's own first tool dispatch does concurrently
    (orchestrator.md, "Progress ticker (R25)")."""
    presenter = FakePresenter()
    cell = orchestrator._ActivityCell()
    cell.set("stale-from-a-previous-call")  # must not leak into the first tick
    ticker = orchestrator._ProgressTicker(
        presenter, "phase 1 — system map", cell, _sequential_clock(), interval=10.0
    )

    ticker.start()
    try:
        # The interval (10s) is far longer than this test can wait, so only the
        # hardcoded first tick is observed -- deterministic, no timing race.
        time.sleep(0.02)
    finally:
        ticker.stop()

    assert presenter.progress_calls == [("phase 1 — system map", 0.0, None)]


def test_contract_progress_ticker_later_ticks_reflect_clock_and_activity_cell() -> None:
    """Ticks after the first read real elapsed time from the injected clock and
    the most recent name from the shared activity cell -- both deterministic
    here via the fake clock and manual `set` calls, no real sleep needed beyond
    what it takes the background thread to iterate a few times at a tiny
    interval."""
    presenter = FakePresenter()
    cell = orchestrator._ActivityCell()
    clock = _sequential_clock(step=3.0)
    ticker = orchestrator._ProgressTicker(presenter, "triage", cell, clock, interval=0.01)

    ticker.start()
    try:
        cell.set("Grep")
        time.sleep(0.05)
    finally:
        ticker.stop()

    assert len(presenter.progress_calls) >= 2
    assert presenter.progress_calls[0] == ("triage", 0.0, None)
    for label, elapsed, activity in presenter.progress_calls[1:]:
        assert label == "triage"
        assert elapsed > 0.0
        assert elapsed % 3.0 == 0.0  # every non-first tick is a multiple of the step
        assert activity == "Grep"


def test_contract_progress_ticker_swallows_a_raising_presenter() -> None:
    """A `presenter.progress` call raising is swallowed -- the ticker keeps
    running (or at least does not crash the calling thread), per R25's
    presentation-only guarantee."""

    class _RaisingPresenter:
        def progress(self, label: str, elapsed_seconds: float, last_activity: str | None) -> None:
            raise RuntimeError("presenter is on fire")

    cell = orchestrator._ActivityCell()
    ticker = orchestrator._ProgressTicker(
        _RaisingPresenter(),  # type: ignore[arg-type]
        "chat", cell, _sequential_clock(), interval=0.01,
    )

    ticker.start()  # must not raise
    time.sleep(0.03)
    ticker.stop()  # must not raise


def test_contract_drive_stops_ticker_before_returning_and_returns_calls_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_drive` returns the wrapped call's own result, and the ticker is stopped
    (joined) before `_drive` returns -- no further progress calls can arrive
    after the caller resumes. The interval is shrunk so this is a genuine
    regression check rather than a race the default ~1s interval would pass
    trivially: with the ticker's thread left running (a broken `stop()`), a
    0.01s interval would produce roughly twenty more ticks in the 0.2s this
    test waits afterward -- this is the check a code-review mutation test
    (removing `ticker.stop()` from `_drive`) actually catches, unlike a
    default-interval version of this same assertion."""
    monkeypatch.setattr(orchestrator, "_PROGRESS_TICK_INTERVAL_SECONDS", 0.01)
    presenter = FakePresenter()
    progress_ctx = orchestrator._ProgressContext(
        activity=orchestrator._ActivityCell(), clock=_sequential_clock()
    )

    result = orchestrator._drive(presenter, progress_ctx, "chat", lambda: "the reply")

    assert result == "the reply"
    # The ticker's background thread is guaranteed joined by the time _drive
    # returns; nothing more will ever be appended from here on, even after
    # waiting twenty times the (shrunk) tick interval.
    count_immediately_after = len(presenter.progress_calls)
    time.sleep(0.2)
    assert len(presenter.progress_calls) == count_immediately_after


def test_contract_progress_ticker_stop_joins_the_background_thread() -> None:
    """`stop()` blocks until the background thread has actually exited -- not
    merely signaled to stop -- so a caller resuming immediately after `stop()`
    can never race a still-running tick against its own next action. Checked
    directly on thread liveness (immediately after `stop()` returns) rather
    than by waiting and counting ticks, which a slow-to-wake but eventually
    -stopping thread could still pass."""
    presenter = FakePresenter()
    ticker = orchestrator._ProgressTicker(
        presenter, "chat", orchestrator._ActivityCell(), _sequential_clock(), interval=0.01
    )

    ticker.start()
    ticker.stop()

    assert not ticker._thread.is_alive()  # noqa: SLF001


def test_contract_progress_ticker_run_phase_ticks_only_before_its_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticker wraps `run_phase`: phase 1 held open past several (shrunk)
    tick intervals produces a first tick at elapsed=0/last_activity=None
    naming phase 1, then further ticks naming the scripted activity -- all
    strictly before phase 1's checkpoint is presented (asserted via the fake
    presenter's unified event order), and never interleaved with it."""
    monkeypatch.setattr(orchestrator, "_PROGRESS_TICK_INTERVAL_SECONDS", 0.02)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(
        monkeypatch,
        slow_call="run_phase:1",
        activity_script=[(0.03, "Read"), (0.03, "propose_edits")],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter, clock=_sequential_clock())

    assert code == 0
    first_checkpoint_index = presenter.event_order.index("checkpoint")
    before = presenter.event_order[:first_checkpoint_index]
    assert before  # at least one tick happened before phase 1's checkpoint
    assert all(kind == "progress" for kind in before)
    phase_1_ticks = presenter.progress_calls[: len(before)]
    assert phase_1_ticks[0] == ("phase 1 — system map", 0.0, None)
    assert all(label == "phase 1 — system map" for label, _e, _a in phase_1_ticks)
    assert any(activity in ("Read", "propose_edits") for _l, _e, activity in phase_1_ticks[1:])


def test_contract_progress_ticker_chat_ticks_between_checkpoint_and_chat_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticker wraps `chat`: a checkpoint's chat reply held open past several
    (shrunk) tick intervals ticks strictly between the checkpoint's own
    presentation and `show_chat_reply` -- proof the ticker is stopped before
    the caller's next presenter call, not merely eventually."""
    monkeypatch.setattr(orchestrator, "_PROGRESS_TICK_INTERVAL_SECONDS", 0.02)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(
        monkeypatch,
        chat_script=["noted"],
        slow_call="chat",
        activity_script=[(0.03, "Grep"), (0.03, "propose_edits")],
    )
    presenter = FakePresenter()
    presenter.checkpoint_replies = [orchestrator.Chat("please reconsider")]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter, clock=_sequential_clock())

    assert code == 0
    checkpoint_index = presenter.event_order.index("checkpoint")
    chat_reply_index = presenter.event_order.index("chat_reply")
    assert chat_reply_index > checkpoint_index
    between = presenter.event_order[checkpoint_index + 1 : chat_reply_index]
    assert between  # at least one tick happened during chat
    assert all(kind == "progress" for kind in between)
    progress_before = presenter.event_order[:checkpoint_index].count("progress")
    between_calls = presenter.progress_calls[progress_before : progress_before + len(between)]
    assert between_calls[0] == ("chat", 0.0, None)
    assert any(name in ("Grep", "propose_edits") for _l, _e, name in between_calls[1:])


def test_contract_progress_ticker_triage_first_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticker wraps `triage` (update mode's first driving call): held open
    past several (shrunk) tick intervals, it ticks with the "triage" label
    before triage's own outcome is acted on, and stops before the no-impact
    confirmation is ever presented."""
    monkeypatch.setattr(orchestrator, "_PROGRESS_TICK_INTERVAL_SECONDS", 0.02)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first_sha = repo_head(repo)
    _write_valid_update_state(repo, first_sha)
    commit_file_update(repo, "src/x.py", "# change\n")
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(
        monkeypatch,
        triage_run_control_calls=[
            RunControlCall(RunControlAction.NO_IMPACT, {"reasoning": "nothing relevant"})
        ],
        slow_call="triage",
        activity_script=[(0.03, "Grep"), (0.03, "Read")],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.UPDATE, repo, presenter, clock=_sequential_clock())

    assert code == 0
    assert presenter.progress_calls[0] == ("triage", 0.0, None)
    assert any(
        label == "triage" and activity in ("Grep", "Read")
        for label, _e, activity in presenter.progress_calls[1:]
    )
    # The ticker had fully stopped (joined) before the no-impact confirmation
    # was ever presented -- no "progress" entries appear from that point on.
    no_impact_index = presenter.event_order.index("no_impact")
    assert "progress" not in presenter.event_order[no_impact_index:]


def test_contract_progress_ticker_request_repair_and_notify_amendment_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticker wraps both `request_repair` (resuming an agent-proposed
    amendment) and `notify_amendment_outcome` (closing it): each ticks with its
    own label ("repair" / "amendment outcome") while held open past several
    (shrunk) tick intervals, and request_repair's ticks land before the
    amendment's re-presentation (orchestrator.md: "every driving call... is
    wrapped")."""
    monkeypatch.setattr(orchestrator, "_PROGRESS_TICK_INTERVAL_SECONDS", 0.02)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    edits = dict(_happy_path_edits())
    sessions = _ready_session(
        monkeypatch,
        edits_by_phase=edits,
        chat_edits_by_text={"please amend phase 1": []},
        chat_run_control_by_text={
            "please amend phase 1": [
                RunControlCall(RunControlAction.AMEND_PROPOSAL, {"phases": [1]})
            ]
        },
        repair_edits_by_phases={
            (Phase.SYSTEM_MAP,): [
                EditBatch(
                    phase=Phase.SYSTEM_MAP,
                    edits=(
                        Edit(
                            EditOp.UPDATE,
                            "system_components",
                            {
                                "id": "sm-web",
                                "name": "web",
                                "kind": "service",
                                "description": "revised during the amendment",
                                "depends_on": [],
                            },
                        ),
                    ),
                )
            ]
        },
        slow_call="request_repair",
        activity_script=[(0.03, "Read"), (0.03, "propose_edits")],
    )
    presenter = FakePresenter()
    presenter.checkpoint_replies = [
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Approve(),
        orchestrator.Chat("please amend phase 1"),
    ]

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter, clock=_sequential_clock())

    assert code == 0
    session = sessions[0]
    assert session.request_repair_calls == [((Phase.SYSTEM_MAP,), ())]
    assert session.notify_outcomes == [(True, ())]
    repair_ticks = [c for c in presenter.progress_calls if c[0] == "repair"]
    assert repair_ticks
    assert repair_ticks[0] == ("repair", 0.0, None)
    assert any(name in ("Read", "propose_edits") for _l, _e, name in repair_ticks[1:])
    # request_repair's ticks all land strictly before the amendment's
    # re-presentation.
    amendment_index = presenter.event_order.index("amendment")
    before_amendment = presenter.event_order[:amendment_index]
    progress_before_amendment = before_amendment.count("progress")
    assert progress_before_amendment >= len(repair_ticks)
    # notify_amendment_outcome's own ticker (label "amendment outcome") fired
    # too, distinct from request_repair's -- both driving calls got their own
    # ticker.
    outcome_ticks = [c for c in presenter.progress_calls if c[0] == "amendment outcome"]
    assert outcome_ticks
    assert outcome_ticks[0] == ("amendment outcome", 0.0, None)


def test_failure_progress_raising_on_activity_does_not_break_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising `on_activity` callback (fired from the agent side, T4.3) never
    propagates through the orchestrator's ticker or interrupts the run --
    swallowed at the source (agent.py's own `_fire_activity`), verified here
    end to end through `orchestrator.run`."""
    monkeypatch.setattr(orchestrator, "_PROGRESS_TICK_INTERVAL_SECONDS", 0.02)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_session(
        monkeypatch,
        slow_call="run_phase:1",
        activity_script=[(0.01, "Read")],
    )
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter, clock=_sequential_clock())

    assert code == 0  # the run completed normally
    assert presenter.summaries[0].outcome == "analysis complete"
