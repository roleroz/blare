"""Unit tests for blare.model."""

from __future__ import annotations

import pytest

from blare.model import BlareError, Phase, RunMode, Violation, ViolationKind


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


def test_contract_phase_values_are_the_phase_numbers() -> None:
    """Phase members carry the phase numbers 1-4 in run order."""
    assert Phase.SYSTEM_MAP.value == 1
    assert Phase.FAILURE_MODES.value == 2
    assert Phase.METRIC_COVERAGE.value == 3
    assert Phase.ALERT_RECOMMENDATIONS.value == 4


@pytest.mark.parametrize(
    "kind,expected_phase",
    [
        (ViolationKind.UNMAPPED_FAILURE_MODE, Phase.ALERT_RECOMMENDATIONS),
        (ViolationKind.LINKAGE_INCONSISTENCY, Phase.ALERT_RECOMMENDATIONS),
        (ViolationKind.INVALID_EXPRESSION, Phase.ALERT_RECOMMENDATIONS),
        (ViolationKind.ALERT_SEVERITY_BELOW_MAX, Phase.ALERT_RECOMMENDATIONS),
        (ViolationKind.EXCLUDED_WITH_ALERT_COVERAGE, Phase.ALERT_RECOMMENDATIONS),
        (ViolationKind.EXCLUDED_WITH_METRIC_COVERAGE, Phase.METRIC_COVERAGE),
        (ViolationKind.EMPTY_LINKAGE_METRIC_RECOMMENDATION, Phase.METRIC_COVERAGE),
        (ViolationKind.EMPTY_LINKAGE_ALERT_RECOMMENDATION, Phase.ALERT_RECOMMENDATIONS),
    ],
)
def test_contract_violation_derives_its_kinds_fixed_repair_phase(
    kind: ViolationKind, expected_phase: Phase
) -> None:
    """Violation.phase is derived mechanically from kind, matching artifacts.md's table."""
    violation = Violation(kind=kind, entry_ids=("fm-x",))

    assert violation.phase is expected_phase
    assert violation.entry_ids == ("fm-x",)


def test_contract_violation_phase_cannot_be_set_independently() -> None:
    """Violation's phase field is init=False: passing it positionally is a TypeError."""
    with pytest.raises(TypeError):
        # Deliberately passing the disallowed third (init=False) argument.
        Violation(  # type: ignore[call-arg]
            ViolationKind.UNMAPPED_FAILURE_MODE, ("fm-x",), Phase.SYSTEM_MAP
        )
