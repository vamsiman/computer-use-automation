"""Stopping to ask a person, and starting again afterwards.

This is the piece the brief calls out as "not just a TODO", and the reason it
is not a TODO is the session manager: the browser is an object with an id that
outlives the function driving it, so pausing a run does not mean abandoning
the work. The window stays open on screen, the operator acts in the *same*
session the automation was using, and the run picks up where it stopped.

The sequence, which is the whole design:

    RUNNING          the engine meets something undeclared and blocking
      -> AWAITING_HUMAN   an InterventionRequest is written; the automation
                          thread parks on an Event and costs nothing
      -> HUMAN_CONTROL    an operator takes the session; from here the
                          automation is *refused* by the control token, not
                          merely discouraged
      -> VERIFYING        they hand it back. Control has returned; trust has
                          not
      -> RUNNING          only once the engine has re-checked the world

The last arrow is the one that matters. The engine re-verifies the step's
checkpoint after the handoff and this module never resumes on the human's
word: ``resolved(False)`` puts the session straight back to AWAITING_HUMAN.
That is not distrust of the person. It is that the person is free to do
something entirely reasonable and unrelated -- fix the record, navigate away,
answer a different question -- and an automation that assumes the screen it
wanted is now in front of it will act on the wrong page.

What is deliberately *not* here: any attempt to decide what the human should
have done, and any path that lets the automation take back control by itself.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from cua.session.human import HumanAction, HumanWatcher
from cua.session.manager import Session
from cua.types import ControlOwner, RunState

#: How long the automation waits for a person before giving up on this run.
#:
#: Not a failure when it expires: an intervention nobody came to is still an
#: intervention, the session is still open, and the operator console still
#: lists it. It only means this call is no longer sitting on a thread waiting.
HANDOFF_TIMEOUT_SECONDS = 900.0
TIMEOUT_ENV = "CUA_HANDOFF_TIMEOUT"


def handoff_timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENV, "")
    try:
        return float(raw) if raw else HANDOFF_TIMEOUT_SECONDS
    except ValueError:
        return HANDOFF_TIMEOUT_SECONDS


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class InterventionRequest:
    """Everything a person needs to decide, without opening a log file.

    Assembled at the moment of the pause, because half of it -- what was on
    screen, what the tree looked like -- stops being true the instant anybody
    touches the window.
    """

    id: str
    reason: str
    capability: str
    step_id: str
    intent: str
    session_id: str | None = None
    run_id: str | None = None
    #: The application's own words for whatever is blocking. Quoted verbatim:
    #: an operator deciding whether to click "Acknowledge" on a compliance
    #: hold needs the bank's wording, not our summary of it.
    observed: str | None = None
    location: str = ""
    #: The rendered accessibility tree, so the console can show what the
    #: automation could actually see rather than what a screenshot suggests.
    tree_text: str = ""
    screenshot_ref: str | None = None
    #: Already redacted. Typed parameters, not free text: an operator who has
    #: to supply a value gets a field validated against the artifact's input
    #: schema, never a box to guess into.
    inputs: dict[str, Any] = field(default_factory=dict)
    #: Parameters the run is *asking* for, name -> the declared spec. The
    #: console renders these as typed fields validated by the capability's own
    #: contract, never as a box to type anything into. An automation that
    #: accepts a free-text answer to "what is the member number" has just
    #: moved the fabrication from the machine to the form.
    needs: dict[str, Any] = field(default_factory=dict)
    opened_at: datetime = field(default_factory=_now)

    def summary(self) -> str:
        return f"{self.capability} stopped at {self.step_id} ({self.intent}): {self.reason}"


#: Where an intervention got to. Kept as plain strings because this is what
#: the console renders and the evidence log records.
OPEN = "open"
GRANTED = "granted"
RESOLVED = "resolved"
UNVERIFIED = "unverified"
EXPIRED = "expired"
ABORTED = "aborted"


@dataclass
class Intervention:
    request: InterventionRequest
    state: str = OPEN
    granted_at: datetime | None = None
    closed_at: datetime | None = None
    #: What the person did while they held the session.
    human_actions: tuple[HumanAction, ...] = ()
    #: Whether the step's checkpoint held afterwards. ``None`` until the
    #: engine has asked, which is the only authority on the question.
    verified: bool | None = None
    #: What a person typed in, already validated against the contract.
    supplied: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def is_open(self) -> bool:
        return self.state in (OPEN, GRANTED)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self.request)
        data["needs"] = sorted(self.request.needs)
        data["opened_at"] = self.request.opened_at.isoformat()
        return {
            **data,
            "state": self.state,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "verified": self.verified,
            "supplied": sorted(self.supplied),
            "note": self.note,
            "human_actions": [action.as_dict() for action in self.human_actions],
        }


class InterventionRegistry:
    """Open interventions, reachable by id.

    Thread-safe for the same reason the session manager is: the operator
    console runs on its own thread and reads what the automation thread is
    writing.
    """

    def __init__(self) -> None:
        self._items: dict[str, Intervention] = {}
        self._lock = threading.RLock()

    def open(self, request: InterventionRequest) -> Intervention:
        item = Intervention(request=request)
        with self._lock:
            self._items[request.id] = item
        return item

    def get(self, intervention_id: str) -> Intervention:
        with self._lock:
            try:
                return self._items[intervention_id]
            except KeyError:
                raise KeyError(f"no intervention {intervention_id!r}") from None

    def all(self) -> list[Intervention]:
        with self._lock:
            return list(self._items.values())

    def open_items(self) -> list[Intervention]:
        return [item for item in self.all() if item.is_open]

    def for_session(self, session_id: str) -> list[Intervention]:
        return [i for i in self.all() if i.request.session_id == session_id]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class Handoff:
    """The escalation handler: pause, ask, wait, hand back.

    Passed to ``ReplayEngine(escalate=...)``. Called from the automation
    thread, and blocks it -- which is the point. The run has not failed and
    has not been abandoned; it is parked on an ``Event`` with its browser
    still open, costing a thread and nothing else.

    The engine calls ``resolved(verified)`` afterwards with the result of
    re-checking the step. That second call is what moves the session out of
    VERIFYING, so the decision to resume is made by the world, reported by the
    engine, and acted on here.
    """

    def __init__(
        self,
        session: Session,
        *,
        registry: InterventionRegistry | None = None,
        recorder: Any = None,
        inputs: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        watcher: HumanWatcher | None = None,
        capability: str = "",
    ) -> None:
        self.session = session
        # `or` would be wrong: an empty registry is falsy, and a caller who
        # passes one in wants that one, not a private replacement.
        self.registry = InterventionRegistry() if registry is None else registry
        self.recorder = recorder
        self.inputs = dict(inputs or {})
        self.timeout = handoff_timeout() if timeout is None else timeout
        self.watcher = watcher or HumanWatcher(session.surface)
        self.capability = capability
        #: The one currently open, for the engine's follow-up call and for
        #: whatever built this to report on afterwards.
        self.current: Intervention | None = None
        self.history: list[Intervention] = []

    # --- the handler -----------------------------------------------------

    def __call__(self, step: Any, reason: str, snapshot: Any) -> bool:
        """True if a person dealt with it and the run should carry on."""
        intervention = self._open(reason, step=step, snapshot=snapshot)
        return self._settle(intervention, self._wait())

    def ask(self, needs: Mapping[str, Any]) -> dict[str, Any] | None:
        """Ask a person for values the caller did not supply.

        The other half of the escalation protocol, and the reason the console
        has typed fields at all. Invoked when a capability cannot run for want
        of a parameter -- which is a question, not a fault, and answering it
        by inventing a member number is the one thing this system must never
        do. A person answers, the answer is checked against the capability's
        own contract, and only then does anything touch the application.

        Returns the supplied values, or ``None`` if nobody answered.
        """
        wanted = ", ".join(sorted(needs))
        intervention = self._open(f"the run needs a value for {wanted}", needs=needs)
        if not self._settle(intervention, self._wait()):
            return None
        return dict(intervention.supplied)

    def provide(self, values: Mapping[str, Any]) -> list[str]:
        """Accept an operator's answers. Returns why they were refused.

        Checked by the capability's own ``InputSpec``, so the console cannot
        be a laxer front door into the same capability than the API is.
        """
        intervention = self.current
        if intervention is None:
            return ["there is no open intervention to answer"]

        specs = intervention.request.needs
        problems: list[str] = []
        accepted: dict[str, Any] = {}
        for name, value in values.items():
            spec = specs.get(name)
            if spec is None:
                problems.append(f"{name!r} is not something this run asked for")
                continue
            found = spec.problems(name, value)
            problems.extend(found)
            if not found:
                accepted[name] = value

        missing = [name for name in specs if name not in accepted]
        problems.extend(f"still missing a value for {name!r}" for name in missing)
        if problems:
            return problems

        intervention.supplied = accepted
        return []

    def _open(
        self,
        reason: str,
        *,
        step: Any = None,
        snapshot: Any = None,
        needs: Mapping[str, Any] | None = None,
    ) -> Intervention:
        intervention = self.registry.open(
            self._request(step, reason, snapshot, needs)
        )
        self.current = intervention
        self.history.append(intervention)

        self._pause(reason)
        self._start_watching(intervention)

        if self.recorder:
            self.recorder.event(
                "intervention_opened",
                intervention_id=intervention.id,
                step_id=intervention.request.step_id,
                reason=reason,
                observed=intervention.request.observed,
                screenshot=intervention.request.screenshot_ref,
                needs=sorted(intervention.request.needs),
            )
        return intervention

    def _wait(self) -> bool:
        """Park the automation until somebody hands the session back.

        Its own method because waiting is the part most likely to be something
        else somewhere else -- a queue, a webhook, a scheduled retry -- while
        everything around it stays the same. Here it is a ``threading.Event``,
        which is all a single-process deployment needs.
        """
        return self.session.wait_for_release(self.timeout)

    def resolved(self, verified: bool) -> None:
        """Told by the engine whether the step held up after the handoff.

        Optional half of the handler protocol, and the reason it exists is
        that the thing which paused the run is the thing that must decide
        whether it resumes. The engine knows whether the checkpoint passed; it
        does not know there is a session, and should not.
        """
        intervention = self.current
        if intervention is not None:
            intervention.verified = verified

        if self.session.state is not RunState.VERIFYING:
            # Cancelled while we were checking, or never paused here at all.
            return

        if verified:
            self.session.resume("state re-verified after handoff")
            if intervention is not None:
                intervention.state = RESOLVED
                intervention.closed_at = _now()
        else:
            # Never correct the human. Ask again.
            self.session.escalate("the step did not verify after the handoff")
            if intervention is not None:
                intervention.state = UNVERIFIED
                intervention.note = (
                    "control was returned but the expected screen was not there"
                )

        self._record_control("re-verified" if verified else "re-verification failed")
        if self.recorder:
            self.recorder.event(
                "intervention_closed",
                intervention_id=intervention.id if intervention else None,
                state=intervention.state if intervention else None,
                verified=verified,
            )

    # --- internals -------------------------------------------------------

    def _request(
        self,
        step: Any,
        reason: str,
        snapshot: Any,
        needs: Mapping[str, Any] | None = None,
    ) -> InterventionRequest:
        screenshot = None
        if self.recorder is not None:
            screenshot = self.recorder.screenshot(
                self.session.surface, label=f"intervention-{uuid4().hex[:6]}"
            )
        return InterventionRequest(
            id=uuid4().hex[:12],
            reason=reason,
            capability=self.capability,
            step_id=getattr(step, "id", ""),
            intent=getattr(step, "intent", ""),
            session_id=self.session.id,
            run_id=self.session.run_id,
            observed=getattr(snapshot, "modal_text", None),
            location=getattr(snapshot, "location", ""),
            tree_text=snapshot.text_view() if hasattr(snapshot, "text_view") else "",
            screenshot_ref=screenshot,
            inputs=self._safe_inputs(),
            needs=dict(needs or {}),
        )

    def _safe_inputs(self) -> dict[str, Any]:
        """The parameters so far, through the run's own redactor.

        An intervention request is a document that leaves the run -- it is
        rendered in a console and written to the evidence log -- so it is
        exactly the place a member number would escape if redaction were
        something applied at the end.
        """
        if self.recorder is not None:
            return self.recorder.redactor.mapping(dict(self.inputs))
        return dict(self.inputs)

    def _pause(self, reason: str) -> None:
        if self.session.state is RunState.AWAITING_HUMAN:
            return
        self.session.escalate(reason)
        self._record_control(reason)

    def _start_watching(self, intervention: Intervention) -> None:
        """Watch from the pause, not from the grant.

        Somebody who reaches over and clicks the window before formally taking
        control has still acted on the live session, and an audit log that
        omits it because the paperwork was not done is worse than no log.
        """
        if not self.watcher.start() and self.recorder:
            self.recorder.event(
                "human_actions_unavailable",
                intervention_id=intervention.id,
                detail="this surface cannot report what a person did to it",
            )

    def _settle(self, intervention: Intervention, came_back: bool) -> bool:
        actions = self.watcher.stop()
        intervention.human_actions = actions
        if self.recorder and actions:
            self.recorder.human_actions([a.as_dict() for a in actions])

        if not came_back:
            intervention.state = EXPIRED
            intervention.note = f"nobody took the session within {self.timeout:g}s"
            if self.recorder:
                self.recorder.event(
                    "intervention_expired", intervention_id=intervention.id
                )
            return False

        if self.session.state is RunState.ABORTED:
            intervention.state = ABORTED
            intervention.closed_at = _now()
            intervention.note = "cancelled by the operator"
            return False

        if self.session.state is not RunState.VERIFYING:
            # Woken without control coming back the way it should have. Say so
            # rather than resuming into an unknown state.
            intervention.note = f"woke in state {self.session.state}"
            return False

        intervention.state = GRANTED
        intervention.granted_at = intervention.granted_at or _now()
        self._record_control("operator returned control")
        return True

    def _record_control(self, reason: str) -> None:
        if not self.recorder:
            return
        self.recorder.control(
            None, self.session.state, self.session.control, reason=reason
        )


def take_control(session: Session) -> None:
    """Operator picks up a paused session. AWAITING_HUMAN -> HUMAN_CONTROL."""
    session.grant(ControlOwner.HUMAN)


def hand_back(session: Session, note: str = "") -> None:
    """Operator is finished. HUMAN_CONTROL -> VERIFYING, and the run wakes."""
    session.release(note)
