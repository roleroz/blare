"""Value types shared across module boundaries.

None of Blare's six architecture modules (cli, orchestrator, gitrepo, artifacts, agent,
stack) owns these types outright, and the dependency graph is one-directional
(cli -> orchestrator -> {gitrepo, artifacts, agent}, agent -> stack, artifacts -> stack).
Putting cross-cutting value types that more than one module needs to reference here
avoids importing "downstream" modules into "upstream" ones just to name a type.

This module currently holds what the T1.1 walking skeleton and T1.4's artifacts module
need. `Phase` and `Violation` live here rather than in `artifacts.py` because **agent**
also names them in its own interface (`request_repair(phases, violations)`,
`engineering/modules/agent.md`) despite having no dependency edge on **artifacts** in the
architecture's module graph (agent -> stack only) -- putting them in `artifacts.py` would
force a disallowed agent -> artifacts import. Later tasks add the remaining shared types
their design docs describe as those modules are built out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RunMode(Enum):
    """Which of Blare's two commands is running: full analysis or diff mode."""

    ANALYZE = "analyze"
    UPDATE = "update"


class Phase(Enum):
    """The four phases of a Blare run, in order (spec, Scope; architecture, Overview).

    Values are the phase numbers themselves (1-4) so they sort and print naturally in
    run summaries and checkpoint views.
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
