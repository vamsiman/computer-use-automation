"""The run state machine and the control token.

No browser: a Session is constructed with stand-in objects so the transition
rules can be exercised directly. These rules are what stop automation acting
on a browser a person is currently using, so they are worth pinning down
precisely rather than inferring from an integration test.
"""

from __future__ import annotations

import threading

import pytest

from cua.session import (
    CONTROL_BY_STATE,
    LEGAL_TRANSITIONS,
    ControlViolation,
    IllegalTransition,
    Session,
    SessionManager,
    can_transition,
)
from cua.types import TERMINAL_STATES, ControlOwner, RunState


class FakeSurface:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def make_session(state: RunState = RunState.PENDING) -> Session:
    session = Session(
        id="test",
        surface=FakeSurface(),
        browser=FakeBrowser(),
        base_url="http://localhost:5000",
    )
    session.state = state
    return session


# --- control is derived, not stored --------------------------------------


def test_every_state_declares_who_may_act():
    assert set(CONTROL_BY_STATE) == set(RunState)


def test_control_follows_state_so_the_two_cannot_disagree():
    session = make_session()
    assert session.control is ControlOwner.NONE

    session.start()
    assert session.control is ControlOwner.AUTOMATION

    session.escalate("stuck")
    assert session.control is ControlOwner.NONE, "paused means nobody drives"

    session.grant()
    assert session.control is ControlOwner.HUMAN

    session.release()
    assert session.control is ControlOwner.AUTOMATION


def test_automation_cannot_act_while_a_human_holds_the_session():
    """The whole point of the token."""
    session = make_session()
    session.start()
    session.assert_control(ControlOwner.AUTOMATION)

    session.escalate("undeclared dialog")
    session.grant()

    with pytest.raises(ControlViolation) as raised:
        session.assert_control(ControlOwner.AUTOMATION)
    assert raised.value.actual is ControlOwner.HUMAN


def test_automation_cannot_act_while_merely_paused():
    """An open intervention nobody has picked up is not a licence to carry
    on: the run stopped for a reason."""
    session = make_session()
    session.start()
    session.escalate("waiting for an operator")
    with pytest.raises(ControlViolation):
        session.assert_control(ControlOwner.AUTOMATION)


def test_a_finished_run_belongs_to_nobody():
    session = make_session()
    session.start()
    session.finish(RunState.SUCCEEDED)
    assert session.control is ControlOwner.NONE
    with pytest.raises(ControlViolation):
        session.assert_control(ControlOwner.AUTOMATION)


# --- transitions ----------------------------------------------------------


def test_control_returns_through_verification_never_straight_to_running():
    """Handing control back is not the same as trusting the world.

    The human may have navigated anywhere. Going HUMAN_CONTROL -> RUNNING
    would resume on an assumption about which screen is in front of us.
    """
    assert RunState.RUNNING not in LEGAL_TRANSITIONS[RunState.HUMAN_CONTROL]
    assert RunState.VERIFYING in LEGAL_TRANSITIONS[RunState.HUMAN_CONTROL]


def test_verification_can_escalate_again_rather_than_guessing():
    session = make_session()
    session.start()
    session.escalate("stuck")
    session.grant()
    session.release()

    session.escalate("not where we expected to be after the handoff")
    assert session.state is RunState.AWAITING_HUMAN


def test_terminal_states_are_final():
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()


def test_a_finished_run_cannot_be_restarted():
    session = make_session()
    session.start()
    session.finish(RunState.FAILED)
    with pytest.raises(IllegalTransition):
        session.resume()


def test_illegal_transitions_raise_and_say_what_was_allowed():
    session = make_session()
    with pytest.raises(IllegalTransition) as raised:
        session.grant()
    assert "pending" in str(raised.value)
    assert "running" in str(raised.value)


def test_a_run_can_be_cancelled_from_any_live_state():
    for state in (
        RunState.PENDING,
        RunState.RUNNING,
        RunState.AWAITING_HUMAN,
        RunState.HUMAN_CONTROL,
        RunState.VERIFYING,
    ):
        assert can_transition(state, RunState.ABORTED), state


def test_finish_refuses_a_state_that_is_not_terminal():
    session = make_session()
    session.start()
    with pytest.raises(ValueError):
        session.finish(RunState.VERIFYING)


# --- history --------------------------------------------------------------


def test_history_records_who_was_in_control_at_each_step():
    """An audit question in a regulated setting, and not reconstructable
    after the fact from interleaved logs."""
    session = make_session()
    session.start("run-1")
    session.escalate("compliance hold")
    session.grant()
    session.release("operator acknowledged the hold")
    session.resume()
    session.finish(RunState.SUCCEEDED)

    assert [(h.frm, h.to) for h in session.history] == [
        (RunState.PENDING, RunState.RUNNING),
        (RunState.RUNNING, RunState.AWAITING_HUMAN),
        (RunState.AWAITING_HUMAN, RunState.HUMAN_CONTROL),
        (RunState.HUMAN_CONTROL, RunState.VERIFYING),
        (RunState.VERIFYING, RunState.RUNNING),
        (RunState.RUNNING, RunState.SUCCEEDED),
    ]
    handoff = session.history[2]
    assert handoff.control is ControlOwner.HUMAN
    assert "acknowledged" in session.history[3].reason


# --- blocking and resuming ------------------------------------------------


def test_automation_blocks_until_control_is_handed_back():
    """A paused run costs nothing while it waits, and wakes on release
    rather than by polling."""
    session = make_session()
    session.start()
    session.escalate("needs a person")
    session.grant()

    released = threading.Event()

    def automation():
        session.wait_for_release(timeout=5)
        released.set()

    worker = threading.Thread(target=automation, daemon=True)
    worker.start()

    assert not released.wait(0.2), "should still be blocked"
    session.release()
    assert released.wait(2), "should have woken on release"


def test_aborting_also_unblocks_a_waiting_run():
    session = make_session()
    session.start()
    session.escalate("needs a person")
    session.abort("operator cancelled")
    assert session.wait_for_release(timeout=1)
    assert session.state is RunState.ABORTED


# --- the manager ----------------------------------------------------------


def test_sessions_are_reachable_by_id_after_the_call_that_made_them():
    """The change that makes a handoff possible at all."""
    manager = SessionManager()
    session = make_session()
    manager._sessions[session.id] = session

    assert manager.get(session.id) is session
    assert len(manager) == 1


def test_unknown_session_id_says_so():
    with pytest.raises(KeyError):
        SessionManager().get("nope")


def test_awaiting_human_is_what_the_operator_console_lists():
    manager = SessionManager()
    running, paused = make_session(), make_session()
    paused.id = "paused"
    manager._sessions = {"running": running, "paused": paused}

    running.start()
    paused.start()
    paused.escalate("stuck")

    assert [s.id for s in manager.awaiting_human()] == ["paused"]


def test_closing_a_session_releases_the_browser():
    """Closing the browser is enough; the page is not closed separately.

    Doing both made teardown hang for minutes, because closing a page runs
    its unload handlers and waits on connections the browser is about to drop
    anyway.
    """
    manager = SessionManager()
    session = make_session()
    manager._sessions[session.id] = session

    manager.close(session.id)
    assert session.browser.stopped
    assert len(manager) == 0
