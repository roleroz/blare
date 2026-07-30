"""Unit tests for blare.stack, per engineering/modules/stack.md's test plan.

Two sets, per the global testing rules: `test_contract_*` covers what the module
promises while its dependency (the pinned `promql-parser` package) behaves normally;
`test_failure_parser_*` covers what happens when that dependency misbehaves.
"""

from __future__ import annotations

from dataclasses import replace

import promql_parser
import pytest

from blare.stack import (
    _MAX_EXPRESSION_LENGTH,
    AlertRuleInput,
    ExpressionVerdict,
    PrometheusStack,
    UnsupportedStackError,
    get_stack,
    supported_stacks,
)


def _valid_alert() -> AlertRuleInput:
    return AlertRuleInput(
        name="HighErrorRate",
        expr="rate(errors_total[5m]) > 0.1",
        for_duration="5m",
        severity="critical",
        annotations={"summary": "error rate high", "description": "error rate exceeded"},
    )


# --- Registry --------------------------------------------------------------------


def test_contract_get_stack_returns_prometheus_for_known_name() -> None:
    """get_stack('prometheus') returns a stack whose name is 'prometheus'."""
    stack = get_stack("prometheus")

    assert isinstance(stack, PrometheusStack)
    assert stack.name == "prometheus"


def test_contract_get_stack_unknown_name_raises_listing_supported_values() -> None:
    """An unregistered stack name raises UnsupportedStackError naming it and the supported set."""
    with pytest.raises(UnsupportedStackError) as exc_info:
        get_stack("datadog")

    assert "datadog" in exc_info.value.cause
    assert "prometheus" in exc_info.value.cause
    assert ".blare/config.yaml" in exc_info.value.next_action


def test_contract_supported_stacks_returns_exactly_registered_names() -> None:
    """supported_stacks() returns exactly the registry's names."""
    assert supported_stacks() == ["prometheus"]


# --- Rule-field validation ---------------------------------------------------------


def test_contract_validate_rule_fields_accepts_valid_input() -> None:
    """A fully populated, well-formed AlertRuleInput passes rule-field validation."""
    stack = get_stack("prometheus")

    verdict = stack.validate_rule_fields(_valid_alert())

    assert verdict == ExpressionVerdict(ok=True, message=None)


def test_contract_validate_rule_fields_rejects_worded_duration() -> None:
    """for_duration of '5 minutes' (not a Prometheus duration) is rejected naming the field."""
    stack = get_stack("prometheus")
    alert = replace(_valid_alert(), for_duration="5 minutes")

    verdict = stack.validate_rule_fields(alert)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "for_duration" in verdict.message


def test_contract_validate_rule_fields_rejects_nonsense_duration() -> None:
    """for_duration of 'soon' (not a Prometheus duration) is rejected naming the field."""
    stack = get_stack("prometheus")
    alert = replace(_valid_alert(), for_duration="soon")

    verdict = stack.validate_rule_fields(alert)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "for_duration" in verdict.message


def test_contract_validate_rule_fields_rejects_empty_severity() -> None:
    """An empty severity is rejected."""
    stack = get_stack("prometheus")
    alert = replace(_valid_alert(), severity="")

    verdict = stack.validate_rule_fields(alert)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "severity" in verdict.message


def test_contract_validate_rule_fields_rejects_missing_summary_annotation() -> None:
    """A missing 'summary' annotation key is rejected naming it."""
    stack = get_stack("prometheus")
    alert = replace(_valid_alert(), annotations={"description": "error rate exceeded"})

    verdict = stack.validate_rule_fields(alert)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "summary" in verdict.message


def test_contract_validate_rule_fields_rejects_missing_description_annotation() -> None:
    """A missing 'description' annotation key is rejected naming it."""
    stack = get_stack("prometheus")
    alert = replace(_valid_alert(), annotations={"summary": "error rate high"})

    verdict = stack.validate_rule_fields(alert)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "description" in verdict.message


# --- Prompt fragments --------------------------------------------------------------


def test_contract_instrumentation_hints_mentions_client_libraries_and_nonempty() -> None:
    """instrumentation_hints names every client library/pattern stack.md lists, non-empty."""
    stack = get_stack("prometheus")

    hints = stack.instrumentation_hints()

    assert hints
    for needle in (
        "prometheus_client",
        "prometheus/client_golang",
        "prom-client",
        "Micrometer",
        "/metrics",
    ):
        assert needle in hints


def test_contract_alerting_hints_names_expression_language_and_fields() -> None:
    """alerting_hints names PromQL and every AlertRuleInput field, incl. required
    annotation keys."""
    stack = get_stack("prometheus")

    hints = stack.alerting_hints()

    assert hints
    assert "PromQL" in hints
    for needle in ("name", "expr", "for_duration", "severity", "annotations", "summary",
                   "description"):
        assert needle in hints
    # The expression-length cap (validate_expression's pathological-input guard) is
    # surfaced to the model here so it can split an over-long alert instead of
    # writing one long expression; assert against the live constant so this test
    # can't silently desync from the cap's actual value.
    assert str(_MAX_EXPRESSION_LENGTH) in hints


# --- Expression validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "up",
        "rate(x[5m]) > 0.1",
        "sum(rate(x[5m])) by (job)",
        "up == 1 and down == 0 or up == 2",
        "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))",
    ],
)
def test_contract_validate_expression_accepts_valid_promql(expr: str) -> None:
    """A bare selector, rate/comparison, aggregation-by, boolean ops, and
    histogram_quantile all validate as ok."""
    stack = get_stack("prometheus")

    verdict = stack.validate_expression(expr)

    assert verdict == ExpressionVerdict(ok=True, message=None)


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "sum(rate(x[5m]))"[:-1],  # unbalanced parens
        "rate(x[5 minutes])",  # bad duration
        "this is definitely not a valid query",  # free text
    ],
)
def test_contract_validate_expression_rejects_invalid_promql(expr: str) -> None:
    """Empty string, unbalanced parens, a bad duration, and free text are all rejected
    with a message."""
    stack = get_stack("prometheus")

    verdict = stack.validate_expression(expr)

    assert verdict.ok is False
    assert verdict.message


def test_contract_validate_expression_accepts_deeply_nested_under_length_cap() -> None:
    """A legal, deeply nested expression that stays under the length cap is actually
    parsed (not rejected by the cap) and accepted -- proving the parser itself, not
    only the cap, is exercised and crash-free short of the pathological case."""
    stack = get_stack("prometheus")
    nested = ("(" * 2000) + "x" + (")" * 2000)  # 4001 chars: under the 5000 cap
    assert len(nested) < 5000

    verdict = stack.validate_expression(nested)

    assert verdict == ExpressionVerdict(ok=True, message=None)


# --- Rule shape ----------------------------------------------------------------------


def test_contract_alert_rule_shape_produces_exact_dict() -> None:
    """alert_rule_shape renders the exact Prometheus rule dict for a fixed recommendation."""
    stack = get_stack("prometheus")
    alert = AlertRuleInput(
        name="HighErrorRate",
        expr="rate(errors_total[5m]) > 0.1",
        for_duration="5m",
        severity="critical",
        annotations={"summary": "error rate high", "description": "error rate exceeded"},
    )

    shape = stack.alert_rule_shape(alert)

    assert shape == {
        "alert": "HighErrorRate",
        "expr": "rate(errors_total[5m]) > 0.1",
        "for": "5m",
        "labels": {"severity": "critical"},
        "annotations": {
            "summary": "error rate high",
            "description": "error rate exceeded",
        },
    }


# --- Failure modes: the pinned promql-parser package ----------------------------------


def test_failure_parser_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parser that raises on input yields a rejection verdict; no exception escapes."""
    stack = get_stack("prometheus")

    def _raise(_expr: str) -> None:
        raise ValueError("boom from the parser")

    monkeypatch.setattr(promql_parser, "parse", _raise)

    verdict = stack.validate_expression("up")

    assert verdict.ok is False
    assert verdict.message is not None
    assert "boom from the parser" in verdict.message


def test_failure_parser_pathological_input() -> None:
    """A very long, deeply nested expression yields a verdict without hanging or crashing.

    The pinned promql-parser 0.10.0 is a recursive-descent parser implemented in Rust
    that overflows its native call stack (a segfault, not a Python exception) on inputs
    nested a few thousand levels deep -- verified experimentally against bare
    parentheses, chained function calls, and long chains of binary operators, all of
    which crash the interpreter once the input passes roughly 13,000-14,000
    characters on this project's reference environment. validate_expression's length
    cap rejects such input before ever invoking the parser; assert the rejection is
    specifically the length-cap path (not some other failure) by checking the
    length limit is named in the message.
    """
    stack = get_stack("prometheus")
    pathological = ("(" * 100_000) + "x" + (")" * 100_000)

    verdict = stack.validate_expression(pathological)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "5000" in verdict.message
