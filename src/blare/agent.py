"""The Claude Agent SDK boundary (architecture): the system's only mock boundary.

T1.1 scope: only the client seam (`create_client`) and enough of `AgentSession` to
reach session start against a replay fixture — the "handshake-only replay fixture
that reaches session start" the walking skeleton's second e2e test drives. The full
module (session lifecycle, the two structured tools over injected handlers, phase
prompts, chat pass-through, transcripts, the record/replay round trip, the full
error taxonomy) is built out in task T2.1 per `engineering/modules/agent.md`.

Provisional: the fixture JSONL shape read here (a metadata line plus one
`{direction, event}` handshake line) is this task's own minimal, hand-authored
guess at the format `agent.md`'s "Client seam and fixtures" section describes in
full; T2.1 is free to extend or reshape it once the real SDK handshake is known.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from blare.model import BlareError

_ENV_VAR = "BLARE_SDK_FIXTURES"
_REPLAY_PREFIX = "replay:"
_RECORD_PREFIX = "record:"


class AuthRequiredError(BlareError):
    """No Claude Code subscription login available (R12)."""


class AgentSessionError(BlareError):
    """The agent session failed: transport, protocol, or a raising handler."""


class FixtureMismatchError(BlareError):
    """Replay divergence, a missing/malformed scenario, or a malformed seam value."""


@dataclass(frozen=True)
class HandshakeResult:
    """The outcome of `AgentSession.start`'s minimal SDK handshake."""

    ready: bool


class SDKClient(Protocol):
    """The small protocol wrapping the SDK client surface this module uses.

    T1.1 needs only the handshake step; `create_client`'s eventual full surface
    (session streaming, tool registration) lands with T2.1.
    """

    def handshake(self) -> HandshakeResult: ...

    def close(self) -> None: ...


class _ReplaySDKClient:
    """Replays a hand-authored handshake fixture (the e2e mock, T1.1 subset)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def handshake(self) -> HandshakeResult:
        scenario_file = self._directory / "handshake.jsonl"
        try:
            lines = scenario_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise FixtureMismatchError(
                cause=f"fixture scenario {scenario_file} is missing or unreadable",
                next_action="Re-author the handshake fixture at that path.",
            ) from exc
        if not lines:
            raise FixtureMismatchError(
                cause=f"fixture scenario {scenario_file} is empty",
                next_action="Re-author the handshake fixture at that path.",
            )
        try:
            metadata = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise FixtureMismatchError(
                cause=f"fixture scenario {scenario_file} has an unparseable metadata line",
                next_action="Re-author the handshake fixture at that path.",
            ) from exc
        if "capture_date" not in metadata or "sdk_version" not in metadata:
            raise FixtureMismatchError(
                cause=(
                    f"fixture scenario {scenario_file} is missing capture_date or sdk_version"
                ),
                next_action="Re-author the handshake fixture at that path.",
            )
        if len(lines) < 2:
            raise FixtureMismatchError(
                cause=f"fixture scenario {scenario_file} has no handshake event",
                next_action="Re-author the handshake fixture at that path.",
            )
        try:
            entry = json.loads(lines[1])
        except json.JSONDecodeError as exc:
            raise FixtureMismatchError(
                cause=f"fixture scenario {scenario_file} has an unparseable handshake line",
                next_action="Re-author the handshake fixture at that path.",
            ) from exc
        event = entry.get("event", {})
        event_type = event.get("type") if entry.get("direction") == "inbound" else None
        if event_type == "session_ready":
            return HandshakeResult(ready=True)
        if event_type == "auth_required":
            return HandshakeResult(ready=False)
        raise FixtureMismatchError(
            cause=(
                f"fixture scenario {scenario_file} does not open with a "
                "session_ready or auth_required event"
            ),
            next_action="Re-author the handshake fixture at that path.",
        )

    def close(self) -> None:
        return None


def create_client() -> SDKClient:
    """Select the SDK client via the `BLARE_SDK_FIXTURES` env-var seam.

    T1.1 wires only the `replay:<dir>` branch the seam-through e2e test needs; the
    live client (unset) and the recording client (`record:<dir>`) are T2.1 and
    T4.1's work respectively.
    """
    spec = os.environ.get(_ENV_VAR)
    if spec is None:
        raise NotImplementedError(
            "the live Claude Agent SDK client is implemented in task T2.1; "
            f"set {_ENV_VAR}=replay:<dir> to use the e2e replay seam"
        )
    if spec.startswith(_REPLAY_PREFIX):
        return _ReplaySDKClient(Path(spec[len(_REPLAY_PREFIX) :]))
    if spec.startswith(_RECORD_PREFIX):
        raise NotImplementedError("the recording SDK client is implemented in task T4.1")
    raise FixtureMismatchError(
        cause=f"malformed {_ENV_VAR} value {spec!r}",
        next_action=f"Set {_ENV_VAR} to 'replay:<dir>' or 'record:<dir>'.",
    )


class AgentSession:
    """One Claude Agent SDK session (architecture: one per run).

    T1.1 exposes only `start` and `close` — enough to reach session start against
    the replay seam. `triage`, `run_phase`, `chat`, `request_repair`,
    `notify_amendment_outcome`, the injected sink/control/stack/transcript
    dependencies, and the full error taxonomy are T2.1's build.
    """

    def __init__(self, client: SDKClient) -> None:
        self._client = client

    def start(self) -> None:
        """Perform the minimal SDK handshake; raise `AuthRequiredError` if it fails."""
        result = self._client.handshake()
        if not result.ready:
            raise AuthRequiredError(
                cause="no Claude Code subscription login available",
                next_action="Run `claude` and log in, then re-run blare.",
            )

    def close(self) -> None:
        self._client.close()
