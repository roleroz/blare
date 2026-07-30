"""The metrics/alerting stack abstraction (architecture): what instrumentation the
agent should look for, whether an alert expression is valid, and what shape a
rendered alert rule takes. One interface, one Prometheus implementation in the MVP
(spec, Constraints); adding a stack requires no artifact-schema change.

Consumed by **agent** (prompt context, via `instrumentation_hints`/`alerting_hints`)
and **artifacts** (alert-expression and rule-field validation, via `validate_expression`
/`validate_rule_fields`, and derived-doc rendering via `alert_rule_shape`) per
`engineering/modules/stack.md`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import promql_parser

from blare.model import BlareError

# promql-parser 0.10.0 (D10) is a recursive-descent parser implemented in Rust: a
# malformed-but-not-pathological expression raises a Python ValueError as expected,
# but sufficiently deep recursion (nested parens, nested function calls, or long
# chains of binary operators — verified experimentally against all three) overflows
# the parser's *native* call stack instead, which segfaults the interpreter rather
# than raising anything Python can catch. On this project's reference environment
# (default 8 MiB thread stack), the cheapest trigger found -- bare nested
# parentheses, 2 characters per nesting level -- crashed at ~6,875 levels of
# nesting (~13,750 characters); the exact figure is empirical and stack-size
# dependent, so it should not be read as a hard architectural constant. Legitimate
# Prometheus alert expressions, including verbose multi-window/multi-burn-rate SLO
# alerts, run at most a few thousand characters, so a cap of 5,000 -- comfortably
# under the measured crash boundary with better than 2x margin, and comfortably
# over any realistic legitimate expression -- rejects pathological input before
# ever calling the parser without costing legitimate input, keeping stack.md's
# "verdict, never a crash" contract true regardless of the pinned library's own
# robustness. Rejections on this path carry a synthesized message rather than the
# parser's own text, since the parser is never invoked for input over the cap;
# every other rejection still carries the parser's verbatim message.
_MAX_EXPRESSION_LENGTH = 5000

_INSTRUMENTATION_HINTS = """\
Inventory instrumentation already implemented in this codebase using any of:

- Python: the `prometheus_client` library (Counter, Gauge, Histogram, Summary
  definitions and their `.labels(...)` call sites).
- Go: `prometheus/client_golang` (`prometheus.NewCounter`/`NewGauge`/`NewHistogram`
  and their registration with a `prometheus.Registry`).
- Node.js: `prom-client` (`new Counter(...)`, `new Gauge(...)`, `new Histogram(...)`).
- Java/JVM: Micrometer's Prometheus registry (`PrometheusMeterRegistry`, meters
  registered against it).
- Any direct `/metrics` HTTP exposition endpoint, whichever library serves it.

A metric definition site is where a metric's name, type, and labels are declared —
not every place it is incremented or observed.\
"""

_ALERTING_HINTS = f"""\
Write every alert expression as PromQL — this stack's expression language.

Each recommended alert is an `AlertRuleInput` with every one of these fields
populated:

- `name`: the alert's identifier.
- `expr`: the PromQL expression that fires the alert, at most
  {_MAX_EXPRESSION_LENGTH} characters. Split an alert that would need more into
  several smaller ones rather than writing one long expression.
- `for_duration`: how long the expression must hold before firing, as a Prometheus
  duration (e.g. `5m`).
- `severity`: `critical` or `warning`, matching the failure mode's severity.
- `annotations`: a dict whose required keys are `summary` (a short one-line
  description) and `description` (the fuller explanation shown on the alert).\
"""


@dataclass(frozen=True)
class ExpressionVerdict:
    """The result of validating an expression or a rule's non-expression fields."""

    ok: bool
    message: str | None


@dataclass(frozen=True)
class AlertRuleInput:
    """One alert recommendation's fields, in the shape a stack validates and renders.

    Deliberately this module's own type, not imported from **artifacts** — the
    dependency runs artifacts -> stack, never the reverse (architecture); artifacts
    maps its `AlertRecommendation` entries into this shape before calling in.
    """

    name: str
    expr: str
    for_duration: str
    severity: str
    annotations: dict[str, str]


class UnsupportedStackError(BlareError):
    """Raised by `get_stack` for a name the registry does not recognize (R23).

    Cause names the given value and the supported set; next action is the fix
    (edit `.blare/config.yaml`'s `stack` key) — **artifacts**, which owns that file,
    attaches the file name itself when propagating this as R23's refusal.
    """


class ObservabilityStack(ABC):
    """One metrics/alerting stack: prompt hints plus expression/rule validation."""

    name: ClassVar[str]

    @abstractmethod
    def instrumentation_hints(self) -> str:
        """The phase-3 prompt fragment: what instrumentation to look for."""

    @abstractmethod
    def alerting_hints(self) -> str:
        """The phase-4 prompt fragment: the expression language and rule fields."""

    @abstractmethod
    def validate_expression(self, expr: str) -> ExpressionVerdict:
        """Whether `expr` is a syntactically valid expression in this stack's language.

        Never raises: a parser failure becomes a rejecting verdict carrying the
        parser's own message verbatim. An implementation may also reject input on a
        precondition it must check before ever invoking the parser (e.g. a length
        cap guarding against a known crash in the underlying library); such a
        rejection carries a synthesized message instead, since the parser's own
        message does not exist for input the parser never saw.
        """

    @abstractmethod
    def validate_rule_fields(self, alert: AlertRuleInput) -> ExpressionVerdict:
        """Whether `alert`'s non-expression fields are valid for this stack.

        Never raises, for the same reason as `validate_expression`.
        """

    @abstractmethod
    def alert_rule_shape(self, alert: AlertRuleInput) -> dict[str, object]:
        """Render `alert` as this stack's native rule structure."""


class PrometheusStack(ObservabilityStack):
    """The MVP's only shipped stack (spec, Constraints and Non-goals)."""

    name: ClassVar[str] = "prometheus"

    def instrumentation_hints(self) -> str:
        return _INSTRUMENTATION_HINTS

    def alerting_hints(self) -> str:
        return _ALERTING_HINTS

    def validate_expression(self, expr: str) -> ExpressionVerdict:
        if len(expr) > _MAX_EXPRESSION_LENGTH:
            return ExpressionVerdict(
                ok=False,
                message=(
                    f"expression is {len(expr)} characters, over the "
                    f"{_MAX_EXPRESSION_LENGTH}-character limit"
                ),
            )
        try:
            promql_parser.parse(expr)
        except Exception as exc:  # intentionally broad: verdict, never an exception (stack.md)
            return ExpressionVerdict(ok=False, message=str(exc))
        return ExpressionVerdict(ok=True, message=None)

    def validate_rule_fields(self, alert: AlertRuleInput) -> ExpressionVerdict:
        try:
            promql_parser.parse_duration(alert.for_duration)
        except Exception as exc:  # intentionally broad: verdict, never an exception (stack.md)
            return ExpressionVerdict(
                ok=False,
                message=f"for_duration {alert.for_duration!r} is not a valid duration: {exc}",
            )
        if not alert.severity.strip():
            return ExpressionVerdict(ok=False, message="severity must not be empty")
        missing = [key for key in ("summary", "description") if key not in alert.annotations]
        if missing:
            return ExpressionVerdict(
                ok=False,
                message=f"annotations missing required key(s): {', '.join(missing)}",
            )
        return ExpressionVerdict(ok=True, message=None)

    def alert_rule_shape(self, alert: AlertRuleInput) -> dict[str, object]:
        return {
            "alert": alert.name,
            "expr": alert.expr,
            "for": alert.for_duration,
            "labels": {"severity": alert.severity},
            "annotations": dict(alert.annotations),
        }


_REGISTRY: dict[str, ObservabilityStack] = {"prometheus": PrometheusStack()}


def get_stack(name: str) -> ObservabilityStack:
    """Look up a stack by its `config.yaml` name; raise `UnsupportedStackError` if unknown."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnsupportedStackError(
            cause=f"unsupported metrics/alerting stack {name!r}; supported: "
            f"{', '.join(supported_stacks())}",
            next_action="Edit the stack value in .blare/config.yaml to a supported stack.",
        ) from None


def supported_stacks() -> list[str]:
    """The registry's names, for R23's refusal messages."""
    return list(_REGISTRY.keys())
