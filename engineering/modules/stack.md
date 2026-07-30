# Module design — stack

## Decisions needed from you

This section contains only open items. **No open items** — D10 (expression validation via
the pinned `promql-parser` PyPI package) is settled and logged in `engineering/decisions.md`.


## Responsibility

The metrics/alerting stack abstraction (architecture): what instrumentation the agent should
look for, whether an alert expression is valid, and what shape a rendered alert rule takes.
One interface, one Prometheus implementation in the MVP; adding a stack must require no
artifact-schema change.

## Interface

```python
class ObservabilityStack(ABC):
    name: ClassVar[str]
    @abstractmethod
    def instrumentation_hints(self) -> str      # phase-3 prompt fragment
    @abstractmethod
    def alerting_hints(self) -> str             # phase-4 prompt fragment
    @abstractmethod
    def validate_expression(self, expr: str) -> ExpressionVerdict
    @abstractmethod
    def validate_rule_fields(self, alert: AlertRuleInput) -> ExpressionVerdict
    @abstractmethod
    def alert_rule_shape(self, alert: AlertRuleInput) -> dict[str, object]

def get_stack(name: str) -> ObservabilityStack   # raises UnsupportedStackError
def supported_stacks() -> list[str]              # the registry's names, for R23 messages

@dataclass(frozen=True)
class ExpressionVerdict:
    ok: bool
    message: str | None

@dataclass(frozen=True)
class AlertRuleInput:                            # stack-owned; artifacts maps its
    name: str                                    # AlertRecommendation entries into it,
    expr: str                                    # preserving the artifacts->stack
    for_duration: str                            # dependency direction
    severity: str
    annotations: dict[str, str]
```

- `instrumentation_hints` returns the phase-3 prompt fragment the **agent** module injects:
  client libraries and patterns to inventory (for Prometheus: `prometheus_client`,
  `prometheus/client_golang`, `prom-client`, Micrometer's Prometheus registry, direct
  `/metrics` exposition), and what counts as a metric definition site.
- `alerting_hints` returns the phase-4 prompt fragment: the expression language the model
  must write in (PromQL) and every `AlertRuleInput` field a recommendation must populate —
  name, expression, `for` duration, severity, and annotations with `summary` and
  `description` as the required keys. The agent module stays stack-agnostic; without this
  channel it would have to hardcode "write PromQL".
- `validate_expression` is consumed by **artifacts** (per-batch content check and the
  semantic tier's R4 language clause). It never raises for bad input — a verdict, not an
  exception; internal parser failures become a failing verdict carrying the parser message.
  The Prometheus implementation additionally enforces a length precondition (5,000
  characters) *before* invoking the parser: `promql-parser` 0.10.0 is a recursive-descent
  parser whose native call stack overflows on sufficiently deep/long adversarial input
  (nested parens, nested calls, long operator chains), segfaulting the interpreter rather
  than raising — an uncatchable failure a Python `try` cannot turn into a verdict after
  the fact. The cap sits with wide margin below the measured crash boundary (~6,875 levels
  of paren nesting on the reference environment) and above any realistic legitimate
  expression; rejections on this path carry a synthesized message, since the parser is
  never invoked for input over the cap.
- `validate_rule_fields` validates the stack-specific non-expression fields — for
  Prometheus: `for_duration` is a valid Prometheus duration, severity non-empty,
  `summary` and `description` annotation keys present — so a malformed duration cannot
  land in the "deployable rule YAML" the derived docs promise. Consumed by **artifacts**
  alongside `validate_expression` in the per-batch check for alert edits; same
  verdict-not-exception contract.
- `supported_stacks` enumerates the registry — the source of the supported values named in
  `UnsupportedStackError` and in artifacts' R23 missing-config error, which has no stack
  name to look up and therefore needs the list directly.
- `alert_rule_shape` renders one recommendation as the stack's native rule structure; for
  Prometheus: `{alert, expr, for, labels: {severity}, annotations: {summary, description}}`.
  Used by artifacts' derived-doc rendering so `alert-recommendations.md` shows deployable
  rule YAML.
- `get_stack` is the registry behind `config.yaml`'s `stack` key; unknown names raise
  `UnsupportedStackError` listing supported values (feeds R23).

## Data structures

`ExpressionVerdict` and `AlertRuleInput` above — `AlertRuleInput` is deliberately this
module's own type so stack never imports from artifacts (the dependency runs
artifacts→stack). The stack is stateless and all instances are safe to share.

## Error handling

`UnsupportedStackError` derives from the system's one error type (architecture), carrying
cause (the name given, the names supported) and next action (edit the `stack` value in
`.blare/config.yaml`); the artifacts module, which owns the config path, attaches the file
name when propagating it (R23). Everything else is a verdict — this module deliberately has
no other failure surface, so a stack bug cannot abort a run: the worst case is a wrongly
rejected expression, visible to the agent as a tool verdict.

## Failure visibility

Verdict messages carry the parser's own error text verbatim for every expression the parser
actually sees, so a rejected expression shows the user (and the model) exactly why. The one
exception is the length-cap precondition above: since the parser is never invoked for
over-cap input, that rejection's message is synthesized (stating the length and the limit)
rather than quoted from the parser. Nothing is logged directly; verdicts surface through the
batch-check tool result and semantic-violation listings.

## Test plan

Fakes: none — the module has no dependencies beyond the pinned `promql-parser` package.

Contract tests, one per behaviour:

- registry returns the Prometheus stack for `prometheus`; unknown name raises listing
  supported values; `supported_stacks` returns exactly the registered names.
- rule-field validation: a valid input passes; `for_duration` of `5 minutes` or `soon` is
  rejected naming the field; missing `summary` or `description` annotation keys rejected;
  empty severity rejected.
- instrumentation hints mention each client library named above and are non-empty.
- alerting hints name the expression language and every `AlertRuleInput` field, with
  summary and description as the required annotation keys — the same list the interface
  bullet defines.
- valid expressions accepted: a bare selector, `rate(x[5m]) > 0.1`, aggregation with `by`,
  boolean operators, a histogram_quantile expression.
- invalid expressions rejected with a message: empty string, unbalanced parens, bad duration,
  free text.
- `alert_rule_shape` produces the exact rule dict for a fixed recommendation (whole-payload
  assertion — this shape is consumer-facing contract).

Failure-mode tests, dependency = the PromQL parser library:

- `test_failure_parser_raises` — parser monkeypatched to raise on input; verdict is a
  rejection carrying the exception message, no exception escapes.
- `test_failure_parser_pathological_input` — very long/deeply nested expression completes
  with a verdict (no hang, no crash).
