"""Unit tests for blare.model."""

from __future__ import annotations

from blare.model import BlareError, RunMode


def test_contract_blare_error_carries_cause_and_next_action() -> None:
    """A BlareError exposes its cause and next_action, and str() is the cause."""
    error = BlareError(cause="something broke", next_action="do the fix")

    assert error.cause == "something broke"
    assert error.next_action == "do the fix"
    assert str(error) == "something broke"


def test_contract_run_mode_values() -> None:
    """RunMode has exactly the analyze and update members the two commands need."""
    assert RunMode.ANALYZE.value == "analyze"
    assert RunMode.UPDATE.value == "update"
