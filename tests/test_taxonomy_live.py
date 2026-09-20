"""The error taxonomy, demonstrated rather than asserted.

Every row of the brief's runtime-conditions list, summoned on cue against the
live application and run through the real replay engine. This file is the
answer to the question the whole of section 3.3 asks: when the application does
something other than the happy path, does the system tell the difference
between an answer, a hiccup, a question for a person, and a break?

| trigger        | expected                                    |
|----------------|---------------------------------------------|
| 10001 / 10002  | Success                                     |
| 99999          | BusinessOutcome(MEMBER_NOT_FOUND)           |
| abc            | Failure(CONTRACT) -- see the note below     |
| 10003          | BusinessOutcome(PERMISSION_DENIED)          |
| 10004          | Success, after the interstitial is dismissed|
| /debug/slow    | Success, after a bounded backoff            |
| /debug/expire  | Success, after re-authenticating            |
| 10005          | Escalated -- undeclared blocking state      |
| 10006          | Failure(LOCATOR_UNRESOLVED)                 |

This is also why the target application is ours. You cannot ask somebody
else's demo server to expire your session on cue, and an error taxonomy you
cannot summon is a claim rather than a result.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.artifact.store import CapabilityStore
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.primitives import Action
from cua.replay import BusinessOutcome, Escalated, Failure, ReplayEngine, Success
from cua.session import Credentials, authenticate
from cua.surface.web import BrowserSession
from cua.types import ActionType, FailureCategory, RiskLevel
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser

CREDENTIALS = Credentials(user=APP_USER, password=APP_PASS)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_app():
    from werkzeug.serving import make_server

    seed.seed()
    exceptional.reset_all()

    port = _free_port()
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser(live_app):
    session = BrowserSession(live_app, headless=True)
    surface = session.start()
    try:
        yield surface
    finally:
        surface.close()
        session.stop()


@pytest.fixture
def surface(browser):
    """A clean console session per test.

    Several of these conditions are remembered for the rest of a session --
    a dismissed notice stays dismissed, an acknowledged hold stays
    acknowledged -- which is correct behaviour and would otherwise make the
    tests depend on the order they run in.
    """
    go(browser, "/debug/reset")
    authenticate(browser, CREDENTIALS)
    return browser


@pytest.fixture(scope="module")
def policy(live_app) -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(live_app,),
                paths=("/", "/home", "/members/**"),
                actions=frozenset(
                    {
                        ActionType.NAVIGATE,
                        ActionType.CLICK,
                        ActionType.TYPE,
                        ActionType.EXTRACT,
                        ActionType.WAIT_FOR,
                    }
                ),
            ),
            risk=RiskPolicy(unattended_max=RiskLevel.CAUTION),
        )
    )


@pytest.fixture
def artifact():
    return CapabilityStore().load("member.read_savings_balance")


def go(surface, path: str) -> None:
    """Drive the browser directly, outside the engine.

    The debug endpoints are deliberately off the allowlist, so the automation
    cannot reach them -- a run able to expire its own session would make this
    whole file untestable. Arming a condition is something an operator does to
    the application, not something the capability does, so the test does it by
    hand.
    """
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": path}))


def replay(surface, artifact, policy, member_id, **kwargs):
    return ReplayEngine(surface, artifact, policy=policy, **kwargs).run(
        {"member_id": member_id}
    )


# --- answers about the world ---------------------------------------------


def test_no_such_member(surface, artifact, policy):
    result = replay(surface, artifact, policy, "99999")

    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.ok is False


def test_a_restricted_member_is_a_permission_outcome(surface, artifact, policy):
    """Not a failure. The application answered, correctly and deliberately;
    the answer is that this operator may not see this record."""
    result = replay(surface, artifact, policy, "10003")

    assert isinstance(result, BusinessOutcome)
    assert result.code == "PERMISSION_DENIED"


def test_a_malformed_member_number_never_reaches_the_application(
    surface, artifact, policy
):
    """The capability declares ``^[0-9]{5}$``, so 'abc' is refused by the
    contract before the browser is touched.

    Worth being explicit, because the obvious expectation is
    VALIDATION_ERROR. Two layers validate here and the cheap one runs first:
    checking a caller's argument costs nothing, while finding out from the
    application costs a sign-in, a navigation and a round trip. The
    application's own validation still matters -- it is the only authority on
    what it will accept -- and the next test shows it still surfaces as an
    outcome when the contract is looser than the application.
    """
    result = replay(surface, artifact, policy, "abc")

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.CONTRACT
    assert result.steps_executed == 0


def test_the_applications_own_validation_is_a_business_outcome(
    surface, artifact, policy
):
    """With the pattern relaxed, the refusal comes from the application -- and
    it is an answer, not a crash. This is the row the brief asks for; the
    shipped artifact simply gets there sooner."""
    artifact.inputs["member_id"].pattern = None
    result = replay(surface, artifact, policy, "abc")

    assert isinstance(result, BusinessOutcome)
    assert result.code == "VALIDATION_ERROR"
    assert result.steps_executed > 0


# --- hiccups the caller never hears about --------------------------------


def test_a_known_interstitial_is_dismissed_and_the_run_completes(
    surface, artifact, policy
):
    """Declared, routine, and none of the caller's business. It shows up in
    the evidence and in the recovery counts, and nowhere else."""
    result = replay(surface, artifact, policy, "10004")

    assert isinstance(result, Success), getattr(result, "observed", result)
    assert result.outputs["member_name"] == "Tomas Lindqvist"
    assert "SYSTEM_NOTICE" in " ".join(result.recoveries)


def test_a_transient_stall_is_waited_out(surface, artifact, policy):
    """Success after waiting -- and the waiting is the checkpoint's own.

    Worth spelling out, because the obvious design is a declared SLOW_LOAD
    recovery and it is the wrong one here. A stall has nothing of its own to
    detect: the symptom is the absence of what we were waiting for. And the
    driver's observation blocks until the navigation completes, so there is no
    moment at which a detector could see a half-loaded screen even if one were
    declared.

    A checkpoint that polls until its deadline already is a bounded retry. The
    right answer is a timeout wide enough for the slowest render the flow
    tolerates, which is what this asserts.
    """
    import time

    # Stall the detail page specifically, so the flow is already half done when
    # it happens -- the interesting shape, and the one a first-page stall would
    # not exercise.
    go(surface, "/debug/slow?ms=5000&path=/members/10001")

    began = time.monotonic()
    result = replay(surface, artifact, policy, "10001")
    elapsed = time.monotonic() - began

    assert isinstance(result, Success), getattr(result, "observed", result)
    assert result.outputs["savings_balance"] == "4210.33"
    assert elapsed >= 5, "the run should have waited the stall out, not skipped it"
    assert result.recoveries == (), "no declared recovery was needed"


def test_a_stalled_click_that_actually_worked_is_not_a_failure(
    surface, artifact, policy
):
    """The driver reports a timeout for a click that went through.

    Playwright gives up waiting for the navigation after its own settle
    budget, so the act comes back ``ok=False`` even though the submit was
    accepted and the page is on its way. The checkpoint is what settles it:
    the world is the authority on whether a step worked, not the driver's
    return value. Trusting the driver here would turn a successful run into a
    failure report on every slow render.
    """
    go(surface, "/debug/slow?ms=4000&path=/members/10002")
    result = replay(surface, artifact, policy, "10002")

    assert isinstance(result, Success), getattr(result, "observed", result)
    assert result.outputs["member_name"] == "Marcus Ellery"


def test_an_expired_session_is_re_authenticated_and_the_run_resumes(
    surface, artifact, policy
):
    """Mid-flow expiry, which presents as the sign-in form appearing *inside
    the frame*. Signing in is not a step in the artifact -- credentials are
    deliberately not artifact inputs -- so the recovery calls the bootstrap
    and resumes from the step the artifact names.
    """
    go(surface, "/debug/expire")

    result = replay(
        surface,
        artifact,
        policy,
        "10001",
        reauthenticate=lambda: authenticate(surface, CREDENTIALS),
    )

    assert isinstance(result, Success), getattr(result, "observed", result)
    assert "SESSION_EXPIRED" in " ".join(result.recoveries)
    assert result.outputs["savings_balance"] == "4210.33"


def test_an_expired_session_with_no_way_back_is_not_guessed_at(
    surface, artifact, policy
):
    """No reauthentication configured means no way to continue. Stopping and
    saying so beats improvising a sign-in."""
    go(surface, "/debug/expire")
    result = replay(surface, artifact, policy, "10001")

    assert isinstance(result, Escalated)


# --- questions for a person ----------------------------------------------


def test_an_undeclared_blocking_state_escalates(surface, artifact, policy):
    """The compliance hold renders through the same template as the routine
    notice and differs only by name, so a detector cannot be right by luck.
    The system has no declaration covering it and does not invent one.

    An automation that reasons its way through an unknown dialog is an
    automation that will one day reason its way through this one, and clicking
    'Acknowledge' on a compliance hold is not a mistake you get to make twice.
    """
    result = replay(surface, artifact, policy, "10005")

    assert isinstance(result, Escalated)
    assert "Compliance Hold" in result.observed or "compliance" in result.reason
    assert result.step_id == "s3"


def test_the_dismiss_recovery_does_not_swallow_the_hold(surface, artifact, policy):
    """The recovery that handles the notice must not fire here. If it did, the
    run would succeed and nobody would ever know a compliance hold had been
    clicked through."""
    result = replay(surface, artifact, policy, "10005")

    assert "SYSTEM_NOTICE" not in " ".join(result.recoveries)
    assert not isinstance(result, Success)


# --- things that are actually broken -------------------------------------


def test_a_member_with_no_savings_row_is_a_failure(surface, artifact, policy):
    """Nothing declared covers it and nothing is blocking the screen. The
    automation has met a world it was not told about."""
    result = replay(surface, artifact, policy, "10006")

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.LOCATOR_UNRESOLVED
    assert result.step_id == "s5"


def test_a_recovery_that_keeps_firing_becomes_a_failure(surface, artifact, policy):
    """Budgets exist so "handle it and carry on" cannot become an infinite
    loop. Past the budget the condition stops being a hiccup and becomes the
    problem."""
    notice = next(r for r in artifact.recoveries if r.code == "SYSTEM_NOTICE")
    notice.dismiss_via = None  # dismissing no longer works

    result = replay(surface, artifact, policy, "10004")

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.RECOVERY_EXHAUSTED


# --- the distinction itself ----------------------------------------------


def test_every_condition_lands_in_a_different_bucket(surface, artifact, policy):
    """The point of the taxonomy in one assertion. Four triggers, four kinds
    of result -- conflating any two of them is the mistake the brief calls the
    most common one in this problem.
    """
    got = {
        "10002": type(replay(surface, artifact, policy, "10002")).__name__,
        "99999": type(replay(surface, artifact, policy, "99999")).__name__,
        "10005": type(replay(surface, artifact, policy, "10005")).__name__,
        "10006": type(replay(surface, artifact, policy, "10006")).__name__,
    }
    assert got == {
        "10002": "Success",
        "99999": "BusinessOutcome",
        "10005": "Escalated",
        "10006": "Failure",
    }
