"""Unit tests for blare.orchestrator: T2.2's nine-step preflight sequence, the lock,
the run log, and the exit-code taxonomy; T2.3's phase engine, checkpoints, the
approval gate, and the write path.

Fakes per orchestrator.md's test plan: `FakeSDKClient` (a scripted `agent.SDKClient`
stand-in, used only by tests that exercise the real `agent.AgentSession`'s own auth
handshake), `FakeAgentSession` (a scripted `agent.AgentSession` stand-in -- phase edit
batches, chat replies -- for every test that needs the phase engine to actually run),
and `FakePresenter` (scripted replies, records every `CheckpointView` presented).
gitrepo and artifacts are real, exercised over temporary git repositories -- matching
the design doc's "gitrepo and artifacts are real, over temp repos".

Amendments (T2.4) and the diff-mode phase engine (T3.x) are out of this file's scope.
"""

from __future__ import annotations

import json
import os
import subprocess
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
)
from blare.orchestrator import (
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

    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
        self.checkpoint_views.append(view)
        if self.checkpoint_replies:
            return self.checkpoint_replies.pop(0)
        return orchestrator.Approve()

    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply:
        raise NotImplementedError

    def present_no_impact(self, view: NoImpactView) -> CheckpointReply:
        raise NotImplementedError

    def show_chat_reply(
        self, text: str, prompt: PromptKind | None
    ) -> AmendmentReply | None:
        self.chat_reply_calls.append((text, prompt))
        if self.chat_reply_script:
            return self.chat_reply_script.pop(0)
        return orchestrator.Approve()

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
    started_with: tuple[RunMode, RunContext] | None = field(default=None, init=False)
    ran_phases: list[Phase] = field(default_factory=list, init=False)
    chat_calls: list[str] = field(default_factory=list, init=False)
    rejected_batches: list[BatchVerdict] = field(default_factory=list, init=False)
    run_control_verdicts: list[RunControlVerdict] = field(default_factory=list, init=False)
    closed: bool = field(default=False, init=False)

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

    def run_phase(self, phase: Phase) -> None:
        self.ran_phases.append(phase)
        self._write_transcript("outbound", {"type": "phase_prompt", "phase": int(phase)})
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
        self._write_transcript("outbound", {"type": "chat", "text": text})
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
        raise NotImplementedError("analyze mode never calls triage")

    def request_repair(self, phases: object, violations: object) -> None:
        raise NotImplementedError("amendments are a later task's scope")

    def notify_amendment_outcome(self, approved: bool, restored_phases: object) -> None:
        raise NotImplementedError("amendments are a later task's scope")


def _ready_session(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool = True,
    edits_by_phase: dict[Phase, list[EditBatch]] | None = None,
    chat_script: list[str] | None = None,
    run_control_calls_by_phase: dict[Phase, list[RunControlCall]] | None = None,
    raise_in_phase: dict[Phase, Exception] | None = None,
) -> list[FakeAgentSession]:
    """Patch `agent.create_client` and `agent.AgentSession` so the phase engine runs
    against a scripted `FakeAgentSession` instead of real SDK wire replay -- what
    every test that needs preflight to reach a completed run (rather than merely
    step 9's auth success) wants. Returns the list `FakeAgentSession` instances get
    appended to as they are constructed (one per `orchestrator.run()` call), so a
    test driving multiple runs can inspect each one afterward.
    """
    monkeypatch.setattr(agent, "create_client", lambda: FakeSDKClient(ready=ready))
    scripted_edits = edits_by_phase or {}
    scripted_chat = list(chat_script or [])
    scripted_run_control = run_control_calls_by_phase or {}
    scripted_raises = raise_in_phase or {}
    sessions: list[FakeAgentSession] = []

    def _factory(
        client: object,
        sink: agent.EditSink,
        control: agent.RunControlHandler,
        stack: object,
        transcript: object,
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


def test_contract_gate_failure_reports_violations_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2.3's scoped boundary: a candidate that fails the final semantic gate (an
    unmapped, non-excluded failure mode) is not silently written, nor is an
    amendment invented -- the run fails clearly (exit 2), naming the violation, and
    writes nothing (R20)."""
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
    sessions = _ready_session(monkeypatch, edits_by_phase=edits_by_phase)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 2
    assert len(presenter.errors) == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "unmapped_failure_mode" in cause
    assert "fm-unmapped" in cause
    assert next_action != ""
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "failed"
    assert presenter.summaries[0].discarded
    assert not (_blare_root(repo) / "state.yaml").exists()
    # The session is closed on every exit from the phase engine, gate failures
    # included -- not only the approval and abort paths (agent.md: `close` is
    # idempotent and safe after any error, which only matters if it is actually
    # called on every path).
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
    """Run-control handling is total (architecture): every `run_control` action is
    rejected with a verdict naming why, never a raise -- exercised here for each
    action kind in analyze mode."""
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
    assert all(not v.ok for v in verdicts)
    assert "diff-mode verdict" in (verdicts[0].message or "")
    assert "diff-mode verdict" in (verdicts[1].message or "")
    assert "not supported in this build" in (verdicts[2].message or "")
    assert "not supported in this build" in (verdicts[3].message or "")


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
