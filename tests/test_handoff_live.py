"""The handoff against the real application, with the compliance hold.

``test_intervention.py`` covers the state machine and the threading with a
fake surface. What only a browser can answer is here: does a real click on a
real button get recorded, does the record survive the navigation that click
causes, and does the run pick up and finish against the live page.

**On the operator's thread.** In production the person uses a mouse, and the
automation thread is parked on an Event while they do -- there is no thread
question, because clicking a window is not a Playwright call. In a test there
is no mouse, so Playwright stands in for one, and Playwright's sync API
belongs to the thread that created the page. So the scripted operator below
acts on the automation's own thread, inside the wait. Everything else is the
real path: the real ``Handoff``, the real state machine, the real engine doing
the re-verification afterwards.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.artifact.store import CapabilityStore
from cua.locators import Locator, RoleNameSpec
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.primitives import Action
from cua.replay import Escalated, ReplayEngine, Success
from cua.session import (
    EXPIRED,
    RESOLVED,
    UNVERIFIED,
    ControlViolation,
    Credentials,
    Handoff,
    InterventionRegistry,
    Session,
    SessionManager,
    authenticate,
    hand_back,
    take_control,
)
from cua.types import ActionType, ControlOwner, RiskLevel, RunState
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser

CREDENTIALS = Credentials(user=APP_USER, password=APP_PASS)

#: The member flagged for compliance review. Its hold screen is deliberately
#: identical to the routine maintenance notice apart from its name, so nothing
#: can handle it correctly by accident.
HELD = "10005"
ACKNOWLEDGE = Locator(primary=RoleNameSpec(role="button", name="Acknowledge"))


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
def manager():
    manager = SessionManager()
    try:
        yield manager
    finally:
        manager.close_all()


@pytest.fixture
def session(live_app, manager) -> Session:
    """A fresh run on a shared browser.

    The hold is remembered once acknowledged, so each test resets the
    application and signs in again -- otherwise the second test would find the
    hold already cleared and pass without ever exercising anything.
    """
    live = manager.all()
    session = live[0] if live else manager.create(live_app, headless=True)

    surface = session.surface
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/debug/reset"}))
    authenticate(surface, CREDENTIALS)

    # A session object is single-use by design: its states are a run's
    # lifecycle, not a browser's. Reusing the browser and taking a new run
    # object is what the manager would do for a second job on the same window.
    run = Session(
        id=f"run-{len(manager)}",
        surface=surface,
        browser=session.browser,
        base_url=session.base_url,
    )
    run.start(run_id="live-handoff")
    return run


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


class ScriptedOperator(Handoff):
    """A person at the desk, standing in for a mouse.

    Overrides only the waiting. The pause, the intervention record, the
    watcher, the control token and the re-verification afterwards are all the
    production path.
    """

    def __init__(self, *args, do=None, cancel=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.do = do
        self.cancel = cancel
        self.took_control = False

    def _wait(self) -> bool:
        take_control(self.session)
        self.took_control = True
        if self.do is not None:
            self.do(self.session.surface)
        if self.cancel:
            self.session.abort()
        else:
            hand_back(self.session, "handled at the desk")
        return True


def acknowledge(surface) -> None:
    """What the person does: read the hold, decide, press the button."""
    surface.act(Action(type=ActionType.CLICK, target=ACKNOWLEDGE))


def replay(session, artifact, policy, handoff, member_id=HELD):
    return ReplayEngine(
        session.controlled, artifact, policy=policy, escalate=handoff
    ).run({"member_id": member_id})


# --- the demo ------------------------------------------------------------


def test_a_person_clears_the_hold_and_the_run_finishes(session, artifact, policy):
    """The brief's section 3.6, end to end, against the real screen.

    The run meets a blocking state nothing declared, stops, a person handles
    it in the same live window, and the run returns the balance it was asked
    for. Nothing about the capability changed; the hold is not in the artifact
    and still is not afterwards.
    """
    registry = InterventionRegistry()
    handoff = ScriptedOperator(
        session,
        registry=registry,
        capability=artifact.ref,
        inputs={"member_id": HELD},
        do=acknowledge,
    )
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Success), getattr(result, "reason", result)
    assert result.outputs["member_name"] == "Aisha Bello"
    assert result.outputs["savings_balance"] == "22047.19"
    assert handoff.took_control

    item = registry.all()[0]
    assert item.state == RESOLVED
    assert item.verified is True
    assert item.request.observed and "Compliance Hold" in item.request.observed

    assert [c.to for c in session.history] == [
        RunState.RUNNING,
        RunState.AWAITING_HUMAN,
        RunState.HUMAN_CONTROL,
        RunState.VERIFYING,
        RunState.RUNNING,
    ]


def test_what_the_person_clicked_is_in_the_record(session, artifact, policy):
    """The click has to survive the navigation it causes.

    Pressing Acknowledge posts and redirects, which throws away anything the
    page was holding -- so the watcher keeps its log in the tab's storage
    rather than in a variable. Without that, the one action worth recording
    would be the one action guaranteed to be lost.
    """
    handoff = ScriptedOperator(session, capability=artifact.ref, do=acknowledge)
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Success), getattr(result, "reason", result)
    actions = handoff.history[0].human_actions
    assert actions, "the operator's click was not captured"

    clicked = [a for a in actions if a.kind == "click" and a.name == "Acknowledge"]
    assert clicked, [a.describe() for a in actions]
    assert clicked[0].role == "button"
    # Described in the same vocabulary as the automation's own steps, because
    # it is produced by the same code.
    assert clicked[0].describe() == "click button 'Acknowledge'"
    assert clicked[0].location.endswith(f"/members/{HELD}")


def test_the_watch_stops_when_the_session_goes_back_to_the_automation(
    session, artifact, policy
):
    """Nothing is recorded while the automation is the one driving.

    The listeners stay installed -- Playwright cannot un-inject a script -- so
    what has to be true is that they are inert. A log that kept filling after
    the handoff would make the automation's own clicks look like a person's,
    which is worse than no log at all.
    """
    handoff = ScriptedOperator(session, capability=artifact.ref, do=acknowledge)
    replay(session, artifact, policy, handoff)

    assert handoff.watcher.watching is False
    # The run carried on clicking after the handoff; none of that is human.
    assert all(a.name == "Acknowledge" for a in handoff.history[0].human_actions)
    assert session.surface.watch_drain() == []


def test_a_person_who_changes_their_mind_can_cancel(session, artifact, policy):
    handoff = ScriptedOperator(session, capability=artifact.ref, cancel=True)
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert session.state is RunState.ABORTED


def test_the_run_does_not_resume_on_the_persons_word(session, artifact, policy):
    """The operator hands the session back without clearing the hold.

    Nothing corrects them and nothing argues. The engine re-checks the world,
    finds the hold still there, and the run stops and asks again -- which is
    the only safe reading of "the human is the higher authority".
    """
    handoff = ScriptedOperator(session, capability=artifact.ref, do=lambda s: None)
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert "did not verify" in result.reason
    assert session.state is RunState.AWAITING_HUMAN
    assert handoff.history[0].state == UNVERIFIED


def test_nobody_comes_and_the_session_stays_open(session, artifact, policy):
    """An unattended run with no operator. Not a failure: the browser is still
    on the hold screen and the intervention is still listed for whoever
    arrives."""
    handoff = Handoff(session, timeout=0.05, capability=artifact.ref)
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert result.intervention_id == handoff.history[0].id
    assert handoff.history[0].state == EXPIRED
    assert session.state is RunState.AWAITING_HUMAN

    snapshot = session.surface.observe()
    assert snapshot.modal_text and "Compliance Hold" in snapshot.modal_text


# --- control -------------------------------------------------------------


def test_the_automation_is_refused_while_the_person_holds_the_window(
    session, artifact, policy
):
    """The control token, enforced against the live browser.

    While an operator holds the session the automation cannot act on it at
    all -- not "should not", cannot. The person's own action goes through the
    unguarded surface, which is the distinction the token exists to draw.
    """
    refused: list[str] = []

    def check_then_acknowledge(surface):
        assert session.control is ControlOwner.HUMAN
        try:
            session.controlled.act(Action(type=ActionType.CLICK, target=ACKNOWLEDGE))
        except ControlViolation as exc:
            refused.append(str(exc))
        acknowledge(surface)

    handoff = ScriptedOperator(
        session, capability=artifact.ref, do=check_then_acknowledge
    )
    result = replay(session, artifact, policy, handoff)

    assert refused, "automation was allowed to act during the handoff"
    assert isinstance(result, Success), getattr(result, "reason", result)
