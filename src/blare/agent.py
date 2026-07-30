"""The Claude Agent SDK boundary (architecture): the system's only mock boundary.

T2.1 scope (`engineering/modules/agent.md`): session lifecycle (`start`, `run_phase`,
`chat`, `close`), the two structured tools (`propose_edits`, `run_control`) dispatched
over orchestrator-injected handlers, the phase/system prompt templates, `create_client`
with the replay and (directory-validation-only) record branches, transcript writing, and
the error taxonomy. Deliberately NOT built here, per this task's scope: `triage`,
`request_repair`, and `notify_amendment_outcome` (amendment/diff-mode-specific; land with
T2.4/T3.1), and the live (`unset`) SDK client (out of scope for T2.1 too — `create_client`
keeps T1.1's `NotImplementedError` for that branch).

Design note on the client/wire boundary: the real `claude_agent_sdk.ClaudeSDKClient`
takes the system prompt, tool registrations, and disallowed-tools policy as
*construction-time options* (`ClaudeAgentOptions`), not as messages exchanged over the
wire. This module's `SDKClient` protocol mirrors that split: `configure_worktree_root`
and `configure_session` are one-shot configuration calls (no fixture entry, not part of
the replayed exchange), while `send`/`receive` carry the actual turn-by-turn exchange
that fixtures replay byte-exact. `create_client`'s env-var seam, fixture format, and this
split are implementation detail agent.md leaves open; T1.1's fixture file is renamed
`scenario.jsonl` (from `handshake.jsonl`) to reflect that a scenario is now a whole
session, not only a handshake.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import NoReturn, Protocol, TypeVar, cast

from blare.model import (
    BatchVerdict,
    BlareError,
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
from blare.stack import ObservabilityStack

_T = TypeVar("_T")

_ENV_VAR = "BLARE_SDK_FIXTURES"
_REPLAY_PREFIX = "replay:"
_RECORD_PREFIX = "record:"
_SCENARIO_FILENAME = "scenario.jsonl"

# The run's worktree root, substituted for a fixed placeholder in recorded/replayed
# wire events (agent.md, "Client seam and fixtures"), so captured fixtures are portable
# across the different temp-repo paths each e2e run constructs.
_WORKTREE_PLACEHOLDER = "<<BLARE_WORKTREE_ROOT>>"

# The target repo is read-only to the model (architecture: agent's SDK usage) — every
# tool capable of writing to it is disallowed; edits flow only through `propose_edits`.
# Filesystem *read* tools (Read, Grep, Glob, ...) are the SDK's own default toolset and
# stay available.
WRITE_TOOLS_DISALLOWED: tuple[str, ...] = ("Write", "Edit", "NotebookEdit", "Bash")


class AuthRequiredError(BlareError):
    """No Claude Code subscription login available (R12)."""


class AgentSessionError(BlareError):
    """The agent session failed: transport, protocol, a raising handler, or a
    transcript write failure. Carries the SDK error (or underlying cause), a context
    label (the phase in progress, or the driving call's name), and whether a tool call
    was in flight (agent.md, Error handling)."""


class FixtureMismatchError(BlareError):
    """Replay divergence, a missing/malformed scenario, a recording failure, or a
    malformed seam value."""


class _MalformedPayloadError(Exception):
    """Internal only: a tool call's `input` did not parse into the expected shape.

    Always caught inside `_handle_tool_use` and turned into a rejecting tool result
    (agent.md: "malformed tool payload... return an error verdict... they never raise
    into the run"); never allowed to escape.
    """


@dataclass(frozen=True)
class HandshakeResult:
    """The outcome of `AgentSession.start`'s minimal SDK handshake."""

    ready: bool


@dataclass(frozen=True)
class ToolDefinition:
    """One SDK tool's registration: name, description, and JSON-Schema-shaped input.

    Schema field names are this module's own implementation detail (mirroring
    artifacts.md's own "changeable freely until first release" stance on its own
    schemas) until T1.5 (artifacts, write side) and T2.2 (orchestrator) exist to
    consume them for real.
    """

    name: str
    description: str
    input_schema: dict[str, object]


def _propose_edits_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "phase": {"type": "integer", "enum": [1, 2, 3, 4]},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["add", "update", "remove"]},
                        "entry_type": {"type": "string"},
                        "payload_or_id": {},
                    },
                    "required": ["op", "entry_type", "payload_or_id"],
                },
            },
        },
        "required": ["phase", "edits"],
    }


def _run_control_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [action.value for action in RunControlAction],
            },
            "payload": {"type": "object"},
        },
        "required": ["action", "payload"],
    }


def tool_definitions() -> tuple[ToolDefinition, ToolDefinition]:
    """The exact two tools exposed to the model (architecture: exactly two tools)."""
    propose_edits = ToolDefinition(
        name="propose_edits",
        description=(
            "Propose structured add/update/remove edits to one phase's artifact "
            "entries. Returns the combined phase-state and content-check verdict; "
            "free-form text never mutates artifacts."
        ),
        input_schema=_propose_edits_schema(),
    )
    run_control = ToolDefinition(
        name="run_control",
        description=(
            "Report a phase-affecting conclusion (affected-phase verdict, no-impact "
            "conclusion) or an amendment proposal/completion."
        ),
        input_schema=_run_control_schema(),
    )
    return (propose_edits, run_control)


# ---- injected handler protocols (orchestrator-injected; defined here per this task's
# instruction to mirror T1.1's pattern of mirroring cli.md's Presenter ahead of cli's
# own build) ----------------------------------------------------------------------


class EditSink(Protocol):
    """Orchestrator-injected: enforces the phase-state rule, then artifacts' per-batch
    content check; its verdict becomes the `propose_edits` tool result verbatim."""

    def __call__(self, batch: EditBatch) -> BatchVerdict: ...


class RunControlHandler(Protocol):
    """Orchestrator-injected: handles every `run_control` action; its verdict becomes
    the tool result verbatim."""

    def __call__(self, call: RunControlCall) -> RunControlVerdict: ...


class TranscriptWriter(Protocol):
    """Orchestrator-constructed and injected (owns the state-dir path scheme via
    gitrepo's repo-id); this module writes every exchanged event through it, flushed
    per event, and exposes its path via `AgentSession.transcript_path`."""

    def write_event(self, direction: str, event: dict[str, object]) -> None: ...

    @property
    def path(self) -> Path: ...


class SDKClient(Protocol):
    """The small protocol wrapping the SDK client surface this module uses.

    `configure_worktree_root`/`configure_session` are one-shot construction-time
    configuration (mirroring `ClaudeAgentOptions`); `send`/`receive` carry the
    turn-by-turn wire exchange that fixtures replay.
    """

    def handshake(self) -> HandshakeResult: ...

    def configure_worktree_root(self, root: Path) -> None: ...

    def configure_session(
        self,
        mode: RunMode,
        system_prompt: str,
        tools: tuple[ToolDefinition, ...],
        disallowed_tools: tuple[str, ...],
    ) -> None: ...

    def send(self, event: dict[str, object]) -> None: ...

    def receive(self) -> dict[str, object]: ...

    def close(self) -> None: ...


# ---- normalization helper (shared by the replaying and recording clients) --------


def _substitute(value: object, old: str, new: str) -> object:
    """Recursively replace every occurrence of `old` with `new` in `value`'s strings.

    A plain substring replace, not path-boundary-aware: a worktree root that happens
    to be a string prefix of an unrelated path (e.g. `/tmp/blare-x` vs.
    `/tmp/blare-x-2/other`) would have that unrelated string corrupted too. e2e/release
    runs construct unique per-run temp directories, which makes this collision
    vanishingly unlikely in practice; agent.md's own "the run's worktree root...
    replaced... nothing else is normalized" does not call for boundary-awareness, so
    this is an accepted trade-off rather than an oversight.
    """
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, dict):
        return {k: _substitute(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, old, new) for v in value]
    return value


def _as_event(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value)


# ---- replaying client (the e2e mock) ----------------------------------------------


class _ReplayingSDKClient:
    """Replays a hand-authored/recorded fixture scenario (agent.md, Client seam and
    fixtures). A single cursor walks the ordered `{direction, event}` entries: outbound
    calls are compared byte-exact (after placeholder normalization) against the next
    entry; inbound calls return the next entry's event, re-rooted from the placeholder
    to this run's real worktree root.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._scenario_file = directory / _SCENARIO_FILENAME
        self._entries = self._load_entries()
        self._pos = 0
        self._root: str | None = None

    def _fail(self, message: str) -> FixtureMismatchError:
        return FixtureMismatchError(
            cause=f"fixture scenario {self._scenario_file}: {message}",
            next_action="Re-author or re-record the scenario at that path.",
        )

    def _load_entries(self) -> list[dict[str, object]]:
        try:
            text = self._scenario_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise self._fail("missing or unreadable") from exc
        lines = text.splitlines()
        if not lines:
            raise self._fail("empty")
        try:
            metadata = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise self._fail("has an unparseable metadata line") from exc
        if (
            not isinstance(metadata, dict)
            or "capture_date" not in metadata
            or "sdk_version" not in metadata
        ):
            raise self._fail("is missing capture_date or sdk_version")
        entries: list[dict[str, object]] = []
        for lineno, line in enumerate(lines[1:], start=2):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise self._fail(f"line {lineno} is unparseable") from exc
            if not isinstance(entry, dict) or "direction" not in entry or "event" not in entry:
                raise self._fail(f"line {lineno} is malformed")
            entries.append(entry)
        if not entries:
            raise self._fail("has no exchange entries")
        return entries

    def handshake(self) -> HandshakeResult:
        entry = self._entries[0]
        event = entry.get("event")
        event_type = event.get("type") if isinstance(event, dict) else None
        if entry.get("direction") != "inbound" or event_type not in (
            "session_ready",
            "auth_required",
        ):
            raise self._fail("does not open with a session_ready or auth_required event")
        self._pos = 1
        return HandshakeResult(ready=event_type == "session_ready")

    def configure_worktree_root(self, root: Path) -> None:
        self._root = str(root)

    def configure_session(
        self,
        mode: RunMode,
        system_prompt: str,
        tools: tuple[ToolDefinition, ...],
        disallowed_tools: tuple[str, ...],
    ) -> None:
        return None  # construction-time config only; not part of the replayed exchange

    def send(self, event: dict[str, object]) -> None:
        comparable = event
        if self._root is not None:
            comparable = _as_event(_substitute(event, self._root, _WORKTREE_PLACEHOLDER))
        if self._pos >= len(self._entries):
            raise self._fail(f"has no more recorded exchange but the session sent {comparable!r}")
        entry = self._entries[self._pos]
        if entry.get("direction") != "outbound" or entry.get("event") != comparable:
            raise self._fail(
                f"diverges at entry {self._pos}: expected {entry!r}, session sent "
                f"{{'direction': 'outbound', 'event': {comparable!r}}}"
            )
        self._pos += 1

    def receive(self) -> dict[str, object]:
        if self._pos >= len(self._entries):
            raise self._fail("has no more recorded events to replay")
        entry = self._entries[self._pos]
        if entry.get("direction") != "inbound":
            raise self._fail(f"entry {self._pos} is not inbound where one was expected")
        self._pos += 1
        event = entry.get("event")
        if not isinstance(event, dict):
            raise self._fail(f"entry {self._pos - 1} has a non-object event")
        if self._root is not None:
            event = _as_event(_substitute(event, _WORKTREE_PLACEHOLDER, self._root))
        return event

    def close(self) -> None:
        return None  # entries left unconsumed at close is legal (agent.md)


# ---- recording client (release-suite capture; wraps a real client) ---------------


class _RecordingSDKClient:
    """Wraps a real client, capturing every exchanged event to a fixture scenario.

    T2.1 builds this against `SDKClient` — real or fake — never a concrete live
    client: wiring the actual `claude_agent_sdk` client this wraps in production is a
    later task's build (this task's explicit scope), so it is tested here wrapping a
    `FakeSDKClient` standing in for "real" (agent.md's `create_client` test plan).
    """

    def __init__(self, real_client: SDKClient, directory: Path, sdk_version: str) -> None:
        self._real = real_client
        self._root: str | None = None
        self._handle: TextIOWrapper | None = None
        self._scenario_file = directory / _SCENARIO_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._handle = self._scenario_file.open("w", encoding="utf-8")
            self._write_line(
                {"capture_date": _dt.date.today().isoformat(), "sdk_version": sdk_version}
            )
        except OSError as exc:
            self._abort_partial()
            raise FixtureMismatchError(
                cause=f"failed starting recorded scenario at {self._scenario_file}: {exc}",
                next_action="Check disk space/permissions for the record:<dir> target and retry.",
            ) from exc

    def handshake(self) -> HandshakeResult:
        result = self._real.handshake()
        event_type = "session_ready" if result.ready else "auth_required"
        self._record("inbound", {"type": event_type})
        return result

    def configure_worktree_root(self, root: Path) -> None:
        self._root = str(root)
        self._real.configure_worktree_root(root)

    def configure_session(
        self,
        mode: RunMode,
        system_prompt: str,
        tools: tuple[ToolDefinition, ...],
        disallowed_tools: tuple[str, ...],
    ) -> None:
        self._real.configure_session(mode, system_prompt, tools, disallowed_tools)

    def send(self, event: dict[str, object]) -> None:
        self._real.send(event)
        self._record("outbound", event)

    def receive(self) -> dict[str, object]:
        event = self._real.receive()
        self._record("inbound", event)
        return event

    def close(self) -> None:
        try:
            if self._handle is not None:
                self._handle.close()
        finally:
            self._real.close()

    def _record(self, direction: str, event: dict[str, object]) -> None:
        normalized = event
        if self._root is not None:
            normalized = _as_event(_substitute(event, self._root, _WORKTREE_PLACEHOLDER))
        try:
            self._write_line({"direction": direction, "event": normalized})
        except OSError as exc:
            self._abort_partial()
            raise FixtureMismatchError(
                cause=f"failed writing recorded scenario at {self._scenario_file}: {exc}",
                next_action="Check disk space/permissions for the record:<dir> target and retry.",
            ) from exc

    def _write_line(self, obj: dict[str, object]) -> None:
        if self._handle is None:
            raise OSError(f"scenario file {self._scenario_file} is not open")
        self._handle.write(json.dumps(obj) + "\n")
        self._handle.flush()

    def _abort_partial(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
        # `missing_ok=True` only suppresses "already gone"; a directory component
        # that never existed as a directory (e.g. the open itself failed because a
        # path component is a file) raises `NotADirectoryError`, also an `OSError`
        # this best-effort cleanup must swallow -- there is nothing left to remove.
        with contextlib.suppress(OSError):
            self._scenario_file.unlink(missing_ok=True)


def _ensure_recordable_directory(directory: Path) -> None:
    """Validate `directory` is writable for `record:<dir>` (agent.md: an unwritable
    `record:<dir>` raises `FixtureMismatchError` at `create_client`)."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".blare-write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        raise FixtureMismatchError(
            cause=f"record directory {directory} is not writable: {exc}",
            next_action="Choose a writable directory for BLARE_SDK_FIXTURES=record:<dir>.",
        ) from exc


def create_client() -> SDKClient:
    """Select the SDK client via the `BLARE_SDK_FIXTURES` env-var seam.

    `replay:<dir>` is fully built (the e2e mock). `record:<dir>` validates the target
    directory is writable (a real, testable behavior) and then raises
    `NotImplementedError`: the recorder it would wrap needs a real live SDK client,
    whose wiring is out of scope for this task — `_RecordingSDKClient` itself is
    complete and unit-tested against a fake standing in for "real" (per this task's
    explicit instructions). `unset` (the live client) is likewise out of scope and
    keeps T1.1's `NotImplementedError`.
    """
    spec = os.environ.get(_ENV_VAR)
    if spec is None:
        raise NotImplementedError(
            "the live Claude Agent SDK client is out of scope for this task; "
            f"set {_ENV_VAR}=replay:<dir> to use the e2e replay seam"
        )
    if spec.startswith(_REPLAY_PREFIX):
        return _ReplayingSDKClient(Path(spec[len(_REPLAY_PREFIX) :]))
    if spec.startswith(_RECORD_PREFIX):
        directory = Path(spec[len(_RECORD_PREFIX) :])
        _ensure_recordable_directory(directory)
        raise NotImplementedError(
            "the live Claude Agent SDK client to wrap for recording is out of scope "
            "for this task; _RecordingSDKClient itself is complete and unit-tested "
            "against a fake real client"
        )
    raise FixtureMismatchError(
        cause=f"malformed {_ENV_VAR} value {spec!r}",
        next_action=f"Set {_ENV_VAR} to 'replay:<dir>' or 'record:<dir>'.",
    )


# ---- prompt templates --------------------------------------------------------------

_EARLY_DETECTION_PRINCIPLES = """\
First principles for this analysis (spec, "What Blare is"):

- Alerting is the target. Documenting failure modes is the first step, not the goal;
  every failure mode must end in an alert recommendation or an explicit, reasoned
  exclusion.
- Failure chains, not just user-visible ends. A user-visible failure is usually the
  last link of a chain. Document the upstream links as failure modes in their own
  right so each can be detected as early as possible.\
"""

_PHASE_CONTRACTS: dict[Phase, str] = {
    Phase.SYSTEM_MAP: """\
Phase 1 -- system map: document the analyzed service's components, external
dependencies, and entry points as SystemComponent entries (kind: service, worker, job,
external-dependency, datastore, or entrypoint), each with a stable ID and a
description; record dependencies between components via `depends_on`. Propose every
entry through the `propose_edits` tool, tagged for phase 1 -- free-form text never
changes the artifacts.\
""",
    Phase.FAILURE_MODES: """\
Phase 2 -- failure modes: document every way this service can fail as FailureMode
entries, each with a stable ID, a severity (`critical` demands immediate human
intervention and pages; `warning` demands action soon and files a ticket), a
`user_visible` flag, and -- for a user-visible failure -- its upstream causes named by
`caused_by`, each of which must itself be a documented entry with its own severity and
visibility. Propose every entry through `propose_edits`, tagged for phase 2.\
""",
    Phase.METRIC_COVERAGE: """\
Phase 3 -- metric coverage: inventory the metrics this codebase actually implements as
Metric entries (tied to where each is emitted in the code), then set every failure
mode's coverage status: `alertable` (implemented metrics suffice; only an alert rule is
missing), `metric-gap` (implemented metrics cannot detect it adequately; propose the
metric change needed), or `excluded` (record the reason). Every metric recommendation
must name the failure mode(s) it serves and state whether it is a new metric or a
change to an existing one. Propose every entry through `propose_edits`, tagged for
phase 3.\
""",
    Phase.ALERT_RECOMMENDATIONS: """\
Phase 4 -- alert recommendations: for every non-excluded failure mode, recommend at
least one alert rule detecting it, naming the failure mode(s) it serves; an alert
serving several failure modes carries the highest severity among them. No failure mode
may go silently unmapped. Propose every entry through `propose_edits`, tagged for
phase 4.\
""",
}


def _phase_prompt(phase: Phase, stack: ObservabilityStack) -> str:
    """Compose one phase's prompt: contract, early-detection principles, stack hint.

    The stack fragment appears only for the phases that have one (agent.md):
    `instrumentation_hints()` in phase 3, `alerting_hints()` in phase 4.
    """
    parts = [_PHASE_CONTRACTS[phase], _EARLY_DETECTION_PRINCIPLES]
    if phase is Phase.METRIC_COVERAGE:
        parts.append(stack.instrumentation_hints())
    elif phase is Phase.ALERT_RECOMMENDATIONS:
        parts.append(stack.alerting_hints())
    return "\n\n".join(parts)


_SYSTEM_PROMPT_COMMON = """\
You are Blare, an agent that gives a service the observability it needs for production
use. The target codebase is mounted read-only -- write tools are disabled; the only way
to change Blare's artifacts is the `propose_edits` tool. You have two tools:

- `propose_edits`: propose add/update/remove edits to one phase's artifact entries.
  Every edit is tagged with the phase it belongs to and is checked before it is
  accepted; the verdict returned is the tool result.
- `run_control`: report phase-affecting conclusions and amendment proposals.

Never assume a change took effect from your own free-form text -- only an accepted
`propose_edits` or `run_control` call changes anything.\
"""

_ANALYZE_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT_COMMON
    + """


This is a full analysis run: work through all four phases in order (system map,
failure modes, metric coverage, alert recommendations). The run pauses for user review
after each phase; you will be prompted again when the next phase starts.\
"""
)

_UPDATE_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT_COMMON
    + """


This is a diff-mode run (`blare update`): only the phases affected by the current
commit range's effective delta need work. You will be asked to triage the delta first
via `run_control`.\
"""
)


def _system_prompt(mode: RunMode) -> str:
    return _ANALYZE_SYSTEM_PROMPT if mode is RunMode.ANALYZE else _UPDATE_SYSTEM_PROMPT


# ---- tool-payload parsing (malformed input -> soft error verdict, never a raise) --


def _parse_edit_batch(raw: object) -> EditBatch:
    if not isinstance(raw, dict):
        raise _MalformedPayloadError("propose_edits input must be an object")
    try:
        phase = Phase(raw["phase"])
        edits_raw = raw["edits"]
    except (KeyError, ValueError) as exc:
        raise _MalformedPayloadError(f"propose_edits input malformed: {exc}") from exc
    if not isinstance(edits_raw, list):
        raise _MalformedPayloadError("propose_edits 'edits' must be a list")
    edits: list[Edit] = []
    for item in edits_raw:
        if not isinstance(item, dict):
            raise _MalformedPayloadError("propose_edits edit entry must be an object")
        try:
            op = EditOp(item["op"])
            entry_type = item["entry_type"]
            payload_or_id = item["payload_or_id"]
        except (KeyError, ValueError) as exc:
            raise _MalformedPayloadError(f"propose_edits edit entry malformed: {exc}") from exc
        if not isinstance(entry_type, str):
            raise _MalformedPayloadError("propose_edits entry_type must be a string")
        if not isinstance(payload_or_id, dict | str):
            raise _MalformedPayloadError("propose_edits payload_or_id must be an object or string")
        edits.append(Edit(op=op, entry_type=entry_type, payload_or_id=payload_or_id))
    return EditBatch(phase=phase, edits=tuple(edits))


def _parse_run_control_call(raw: object) -> RunControlCall:
    if not isinstance(raw, dict):
        raise _MalformedPayloadError("run_control input must be an object")
    try:
        action = RunControlAction(raw["action"])
        payload = raw["payload"]
    except (KeyError, ValueError) as exc:
        raise _MalformedPayloadError(f"run_control input malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise _MalformedPayloadError("run_control 'payload' must be an object")
    return RunControlCall(action=action, payload=payload)


# ---- the session --------------------------------------------------------------------


class AgentSession:
    """One Claude Agent SDK session (architecture: one per run).

    T2.1 builds `start`, `run_phase`, `chat`, `close`, and `transcript_path` in full,
    plus the two-tool dispatch over the injected `sink`/`control` handlers. `triage`,
    `request_repair`, and `notify_amendment_outcome` are deliberately not built here
    (T2.4/T3.1's scope, per architecture.md's Tasks section).
    """

    def __init__(
        self,
        client: SDKClient,
        sink: EditSink,
        control: RunControlHandler,
        stack: ObservabilityStack,
        transcript: TranscriptWriter,
    ) -> None:
        self._client = client
        self._sink = sink
        self._control = control
        self._stack = stack
        self._transcript = transcript
        self._closed = False
        self._current_phase: Phase | None = None
        self._current_driving_call = ""
        self._tool_call_in_flight = False

    def start(self, mode: RunMode, context: RunContext) -> None:
        """Handshake, then configure the client for this run.

        Raises `AuthRequiredError` if the handshake reports no login available (R12).
        Sends no wire message of its own: the system prompt, tool registrations, and
        dynamic context are construction-time configuration (see this module's
        docstring), not part of the replayed turn exchange.
        """
        self._current_driving_call = "start"
        self._current_phase = None
        result = self._call_client(self._client.handshake)
        if not result.ready:
            # Close proactively: unlike an `AgentSessionError` (whose every raise
            # site already closes via `_raise_error`), nothing downstream is
            # guaranteed to call `close()` after a raised `AuthRequiredError` until
            # the real driving loop exists (T2.2) -- closing here rather than
            # leaving it to the caller is cheap insurance against a lingering
            # connection on what is likely a common path (not logged in yet).
            self.close()
            raise AuthRequiredError(
                cause="no Claude Code subscription login available",
                next_action="Run `claude` and log in, then re-run blare.",
            )
        self._call_client(lambda: self._client.configure_worktree_root(context.worktree_root))
        system_prompt = _system_prompt(mode)
        tools = tool_definitions()
        self._call_client(
            lambda: self._client.configure_session(
                mode, system_prompt, tools, WRITE_TOOLS_DISALLOWED
            )
        )
        # Not part of the replayed wire exchange (construction-time config, per this
        # module's docstring) but still a "prompt" R14's transcript must capture, so it
        # is written directly rather than through `_send`.
        self._write_transcript(
            "outbound",
            {
                "type": "session_init",
                "mode": mode.value,
                "system_prompt": system_prompt,
                "tools": [d.name for d in tools],
                "disallowed_tools": list(WRITE_TOOLS_DISALLOWED),
                "worktree_root": str(context.worktree_root),
                "delta_files": list(context.delta_files),
                "patch_text": context.patch_text,
            },
        )

    def run_phase(self, phase: Phase) -> None:
        """Send that phase's prompt (contract, principles, stack hint) and drain the
        turn; results live in the candidate set via the sink, so nothing is returned."""
        self._current_driving_call = "run_phase"
        self._current_phase = phase
        prompt = _phase_prompt(phase, self._stack)
        self._send({"type": "phase_prompt", "phase": int(phase), "text": prompt})
        self._drain_turn()

    def chat(self, text: str) -> str:
        """Pass `text` through to the live session; return the turn's concatenated
        text blocks (empty when the turn was tool-calls-only); the turn is drained."""
        self._current_driving_call = "chat"
        self._send({"type": "chat", "text": text})
        return self._drain_turn()

    def close(self) -> None:
        """End the SDK session only (the orchestrator owns and closes the
        `TranscriptWriter` itself). Idempotent; safe after any `AgentSessionError`.

        The underlying client's own `close()` failing (plausible right after a
        transport error -- a connection that just dropped, or a process that already
        exited, will often also fail to close cleanly) is swallowed: this is cleanup,
        there is no "next action" to report for a close failure, and letting it
        escape would violate "safe after any AgentSessionError" -- worse, it would
        silently replace whatever error triggered the close (see `_raise_error`) with
        an unrelated, unattributed exception.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._client.close()

    @property
    def transcript_path(self) -> Path:
        return self._transcript.path

    # ---- internals ----

    def _context_label(self) -> str:
        if self._current_phase is not None:
            return f"phase {int(self._current_phase)}"
        return self._current_driving_call

    def _raise_error(self, cause: str, tool_call_in_flight: bool) -> NoReturn:
        context = self._context_label()
        full_cause = f"{context}: {cause}" if context else cause
        if tool_call_in_flight:
            full_cause = f"{full_cause} (tool call in flight)"
        next_action = "Re-run blare."
        try:
            path = self._transcript.path
        except Exception:  # noqa: BLE001 - best-effort path for the message only
            path = None
        if path is not None:
            next_action = f"{next_action} Read the transcript at {path} for details."
        self.close()
        raise AgentSessionError(cause=full_cause, next_action=next_action)

    def _call_client(self, fn: Callable[[], _T]) -> _T:
        """Run one `SDKClient` call, wrapping any exception it raises other than this
        module's own error types as `AgentSessionError` (transport/protocol failures,
        including from `start`'s handshake/configure_* calls, per agent.md's error
        taxonomy listing `start` among the driving-call context labels).

        A `FixtureMismatchError` the client itself raises (e.g. a replaying client's
        own divergence check) closes the session before propagating, same as
        `AgentSessionError` (via `_raise_error`) and `start`'s `AuthRequiredError`
        path: closing is cheap and idempotent, and every raise site that ends the
        run should leave nothing lingering open, not only the ones this module
        raises itself.
        """
        try:
            return fn()
        except AgentSessionError:
            raise
        except (FixtureMismatchError, AuthRequiredError):
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001 - transport/protocol errors, wrapped
            self._raise_error(str(exc) or type(exc).__name__, self._tool_call_in_flight)

    def _send(self, event: dict[str, object]) -> None:
        self._call_client(lambda: self._client.send(event))
        self._write_transcript("outbound", event)

    def _receive(self) -> dict[str, object]:
        event = self._call_client(self._client.receive)
        self._write_transcript("inbound", event)
        return event

    def _write_transcript(self, direction: str, event: dict[str, object]) -> None:
        try:
            self._transcript.write_event(direction, event)
        except Exception as exc:  # noqa: BLE001 - R14 is hard: unwritable aborts the run
            self._raise_error(f"transcript write failed: {exc}", self._tool_call_in_flight)

    def _drain_turn(self) -> str:
        text_parts: list[str] = []
        while True:
            event = self._receive()
            event_type = event.get("type")
            if event_type == "text":
                text = event.get("text")
                if not isinstance(text, str):
                    self._raise_error("malformed text event: missing text", False)
                text_parts.append(text)
            elif event_type == "tool_use":
                self._handle_tool_use(event)
            elif event_type == "turn_end":
                return "".join(text_parts)
            else:
                self._raise_error(f"protocol failure: out-of-contract event {event_type!r}", False)

    def _handle_tool_use(self, event: dict[str, object]) -> None:
        tool_use_id = event.get("id")
        name = event.get("name")
        if not isinstance(tool_use_id, str) or not isinstance(name, str):
            self._raise_error("malformed tool_use event: missing id or name", True)
        raw_input = event.get("input")
        self._tool_call_in_flight = True
        try:
            if name == "propose_edits":
                result = self._dispatch_propose_edits(raw_input)
            elif name == "run_control":
                result = self._dispatch_run_control(raw_input)
            else:
                self._raise_error(f"unknown tool call {name!r}", True)
            self._send({"type": "tool_result", "tool_use_id": tool_use_id, "result": result})
        finally:
            self._tool_call_in_flight = False

    def _dispatch_propose_edits(self, raw_input: object) -> dict[str, object]:
        try:
            batch = _parse_edit_batch(raw_input)
        except _MalformedPayloadError as exc:
            return {"ok": False, "message": str(exc)}
        try:
            verdict = self._sink(batch)
        except Exception as exc:  # noqa: BLE001 - a raising handler is a programmer error
            self._raise_error(f"edit sink raised: {exc}", True)
        return {"ok": verdict.ok, "message": verdict.message}

    def _dispatch_run_control(self, raw_input: object) -> dict[str, object]:
        try:
            call = _parse_run_control_call(raw_input)
        except _MalformedPayloadError as exc:
            return {"ok": False, "message": str(exc)}
        try:
            verdict = self._control(call)
        except Exception as exc:  # noqa: BLE001 - a raising handler is a programmer error
            self._raise_error(f"run-control handler raised: {exc}", True)
        return {"ok": verdict.ok, "message": verdict.message}
