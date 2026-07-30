"""Unit tests for blare.orchestrator (T1.1 subset: the seam-through skeleton flow).

The full nine-step preflight sequence, phase engine, amendments, lock, and write
path are T2.2/T2.3's build; these tests cover only what `run()` does today: repo
discovery, the agent handshake, and mapping the outcome to an exit code.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from blare import agent, gitrepo, orchestrator
from blare.model import RunMode
from blare.orchestrator import (
    AmendmentReply,
    AmendmentView,
    CheckpointReply,
    CheckpointView,
    NoImpactView,
    PromptKind,
    RunSummary,
)


@dataclass
class FakePresenter:
    """Records what the orchestrator reports; the unit-level stand-in for a TTY."""

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
        pass

    def error(self, cause: str, next_action: str, detail: str | None = None) -> None:
        self.errors.append((cause, next_action, detail))

    def summary(self, s: RunSummary) -> None:
        self.summaries.append(s)

    def is_interactive(self) -> bool:
        return True


@dataclass
class FakeSDKClient:
    """A scripted SDKClient stand-in for orchestrator-level tests (agent.md's
    replay client itself is exercised in test_agent.py). The seam-through flow only
    ever calls `handshake`/`configure_worktree_root`/`configure_session`/`close` (it
    never runs a phase), but all `agent.SDKClient` methods are implemented so this
    fake type-checks against the full protocol."""

    ready: bool

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
        raise NotImplementedError("the seam-through flow never sends a turn message")

    def receive(self) -> dict[str, object]:
        raise NotImplementedError("the seam-through flow never receives a turn message")

    def close(self) -> None:
        pass


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)


def test_contract_run_reaches_session_start_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a git repo, with a ready handshake, run() exits 0 with a "no changes"
    placeholder summary and no error rendered."""
    _init_repo(tmp_path)
    monkeypatch.setattr(agent, "create_client", lambda: FakeSDKClient(ready=True))
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, tmp_path, presenter)

    assert code == 0
    assert presenter.errors == []
    assert len(presenter.summaries) == 1
    assert presenter.summaries[0].outcome == "no changes"


def test_contract_run_refuses_outside_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a git repository, run() exits 1 and renders the refusal (R11)."""
    monkeypatch.setattr(agent, "create_client", lambda: FakeSDKClient(ready=True))
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, tmp_path, presenter)

    assert code == 1
    assert len(presenter.errors) == 1
    cause, next_action, _detail = presenter.errors[0]
    assert "not inside a git repository" in cause
    assert next_action != ""
    assert presenter.summaries == []


def test_contract_run_exits_1_when_auth_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handshake that is not ready (no login) exits 1, naming the login step (R12)."""
    _init_repo(tmp_path)
    monkeypatch.setattr(agent, "create_client", lambda: FakeSDKClient(ready=False))
    presenter = FakePresenter()

    code = orchestrator.run(RunMode.ANALYZE, tmp_path, presenter)

    assert code == 1
    assert len(presenter.errors) == 1
    _cause, next_action, _detail = presenter.errors[0]
    assert "claude" in next_action


def test_contract_unexpected_exception_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-BlareError exception (a defect, not a refusal) exits 2 with a traceback
    detail, per the architecture's non-module-exception carve-out (orchestrator.md's
    own test plan lists this as a contract behaviour, not a dependency failure mode)."""

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
