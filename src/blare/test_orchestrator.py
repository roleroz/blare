"""Unit tests for blare.orchestrator (T2.2: the nine-step preflight sequence, the
lock, the run log, and the exit-code taxonomy).

Fakes per orchestrator.md's test plan: `FakeSDKClient` (a scripted `agent.SDKClient`
stand-in) and `FakePresenter` (records what the orchestrator reports). gitrepo and
artifacts are real, exercised over temporary git repositories -- matching the design
doc's "gitrepo and artifacts are real, over temp repos".

The phase engine, checkpoints, amendments, and the write path are T2.3 onward and are
not covered here; this file's scope is exactly the nine preflight steps (5-6 wired
but their happy-path e2e coverage is T3.x's), the lock, the run log, and exit codes.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from blare import agent, artifacts, gitrepo, orchestrator
from blare.model import RunMode
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
    """Records what the orchestrator reports; the unit-level stand-in for a TTY."""

    interactive: bool = True
    notices: list[str] = field(default_factory=list)
    errors: list[tuple[str, str, str | None]] = field(default_factory=list)
    summaries: list[RunSummary] = field(default_factory=list)

    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply:
        raise NotImplementedError

    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply:
        raise NotImplementedError

    def present_no_impact(self, view: NoImpactView) -> CheckpointReply:
        raise NotImplementedError

    def show_chat_reply(
        self, text: str, prompt: PromptKind | None
    ) -> AmendmentReply | None:
        raise NotImplementedError

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
    """A clean repo with no `.blare/` state: preflight completes, the session starts
    and closes, and the placeholder summary carries real (zero) gap counts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, repo, presenter)

    assert code == 0
    assert presenter.errors == []
    assert len(presenter.summaries) == 1
    summary = presenter.summaries[0]
    assert summary.outcome == "no changes"
    assert summary.transcript_path is not None
    assert summary.transcript_path.is_file()
    assert summary.gap_counts == artifacts.GapSummary(alertable=0, metric_gap=0, excluded=0)


def test_contract_analyze_over_existing_state_reaches_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R16: analyze with an existing, valid state file loads it (rather than
    refusing per R1) and proceeds through preflight."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_minimal_analyzed_state(repo, analyzed_sha=repo_head(repo))
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)
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
    _ready_client(monkeypatch)
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
    _ready_client(monkeypatch)
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
    _ready_client(monkeypatch)

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
    """A SIGINT after the session has started (post step 9, during the placeholder
    tail) is a session-bearing abort: it renders a summary naming the transcript
    path rather than the pre-session `aborted` notice (orchestrator.md, Error
    handling: "the summary still naming the transcript path (R14 -- a session
    ran)")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _isolate_state_home(monkeypatch, tmp_path)
    _ready_client(monkeypatch)

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
    _ready_client(monkeypatch)
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
    _ready_client(monkeypatch)

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
    _ready_client(monkeypatch)
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
