"""The handoff, with a real thread and a scripted operator.

A fake surface, but a real ``Session``, a real state machine and a real second
thread, because the thing under test *is* the concurrency: the automation
parks, somebody else takes the session, and the automation wakes up. Testing
that with everything on one thread would prove only that the functions can be
called in order.

The live version -- a browser, the compliance hold on member 10005, a person
clicking Acknowledge in the window -- is ``test_handoff_live.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

from cua.artifact.store import CapabilityStore
from cua.evidence import EvidenceConfig, Recorder
from cua.replay import Escalated, ReplayEngine, Success
from cua.session import (
    ABORTED,
    EXPIRED,
    GRANTED,
    RESOLVED,
    UNVERIFIED,
    ControlViolation,
    GuardedSurface,
    Handoff,
    HumanAction,
    HumanWatcher,
    InterventionRegistry,
    Session,
    hand_back,
    take_control,
)
from cua.types import ActionType, ControlOwner, RunState, Sensitivity
from tests.test_replay import FAST_MS, FakeSurface, engine_policy, node, screen

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: Short, because every one of these tests that waits is waiting for a thread
#: that is already running. A generous timeout here only slows down the cases
#: that are meant to fail.
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


BLOCKED = screen("Whatever", node("dialog", "Compliance Hold"), modal="Compliance Hold")


def blocked_run_screens():
    """Search, search, a dialog nothing declared, and the detail page behind it."""
    return [
        screen("Member Search"),
        screen("Member Search"),
        BLOCKED,
        screen("Member Details"),
    ]


@pytest.fixture
def surface():
    return FakeSurface(
        blocked_run_screens(),
        values={"member_name": "Aisha Bello", "savings_balance": "22,047.19"},
    )


@pytest.fixture
def session(surface):
    session = Session(
        id="s-test", surface=surface, browser=None, base_url="http://localhost:5000"
    )
    session.start(run_id="r-test")
    return session


class Operator:
    """A person, on their own thread.

    Waits for the run to stop, takes the session, does whatever the test says,
    and hands it back. Never signals the automation directly -- everything
    goes through the same state machine the console uses, which is the point.
    """

    def __init__(self, session, do=None, *, cancel=False, take=True):
        self.session = session
        self.do = do
        self.cancel = cancel
        self.take = take
        self.acted = False
        self.thread = threading.Thread(target=self._work, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _wait_until_asked(self, timeout=WAIT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session.state is RunState.AWAITING_HUMAN:
                return True
            time.sleep(0.01)
        return False

    def _work(self):
        if not self._wait_until_asked():
            return
        if not self.take:
            return
        take_control(self.session)
        if self.do is not None:
            self.do()
            self.acted = True
        if self.cancel:
            self.session.abort()
        else:
            hand_back(self.session, "dialog acknowledged")


def replay(session, artifact, policy, handoff, member_id="10005"):
    return ReplayEngine(
        session.controlled, artifact, policy=policy, escalate=handoff
    ).run({"member_id": member_id})


#: The same allowlist the replay tests use. Imported as a fixture rather than
#: rebuilt, so the two files cannot drift apart about what is permitted.
policy = engine_policy


# --- the sequence --------------------------------------------------------


def test_a_person_clears_the_block_and_the_run_finishes(
    session, surface, artifact, policy
):
    """The whole of section 3.6 in one test.

    The run stops at the undeclared dialog, a person takes the *same* session,
    deals with it, hands it back, and the run completes -- with the states it
    passed through recorded in order.
    """
    registry = InterventionRegistry()
    handoff = Handoff(session, registry=registry, capability=artifact.ref)

    def deal_with_it():
        # What a person does: the screen behind the dialog becomes reachable.
        surface.index = 3

    operator = Operator(session, do=deal_with_it).start()
    result = replay(session, artifact, policy, handoff)
    operator.thread.join(WAIT)

    assert isinstance(result, Success), getattr(result, "reason", result)
    assert result.outputs["savings_balance"] == "22047.19"
    assert session.state is RunState.RUNNING

    visited = [change.to for change in session.history]
    assert visited == [
        RunState.RUNNING,
        RunState.AWAITING_HUMAN,
        RunState.HUMAN_CONTROL,
        RunState.VERIFYING,
        RunState.RUNNING,
    ]

    assert len(registry) == 1
    assert registry.open_items() == []
    assert registry.all()[0].state == RESOLVED
    assert registry.all()[0].verified is True


def test_the_intervention_says_what_a_person_needs_to_decide(
    session, surface, artifact, policy
):
    """A request nobody can act on without opening a log file is a pager
    alert, not an intervention."""
    handoff = Handoff(
        session, inputs={"member_id": "10005"}, capability=artifact.ref
    )
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    replay(session, artifact, policy, handoff)

    request = handoff.history[0].request
    assert request.step_id == "s3"
    assert request.intent
    assert request.capability == artifact.ref
    assert request.session_id == session.id
    assert request.run_id == "r-test"
    # The application's own words, not a summary of them.
    assert request.observed == "Compliance Hold"
    assert "Compliance Hold" in request.tree_text
    assert request.reason


def test_the_result_names_the_open_intervention(session, artifact, policy):
    """An escalated replay a caller cannot trace to a request is a dead end."""
    handoff = Handoff(session, timeout=0.05, capability=artifact.ref)
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert result.intervention_id == handoff.history[0].id


# --- control ------------------------------------------------------------


def test_automation_cannot_act_while_a_person_holds_the_session(session, surface):
    """The control token enforced rather than documented.

    ``assert_control`` existing is worth nothing if acting does not call it,
    so the engine is handed a guarded surface and the guard is what makes
    "automation must not act during a handoff" a property of the system.
    """
    from cua.primitives import Action

    guarded = session.controlled
    assert isinstance(guarded, GuardedSurface)

    session.escalate("something blocking")
    take_control(session)

    with pytest.raises(ControlViolation):
        guarded.act(Action(type=ActionType.CLICK, args={}))

    # Looking is still allowed: the console needs to show the operator what is
    # on screen precisely while they are the one driving.
    assert guarded.observe() is surface.observe()
    assert surface.acted == []


def test_looking_at_the_page_is_never_blocked(session):
    session.escalate("blocked")
    take_control(session)
    assert session.control is ControlOwner.HUMAN
    assert session.controlled.observe() is not None


def test_the_guard_passes_through_what_a_surface_can_do(session, surface):
    """Wrapping a surface must not cost it a capability -- the watcher and the
    page-source capture are both reached this way."""
    surface.page_source = lambda: "<html/>"
    assert session.controlled.page_source() == "<html/>"


# --- handing back --------------------------------------------------------


def test_a_handoff_that_did_not_fix_it_asks_again(session, artifact, policy):
    """Never correct the human, and never resume on their word.

    The operator says they are done and the expected screen is not there. The
    run does not continue, does not fail, and does not tell them they were
    wrong: it stops and asks again.
    """
    handoff = Handoff(session, capability=artifact.ref)
    Operator(session, do=lambda: None).start()  # takes control, changes nothing
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert "did not verify" in result.reason
    assert session.state is RunState.AWAITING_HUMAN
    assert handoff.history[0].state == UNVERIFIED
    assert handoff.history[0].verified is False


def test_a_run_resumes_even_if_the_person_did_nothing(artifact, policy):
    """Nothing here judges what the human did.

    The person takes the session, decides nothing needs doing -- a colleague
    cleared the hold in another window, say -- and hands it straight back. The
    world is re-checked, not their work, so the run continues. That is what
    "the human is the higher authority" has to mean in code: no rule anywhere
    requires them to have clicked anything.
    """
    surface = WatchableSurface(
        blocked_run_screens(),
        values={"member_name": "Aisha Bello", "savings_balance": "22,047.19"},
        records=[],
    )
    session = Session(
        id="s-idle", surface=surface, browser=None, base_url="http://localhost:5000"
    )
    session.start()
    handoff = Handoff(session, capability=artifact.ref)
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Success)
    assert handoff.history[0].human_actions == ()
    assert handoff.history[0].state == RESOLVED


def test_nobody_comes_and_the_run_stops_without_failing(session, artifact, policy):
    """An unattended run with nobody to ask.

    The timeout is not a failure: the capability is not broken, the session is
    still open and the intervention is still listed. It only means this call
    is no longer occupying a thread.
    """
    handoff = Handoff(session, timeout=0.05, capability=artifact.ref)
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert session.state is RunState.AWAITING_HUMAN
    assert handoff.history[0].state == EXPIRED
    assert "nobody took the session" in handoff.history[0].note


def test_an_operator_can_cancel_the_run(session, artifact, policy):
    handoff = Handoff(session, capability=artifact.ref)
    Operator(session, do=lambda: None, cancel=True).start()
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Escalated)
    assert session.state is RunState.ABORTED
    assert handoff.history[0].state == ABORTED


def test_taking_control_is_recorded_even_when_the_run_moves_on(
    session, surface, artifact, policy
):
    handoff = Handoff(session, capability=artifact.ref)
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    replay(session, artifact, policy, handoff)

    granted = [c for c in session.history if c.to is RunState.HUMAN_CONTROL]
    assert granted and granted[0].control is ControlOwner.HUMAN


# --- what the person did -------------------------------------------------


class WatchableSurface(FakeSurface):
    """A fake that can report human actions, like ``WebSurface`` does."""

    def __init__(self, *args, records=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.pending = list(records)
        self.watching = False
        self.stopped = False

    def watch_start(self):
        self.watching = True

    def watch_drain(self):
        out, self.pending = self.pending, []
        return out

    def watch_stop(self):
        self.stopped = True
        return self.watch_drain()


def raw(kind, role, name, value=None, ref="n1", at="2026-01-01T00:00:00Z", id="a"):
    return {
        "id": id,
        "at": at,
        "kind": kind,
        "role": role,
        "name": name,
        "value": value,
        "ref": ref,
        "location": "http://localhost:5000/members/10005",
    }


def test_what_the_person_did_is_recorded(artifact, policy):
    """The one moment the automation is not the actor is the one moment the
    log would otherwise be blank."""
    surface = WatchableSurface(
        blocked_run_screens(),
        values={"member_name": "Aisha Bello", "savings_balance": "22,047.19"},
        records=[
            raw("click", "button", "Acknowledge", id="a"),
            raw("change", "textbox", "Reason", value="reviewed", id="b"),
        ],
    )
    session = Session(
        id="s-w", surface=surface, browser=None, base_url="http://localhost:5000"
    )
    session.start()
    handoff = Handoff(session, capability=artifact.ref)
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    replay(session, artifact, policy, handoff)

    actions = handoff.history[0].human_actions
    assert [a.name for a in actions] == ["Acknowledge", "Reason"]
    assert actions[0].describe() == "click button 'Acknowledge'"
    assert surface.stopped


def test_the_same_record_arriving_from_two_frames_is_counted_once():
    """Frames of one origin share the tab's storage, so a drain can see the
    same event twice. Deduplicating is cheaper than deciding which frame owns
    the log while pages are navigating underneath us."""
    surface = WatchableSurface(
        [screen("x")], records=[raw("click", "button", "Acknowledge", id="same")] * 2
    )
    watcher = HumanWatcher(surface)
    watcher.start()
    assert len(watcher.stop()) == 1


def test_records_are_ordered_by_when_they_happened():
    surface = WatchableSurface(
        [screen("x")],
        records=[
            raw("click", "button", "Second", at="2026-01-01T00:00:02Z", id="2"),
            raw("click", "button", "First", at="2026-01-01T00:00:01Z", id="1"),
        ],
    )
    watcher = HumanWatcher(surface)
    watcher.start()
    assert [a.name for a in watcher.stop()] == ["First", "Second"]


def test_a_surface_that_cannot_be_watched_does_not_block_a_handoff(
    session, surface, artifact, policy, tmp_path
):
    """A desktop driver may have no way to report what a person did. The
    handoff still works; the log says the record is unavailable, which is a
    better answer than silence a reader would take for "they did nothing"."""
    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10005"},
        config=EvidenceConfig(root=tmp_path, screenshots=False),
    )
    watcher = HumanWatcher(surface)
    assert watcher.supported is False

    handoff = Handoff(session, recorder=recorder, capability=artifact.ref)
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    result = replay(session, artifact, policy, handoff)

    assert isinstance(result, Success)
    log = (recorder.dir / "run.jsonl").read_text(encoding="utf-8")
    assert "human_actions_unavailable" in log


# --- evidence ------------------------------------------------------------


def test_an_intervention_carries_what_was_declared_and_nothing_more(
    session, surface, artifact, policy, tmp_path
):
    """``member_id`` is declared ``internal`` and appears in the clear.

    Worth asserting, because it is the argument against pattern-matching
    stated as a test. A five-digit number beside the word "member" is exactly
    what a clever regular expression would mask, and masking it would make
    every intervention unreadable to the operator who has to act on it. What
    is written is what the contract says, not what a heuristic suspects.
    """
    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10005"},
        config=EvidenceConfig(root=tmp_path, screenshots=False),
    )
    handoff = Handoff(
        session,
        recorder=recorder,
        inputs={"member_id": "10005"},
        capability=artifact.ref,
    )
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    replay(session, artifact, policy, handoff)

    assert handoff.history[0].request.inputs == {"member_id": "10005"}
    log = (recorder.dir / "run.jsonl").read_text(encoding="utf-8")
    assert "intervention_opened" in log
    assert "intervention_closed" in log


def test_an_intervention_masks_a_value_the_contract_calls_sensitive(
    session, surface, artifact, policy, tmp_path
):
    """The same request with the declaration changed.

    An intervention leaves the run -- rendered in a console, written to the
    log -- so it is exactly where a classified value escapes if redaction is
    something applied at the end rather than on the one path to disk.
    """
    artifact.inputs["member_id"].sensitivity = Sensitivity.PII
    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10005"},
        config=EvidenceConfig(root=tmp_path, screenshots=False),
    )
    handoff = Handoff(
        session,
        recorder=recorder,
        inputs={"member_id": "10005"},
        capability=artifact.ref,
    )
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    replay(session, artifact, policy, handoff)

    assert handoff.history[0].request.inputs["member_id"].startswith("[pii:")
    assert "10005" not in (recorder.dir / "run.jsonl").read_text(encoding="utf-8")


def test_human_actions_go_through_the_same_redactor(artifact, policy, tmp_path):
    """A person typing a classified value does not make it less classified.

    This is the path that has no name attached: the value arrives as a field
    called ``value`` on a click record, so declared classification cannot see
    it and the literal scrub is what catches it. The handoff is precisely
    where that matters, because it is the one moment values are entered by
    hand rather than interpolated from the contract.
    """
    artifact.inputs["member_id"].sensitivity = Sensitivity.PII
    surface = WatchableSurface(
        blocked_run_screens(),
        values={"member_name": "Aisha Bello", "savings_balance": "22,047.19"},
        records=[raw("change", "textbox", "Member ID", value="10005", id="x")],
    )
    session = Session(
        id="s-r", surface=surface, browser=None, base_url="http://localhost:5000"
    )
    session.start()
    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10005"},
        config=EvidenceConfig(root=tmp_path, screenshots=False),
    )
    handoff = Handoff(session, recorder=recorder, capability=artifact.ref)
    Operator(session, do=lambda: setattr(surface, "index", 3)).start()
    replay(session, artifact, policy, handoff)

    log = (recorder.dir / "run.jsonl").read_text(encoding="utf-8")
    assert "human_actions" in log
    assert "Member ID" in log
    assert "10005" not in log


# --- the record itself ---------------------------------------------------


def test_an_intervention_serialises_for_a_console():
    from cua.session import InterventionRequest

    registry = InterventionRegistry()
    item = registry.open(
        InterventionRequest(
            id="i1",
            reason="an undeclared dialog is blocking the screen",
            capability="member.read_savings_balance@1.0.0",
            step_id="s3",
            intent="open the member record",
            observed="Compliance Hold",
        )
    )
    item.human_actions = (HumanAction(at="t", kind="click", role="button", name="Ok"),)
    data = item.as_dict()

    assert data["state"] == "open"
    assert data["human_actions"][0]["name"] == "Ok"
    assert "Compliance Hold" in data["observed"]
    assert item.request.summary().startswith("member.read_savings_balance@1.0.0")
