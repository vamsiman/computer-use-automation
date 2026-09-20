"""Reading the screen for the things the capability declared it might meet.

Two families, and the difference between them is the whole error taxonomy:

**Outcomes** are answers. "No member exists with that number" is the
application working correctly, and the caller wants the code, not a stack
trace. They stop the run and return.

**Recoveries** are noise. A maintenance interstitial, a session that timed out,
a page that took too long. The caller never hears about them; the engine deals
with them and carries on. Each carries a budget, because a recovery without one
is an infinite loop waiting for a bad day.

Order matters and it is not arbitrary. Outcomes are checked first, so that a
screen which is both -- an interstitial that happens to contain the words "not
found" -- is read as the answer rather than dismissed as furniture.

What is *not* here is anything that guesses. A condition the artifact did not
declare is not classified by this module; it goes up to the engine, which
escalates. Inventing a classification for an unknown screen is how an
automation clicks "Acknowledge" on a compliance hold.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from cua.artifact.models import Artifact, Outcome, Recovery
from cua.primitives import Snapshot
from cua.surface.resolve import resolve_in_tree


@dataclass
class Detected:
    """Something declared, found on screen."""

    outcome: Outcome | None = None
    recovery: Recovery | None = None

    def __bool__(self) -> bool:
        return self.outcome is not None or self.recovery is not None


@dataclass
class ConditionEvaluator:
    """Matches declared conditions against a snapshot, and counts them.

    Stateful on purpose: the budget is per run, not per step. A session that
    expires once is routine; the same session expiring four times in one run is
    a different problem wearing the same clothes, and only something that
    remembers can tell them apart.
    """

    artifact: Artifact
    occurrences: Counter[str] = field(default_factory=Counter)

    def evaluate(self, snapshot: Snapshot) -> Detected:
        for outcome in self.artifact.outcomes:
            if self._matches(snapshot, outcome.detect):
                return Detected(outcome=outcome)
        for recovery in self.artifact.recoveries:
            if self._matches(snapshot, recovery.detect):
                return Detected(recovery=recovery)
        return Detected()

    @staticmethod
    def _matches(snapshot: Snapshot, detector) -> bool:
        return resolve_in_tree(snapshot.tree, detector).resolved

    # --- budgets ---------------------------------------------------------

    def record(self, code: str) -> int:
        self.occurrences[code] += 1
        return self.occurrences[code]

    def within_budget(self, recovery: Recovery) -> bool:
        return self.occurrences[recovery.code] <= recovery.max_occurrences

    def fired(self) -> tuple[str, ...]:
        return tuple(
            f"{code}x{count}" for code, count in sorted(self.occurrences.items())
        )


def blocking(snapshot: Snapshot) -> str | None:
    """Is something standing in the way that we have no declaration for?

    A modal is the honest signal here: the application has stopped and is
    waiting for an answer. Everything else on the page is unreachable until
    somebody gives it one, so a run that carries on regardless is not
    continuing the flow, it is typing into a screen nobody is reading.

    This is what turns an unknown dialog into an escalation instead of a
    timeout three steps later.
    """
    return snapshot.modal_text or None
