"""The run state machine, and who is allowed to touch the browser.

The brief asks for "a way to know who is (or should be) in control". The
answer here is that control is not stored at all -- it is *derived* from the
run state. There is exactly one variable, so there is no state in which the
machine thinks automation is driving while the control token says a human is.
That class of bug is simply unavailable.

The transitions below encode the handoff the brief describes: automation can
pause, cede control, and resume on the same session. The one that carries the
most weight is ``HUMAN_CONTROL -> VERIFYING`` rather than straight back to
``RUNNING``. Handing control back is not the same as trusting the world: the
human may have navigated somewhere else entirely, so the run re-checks where
it is before continuing, and escalates again rather than guessing if the
answer is not what it expected.
"""

from __future__ import annotations

from cua.types import TERMINAL_STATES, ControlOwner, RunState

#: Who may act, for each state. Control is a function of state, never a
#: second variable that could disagree with it.
CONTROL_BY_STATE: dict[RunState, ControlOwner] = {
    RunState.PENDING: ControlOwner.NONE,
    RunState.RUNNING: ControlOwner.AUTOMATION,
    #: Paused with an intervention open. Deliberately nobody: the automation
    #: has stopped, and the operator has not picked it up yet.
    RunState.AWAITING_HUMAN: ControlOwner.NONE,
    RunState.HUMAN_CONTROL: ControlOwner.HUMAN,
    RunState.VERIFYING: ControlOwner.AUTOMATION,
    RunState.SUCCEEDED: ControlOwner.NONE,
    RunState.BUSINESS_OUTCOME: ControlOwner.NONE,
    RunState.FAILED: ControlOwner.NONE,
    RunState.ABORTED: ControlOwner.NONE,
}

LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.RUNNING, RunState.ABORTED}),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_HUMAN,
            RunState.SUCCEEDED,
            RunState.BUSINESS_OUTCOME,
            RunState.FAILED,
            RunState.ABORTED,
        }
    ),
    RunState.AWAITING_HUMAN: frozenset(
        {RunState.HUMAN_CONTROL, RunState.FAILED, RunState.ABORTED}
    ),
    #: No direct route back to RUNNING. Control returns through VERIFYING.
    RunState.HUMAN_CONTROL: frozenset({RunState.VERIFYING, RunState.ABORTED}),
    RunState.VERIFYING: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_HUMAN,
            RunState.SUCCEEDED,
            RunState.BUSINESS_OUTCOME,
            RunState.FAILED,
            RunState.ABORTED,
        }
    ),
    RunState.SUCCEEDED: frozenset(),
    RunState.BUSINESS_OUTCOME: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.ABORTED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """A state change the machine does not allow.

    Raised rather than logged. A run that reached an impossible state has lost
    track of who is driving a live browser session, and continuing from there
    is how automation acts while a human believes they have the controls.
    """

    def __init__(self, current: RunState, requested: RunState) -> None:
        self.current = current
        self.requested = requested
        allowed = sorted(LEGAL_TRANSITIONS[current]) or ["(terminal)"]
        super().__init__(
            f"cannot go from {current} to {requested}; "
            f"allowed from {current}: {', '.join(allowed)}"
        )


class ControlViolation(RuntimeError):
    """Something tried to act without holding the control token."""

    def __init__(self, expected: ControlOwner, actual: ControlOwner, state: RunState) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{expected} tried to act while control belongs to {actual} "
            f"(state {state})"
        )


def control_for(state: RunState) -> ControlOwner:
    return CONTROL_BY_STATE[state]


def can_transition(current: RunState, requested: RunState) -> bool:
    return requested in LEGAL_TRANSITIONS[current]


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES
