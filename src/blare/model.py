"""Value types shared across module boundaries.

None of Blare's six architecture modules (cli, orchestrator, gitrepo, artifacts, agent,
stack) owns these types outright, and the dependency graph is one-directional
(cli -> orchestrator -> {gitrepo, artifacts, agent}, agent -> stack, artifacts -> stack).
Putting cross-cutting value types that more than one module needs to reference here
avoids importing "downstream" modules into "upstream" ones just to name a type.

T1.1 added only what the walking skeleton needed (`RunMode`, `BlareError`). T1.4
(artifacts) added `Phase` and `Violation`/`ViolationKind`: **agent** names them too
(`request_repair(phases, violations)`, `engineering/modules/agent.md`) despite having no
dependency edge on **artifacts** in the architecture's module graph (agent -> stack
only), so putting them in `artifacts.py` would force a disallowed agent -> artifacts
import. T2.1 (agent) added the edit-proposal and run-control payload/verdict types and
`RunContext` — the same reasoning: this is the currency the agent module's injected
sink/control handlers and `start` exchange with the orchestrator, none of which agent
may get by importing artifacts or orchestrator directly. Later tasks add what remains as
those modules are built out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum, StrEnum
from pathlib import Path


class RunMode(Enum):
    """Which of Blare's two commands is running: full analysis or diff mode."""

    ANALYZE = "analyze"
    UPDATE = "update"


class Phase(IntEnum):
    """One of the four full-analysis phases (spec, Scope): system map, failure modes,
    metric coverage, alert recommendations, in run order.

    An `IntEnum` rather than a plain `Enum`: `orchestrator.md`'s phase queue and
    `artifacts.md`'s `referencing_phases`/`Violation.phase` all reason about phase order
    and compare against bare phase numbers ("phase 3", "phase 4"), which an int-valued
    enum supports directly.
    """

    SYSTEM_MAP = 1
    FAILURE_MODES = 2
    METRIC_COVERAGE = 3
    ALERT_RECOMMENDATIONS = 4


class ViolationKind(Enum):
    """Every kind of semantic-invariant violation `artifacts.semantic_violations` reports
    (R4-R5 plus the excluded-empty-sets property; `engineering/modules/artifacts.md`).
    """

    UNMAPPED_FAILURE_MODE = "unmapped_failure_mode"
    LINKAGE_INCONSISTENCY = "linkage_inconsistency"
    INVALID_EXPRESSION = "invalid_expression"
    ALERT_SEVERITY_BELOW_MAX = "alert_severity_below_max"
    EXCLUDED_WITH_ALERT_COVERAGE = "excluded_with_alert_coverage"
    EXCLUDED_WITH_METRIC_COVERAGE = "excluded_with_metric_coverage"
    EMPTY_LINKAGE_METRIC_RECOMMENDATION = "empty_linkage_metric_recommendation"
    EMPTY_LINKAGE_ALERT_RECOMMENDATION = "empty_linkage_alert_recommendation"


# The repair phase is fixed per violation kind (artifacts.md), never the phase owning the
# entries a violation names -- this is what seeds R18's affected-phase queue and what a
# system-originated amendment opens. This list and artifacts.md's semantic-check
# enumeration are the same list; every kind has a phase, every phase claim has a kind.
_REPAIR_PHASE: dict[ViolationKind, Phase] = {
    ViolationKind.UNMAPPED_FAILURE_MODE: Phase.ALERT_RECOMMENDATIONS,
    ViolationKind.LINKAGE_INCONSISTENCY: Phase.ALERT_RECOMMENDATIONS,
    ViolationKind.INVALID_EXPRESSION: Phase.ALERT_RECOMMENDATIONS,
    ViolationKind.ALERT_SEVERITY_BELOW_MAX: Phase.ALERT_RECOMMENDATIONS,
    ViolationKind.EXCLUDED_WITH_ALERT_COVERAGE: Phase.ALERT_RECOMMENDATIONS,
    ViolationKind.EXCLUDED_WITH_METRIC_COVERAGE: Phase.METRIC_COVERAGE,
    ViolationKind.EMPTY_LINKAGE_METRIC_RECOMMENDATION: Phase.METRIC_COVERAGE,
    ViolationKind.EMPTY_LINKAGE_ALERT_RECOMMENDATION: Phase.ALERT_RECOMMENDATIONS,
}


@dataclass(frozen=True)
class Violation:
    """One semantic-invariant violation found by `artifacts.semantic_violations`.

    `entry_ids` names the entries the violation concerns (for diagnosability, not for
    phase attribution). `phase` is derived mechanically from `kind` at construction --
    never set independently -- since the repair phase is a fixed property of the kind
    (artifacts.md), not of which phase owns the named entries.
    """

    kind: ViolationKind
    entry_ids: tuple[str, ...]
    phase: Phase = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", _REPAIR_PHASE[self.kind])


class EditOp(StrEnum):
    """One edit operation kind, per `artifacts.md`'s `Edit(op, entry_type, payload_or_id)`."""

    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


@dataclass(frozen=True)
class Edit:
    """One structured artifact edit (architecture: Edit-proposal protocol).

    `payload_or_id` is the new/changed entry's fields for `add`/`update`, or the target
    entry's ID (the `failure_mode_id` for a coverage entry) for `remove` -- a single field
    typed as a union rather than two optional fields, matching `artifacts.md`'s own naming.
    """

    op: EditOp
    entry_type: str
    payload_or_id: dict[str, object] | str


@dataclass(frozen=True)
class EditBatch:
    """One `propose_edits` call's payload: edits tagged for a single phase.

    Defined here rather than in `artifacts` (which owns validating and applying it)
    because the architecture draws no edge between **agent** and **artifacts** -- the
    edit sink is an orchestrator-injected callable agent calls without importing
    artifacts, so the batch shape has to be common currency both modules can reference
    without either importing the other (this module's own stated purpose).
    """

    phase: Phase
    edits: tuple[Edit, ...] = ()


@dataclass(frozen=True)
class BatchVerdict:
    """The edit sink's verdict on one `EditBatch` -- the tool result the model sees.

    Field names/shape are implementation detail (artifacts.md's own words) until the
    write-side module (T1.5) settles them; kept minimal and symmetric with
    `stack.ExpressionVerdict`'s ok/message convention used elsewhere in this codebase.
    """

    ok: bool
    message: str | None = None


class RunControlAction(StrEnum):
    """One `run_control` action kind (architecture: Run-control channel)."""

    AFFECTED_VERDICT = "affected_verdict"
    NO_IMPACT = "no_impact"
    AMEND_PROPOSAL = "amend_proposal"
    AMEND_COMPLETE = "amend_complete"


@dataclass(frozen=True)
class RunControlCall:
    """One `run_control` tool call's payload: an action plus its action-specific fields."""

    action: RunControlAction
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RunControlVerdict:
    """The run-control handler's verdict on one `RunControlCall` -- the tool result seen."""

    ok: bool
    message: str | None = None


@dataclass(frozen=True)
class RunContext:
    """Dynamic per-run context that enters the agent session once, at `start` (agent.md).

    `delta_files`/`patch_text` are update-mode-only (the effective delta's file list and
    patch text); both default empty for a full-analysis run, which never populates them.
    """

    worktree_root: Path
    delta_files: tuple[str, ...] = ()
    patch_text: str = ""


class BlareError(Exception):
    """The system's one error shape: a cause and the user's next action (R13).

    Every refusal and failure Blare raises derives from this (directly, as here, or
    through a module-specific subclass) so the cli can render "cause line, then
    -> next action" without inspecting exception subtypes. Deliberately a plain
    Exception subclass rather than a frozen dataclass: BaseException's own
    __new__/__init__ protocol does not mix cleanly with dataclass-generated
    __init__, so the fields are set explicitly instead.
    """

    def __init__(self, cause: str, next_action: str) -> None:
        super().__init__(cause)
        self.cause = cause
        self.next_action = next_action

    def __str__(self) -> str:
        return self.cause
