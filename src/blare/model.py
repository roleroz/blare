"""Value types shared across module boundaries.

None of Blare's six architecture modules (cli, orchestrator, gitrepo, artifacts, agent,
stack) owns these types outright, and the dependency graph is one-directional
(cli -> orchestrator -> {gitrepo, artifacts, agent}, agent -> stack, artifacts -> stack).
Putting cross-cutting value types that more than one module needs to reference here
avoids importing "downstream" modules into "upstream" ones just to name a type.

This module currently holds only what the T1.1 walking skeleton needs. Later tasks
(T1.2-T4.2) add the remaining types their design docs describe (Phase, Violation, and
so on) as those modules are built out.
"""

from __future__ import annotations

from enum import Enum


class RunMode(Enum):
    """Which of Blare's two commands is running: full analysis or diff mode."""

    ANALYZE = "analyze"
    UPDATE = "update"


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
