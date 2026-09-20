"""What a replay hands back, and why it is four things rather than one.

The brief calls conflating these the most common design mistake in the
problem, so the distinction is made in the type system rather than in a status
string somebody has to remember to check:

``Success``          the capability did what it promises, outputs and all
``BusinessOutcome``  the application gave a legitimate answer that is not the
                     happy one -- no such member, not authorised. The system
                     worked. Nothing is broken. A caller branches on the code
``Escalated``        something a person has to decide. The run is paused, not
                     over, and the session is still alive and waiting
``Failure``          the automation is broken: a locator that no longer
                     resolves, a checkpoint that never held, a budget spent

Treating the second as the fourth is how an automation ends up raising an
incident because a member does not exist. Treating the fourth as the second is
how a broken capability quietly returns "not found" for a week.

Every variant carries ``evidence_ref`` and ``tier_log``, because the questions
asked after the fact -- what did it do, was it drifting -- are asked of
successes just as often as of failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cua.types import FailureCategory


@dataclass(frozen=True)
class TierEntry:
    """Which rule found a control, on one step of one run.

    Collected on success too. A capability sliding from its primary rule to its
    third fallback is degrading weeks before it breaks, and nobody reads the
    logs of runs that worked.
    """

    step_id: str
    intent: str
    strategy: str | None
    tier: int | None
    degraded: bool = False
    match_count: int = 1


@dataclass(frozen=True)
class ReplayResult:
    capability: str = ""
    run_id: str | None = None
    evidence_ref: str | None = None
    steps_executed: int = 0
    duration_ms: int = 0
    tier_log: tuple[TierEntry, ...] = ()
    #: Recovery codes that fired, with counts. Never surfaced as failures --
    #: dismissing a maintenance notice is not news -- but a recovery firing
    #: twice as often this month as last is the first sign of drift.
    recoveries: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Did the capability return what it promises?

        False for a business outcome too. The run worked; the caller did not
        get the balance it asked for, and code that treats "not found" as a
        balance is the bug this property exists to prevent.
        """
        return False

    @property
    def kind(self) -> str:
        return type(self).__name__

    @property
    def degraded_steps(self) -> tuple[TierEntry, ...]:
        return tuple(entry for entry in self.tier_log if entry.degraded)


@dataclass(frozen=True)
class Success(ReplayResult):
    outputs: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class BusinessOutcome(ReplayResult):
    """A legitimate answer about the world. Not an error."""

    code: str = ""
    message: str | None = None
    #: Anything read before the outcome was detected. A caller may well want
    #: the member's name even when the balance was refused.
    partial_outputs: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None


@dataclass(frozen=True)
class Escalated(ReplayResult):
    """Paused for a person. The live session is still open."""

    intervention_id: str | None = None
    step_id: str | None = None
    intent: str = ""
    reason: str = ""
    #: What was on screen that nothing declared. Quoted verbatim, because the
    #: operator deciding what to do needs the application's words, not ours.
    observed: str | None = None
    session_id: str | None = None
    partial_outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Failure(ReplayResult):
    """The automation is broken."""

    category: FailureCategory = FailureCategory.SURFACE_ERROR
    step_id: str | None = None
    intent: str = ""
    #: Stated as a pair on purpose. "Expected the Member Details heading,
    #: observed the search screen" is a diagnosis; "step 4 failed" is a
    #: notification that somebody now has to go and reproduce.
    expected: str = ""
    observed: str = ""
    detail: str | None = None


#: Everything a caller can receive.
Result = Success | BusinessOutcome | Escalated | Failure
