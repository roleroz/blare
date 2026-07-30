"""Unit tests for blare.agent (T1.1 subset: the client seam and handshake replay)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blare.agent import (
    AgentSession,
    AuthRequiredError,
    FixtureMismatchError,
    create_client,
)


def _write_fixture(directory: Path, event: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"capture_date": "2026-07-30", "sdk_version": "provisional-skeleton"}),
        json.dumps({"direction": "inbound", "event": event}),
    ]
    (directory / "handshake.jsonl").write_text("\n".join(lines) + "\n")


def test_contract_create_client_replay_seam_reaches_session_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """replay:<dir> selects the replaying client; a well-formed handshake fixture
    lets AgentSession.start() succeed."""
    _write_fixture(tmp_path, {"type": "session_ready"})
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{tmp_path}")

    client = create_client()
    session = AgentSession(client)
    session.start()  # must not raise
    session.close()


def test_contract_create_client_unset_is_not_yet_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BLARE_SDK_FIXTURES unset, create_client raises NotImplementedError
    (the live SDK client is T2.1's build)."""
    monkeypatch.delenv("BLARE_SDK_FIXTURES", raising=False)

    with pytest.raises(NotImplementedError):
        create_client()


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


def test_contract_auth_required_maps_to_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auth_required handshake event raises AuthRequiredError naming the login step."""
    _write_fixture(tmp_path, {"type": "auth_required"})
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{tmp_path}")
    client = create_client()
    session = AgentSession(client)

    with pytest.raises(AuthRequiredError) as exc_info:
        session.start()

    assert "claude" in exc_info.value.next_action


def test_failure_missing_fixture_scenario_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay directory with no handshake.jsonl raises FixtureMismatchError."""
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{tmp_path}")
    client = create_client()

    with pytest.raises(FixtureMismatchError):
        client.handshake()


def test_failure_malformed_fixture_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handshake fixture missing capture metadata raises FixtureMismatchError."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "handshake.jsonl").write_text(
        json.dumps({"direction": "inbound", "event": {"type": "session_ready"}}) + "\n"
    )
    monkeypatch.setenv("BLARE_SDK_FIXTURES", f"replay:{tmp_path}")
    client = create_client()

    with pytest.raises(FixtureMismatchError):
        client.handshake()
