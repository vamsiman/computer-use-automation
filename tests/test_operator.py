"""The console, driving a real paused run.

Flask's test client rather than a browser, because the console is a form and
three buttons and none of that needs rendering to be tested. What does need to
be real is the thing on the other side: an actual `ReplayEngine` blocked on an
actual `Handoff`, on a thread, so that "grant" and "hand back" move a live
state machine rather than a mock.

The interesting tests here are the ones about what the console is *not*
allowed to do. A console that could mark a step complete, or accept a value
the capability would have rejected, is a second and quieter way into a
production banking session.
"""

from __future__ import annotations

import threading
import time

import pytest

from cua.artifact.store import CapabilityStore
from cua.operator import ConsoleServer, create_app
from cua.replay import Escalated, Failure, ReplayEngine, Success
from cua.session import (
    Handoff,
    InterventionRegistry,
    Session,
    SessionManager,
)
from cua.types import FailureCategory, RunState
from tests.test_replay import FAST_MS, FakeSurface, engine_policy, node, screen

policy = engine_policy

WAIT = 5.0


@pytest.fixture
def artifact():
    loaded = CapabilityStore().load("member.read_savings_balance")
    for step in loaded.steps:
        if step.checkpoint is not None:
            step.checkpoint.timeout_ms = FAST_MS
    if loaded.success.checkpoint is not None:
        loaded.success.checkpoint.timeout_ms = FAST_MS
    return loaded


@pytest.fixture
def surface():
    return FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Whatever", node("dialog", "Compliance Hold"), modal="Compliance Hold"),
            screen("Member Details"),
        ],
        values={"member_name": "Aisha Bello", "savings_balance": "22,047.19"},
    )


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def session(surface, manager):
    session = Session(
        id="s-console", surface=surface, browser=None, base_url="http://localhost:5000"
    )
    manager._sessions[session.id] = session
    session.start(run_id="r-console")
    return session


@pytest.fixture
def registry():
    return InterventionRegistry()


@pytest.fixture
def console(manager, registry, handoffs):
    return create_app(manager, registry, handoffs).test_client()


@pytest.fixture
def handoffs():
    return {}


class Run:
    """A replay on its own thread, so the console can act while it waits."""

    def __init__(self, session, artifact, policy, handoff, inputs=None):
        self.result = None
        self.session = session
        # `or` would be wrong here, and the empty dict is the whole point of
        # two of these tests: no inputs at all is a case, not an absence.
        default = {"member_id": "10005"}
        self._args = (
            session, artifact, policy, handoff,
            default if inputs is None else inputs,
        )
        self.thread = threading.Thread(target=self._go, daemon=True)

    def _go(self):
        session, artifact, policy, handoff, inputs = self._args
        self.result = ReplayEngine(
            session.controlled, artifact, policy=policy, escalate=handoff
        ).run(inputs)

    def start(self):
        self.thread.start()
        return self

    def wait_until_asked(self, timeout=WAIT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session.state is RunState.AWAITING_HUMAN:
                return True
            time.sleep(0.01)
        return False

    def finish(self, timeout=WAIT):
        self.thread.join(timeout)
        return self.result


def paused(session, artifact, policy, registry, handoffs, **kwargs):
    handoff = Handoff(session, registry=registry, capability=artifact.ref, **kwargs)
    handoffs[session.id] = handoff
    run = Run(session, artifact, policy, handoff).start()
    assert run.wait_until_asked(), "the run never stopped for a person"
    return run, handoff


def only(console):
    return console.get("/api/state").get_json()["interventions"][0]


# --- what an operator is shown -------------------------------------------


def test_a_paused_run_appears_with_enough_to_decide_on(
    session, artifact, policy, registry, handoffs, console
):
    run, _ = paused(session, artifact, policy, registry, handoffs)
    item = only(console)

    assert item["reason"]
    assert item["observed"] == "Compliance Hold"
    assert item["step_id"] == "s3"
    assert item["intent"]
    assert item["session_state"] == "awaiting_human"
    # Nobody has it yet -- the automation has stopped and no operator has
    # picked it up. That is a real state, not a gap.
    assert item["control"] == "none"
    assert item["can_grant"] is True
    assert item["can_release"] is False

    session.abort()
    run.finish()


def test_the_page_shows_the_applications_own_words(
    session, artifact, policy, registry, handoffs, console
):
    """Quoted verbatim. An operator deciding whether to clear a compliance
    hold needs the bank's wording, not our summary of it."""
    run, _ = paused(session, artifact, policy, registry, handoffs)
    page = console.get("/").get_data(as_text=True)

    assert "Compliance Hold" in page
    assert "Take control" in page

    session.abort()
    run.finish()


def test_an_idle_console_says_so(console):
    assert "Nothing is waiting" in console.get("/").get_data(as_text=True)


# --- the three buttons ---------------------------------------------------


def test_a_run_is_granted_completed_and_resumed_from_the_console(
    session, surface, artifact, policy, registry, handoffs, console
):
    """The issue's acceptance, end to end.

    Take control, deal with the thing (which happens in the browser window,
    represented here by the screen moving on), hand back, and the run
    finishes.
    """
    run, _ = paused(session, artifact, policy, registry, handoffs)
    item = only(console)

    assert console.post(f"/interventions/{item['id']}/grant").status_code == 302
    assert session.state is RunState.HUMAN_CONTROL
    assert only(console)["can_release"] is True

    surface.index = 3  # what the operator does, in the other window

    assert console.post(f"/interventions/{item['id']}/resume").status_code == 302
    result = run.finish()

    assert isinstance(result, Success), getattr(result, "reason", result)
    assert result.outputs["savings_balance"] == "22047.19"
    assert session.state is RunState.RUNNING


def test_handing_back_does_not_declare_the_step_done(
    session, artifact, policy, registry, handoffs, console
):
    """The console returns the controls. It does not get a vote on whether the
    world is in the state the run wanted.

    A button that could mark a step complete would be a button that can be
    wrong about a live banking session, so the operator presses hand back, the
    automation re-checks the screen, finds the hold still there, and stops
    again.
    """
    run, handoff = paused(session, artifact, policy, registry, handoffs)
    item = only(console)

    console.post(f"/interventions/{item['id']}/grant")
    console.post(f"/interventions/{item['id']}/resume")  # nothing was fixed
    result = run.finish()

    assert isinstance(result, Escalated)
    assert "did not verify" in result.reason
    assert session.state is RunState.AWAITING_HUMAN
    assert handoff.history[0].state == "unverified"


def test_an_operator_can_cancel_a_run(
    session, artifact, policy, registry, handoffs, console
):
    run, _ = paused(session, artifact, policy, registry, handoffs)
    item = only(console)

    console.post(f"/interventions/{item['id']}/cancel")
    result = run.finish()

    assert isinstance(result, Escalated)
    assert session.state is RunState.ABORTED
    assert only(console)["state"] == "aborted"


def test_the_buttons_offered_match_what_the_session_will_accept(
    session, artifact, policy, registry, handoffs, console
):
    """Every button is a state transition, and the machine refuses illegal
    ones by raising. Offering one that cannot be taken would turn a refusal
    the design is proud of into a stack trace in a person's face."""
    run, _ = paused(session, artifact, policy, registry, handoffs)
    item = only(console)

    assert (item["can_grant"], item["can_release"]) == (True, False)
    console.post(f"/interventions/{item['id']}/grant")
    after = only(console)
    assert (after["can_grant"], after["can_release"]) == (False, True)

    session.abort()
    run.finish()


# --- asking a person for a value -----------------------------------------


def test_a_missing_input_is_asked_for_rather_than_invented(
    session, surface, artifact, policy, registry, handoffs, console
):
    """The capability cannot run without a member number, and there are
    exactly two honest options: stop, or ask. It asks.

    What it must never do is supply one. This is an application that writes to
    member records, and a defaulted member number is a correct-looking
    operation performed on the wrong person.
    """
    handoff = Handoff(session, registry=registry, capability=artifact.ref)
    handoffs[session.id] = handoff
    run = Run(session, artifact, policy, handoff, inputs={}).start()
    assert run.wait_until_asked()

    item = only(console)
    assert item["needs"] == ["member_id"]
    field = item["fields"][0]
    assert (field["name"], field["type"]) == ("member_id", "string")
    assert field["pattern"] == "^[0-9]{5}$"
    assert surface.acted == [], "nothing should have happened to the application yet"

    console.post(f"/interventions/{item['id']}/grant")
    console.post(
        f"/interventions/{item['id']}/answer", data={"member_id": "10005"}
    )
    # The run starts from the beginning now that it has what it needs, so it
    # meets the compliance hold on the way -- and that is a second, ordinary
    # handoff.
    assert run.wait_until_asked()
    item = only(console)
    console.post(f"/interventions/{item['id']}/grant")
    surface.index = 3
    console.post(f"/interventions/{item['id']}/resume")
    result = run.finish()

    assert isinstance(result, Success), getattr(result, "reason", result)
    assert session.state is RunState.RUNNING


def test_a_typed_field_is_checked_by_the_capabilitys_own_contract(
    session, artifact, policy, registry, handoffs, console
):
    """The console is not a laxer front door.

    'abc' is refused here by exactly the code that refuses it to an API
    caller -- the input's own ``InputSpec`` -- so there is no way to get a
    value past the contract by typing it into a form instead.
    """
    handoff = Handoff(session, registry=registry, capability=artifact.ref)
    handoffs[session.id] = handoff
    run = Run(session, artifact, policy, handoff, inputs={}).start()
    assert run.wait_until_asked()
    item = only(console)

    console.post(f"/interventions/{item['id']}/grant")
    page = console.post(
        f"/interventions/{item['id']}/answer", data={"member_id": "abc"}
    ).get_data(as_text=True)

    assert "does not match" in page
    assert session.state is RunState.HUMAN_CONTROL, "the run must still be waiting"
    assert handoff.current.supplied == {}

    session.abort()
    run.finish()


def test_an_unanswered_question_ends_as_a_contract_failure(
    session, artifact, policy, registry, handoffs, console
):
    """Nobody answered. The capability still does not run, and the reason
    given is the honest one -- the caller did not supply what it promised."""
    handoff = Handoff(
        session, registry=registry, capability=artifact.ref, timeout=0.05
    )
    result = ReplayEngine(
        session.controlled, artifact, policy=policy, escalate=handoff
    ).run({})

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.CONTRACT
    assert "was not supplied" in result.observed


def test_an_ill_typed_input_is_not_turned_into_a_question(
    session, artifact, policy, registry, handoffs
):
    """Asking a person to retype a value the caller *did* supply hides a bug
    in whatever called us. Only a missing value is a question."""
    handoff = Handoff(session, registry=registry, capability=artifact.ref)
    result = ReplayEngine(
        session.controlled, artifact, policy=policy, escalate=handoff
    ).run({"member_id": "abc"})

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.CONTRACT
    assert len(registry) == 0, "nobody should have been asked anything"


def test_a_value_nobody_asked_for_is_refused(
    session, artifact, policy, registry, handoffs, console
):
    handoff = Handoff(session, registry=registry, capability=artifact.ref)
    handoffs[session.id] = handoff
    run = Run(session, artifact, policy, handoff, inputs={}).start()
    assert run.wait_until_asked()

    assert "not something this run asked for" in " ".join(
        handoff.provide({"member_id": "10005", "sort_code": "00-00-00"})
    )

    session.abort()
    run.finish()


# --- the server ----------------------------------------------------------


def test_the_console_runs_beside_the_automation(manager, registry):
    """On a thread, in the same process, because there is one session manager
    and it lives in memory."""
    import urllib.request

    with ConsoleServer(manager, registry, port=0) as server:
        with urllib.request.urlopen(f"{server.url}/api/state", timeout=5) as response:
            assert response.status == 200
