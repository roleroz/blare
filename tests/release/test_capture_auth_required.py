"""live: capture the auth-required fixture (R12) with the live Claude Agent
SDK, run with a scratch `HOME` carrying no Claude Code login (T4.1's
continuation).

Runs against a throwaway scratch repo, never miniflux_v2 -- this scenario
needs no real codebase, only the real auth-handshake failure shape. Tagged
`live`, `exclusive`, but touches no shared mutable state.
"""

from __future__ import annotations

from pathlib import Path

from tests.release import capture
from tests.release.scenario_driver import finalize_capture


def test_live_capture_auth_required(tmp_path: Path) -> None:
    """A `blare analyze` run with a scratch, credential-less `HOME` fails the
    real SDK auth handshake and exits 1 -- the fixture captures that real
    refusal shape."""
    cap = capture.capture_auth_required(tmp_path)
    finalize_capture(cap.record_dir, capture.FIXTURES_ROOT / "auth-required")
