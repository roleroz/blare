"""Unit tests for blare.agent: T2.1 scope (session lifecycle, the two-tool dispatch
over injected handlers, prompt templates, `create_client`'s replay/record branches,
transcripts, and the error taxonomy) plus T2.4's `request_repair` and
`notify_amendment_outcome`. `triage` is diff-mode-only (T3.1's scope) and is not
tested here.

Contract tests cover what this module promises while its dependencies behave;
failure-mode tests cover one per dependency failure mode (agent.md's Test plan):
`SDKClient` (transport, rate/overload, protocol failure, malformed tool payload,
fixture-scenario problems), `TranscriptWriter` (armed write failure), and the injected
sink/control handlers (raising vs. a rejecting verdict).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import claude_agent_sdk
import pytest

from blare.agent import (
    _WORKTREE_PLACEHOLDER,
    WRITE_TOOLS_DISALLOWED,
    AgentSession,
    AgentSessionError,
    AuthRequiredError,
    FixtureMismatchError,
    HandshakeResult,
    ToolDefinition,
    _RecordingSDKClient,
    _ReplayingSDKClient,
    create_client,
    tool_definitions,
)
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
from blare.stack import PrometheusStack

# ---- fakes (agent.md's Test plan: FakeSDKClient, FakeTranscriptWriter) -----------


class FakeSDKClient:
    """Scripted event streams per scenario; models conversation state (records every
    outbound send and every configure_* call) so tests assert on prompts actually
    sent, not on call counts."""

    def __init__(
        self,
        *,
        handshake_ready: bool = True,
        turns: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.handshake_ready = handshake_ready
        self._events: list[dict[str, object]] = []
        for turn in turns or []:
            self._events.extend(turn)
            self._events.append({"type": "turn_end"})
        self._cursor = 0
        self.sent_events: list[dict[str, object]] = []
        self.configured_sessions: list[tuple[object, object, object, object]] = []
        self.configured_roots: list[Path] = []
        self.close_calls = 0
        self._send_exc: Exception | None = None
        self._receive_exc: Exception | None = None

    def handshake(self) -> HandshakeResult:
        return HandshakeResult(ready=self.handshake_ready)

    def configure_worktree_root(self, root: Path) -> None:
        self.configured_roots.append(root)

    def configure_session(
        self,
        mode: RunMode,
        system_prompt: str,
        tools: tuple[ToolDefinition, ...],
        disallowed_tools: tuple[str, ...],
    ) -> None:
        self.configured_sessions.append((mode, system_prompt, tools, disallowed_tools))

    def send(self, event: dict[str, object]) -> None:
        if self._send_exc is not None:
            exc, self._send_exc = self._send_exc, None
            raise exc
        self.sent_events.append(event)

    def receive(self) -> dict[str, object]:
        if self._receive_exc is not None:
            exc, self._receive_exc = self._receive_exc, None
            raise exc
        event = self._events[self._cursor]
        self._cursor += 1
        return event

    def close(self) -> None:
        self.close_calls += 1

    def raise_on_next_send(self, exc: Exception) -> None:
        self._send_exc = exc

    def raise_on_next_receive(self, exc: Exception) -> None:
        self._receive_exc = exc

    def queue_turn(self, events: list[dict[str, object]]) -> None:
        self._events.extend(events)
        self._events.append({"type": "turn_end"})


class FakeTranscriptWriter:
    """Holds written events in memory; can be armed to fail."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.events: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self._fail_message: str | None = None

    def write_event(self, direction: str, event: dict[str, object]) -> None:
        if self._fail_message is not None:
            raise OSError(self._fail_message)
        self.events.append((direction, event))

    @property
    def path(self) -> Path:
        return self._path

    def arm_failure(self, message: str) -> None:
        self._fail_message = message

    def close(self) -> None:
        # Not part of `agent.TranscriptWriter` -- AgentSession must never call this
        # (the orchestrator owns and closes the writer itself); tests assert on
        # `closed` staying False through any `AgentSession.close()`.
        self.closed = True


@dataclass
class RecordingSink:
    verdict: BatchVerdict = field(default_factory=lambda: BatchVerdict(ok=True, message=None))
    raise_exc: Exception | None = None
    calls: list[EditBatch] = field(default_factory=list)

    def __call__(self, batch: EditBatch) -> BatchVerdict:
        self.calls.append(batch)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.verdict


@dataclass
class RecordingControl:
    verdict: RunControlVerdict = field(
        default_factory=lambda: RunControlVerdict(ok=True, message=None)
    )
    raise_exc: Exception | None = None
    calls: list[RunControlCall] = field(default_factory=list)

    def __call__(self, call: RunControlCall) -> RunControlVerdict:
        self.calls.append(call)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.verdict


def _session(
    client: object,
    tmp_path: Path,
    *,
    sink: RecordingSink | None = None,
    control: RecordingControl | None = None,
    transcript: FakeTranscriptWriter | None = None,
) -> tuple[AgentSession, RecordingSink, RecordingControl, FakeTranscriptWriter]:
    sink = sink if sink is not None else RecordingSink()
    control = control if control is not None else RecordingControl()
    if transcript is None:
        transcript = FakeTranscriptWriter(tmp_path / "t.jsonl")
    session = AgentSession(
        client,  # type: ignore[arg-type]
        sink,
        control,
        PrometheusStack(),
        transcript,
    )
    return session, sink, control, transcript


def _write_scenario(
    directory: Path,
    entries: list[dict[str, object]],
    *,
    metadata: dict[str, object] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    meta = metadata if metadata is not None else {
        "capture_date": "2026-07-30",
        "sdk_version": "test-fake",
    }
    lines = [json.dumps(meta)] + [json.dumps(e) for e in entries]
    (directory / "scenario.jsonl").write_text("\n".join(lines) + "\n")


def _handshake_only(directory: Path) -> None:
    _write_scenario(directory, [{"direction": "inbound", "event": {"type": "session_ready"}}])


# ==== create_client seam ===========================================================


def test_contract_create_client_unset_is_not_yet_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BLARE_SDK_FIXTURES unset, create_client raises NotImplementedError (the
    live SDK client is out of scope for this task)."""
    monkeypatch.delenv("BLARE_SDK_FIXTURES", raising=False)

    with pytest.raises(NotImplementedError):
        create_client()


def test_contract_create_client_replay_seam_reaches_session_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """replay:<dir> selects the replaying client; a well-formed handshake fixture lets
    AgentSession.start() succeed."""
    _handshake_only(tmp_path)
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{tmp_path}")

    client = create_client()
    session, _, _, _ = _session(client, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))  # must not raise
    session.close()


def test_contract_create_client_malformed_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BLARE_SDK_FIXTURES value that is neither replay: nor record: raises
    FixtureMismatchError naming the expected forms."""
    monkeypatch.setenv("BLARE_SDK_FIXTURES", "bogus")

    with pytest.raises(FixtureMismatchError) as exc_info:
        create_client()

    assert "replay:" in exc_info.value.next_action
    assert "record:" in exc_info.value.next_action


def test_contract_create_client_record_seam_validates_directory_then_not_implemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record:<dir> validates the directory is writable (a real, testable behavior)
    then raises NotImplementedError -- there is no live client to wrap yet (this
    task's explicit scope carve-out); the recorder itself is tested directly below."""
    record_dir = tmp_path / "recorded"
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"record:{record_dir}")

    with pytest.raises(NotImplementedError):
        create_client()

    assert record_dir.is_dir()


def test_failure_create_client_record_unwritable_directory_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable record:<dir> raises FixtureMismatchError at create_client."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")  # a file where a directory is expected
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"record:{blocked / 'sub'}")

    with pytest.raises(FixtureMismatchError):
        create_client()


# ==== handshake / auth =============================================================


def test_contract_auth_required_maps_to_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auth_required handshake event raises AuthRequiredError naming the login step."""
    _write_scenario(tmp_path, [{"direction": "inbound", "event": {"type": "auth_required"}}])
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{tmp_path}")
    client = create_client()
    session, _, _, _ = _session(client, tmp_path)

    with pytest.raises(AuthRequiredError) as exc_info:
        session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    assert "claude" in exc_info.value.next_action


def test_failure_missing_fixture_scenario_raises(tmp_path: Path) -> None:
    """A replay directory with no scenario.jsonl raises FixtureMismatchError."""
    with pytest.raises(FixtureMismatchError):
        _ReplayingSDKClient(tmp_path)


def test_failure_unreadable_scenario_file_raises(tmp_path: Path) -> None:
    """A scenario.jsonl with no read permission raises FixtureMismatchError."""
    scenario = tmp_path / "scenario.jsonl"
    scenario.write_text("{}\n")
    scenario.chmod(0o000)
    try:
        with pytest.raises(FixtureMismatchError):
            _ReplayingSDKClient(tmp_path)
    finally:
        scenario.chmod(0o644)


def test_failure_malformed_fixture_missing_metadata_raises(tmp_path: Path) -> None:
    """A handshake fixture missing capture_date/sdk_version raises FixtureMismatchError."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scenario.jsonl").write_text(
        json.dumps({"direction": "inbound", "event": {"type": "session_ready"}}) + "\n"
    )

    with pytest.raises(FixtureMismatchError):
        _ReplayingSDKClient(tmp_path)


def test_failure_truncated_scenario_line_raises(tmp_path: Path) -> None:
    """An unparseable (truncated) exchange line raises FixtureMismatchError naming the file."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"capture_date": "2026-07-30", "sdk_version": "x"}),
        json.dumps({"direction": "inbound", "event": {"type": "session_ready"}}),
        '{"direction": "outbound", "event": {"type"',  # truncated
    ]
    (tmp_path / "scenario.jsonl").write_text("\n".join(lines) + "\n")

    with pytest.raises(FixtureMismatchError) as exc_info:
        _ReplayingSDKClient(tmp_path)
    assert "scenario.jsonl" in exc_info.value.cause


def test_failure_scenario_with_no_exchange_entries_raises(tmp_path: Path) -> None:
    """A scenario file with only the metadata line raises FixtureMismatchError."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scenario.jsonl").write_text(
        json.dumps({"capture_date": "2026-07-30", "sdk_version": "x"}) + "\n"
    )

    with pytest.raises(FixtureMismatchError):
        _ReplayingSDKClient(tmp_path)


# ==== tool definitions / disallowed tools ==========================================


def test_contract_tool_definitions_expose_exactly_two_tools_with_expected_schemas() -> None:
    """Exactly propose_edits and run_control are registered, each with a schema
    covering its documented fields; no third tool exists."""
    defs = tool_definitions()

    assert [d.name for d in defs] == ["propose_edits", "run_control"]
    propose, control = defs
    assert propose.input_schema["required"] == ["phase", "edits"]
    assert control.input_schema["required"] == ["action", "payload"]
    action_enum = control.input_schema["properties"]["action"]["enum"]  # type: ignore[index]
    assert set(action_enum) == {a.value for a in RunControlAction}


def test_contract_write_tools_are_disallowed() -> None:
    """The model has no write-capable tool -- the target repo is read-only to it."""
    assert set(WRITE_TOOLS_DISALLOWED) == {"Write", "Edit", "NotebookEdit", "Bash"}
    assert "Read" not in WRITE_TOOLS_DISALLOWED


# ==== system prompt / phase prompt =================================================


def test_contract_system_prompt_differs_by_mode_and_sent_at_start(tmp_path: Path) -> None:
    """The system prompt differs by mode and is configured at start (asserted on the
    fake's recorded configure_session call)."""
    fake = FakeSDKClient()
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.close()

    fake2 = FakeSDKClient()
    session2, _, _, _ = _session(fake2, tmp_path)
    session2.start(RunMode.UPDATE, RunContext(worktree_root=tmp_path))
    session2.close()

    analyze_prompt = fake.configured_sessions[0][1]
    update_prompt = fake2.configured_sessions[0][1]
    assert isinstance(analyze_prompt, str)
    assert isinstance(update_prompt, str)
    assert analyze_prompt != update_prompt
    assert "full analysis" in analyze_prompt
    assert "diff-mode" in update_prompt


@pytest.mark.parametrize("phase", list(Phase))
def test_contract_run_phase_sends_phase_contract_and_stack_hints(
    tmp_path: Path, phase: Phase
) -> None:
    """run_phase sends that phase's contract; phase 3 carries instrumentation_hints(),
    phase 4 carries alerting_hints(), and phases 1-2 carry neither."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "ok"}]])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.run_phase(phase)
    session.close()

    sent_text = fake.sent_events[0]["text"]
    assert isinstance(sent_text, str)
    stack = PrometheusStack()
    assert f"Phase {int(phase)} --" in sent_text
    assert (stack.instrumentation_hints() in sent_text) == (phase is Phase.METRIC_COVERAGE)
    assert (stack.alerting_hints() in sent_text) == (phase is Phase.ALERT_RECOMMENDATIONS)


# ==== tool dispatch =================================================================


def test_contract_propose_edits_reaches_sink_verbatim_and_returns_verdict_unchanged(
    tmp_path: Path,
) -> None:
    """An inbound propose_edits tool_use reaches the injected sink verbatim; the
    sink's verdict is returned to the model unchanged."""
    edit_input = {
        "phase": 2,
        "edits": [
            {
                "op": "add",
                "entry_type": "failure_mode",
                "payload_or_id": {"id": "fm-x", "title": "x"},
            }
        ],
    }
    fake = FakeSDKClient(
        turns=[
            [
                {"type": "tool_use", "id": "t1", "name": "propose_edits", "input": edit_input},
                {"type": "text", "text": "done"},
            ]
        ]
    )
    sink = RecordingSink(verdict=BatchVerdict(ok=False, message="rejected: bad id"))
    session, sink, _, _ = _session(fake, tmp_path, sink=sink)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.run_phase(Phase.FAILURE_MODES)
    session.close()

    assert len(sink.calls) == 1
    assert sink.calls[0] == EditBatch(
        phase=Phase.FAILURE_MODES,
        edits=(
            Edit(
                op=EditOp.ADD,
                entry_type="failure_mode",
                payload_or_id={"id": "fm-x", "title": "x"},
            ),
        ),
    )
    tool_result = next(e for e in fake.sent_events if e.get("type") == "tool_result")
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "result": {"ok": False, "message": "rejected: bad id"},
    }


@pytest.mark.parametrize("action", list(RunControlAction))
def test_contract_run_control_reaches_handler_verbatim_for_each_action(
    tmp_path: Path, action: RunControlAction
) -> None:
    """An inbound run_control tool_use reaches the injected control handler verbatim
    for every action kind; the handler's verdict is returned to the model unchanged."""
    payload: dict[str, object] = (
        {"phases": [1, 2]} if action is RunControlAction.AFFECTED_VERDICT else {}
    )
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": action.value, "payload": payload},
                },
                {"type": "text", "text": "ack"},
            ]
        ]
    )
    control = RecordingControl(verdict=RunControlVerdict(ok=True, message="noted"))
    session, _, control, _ = _session(fake, tmp_path, control=control)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.chat("go")
    session.close()

    assert control.calls == [RunControlCall(action=action, payload=payload)]
    tool_result = next(e for e in fake.sent_events if e.get("type") == "tool_result")
    assert tool_result["result"] == {"ok": True, "message": "noted"}


# ==== amendments: request_repair / notify_amendment_outcome (T2.4) ================


def test_contract_request_repair_sends_phases_and_violations_and_returns_on_amend_complete(
    tmp_path: Path,
) -> None:
    """request_repair sends the named phases and violations, and returns once the
    model calls run_control with amend_complete (agent.md: "each call waits for its
    own amend_complete")."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "amend_complete", "payload": {}},
                },
            ]
        ]
    )
    control = RecordingControl(verdict=RunControlVerdict(ok=True, message="noted"))
    session, _, control, _ = _session(fake, tmp_path, control=control)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.request_repair(
        [Phase.ALERT_RECOMMENDATIONS],
        [Violation(ViolationKind.UNMAPPED_FAILURE_MODE, ("fm-1",))],
    )
    session.close()

    sent = fake.sent_events[0]
    assert sent["type"] == "request_repair"
    assert sent["phases"] == [4]
    assert sent["violations"] == [
        {"kind": "unmapped_failure_mode", "entry_ids": ["fm-1"], "phase": 4}
    ]
    assert control.calls == [RunControlCall(action=RunControlAction.AMEND_COMPLETE, payload={})]


def test_contract_request_repair_wording_violations_present(tmp_path: Path) -> None:
    """A request_repair call with non-empty violations always carries the violation
    wording, regardless of any standing proposal."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "amend_complete", "payload": {}},
                }
            ]
        ]
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.request_repair(
        [Phase.METRIC_COVERAGE], [Violation(ViolationKind.INVALID_EXPRESSION, ("ar-1",))]
    )
    session.close()

    text = fake.sent_events[0]["text"]
    assert isinstance(text, str)
    assert "invalid_expression" in text
    assert "ar-1" in text


def test_contract_request_repair_wording_follows_standing_proposal_discriminator(
    tmp_path: Path,
) -> None:
    """With an unresolved amend_proposal held (from a prior drained turn), an
    empty-violations request_repair call states the standing proposal's phases are
    open; with none held, the same arguments produce the cascade wording (agent.md's
    message discriminator)."""
    # First: a turn that proposes an amendment and ends without amend_complete --
    # the proposal stands (legal), leaving an unresolved proposal held.
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {
                        "action": "amend_proposal",
                        "payload": {"phases": [1]},
                    },
                }
            ]
        ]
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.run_phase(Phase.FAILURE_MODES)  # turn ends without amend_complete

    fake.queue_turn(
        [
            {
                "type": "tool_use",
                "id": "t2",
                "name": "run_control",
                "input": {"action": "amend_complete", "payload": {}},
            }
        ]
    )
    session.request_repair([Phase.SYSTEM_MAP], [])
    resume_text = fake.sent_events[-2]["text"]
    assert isinstance(resume_text, str)
    assert "standing" in resume_text.lower() or "your proposed amendment" in resume_text.lower()
    session.close()

    # Second, fresh session: no amend_proposal ever made -- the same empty-violations
    # call must get the cascade wording instead.
    fake2 = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "amend_complete", "payload": {}},
                }
            ]
        ]
    )
    session2, _, _, _ = _session(fake2, tmp_path)
    session2.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session2.request_repair([Phase.SYSTEM_MAP], [])
    cascade_text = fake2.sent_events[-2]["text"]
    session2.close()

    assert isinstance(cascade_text, str)
    assert cascade_text != resume_text
    assert "reference" in cascade_text.lower() or "cascade" in cascade_text.lower() or (
        "covers" in cascade_text.lower()
    )


def test_contract_turn_ending_with_unresolved_amend_proposal_returns_normally(
    tmp_path: Path,
) -> None:
    """A turn ending with an unresolved amend_proposal (no amend_complete) is legal:
    the driving call (run_phase) returns normally rather than raising."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "amend_proposal", "payload": {"phases": [1]}},
                }
            ]
        ]
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.run_phase(Phase.FAILURE_MODES)  # must not raise
    session.close()


def test_failure_request_repair_reminds_once_then_raises_without_amend_complete(
    tmp_path: Path,
) -> None:
    """request_repair reminds the model once via a follow-up message, then raises
    AgentSessionError if a second turn also ends without amend_complete."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "still thinking"}]])
    fake.queue_turn([{"type": "text", "text": "still thinking again"}])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.request_repair([Phase.ALERT_RECOMMENDATIONS], [])

    assert "amend_complete" in exc_info.value.cause
    assert "phase" in exc_info.value.cause.lower()
    # Exactly one reminder was sent: the initial request_repair message plus one
    # follow-up -- two outbound sends total.
    outbound_texts = [e for e in fake.sent_events if "text" in e]
    assert len(outbound_texts) == 2


def test_contract_notify_amendment_outcome_approved_message(tmp_path: Path) -> None:
    """notify_amendment_outcome(approved=True) sends an approval message and blocks
    until the model's acknowledgment turn ends."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "understood"}]])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.notify_amendment_outcome(approved=True, restored_phases=[])
    session.close()

    sent = fake.sent_events[0]
    assert sent["type"] == "amendment_outcome"
    assert sent["approved"] is True
    assert sent["restored_phases"] == []


def test_contract_notify_amendment_outcome_rejected_names_restored_phases(
    tmp_path: Path,
) -> None:
    """notify_amendment_outcome(approved=False) names every restored phase in the
    message -- this, together with the approved variant, is what distinguishes the
    two fixture variants at the SDK boundary (agent.md)."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "understood"}]])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.notify_amendment_outcome(
        approved=False, restored_phases=[Phase.SYSTEM_MAP, Phase.FAILURE_MODES]
    )
    session.close()

    sent = fake.sent_events[0]
    assert sent["approved"] is False
    assert sent["restored_phases"] == [1, 2]
    text = sent["text"]
    assert isinstance(text, str)
    assert "1" in text and "2" in text


def test_contract_notify_amendment_outcome_tool_calls_flow_through_normal_handlers(
    tmp_path: Path,
) -> None:
    """Anything the model does during its acknowledgment turn flows through the
    normal tool handlers (agent.md): a fresh amend_proposal in that turn is legal
    and reaches the control handler like any other."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "amend_proposal", "payload": {"phases": [3]}},
                }
            ]
        ]
    )
    control = RecordingControl()
    session, _, control, _ = _session(fake, tmp_path, control=control)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.notify_amendment_outcome(approved=False, restored_phases=[Phase.METRIC_COVERAGE])
    session.close()

    assert control.calls == [
        RunControlCall(action=RunControlAction.AMEND_PROPOSAL, payload={"phases": [3]})
    ]


def test_contract_request_repair_context_label_names_the_phases(tmp_path: Path) -> None:
    """An error during a request_repair call reports the phase list in its cause
    (agent.md: the context label is "request_repair with its phase list")."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "..."}]])
    fake.queue_turn([{"type": "text", "text": "..."}])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.request_repair([Phase.ALERT_RECOMMENDATIONS, Phase.METRIC_COVERAGE], [])

    assert "3" in exc_info.value.cause
    assert "4" in exc_info.value.cause


# ==== chat ===========================================================================


def test_contract_chat_passes_text_through_and_returns_concatenated_text(
    tmp_path: Path,
) -> None:
    """chat text passes through to the live session; the reply is the turn's
    concatenated text blocks, and the turn is drained at return."""
    fake = FakeSDKClient(
        turns=[[{"type": "text", "text": "part one. "}, {"type": "text", "text": "part two."}]]
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    reply = session.chat("what's the status?")
    session.close()

    assert reply == "part one. part two."
    assert fake.sent_events[0] == {"type": "chat", "text": "what's the status?"}


def test_contract_chat_returns_empty_string_when_turn_is_tool_calls_only(
    tmp_path: Path,
) -> None:
    """A turn that produced only tool calls yields the empty string from chat()."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "no_impact", "payload": {}},
                }
            ]
        ]
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    reply = session.chat("anything to report?")
    session.close()

    assert reply == ""


def test_contract_driving_calls_drain_the_turn_fully(tmp_path: Path) -> None:
    """After run_phase returns, no turn is left in flight: the fake's cursor sits at
    the start of the next queued turn, not mid-turn."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "phase done"}]])
    fake.queue_turn([{"type": "text", "text": "second turn"}])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.run_phase(Phase.SYSTEM_MAP)
    boundary = fake._cursor
    reply = session.chat("next")
    session.close()

    assert reply == "second turn"
    assert boundary == 2  # exactly [text, turn_end] consumed, nothing of turn 2 touched


# ==== transcript =====================================================================


def test_contract_transcript_records_every_exchanged_event_in_order(tmp_path: Path) -> None:
    """The transcript contains every exchanged event in order, in real time (asserted
    mid-session), and transcript_path reports the writer's path."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "ok"}]])
    transcript = FakeTranscriptWriter(tmp_path / "run.jsonl")
    session, _, _, transcript = _session(fake, tmp_path, transcript=transcript)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    assert transcript.events[-1][0] == "outbound"  # the session_init record, from start()

    session.run_phase(Phase.SYSTEM_MAP)
    directions = [d for d, _ in transcript.events]
    assert directions == ["outbound", "outbound", "inbound", "inbound"]
    session.close()

    assert session.transcript_path == tmp_path / "run.jsonl"


def test_failure_transcript_write_failure_raises_and_closes_session(tmp_path: Path) -> None:
    """An unwritable TranscriptWriter raises AgentSessionError naming the transcript
    path and the write failure; the SDK session is closed."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "ok"}]])
    transcript = FakeTranscriptWriter(tmp_path / "run.jsonl")
    transcript.arm_failure("disk full")
    session, _, _, transcript = _session(fake, tmp_path, transcript=transcript)

    with pytest.raises(AgentSessionError) as exc_info:
        session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    assert "disk full" in exc_info.value.cause
    assert str(tmp_path / "run.jsonl") in exc_info.value.next_action
    assert fake.close_calls == 1


# ==== close ==========================================================================


def test_contract_close_ends_session_idempotent_and_leaves_transcript_open(
    tmp_path: Path,
) -> None:
    """close() ends the SDK session, is idempotent, and never closes the
    TranscriptWriter (the orchestrator owns that)."""
    fake = FakeSDKClient()
    session, _, _, transcript = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.close()
    session.close()

    assert fake.close_calls == 1
    assert transcript.closed is False


def test_contract_close_is_safe_after_a_session_error(tmp_path: Path) -> None:
    """close() is safe to call again after an AgentSessionError already closed it."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "ok"}]])
    sink = RecordingSink(raise_exc=RuntimeError("boom"))
    fake.queue_turn(
        [
            {
                "type": "tool_use",
                "id": "t1",
                "name": "propose_edits",
                "input": {"phase": 1, "edits": []},
            }
        ]
    )
    session, sink, _, _ = _session(fake, tmp_path, sink=sink)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.run_phase(Phase.SYSTEM_MAP)  # consumes the first (harmless) turn

    with pytest.raises(AgentSessionError):
        session.run_phase(Phase.SYSTEM_MAP)  # consumes the sink-raising turn

    session.close()  # must not raise


# ==== failure modes: SDK client ======================================================


def test_failure_transport_error_mid_phase_raises_agent_session_error(tmp_path: Path) -> None:
    """A transport error mid-phase raises AgentSessionError carrying the phase, cause,
    and the in-flight flag; the fake raises the pinned SDK's own CLIConnectionError so
    the scripted type cannot drift from the real package."""
    fake = FakeSDKClient()
    fake.raise_on_next_receive(claude_agent_sdk.CLIConnectionError("connection reset"))
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.run_phase(Phase.METRIC_COVERAGE)

    assert "phase 3" in exc_info.value.cause
    assert "connection reset" in exc_info.value.cause
    assert "in flight" not in exc_info.value.cause


def test_failure_rate_overload_raises_agent_session_error_with_distinguishable_message(
    tmp_path: Path,
) -> None:
    """A rate/overload failure raises the same AgentSessionError type as a transport
    failure but with a distinguishable message; scripted via the pinned SDK's own
    ProcessError so the type cannot drift."""
    fake = FakeSDKClient()
    fake.raise_on_next_receive(
        claude_agent_sdk.ProcessError("CLI exited", exit_code=529, stderr="overloaded_error")
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.run_phase(Phase.METRIC_COVERAGE)

    assert "529" in exc_info.value.cause
    assert "overloaded_error" in exc_info.value.cause


def test_failure_protocol_failure_out_of_contract_event_raises(tmp_path: Path) -> None:
    """A scripted malformed, out-of-contract event in the stream raises
    AgentSessionError (a protocol failure, not a soft tool-payload verdict)."""
    fake = FakeSDKClient(turns=[[{"type": "unexpected_event_kind"}]])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.run_phase(Phase.SYSTEM_MAP)

    assert "protocol failure" in exc_info.value.cause


def test_failure_tool_use_event_with_malformed_envelope_raises(tmp_path: Path) -> None:
    """A tool_use event missing/mistyping its own id or name (as opposed to a
    malformed `input` payload) is a protocol failure: AgentSessionError, with the
    tool-call-in-flight flag set, distinct from the malformed-payload soft-error
    path below."""
    fake = FakeSDKClient(turns=[[{"type": "tool_use", "id": None, "name": "propose_edits"}]])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.run_phase(Phase.SYSTEM_MAP)

    assert "malformed tool_use event" in exc_info.value.cause
    assert "in flight" in exc_info.value.cause


def test_failure_unknown_tool_name_raises(tmp_path: Path) -> None:
    """A tool_use event naming a tool this session never registered raises
    AgentSessionError -- the SDK would never call an unregistered tool for real, so
    this only happens via a deliberately malformed scripted/replayed event."""
    fake = FakeSDKClient(
        turns=[[{"type": "tool_use", "id": "t1", "name": "not_a_real_tool", "input": {}}]]
    )
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.run_phase(Phase.SYSTEM_MAP)

    assert "unknown tool call" in exc_info.value.cause
    assert "in flight" in exc_info.value.cause


def test_failure_malformed_tool_payload_returns_error_verdict_without_raising(
    tmp_path: Path,
) -> None:
    """A malformed tool payload returns an error verdict to the model; the session
    continues and nothing raises; the sink is never called for the malformed input."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "propose_edits",
                    "input": {"phase": "not-an-int", "edits": []},
                },
                {"type": "text", "text": "carrying on"},
            ]
        ]
    )
    session, sink, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.run_phase(Phase.SYSTEM_MAP)  # must not raise

    assert sink.calls == []
    tool_result = next(e for e in fake.sent_events if e.get("type") == "tool_result")
    result = tool_result["result"]
    assert isinstance(result, dict)
    assert result["ok"] is False
    session.close()


def test_failure_sink_raising_is_agent_session_error_distinct_from_rejecting_verdict(
    tmp_path: Path,
) -> None:
    """A raising sink (programmer error) raises AgentSessionError, distinct from a
    sink that merely returns a rejecting verdict (which returns to the model, tested
    side by side here)."""
    rejecting_fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "propose_edits",
                    "input": {"phase": 1, "edits": []},
                },
                {"type": "text", "text": "ok"},
            ]
        ]
    )
    rejecting_sink = RecordingSink(verdict=BatchVerdict(ok=False, message="no"))
    session, _, _, _ = _session(rejecting_fake, tmp_path, sink=rejecting_sink)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.run_phase(Phase.SYSTEM_MAP)  # does not raise
    session.close()

    raising_fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "propose_edits",
                    "input": {"phase": 1, "edits": []},
                }
            ]
        ]
    )
    raising_sink = RecordingSink(raise_exc=ValueError("programmer error"))
    session2, _, _, _ = _session(raising_fake, tmp_path, sink=raising_sink)
    session2.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    with pytest.raises(AgentSessionError) as exc_info:
        session2.run_phase(Phase.SYSTEM_MAP)
    assert "edit sink raised" in exc_info.value.cause
    assert "in flight" in exc_info.value.cause


def test_failure_control_handler_raising_is_agent_session_error(tmp_path: Path) -> None:
    """A raising run-control handler raises AgentSessionError, distinct from a
    rejecting verdict."""
    fake = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_control",
                    "input": {"action": "no_impact", "payload": {}},
                }
            ]
        ]
    )
    control = RecordingControl(raise_exc=ValueError("boom"))
    session, _, _, _ = _session(fake, tmp_path, control=control)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.chat("status?")

    assert "run-control handler raised" in exc_info.value.cause


def test_failure_transport_error_at_start_raises_agent_session_error(tmp_path: Path) -> None:
    """A transport failure during start()'s handshake/configure_* calls raises
    AgentSessionError with the "start" context label -- agent.md lists `start` among
    the driving-call names used when no phase is open, so `start` itself must be
    capable of surfacing a transport failure, not only the AuthRequiredError path."""

    class _BrokenConfigureClient(FakeSDKClient):
        def configure_session(
            self,
            mode: RunMode,
            system_prompt: str,
            tools: tuple[ToolDefinition, ...],
            disallowed_tools: tuple[str, ...],
        ) -> None:
            raise claude_agent_sdk.CLIConnectionError("lost connection during configure")

    session, _, _, _ = _session(_BrokenConfigureClient(), tmp_path)

    with pytest.raises(AgentSessionError) as exc_info:
        session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    assert "start" in exc_info.value.cause
    assert "lost connection during configure" in exc_info.value.cause


def test_failure_transport_error_at_handshake_raises_agent_session_error(
    tmp_path: Path,
) -> None:
    """A transport failure raised directly out of the handshake call itself (as
    opposed to a `ready=False` result) also raises AgentSessionError with the "start"
    context label -- the other half of `_call_client`'s coverage of `start()`."""

    class _BrokenHandshakeClient(FakeSDKClient):
        def handshake(self) -> HandshakeResult:
            raise claude_agent_sdk.CLIConnectionError("connection refused")

    session, _, _, _ = _session(_BrokenHandshakeClient(), tmp_path)

    with pytest.raises(AgentSessionError) as exc_info:
        session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    assert "start" in exc_info.value.cause
    assert "connection refused" in exc_info.value.cause


def test_contract_close_swallows_underlying_client_close_failure(tmp_path: Path) -> None:
    """close() never raises, even when the underlying client's own close() fails --
    it is best-effort cleanup with no next action to report, and letting it escape
    would violate "safe after any AgentSessionError" (agent.md)."""

    class _BrokenCloseClient(FakeSDKClient):
        def close(self) -> None:
            raise RuntimeError("already gone")

    session, _, _, _ = _session(_BrokenCloseClient(), tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.close()  # must not raise
    session.close()  # idempotent even though the first call's close attempt "failed"


def test_failure_close_failure_does_not_mask_the_triggering_error(tmp_path: Path) -> None:
    """When an AgentSessionError-triggering failure's own cleanup close() also fails,
    the original AgentSessionError (cause, phase, transcript next action) still
    surfaces -- the close failure must never replace it with an unrelated exception."""

    class _BrokenCloseClient(FakeSDKClient):
        def close(self) -> None:
            raise RuntimeError("close also failed")

    fake = _BrokenCloseClient()
    fake.raise_on_next_receive(claude_agent_sdk.CLIConnectionError("dropped"))
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(AgentSessionError) as exc_info:
        session.run_phase(Phase.SYSTEM_MAP)

    assert "dropped" in exc_info.value.cause
    assert "phase 1" in exc_info.value.cause


def test_contract_chat_error_context_label_reflects_last_run_phase(tmp_path: Path) -> None:
    """An error during checkpoint chat reports the phase last run, not "chat" --
    agent.md's context-label rule pointedly omits `chat` from its driving-call name
    list because a checkpoint's phase is still open (not yet frozen) while its chat
    is in progress."""
    fake = FakeSDKClient(turns=[[{"type": "text", "text": "phase output"}]])
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))
    session.run_phase(Phase.FAILURE_MODES)

    fake.raise_on_next_receive(claude_agent_sdk.CLIConnectionError("dropped"))
    with pytest.raises(AgentSessionError) as exc_info:
        session.chat("any redirection?")

    assert "phase 2" in exc_info.value.cause


def test_failure_scenario_missing_at_replay_raises(tmp_path: Path) -> None:
    """A missing fixture scenario raises FixtureMismatchError (duplicated from the
    handshake-level test above, phrased at the create_client seam)."""
    with pytest.raises(FixtureMismatchError):
        _ReplayingSDKClient(tmp_path).handshake()


def test_failure_recorder_write_failure_deletes_partial_scenario(tmp_path: Path) -> None:
    """A write failure mid-capture raises FixtureMismatchError and deletes the partial
    scenario, so a truncated leftover can never later fail replay as a malformed file."""
    real = FakeSDKClient(turns=[[{"type": "text", "text": "ok"}]])
    record_dir = tmp_path / "recorded"
    recorder = _RecordingSDKClient(real, record_dir, sdk_version="fake-1.0")
    scenario_file = record_dir / "scenario.jsonl"
    assert scenario_file.exists()

    def _broken_write(_text: str) -> int:
        raise OSError("disk full")

    assert recorder._handle is not None
    recorder._handle.write = _broken_write  # type: ignore[method-assign]

    with pytest.raises(FixtureMismatchError):
        recorder.send({"type": "chat", "text": "hi"})

    assert not scenario_file.exists()


def test_failure_recorder_open_failure_raises_fixture_mismatch_error(tmp_path: Path) -> None:
    """A directory that cannot hold the scenario file (blocked by a same-named file
    one level up) raises FixtureMismatchError at construction, not an AttributeError
    from an unset internal handle."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    real = FakeSDKClient()

    with pytest.raises(FixtureMismatchError):
        _RecordingSDKClient(real, blocked / "sub", sdk_version="fake-1.0")


# ==== SDK exception types (importable from the pinned real package) ================


def test_contract_pinned_sdk_exception_classes_are_importable_and_typed() -> None:
    """The exception classes the transport/rate-overload fakes script are the real,
    pinned SDK's own types (agent.md: "the fakes script the SDK's own types, so there
    is no wire shape to record and nothing left unverified")."""
    assert issubclass(claude_agent_sdk.CLIConnectionError, claude_agent_sdk.ClaudeSDKError)
    assert issubclass(claude_agent_sdk.ProcessError, claude_agent_sdk.ClaudeSDKError)
    err = claude_agent_sdk.ProcessError("x", exit_code=1, stderr="y")
    assert err.exit_code == 1


# ==== recorder / replay round trip and determinism =================================


def test_contract_record_then_replay_round_trip(tmp_path: Path) -> None:
    """A session run through the recorder (over the fake real client) produces a
    scenario directory that the replaying client then replays to an identical event
    stream."""
    root = tmp_path / "repo"
    root.mkdir()
    real = FakeSDKClient(turns=[[{"type": "text", "text": "phase done"}]])
    record_dir = tmp_path / "recorded"
    recorder = _RecordingSDKClient(real, record_dir, sdk_version="fake-1.0")
    transcript1 = FakeTranscriptWriter(tmp_path / "t1.jsonl")
    session1, _, _, transcript1 = _session(recorder, tmp_path, transcript=transcript1)
    session1.start(RunMode.ANALYZE, RunContext(worktree_root=root))
    session1.run_phase(Phase.SYSTEM_MAP)
    session1.close()

    scenario_text = (record_dir / "scenario.jsonl").read_text()
    lines = scenario_text.splitlines()
    metadata = json.loads(lines[0])
    assert metadata["capture_date"] and metadata["sdk_version"] == "fake-1.0"
    assert len(lines) > 1

    replaying = _ReplayingSDKClient(record_dir)
    transcript2 = FakeTranscriptWriter(tmp_path / "t2.jsonl")
    session2, _, _, transcript2 = _session(replaying, tmp_path, transcript=transcript2)
    session2.start(RunMode.ANALYZE, RunContext(worktree_root=root))
    session2.run_phase(Phase.SYSTEM_MAP)
    session2.close()

    assert transcript2.events == transcript1.events


def test_contract_replay_reroots_worktree_placeholder_to_current_run_root(
    tmp_path: Path,
) -> None:
    """Recorded events are normalized to the placeholder on disk; replaying against a
    different worktree root re-roots inbound events to that new root (the tool_use
    payload the sink receives carries the replay run's root, not the capture root or
    the placeholder)."""
    capture_root = tmp_path / "capture-repo"
    capture_root.mkdir()
    real = FakeSDKClient(
        turns=[
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "propose_edits",
                    "input": {
                        "phase": 1,
                        "edits": [
                            {
                                "op": "add",
                                "entry_type": "system_component",
                                "payload_or_id": {
                                    "id": "sm-x",
                                    "path": str(capture_root / "main.go"),
                                },
                            }
                        ],
                    },
                }
            ]
        ]
    )
    record_dir = tmp_path / "recorded"
    recorder = _RecordingSDKClient(real, record_dir, sdk_version="fake-1.0")
    session, sink, _, _ = _session(recorder, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=capture_root))
    session.run_phase(Phase.SYSTEM_MAP)
    session.close()

    recorded_text = (record_dir / "scenario.jsonl").read_text()
    assert str(capture_root) not in recorded_text
    assert _WORKTREE_PLACEHOLDER in recorded_text

    replay_root = tmp_path / "replay-repo"
    replay_root.mkdir()
    replaying = _ReplayingSDKClient(record_dir)
    session2, sink2, _, _ = _session(replaying, tmp_path)
    session2.start(RunMode.ANALYZE, RunContext(worktree_root=replay_root))
    session2.run_phase(Phase.SYSTEM_MAP)
    session2.close()

    payload = sink2.calls[0].edits[0].payload_or_id
    assert isinstance(payload, dict)
    assert payload["path"] == str(replay_root / "main.go")


def test_contract_replay_raises_on_divergence(tmp_path: Path) -> None:
    """A live message diverging from the recorded outbound entry raises
    FixtureMismatchError naming the mismatch."""
    _write_scenario(
        tmp_path,
        [
            {"direction": "inbound", "event": {"type": "session_ready"}},
            {"direction": "outbound", "event": {"type": "phase_prompt", "phase": 1, "text": "X"}},
        ],
    )
    client = _ReplayingSDKClient(tmp_path)
    session, _, _, _ = _session(client, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(FixtureMismatchError):
        session.run_phase(Phase.SYSTEM_MAP)  # the real phase-1 prompt text diverges


def test_contract_fixture_mismatch_from_the_client_closes_the_session(tmp_path: Path) -> None:
    """A FixtureMismatchError the client itself raises (not this module's own
    wrapping) still closes the session before propagating -- every raise site that
    ends the run should leave nothing lingering open, not only the ones agent.py
    raises itself."""

    class _DivergingClient(FakeSDKClient):
        def receive(self) -> dict[str, object]:
            raise FixtureMismatchError(cause="scripted divergence", next_action="re-record")

    fake = _DivergingClient()
    session, _, _, _ = _session(fake, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    with pytest.raises(FixtureMismatchError):
        session.run_phase(Phase.SYSTEM_MAP)

    assert fake.close_calls == 1


def test_contract_replay_close_with_unconsumed_entries_is_legal(tmp_path: Path) -> None:
    """Closing the session with recorded events still unconsumed is legal, not a
    mismatch (abort-path fixtures replay a longer scenario and end early)."""
    _write_scenario(
        tmp_path,
        [
            {"direction": "inbound", "event": {"type": "session_ready"}},
            {"direction": "outbound", "event": {"type": "chat", "text": "never sent"}},
            {"direction": "inbound", "event": {"type": "text", "text": "never received"}},
        ],
    )
    client = _ReplayingSDKClient(tmp_path)
    session, _, _, _ = _session(client, tmp_path)
    session.start(RunMode.ANALYZE, RunContext(worktree_root=tmp_path))

    session.close()  # must not raise despite two unconsumed entries


# ==== the hand-authored analyze-happy-path fixture (provisional) ===================


def _repo_root_fixture(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    return repo


def _analyze_happy_path_fixture_dir() -> Path:
    """Locate the checked-in `analyze-happy-path` fixture directory.

    Prefers Bazel's runfiles resolution (hermetic under `bazel test`); falls back to
    a path relative to this file for a plain `pytest` invocation.
    """
    try:
        from python.runfiles import Runfiles

        runfiles = Runfiles.Create()
        if runfiles is not None:
            located = runfiles.Rlocation(
                "blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl"
            )
            if located is not None:
                return Path(located).parent
    except ImportError:
        pass
    return (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "claude-sdk"
        / "analyze-happy-path"
    )


def test_contract_analyze_happy_path_fixture_replays_all_four_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-authored, provisional analyze-happy-path fixture (four phases,
    approvals only) replays end to end: each phase's propose_edits reaches the sink,
    which approves, and the session closes cleanly."""
    fixture_dir = _analyze_happy_path_fixture_dir()
    assert fixture_dir.exists(), f"analyze-happy-path fixture not found at {fixture_dir}"
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{fixture_dir}")
    client = create_client()
    root = _repo_root_fixture(tmp_path)
    session, sink, _, _ = _session(client, tmp_path)

    session.start(RunMode.ANALYZE, RunContext(worktree_root=root))
    for phase in Phase:
        session.run_phase(phase)
    session.close()

    assert [batch.phase for batch in sink.calls] == list(Phase)
    assert len(sink.calls) == 4
