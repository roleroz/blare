"""The Claude Agent SDK boundary (architecture): the system's only mock boundary.

T2.1 scope (`engineering/modules/agent.md`): session lifecycle (`start`, `run_phase`,
`chat`, `close`), the two structured tools (`propose_edits`, `run_control`) dispatched
over orchestrator-injected handlers, the phase/system prompt templates, `create_client`
with the replay and (directory-validation-only) record branches, transcript writing, and
the error taxonomy.

T2.4 scope: `request_repair` (the channel for every system-initiated repair and for
resuming an agent-proposed amendment whose turn ended before `amend_complete`) and
`notify_amendment_outcome` (closes the loop on every amendment unit).

T3.1 scope: `triage` (diff mode's first step, R18) -- sends the effective delta plus
the verdict contract and returns once an `affected_verdict`/`no_impact` verdict is
accepted, reminding once then raising on a verdict-less turn.

T2.6 scope: `create_client`'s `unset` (live) branch -- `_LiveSDKClient`, a real
`claude_agent_sdk.ClaudeSDKClient` wrapped to satisfy the `SDKClient` protocol below,
with no `model` override (2026-07-30 decision: the Claude Code subscription's own
default), wired into `start`'s auth-handshake preflight and the two in-process MCP
tools. Also wires a `_LiveSDKClient` into the `record:<dir>` branch's already-complete
`_RecordingSDKClient`.

T4.3 scope: `AgentSession.__init__`'s `on_activity` callback (R25) -- fired with
the dispatched tool's name for every tool call in a driving call's turn, not only
`propose_edits`/`run_control`'s round trip but also every SDK filesystem-read
tool (`Read`, `Grep`, `Glob`, ...), which the live client (`_LiveSDKClient
._consume_turn`) now translates from `ToolUseBlock` content into a new, additive
"activity" wire event (`_ReplayingSDKClient`/`_RecordingSDKClient` need no
special-casing: both already pass an arbitrary event dict through verbatim).
Also adds `_ReplayingSDKClient`'s optional `delay_before` fixture field, a
hand-authored-only e2e seam letting a scripted scenario take real wall-clock
time so the orchestrator's real-clock progress ticker has something to tick
against before a slow phase's turn ends.

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

import asyncio
import concurrent.futures
import contextlib
import datetime as _dt
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import Any, NoReturn, Protocol, TypeVar, cast

import claude_agent_sdk

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
    Violation,
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
        # T4.3 (R25): an optional, hand-authored-only delay sibling to
        # "direction"/"event" -- never produced by the recorder, additive to the
        # wire format (absent on every existing fixture) -- so an e2e scenario
        # can make a scripted phase take real wall-clock time, giving the
        # orchestrator's real-clock progress ticker something to actually tick
        # against before the turn ends.
        delay = entry.get("delay_before")
        if isinstance(delay, int | float) and delay > 0:
            time.sleep(delay)
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


# ---- live client (the real claude_agent_sdk.ClaudeSDKClient, T2.6) ---------------

# The name registered for Blare's in-process MCP server (agent.md, SDK usage: "an
# in-process MCP server exposes exactly two tools"). Only ever seen by the model
# through the tool names themselves; not otherwise meaningful.
_MCP_SERVER_NAME = "blare"

# A cheap, fixed probe turn for the auth-handshake preflight (agent.md, Auth
# preflight: "start performs a minimal SDK handshake"). The installed SDK exposes no
# dedicated auth-failure exception (verified against claude_agent_sdk==0.2.128's
# actual API before writing this): the only concrete signal it defines is
# `AssistantMessage.error == "authentication_failed"` (types.py's
# `AssistantMessageError` literal), which is observable only in response to an
# actual turn -- there is no cheaper connect-only signal the installed SDK exposes.
_HANDSHAKE_PROBE_PROMPT = "Reply with the single word: ready."


def _result_message_failure(message: claude_agent_sdk.ResultMessage) -> str:
    """Format a failing `ResultMessage`'s subtype and any detail into one string
    (e.g. `"error_max_turns"`, `"error_during_execution (disk full)"`, or
    `"success (HTTP 529)"` for the documented `is_error=True, subtype="success"`
    shape that carries only an `api_error_status`, no `errors` entries) -- shared
    by the handshake probe and `_consume_turn`'s own `ResultMessage.is_error`
    handling, both of which must not miss this: `_internal/query.py` documents
    CLI-reported failures (`error_max_turns`, `error_during_execution`, ...) that
    surface only here, with no `AssistantMessage.error` ever set."""
    details = list(message.errors) if message.errors else []
    if message.api_error_status is not None:
        details.append(f"HTTP {message.api_error_status}")
    detail = f" ({'; '.join(details)})" if details else ""
    return f"{message.subtype}{detail}"


def _interpret_handshake_message(message: object) -> bool | None:
    """Settle the handshake's ready/not-ready verdict from one message of the probe
    turn, or return None when this message doesn't settle it (the caller keeps
    draining). `authentication_failed` is the one SDK error this handshake
    interprets as "no login available" (R12); any other named SDK error (billing,
    rate limit, server error, ..., or a failing `ResultMessage` -- max-turns,
    execution error, ...) is a real operational failure, not a login problem, so
    it is raised (a plain `RuntimeError`, deliberately *not* one of this module's
    own error types -- `AgentSession._call_client`'s generic exception handling
    is what must wrap it, closing the session and attaching the phase/driving
    -call context label; an `AgentSessionError` raised directly from here would
    instead hit `_call_client`'s "already enriched, just re-raise" branch and
    skip both).
    """
    if isinstance(message, claude_agent_sdk.AssistantMessage) and message.error is not None:
        if message.error == "authentication_failed":
            return False
        raise RuntimeError(f"handshake probe returned SDK error {message.error!r}")
    if isinstance(message, claude_agent_sdk.ResultMessage) and message.is_error:
        raise RuntimeError(
            f"handshake probe turn ended in error: {_result_message_failure(message)}"
        )
    return None


def _is_own_tool_wire_name(name: str, own_names: frozenset[str]) -> bool:
    """True when `name` -- a `ToolUseBlock`'s wire-reported name -- refers to one
    of Blare's own registered tools (`propose_edits`/`run_control`) rather than
    an SDK built-in (`Read`, `Grep`, `Glob`, ...). The CLI may report an
    in-process MCP tool's name bare or prefixed (`mcp__<server>__<tool>`); both
    forms are matched. Blare's own two tools already get `on_activity` (R25)
    fired from the bridge's own "tool_use" round trip
    (`AgentSession._handle_tool_use`) -- translating their `ToolUseBlock` in
    `_consume_turn` too would fire it a second time for the same call."""
    return name in own_names or any(name.endswith(f"__{n}") for n in own_names)


def _to_mcp_tool_result(result: dict[str, object]) -> dict[str, Any]:
    """Map this module's `{"ok": bool, "message": str | None}` verdict shape (the
    `propose_edits`/`run_control` tool result, agent.md) to the SDK MCP tool
    handler's documented return contract: a `content` block list plus `is_error`
    (claude_agent_sdk.tool's docstring)."""
    message = result.get("message")
    return {
        "content": [{"type": "text", "text": str(message) if message is not None else ""}],
        "is_error": not result.get("ok", False),
    }


class _ToolBridge:
    """Bridges the SDK's async in-process MCP tool handlers to this module's
    synchronous `send`/`receive` wire protocol.

    Each real tool call blocks its async handler (running on the client's
    background event-loop thread) on a `concurrent.futures.Future` until the
    corresponding `tool_result` is delivered via `send` -- called from whatever
    thread drives `AgentSession` -- exactly mirroring the tool_use/tool_result
    round trip the replaying/recording clients exchange over their `send`/`receive`
    pair. `concurrent.futures.Future.set_result` is thread-safe, and
    `asyncio.wrap_future` schedules the corresponding asyncio Future's result via
    the owning loop's thread-safe call scheduling, so `resolve` needs no loop
    handle of its own.
    """

    def __init__(self, events: queue.Queue[dict[str, object]]) -> None:
        self._events = events
        self._pending: dict[str, concurrent.futures.Future[dict[str, object]]] = {}
        self._lock = threading.Lock()

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool_use_id = uuid.uuid4().hex
        future: concurrent.futures.Future[dict[str, object]] = concurrent.futures.Future()
        with self._lock:
            self._pending[tool_use_id] = future
        self._events.put({"type": "tool_use", "id": tool_use_id, "name": name, "input": args})
        try:
            result = await asyncio.wrap_future(future)
        finally:
            # Always remove this call's own entry, regardless of outcome --
            # including a cancellation (e.g. the client closes with this call
            # still in flight and no handler ever raised: the SDK's own task
            # teardown on disconnect cancels this coroutine, which would
            # otherwise abandon the entry in `_pending` forever).
            with self._lock:
                self._pending.pop(tool_use_id, None)
        return _to_mcp_tool_result(result)

    def resolve(self, tool_use_id: str, result: dict[str, object]) -> None:
        with self._lock:
            future = self._pending.pop(tool_use_id, None)
        if future is None:
            raise KeyError(f"no pending live-SDK tool call with id {tool_use_id!r}")
        future.set_result(result)


def _build_sdk_tools(
    tools: tuple[ToolDefinition, ...], bridge: _ToolBridge
) -> list[claude_agent_sdk.SdkMcpTool[Any]]:
    """Build the real in-process MCP tools from this module's own `ToolDefinition`s
    (T2.1's `tool_definitions()`), each handler forwarding into `bridge.dispatch`
    under its own name -- so real dispatch reuses the exact schemas the
    fixture/replay world already tests against, and the SDK routes each call to
    the right handler by construction (this module never needs to parse whatever
    name the CLI reports back on the wire)."""
    built: list[claude_agent_sdk.SdkMcpTool[Any]] = []
    for definition in tools:

        async def _handler(
            args: dict[str, Any], _name: str = definition.name
        ) -> dict[str, Any]:
            return await bridge.dispatch(_name, args)

        built.append(
            claude_agent_sdk.tool(
                definition.name, definition.description, definition.input_schema
            )(_handler)
        )
    return built


def _build_live_options(
    system_prompt: str,
    tools: tuple[ToolDefinition, ...],
    disallowed_tools: tuple[str, ...],
    worktree_root: Path | None,
    bridge: _ToolBridge,
) -> claude_agent_sdk.ClaudeAgentOptions:
    """Build the real `ClaudeAgentOptions` for the live session: no `model` field
    (2026-07-30 decision -- the Claude Code subscription's own default is used,
    never pinned), the two in-process MCP tools built from `tools`, and the
    write-tools-disallowed policy already established in T2.1
    (`WRITE_TOOLS_DISALLOWED`). Split out from `configure_session` so the
    construction itself -- what a unit test can assert on without ever
    connecting -- is separate from actually connecting the real client.
    """
    sdk_tools = _build_sdk_tools(tools, bridge)
    server = claude_agent_sdk.create_sdk_mcp_server(_MCP_SERVER_NAME, tools=sdk_tools)
    return claude_agent_sdk.ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={_MCP_SERVER_NAME: server},
        # Only Blare's own two tools, never a target-repo .mcp.json or other
        # ambient MCP configuration (the target repo is otherwise read-only).
        strict_mcp_config=True,
        allowed_tools=[definition.name for definition in tools],
        disallowed_tools=list(disallowed_tools),
        # No per-tool permission prompts: Blare's own checkpoint loop is the
        # human-facing approval surface; disallowed_tools above still wins over
        # bypassPermissions for the write tools (ClaudeAgentOptions' own
        # documented precedence).
        permission_mode="bypassPermissions",
        cwd=worktree_root,
        # model intentionally omitted (2026-07-30 decision): the CLI's own
        # default is used, never pinned.
    )


class _LiveSDKClient:
    """The real `claude_agent_sdk.ClaudeSDKClient`, wrapped to satisfy `SDKClient`.

    Bridges the real client's async, message-stream API to this module's
    synchronous, per-turn `send`/`receive` wire protocol (agent.md leaves the
    client seam's exact shape as this module's own implementation detail). A
    dedicated background thread runs one asyncio event loop for the client's whole
    lifetime; every call into the real SDK is scheduled onto it and its result (or
    exception) is bridged back to the calling thread.

    `configure_session` never sets `ClaudeAgentOptions.model` (2026-07-30 decision:
    the Claude Code subscription's own default is used, never pinned) and builds
    the two in-process MCP tools from the `ToolDefinition`s T2.1 already defines,
    via `claude_agent_sdk.create_sdk_mcp_server`/`.tool` (verified against the
    installed `claude_agent_sdk==0.2.128` package before writing this).
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="blare-live-sdk-loop", daemon=True
        )
        self._loop_thread.start()
        self._worktree_root: Path | None = None
        self._client: claude_agent_sdk.ClaudeSDKClient | None = None
        self._events: queue.Queue[dict[str, object]] = queue.Queue()
        self._bridge = _ToolBridge(self._events)
        self._closed = False
        # R25 (T4.3): the registered tool names for this session, set by
        # `configure_session` -- lets `_consume_turn` tell Blare's own two tools'
        # `ToolUseBlock`s (already covered by the bridge's "tool_use" round trip)
        # apart from every SDK built-in it must translate to an "activity" event.
        self._own_tool_names: frozenset[str] = frozenset()

    def _run_coro(self, coro: Coroutine[Any, Any, _T]) -> _T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def handshake(self) -> HandshakeResult:
        """A separate, throwaway probe connection with minimal default options --
        the real config (system prompt, tools, cwd) isn't known yet at this point
        in `AgentSession.start`'s call order, which asks for the handshake before
        `configure_worktree_root`/`configure_session`."""
        return self._run_coro(self._handshake_async())

    async def _handshake_async(self) -> HandshakeResult:
        # No tools at all for the probe turn (agent.md: "the target repo is
        # read-only to the model" -- true even for this throwaway preflight,
        # which has no legitimate reason to touch any tool at all); strict_mcp
        # _config so no ambient MCP server config could offer one either,
        # matching the real session's own hardening in _build_live_options.
        probe_options = claude_agent_sdk.ClaudeAgentOptions(
            max_turns=1, tools=[], strict_mcp_config=True
        )
        probe = claude_agent_sdk.ClaudeSDKClient(probe_options)
        ready = True
        try:
            await probe.connect()
            await probe.query(_HANDSHAKE_PROBE_PROMPT)
            async for message in probe.receive_response():
                verdict = _interpret_handshake_message(message)
                if verdict is not None:
                    # A settled verdict is final: stop draining rather than
                    # continue into the turn's terminating ResultMessage, which
                    # a real auth failure also marks is_error=True (the CLI still
                    # emits it before exiting) -- letting the loop reach that
                    # would raise a generic RuntimeError and discard the already
                    # -correct "not logged in" verdict this handshake exists to
                    # detect (R12).
                    ready = verdict
                    break
        finally:
            # Suppressed for the same reason `close()` suppresses the real
            # client's own close failure: a `disconnect()` failure while a real
            # SDK error (e.g. rate_limit) is already propagating out of the
            # `try` block would otherwise replace it in flight (plain
            # try/finally semantics), silently losing the actual diagnostic
            # (R13) behind an unrelated transport-close error -- there is no
            # "next action" to report for a cleanup failure either way.
            with contextlib.suppress(Exception):
                await probe.disconnect()
        return HandshakeResult(ready=ready)

    def configure_worktree_root(self, root: Path) -> None:
        self._worktree_root = root

    def configure_session(
        self,
        mode: RunMode,
        system_prompt: str,
        tools: tuple[ToolDefinition, ...],
        disallowed_tools: tuple[str, ...],
    ) -> None:
        options = _build_live_options(
            system_prompt, tools, disallowed_tools, self._worktree_root, self._bridge
        )
        self._own_tool_names = frozenset(t.name for t in tools)
        self._client = claude_agent_sdk.ClaudeSDKClient(options)
        self._run_coro(self._client.connect())

    def send(self, event: dict[str, object]) -> None:
        # Both raises below are plain `RuntimeError`s, deliberately not this
        # module's own `AgentSessionError`: `AgentSession._call_client` (the sole
        # caller of `send`, via its `_send` wrapper) special-cases `AgentSessionError`
        # as "already enriched, just re-raise" and would skip both closing the
        # session and attaching the phase/driving-call context label that its
        # generic `except Exception` branch adds for every other internal failure.
        if event.get("type") == "tool_result":
            tool_use_id = event["tool_use_id"]
            result = event["result"]
            if not isinstance(tool_use_id, str) or not isinstance(result, dict):
                raise RuntimeError(f"malformed tool_result event sent to live client: {event!r}")
            self._bridge.resolve(tool_use_id, cast("dict[str, object]", result))
            return
        text = event.get("text")
        if not isinstance(text, str):
            raise RuntimeError(f"live SDK client cannot send event with no text field: {event!r}")
        self._run_coro(self._start_turn(text))

    async def _start_turn(self, text: str) -> None:
        assert self._client is not None
        await self._client.query(text)
        # Fire-and-forget on this same loop: `send` must return once the turn is
        # under way, not once it finishes -- `receive` is what drains it
        # incrementally, one event at a time, per this module's wire protocol.
        self._loop.create_task(self._consume_turn())

    async def _consume_turn(self) -> None:
        assert self._client is not None
        try:
            async for message in self._client.receive_response():
                if isinstance(message, claude_agent_sdk.AssistantMessage):
                    if message.error is not None:
                        self._events.put({"type": "_sdk_error", "error": message.error})
                        return
                    for block in message.content:
                        if isinstance(block, claude_agent_sdk.TextBlock) and block.text:
                            self._events.put({"type": "text", "text": block.text})
                        elif isinstance(
                            block, claude_agent_sdk.ToolUseBlock
                        ) and not _is_own_tool_wire_name(block.name, self._own_tool_names):
                            # R25 (T4.3): every tool call the SDK itself executes
                            # (Read, Grep, Glob, ...) dominates a phase's actual
                            # wall-clock time, so on_activity must see these too,
                            # not only propose_edits/run_control's round trip --
                            # agent.md's on_activity contract. A dedicated event
                            # kind, distinct from "tool_use" (still reserved for
                            # Blare's own two tools' request/response round trip
                            # via _ToolBridge below): AgentSession._drain_turn
                            # fires on_activity for it and keeps draining, no
                            # tool_result ever expected.
                            self._events.put({"type": "activity", "name": block.name})
                    continue
                if isinstance(message, claude_agent_sdk.ResultMessage):
                    # A CLI-reported turn failure (error_max_turns,
                    # error_during_execution, ...) surfaces only here, with no
                    # AssistantMessage.error ever set (_internal/query.py) --
                    # missing this would silently report a failed turn as an
                    # empty successful one.
                    if message.is_error:
                        self._events.put(
                            {"type": "_sdk_error", "error": _result_message_failure(message)}
                        )
                        return
                    continue  # the turn ends right after; "turn_end" is pushed below
                # Blare's own two tools' ToolUseBlock content is not translated
                # here: the registered SDK MCP tool handler itself pushes the
                # "tool_use" event (_ToolBridge.dispatch) under this module's own
                # canonical tool name, decoupled from whatever name the CLI
                # reports on the wire for an in-process MCP tool -- translating
                # the same call a second time here would double-report it (see
                # _is_own_tool_wire_name's filter above). Any other message type
                # is skipped, draining to the next one.
        except Exception as exc:  # noqa: BLE001 - must reach receive(), not vanish in this task
            self._events.put({"type": "_transport_error", "error": exc})
            return
        self._events.put({"type": "turn_end"})

    def receive(self) -> dict[str, object]:
        event = self._events.get()
        event_type = event.get("type")
        if event_type == "_transport_error":
            # The captured exception's own type (CLIConnectionError, ProcessError,
            # ...) is what must reach `AgentSession._call_client`'s generic
            # `except Exception` branch -- re-raising it verbatim (rather than
            # wrapping it) keeps that branch's phase-context/close enrichment
            # working exactly as it already does for the replaying/recording
            # clients' own raised exceptions.
            raise cast(BaseException, event["error"])
        if event_type == "_sdk_error":
            # Plain RuntimeError, not AgentSessionError: see send()'s comment --
            # the same "must reach the generic exception branch" reasoning.
            raise RuntimeError(f"assistant turn reported SDK error: {event['error']}")
        return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._run_coro(self._client.disconnect())
        with contextlib.suppress(Exception):
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5)


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

    `unset` — the real client (T2.6): a `_LiveSDKClient` wrapping
    `claude_agent_sdk.ClaudeSDKClient`, unpinned (no `model` override — the
    2026-07-30 decision). `replay:<dir>` is the e2e mock. `record:<dir>` validates
    the target directory is writable, then wraps a fresh `_LiveSDKClient` in
    `_RecordingSDKClient` (T2.1's recorder, unmodified) for release-suite capture.
    """
    spec = os.environ.get(_ENV_VAR)
    if spec is None:
        return _LiveSDKClient()
    if spec.startswith(_REPLAY_PREFIX):
        return _ReplayingSDKClient(Path(spec[len(_REPLAY_PREFIX) :]))
    if spec.startswith(_RECORD_PREFIX):
        directory = Path(spec[len(_RECORD_PREFIX) :])
        _ensure_recordable_directory(directory)
        return _RecordingSDKClient(_LiveSDKClient(), directory, claude_agent_sdk.__version__)
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


# `triage`'s message (T3.1) -- the delta travels here, never in a phase prompt
# (agent.md). Kept as one fixed constant (not re-derived from RunContext) so its
# exact bytes are stable for the replaying client's byte-exact comparison.
_TRIAGE_MESSAGE = (
    "Diff-mode triage: review the effective delta's file list and patch text "
    "(above) and decide which phase(s) need work as a result -- system map (1), "
    "failure modes (2), metric coverage (3), alert recommendations (4). Report "
    "your conclusion through the run_control tool before ending your turn: call "
    "it with action \"affected_verdict\" and payload {\"phases\": [<phase "
    "numbers>]} naming every phase that needs work, or with action \"no_impact\" "
    "and payload {\"reasoning\": \"<why the delta needs no artifact changes>\"} "
    "if none do."
)

_TRIAGE_REMINDER = (
    "Please call run_control with affected_verdict or no_impact before ending "
    "your turn."
)


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

    T2.1 built `start`, `run_phase`, `chat`, `close`, and `transcript_path` in full,
    plus the two-tool dispatch over the injected `sink`/`control` handlers. T2.4 adds
    `request_repair` and `notify_amendment_outcome`. T3.1 adds `triage`. T4.3 adds
    `on_activity` (R25): fired with the dispatched tool's name for every tool call
    in a turn -- `propose_edits`/`run_control` (via `_handle_tool_use`) and every
    SDK filesystem-read tool alike (via the new "activity" event `_drain_turn`
    handles) -- feeding the orchestrator's progress ticker.
    """

    def __init__(
        self,
        client: SDKClient,
        sink: EditSink,
        control: RunControlHandler,
        stack: ObservabilityStack,
        transcript: TranscriptWriter,
        on_activity: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._sink = sink
        self._control = control
        self._stack = stack
        self._transcript = transcript
        self._on_activity = on_activity
        self._closed = False
        self._current_phase: Phase | None = None
        self._current_driving_call = ""
        self._current_repair_phases: tuple[Phase, ...] = ()
        self._tool_call_in_flight = False
        # Message-wording state for `request_repair` (agent.md's discriminator): set
        # when an `amend_proposal` is accepted, cleared when the following
        # `amend_complete` is accepted -- "the session's own state, not the
        # argument". `_awaiting_amend_complete` is per-`request_repair`-call: set
        # before sending, cleared the moment an `amend_complete` is accepted during
        # the drained turn.
        self._unresolved_amend_proposal = False
        self._awaiting_amend_complete = False
        # T3.1: `triage`'s own remind-once-then-raise state, and the delta context
        # `start` captures for `triage` to send later (the delta travels in the
        # triage message, not at `start` time as a wire event -- agent.md).
        self._triage_verdict_received = False
        self._delta_files: tuple[str, ...] = ()
        self._patch_text = ""

    def start(self, mode: RunMode, context: RunContext) -> None:
        """Handshake, then configure the client for this run.

        Raises `AuthRequiredError` if the handshake reports no login available (R12).
        Sends no wire message of its own: the system prompt, tool registrations, and
        dynamic context are construction-time configuration (see this module's
        docstring), not part of the replayed turn exchange.
        """
        self._current_driving_call = "start"
        self._current_phase = None
        self._delta_files = context.delta_files
        self._patch_text = context.patch_text
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

    def triage(self) -> None:
        """Diff mode's first step (agent.md, R18): send the effective delta's file
        list and patch text -- captured from `RunContext` at `start`, never in a
        phase prompt -- plus the verdict contract, then drain the turn. Returns
        once an `affected_verdict` or `no_impact` conclusion has been *accepted*
        by the injected run-control handler during the turn; reminds once via a
        follow-up message when a turn ends without one, then raises
        `AgentSessionError` after a second verdict-less turn -- the same
        remind-once-then-raise shape `request_repair` uses for `amend_complete`.
        A *rejected* run_control call (e.g. a `no_impact` bounced back because
        seeded phases still need work) does not count as arrived: the model's
        turn is expected to continue with another attempt, and only an accepted
        verdict flips `_triage_verdict_received` (set in `_dispatch_run_control`).
        """
        self._current_driving_call = "triage"
        self._current_phase = None
        self._triage_verdict_received = False
        self._send(
            {
                "type": "triage",
                "delta_files": list(self._delta_files),
                "patch_text": self._patch_text,
                "text": _TRIAGE_MESSAGE,
            }
        )
        self._drain_turn()
        if self._triage_verdict_received:
            return
        self._send({"type": "triage_reminder", "text": _TRIAGE_REMINDER})
        self._drain_turn()
        if self._triage_verdict_received:
            return
        self._raise_error(
            "triage turn ended without an accepted affected_verdict or no_impact "
            "verdict after a reminder",
            False,
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

    def request_repair(self, phases: list[Phase], violations: list[Violation]) -> None:
        """The channel for every system-initiated repair (the approval-gate system
        amendment, and R18's load-seeded violations) and for resuming an
        agent-proposed amendment whose turn ended after `amend_proposal` but before
        `amend_complete` (agent.md). Sends a message naming the phases and (when
        non-empty) the violations, then waits for the model to call `run_control`
        with `amend_complete` -- reminding once via a follow-up message when a
        drained turn ends without it, and raising `AgentSessionError` after a
        second eventless turn.
        """
        self._current_driving_call = "request_repair"
        self._current_phase = None
        self._current_repair_phases = tuple(phases)
        message = self._request_repair_message(phases, violations)
        self._awaiting_amend_complete = True
        self._send(
            {
                "type": "request_repair",
                "phases": [int(p) for p in phases],
                "violations": [self._violation_payload(v) for v in violations],
                "text": message,
            }
        )
        self._drain_turn()
        if not self._awaiting_amend_complete:
            return
        self._send(
            {
                "type": "request_repair_reminder",
                "text": (
                    "Please call run_control with amend_complete once the repair "
                    "for the named phase(s) is done."
                ),
            }
        )
        self._drain_turn()
        if not self._awaiting_amend_complete:
            return
        self._raise_error(
            "repair turn ended without amend_complete after a reminder", False
        )

    def notify_amendment_outcome(
        self, approved: bool, restored_phases: list[Phase]
    ) -> None:
        """Closes the loop on every amendment unit (R2): sends a message stating
        approval, or rejection with the restored phases, and blocks until the
        model's acknowledgment turn ends. Anything the model does in that turn
        (a batch against a re-frozen phase, a fresh amend_proposal) flows through
        the normal tool handlers.
        """
        self._current_driving_call = "notify_amendment_outcome"
        self._current_phase = None
        if approved:
            message = "The amendment was approved; its changes are now part of the run."
        else:
            phases_text = ", ".join(f"phase {int(p)}" for p in restored_phases)
            message = (
                "The amendment was rejected; the following phase(s) were restored "
                f"to their pre-amendment state: {phases_text}. Any edits you made "
                "to them during the amendment no longer exist."
            )
        self._send(
            {
                "type": "amendment_outcome",
                "approved": approved,
                "restored_phases": [int(p) for p in restored_phases],
                "text": message,
            }
        )
        self._drain_turn()

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
        if self._current_driving_call == "request_repair":
            phases_text = ", ".join(str(int(p)) for p in self._current_repair_phases)
            return f"request_repair (phases {phases_text})"
        return self._current_driving_call

    def _request_repair_message(self, phases: list[Phase], violations: list[Violation]) -> str:
        """The message `request_repair` sends: violations wording whenever any are
        named; otherwise the discriminator between the resume and cascade wording
        is this session's own state (agent.md), not the call's arguments -- the
        resume wording iff an `amend_proposal` still stands unresolved from a prior
        drained turn."""
        phases_text = ", ".join(f"phase {int(p)}" for p in phases)
        if violations:
            violations_text = "; ".join(
                f"{v.kind.value} ({', '.join(v.entry_ids)})" for v in violations
            )
            return (
                f"Repair needed in {phases_text}: {violations_text}. Propose edits "
                "via propose_edits tagged for these phase(s), then call run_control "
                "with amend_complete when the repair is done."
            )
        if self._unresolved_amend_proposal:
            return (
                "Your proposed amendment's standing phase(s) are now open: "
                f"{phases_text}. Propose repairs via propose_edits, then call "
                "run_control with amend_complete when done."
            )
        return (
            f"The amendment now also covers {phases_text} (pulled in by reference "
            "to your changes). Propose repairs via propose_edits, then call "
            "run_control with amend_complete when done."
        )

    def _violation_payload(self, v: Violation) -> dict[str, object]:
        return {"kind": v.kind.value, "entry_ids": list(v.entry_ids), "phase": int(v.phase)}

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

    def _fire_activity(self, name: str) -> None:
        """R25: notify the injected `on_activity` callback of one dispatched tool
        call. A pure notification -- its return value is ignored, and any
        exception it raises is caught and dropped here rather than propagated:
        R25 is presentation-only and must never affect a run's outcome or
        interrupt the turn (agent.md). A no-op when no callback was injected."""
        if self._on_activity is None:
            return
        with contextlib.suppress(Exception):
            self._on_activity(name)

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
            elif event_type == "activity":
                # R25: a tool call the SDK executed itself (a filesystem-read
                # tool such as Read/Grep/Glob) that never goes through Blare's
                # own propose_edits/run_control round trip -- fire on_activity
                # and keep draining; no tool_result is ever expected or sent
                # for this event kind (agent.md, on_activity).
                name = event.get("name")
                if isinstance(name, str):
                    self._fire_activity(name)
            elif event_type == "turn_end":
                return "".join(text_parts)
            else:
                self._raise_error(f"protocol failure: out-of-contract event {event_type!r}", False)

    def _handle_tool_use(self, event: dict[str, object]) -> None:
        tool_use_id = event.get("id")
        name = event.get("name")
        if not isinstance(tool_use_id, str) or not isinstance(name, str):
            self._raise_error("malformed tool_use event: missing id or name", True)
        self._fire_activity(name)
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
        if verdict.ok:
            # Message-wording state for `request_repair` (see its docstring and
            # `_request_repair_message`): an accepted amend_proposal leaves a
            # standing, unresolved proposal; an accepted amend_complete resolves it
            # and also satisfies whichever request_repair call is currently
            # awaiting one.
            if call.action is RunControlAction.AMEND_PROPOSAL:
                self._unresolved_amend_proposal = True
            elif call.action is RunControlAction.AMEND_COMPLETE:
                self._unresolved_amend_proposal = False
                self._awaiting_amend_complete = False
            elif call.action in (
                RunControlAction.AFFECTED_VERDICT,
                RunControlAction.NO_IMPACT,
            ):
                self._triage_verdict_received = True
        return {"ok": verdict.ok, "message": verdict.message}
