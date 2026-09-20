"""Sessions that outlive the function that started them.

The obvious way to write this system is for a run to launch a browser, drive
it, and close it. That design cannot satisfy the brief: it asks for a human to
take over *the same live session the automation was using -- not a fresh one*,
and a browser held in a local variable inside a blocked function is
unreachable by anything else.

So a session is an object with an id, held in a dictionary. That single change
is the whole architectural move. Everything else -- the state machine, the
control token, the operator console -- is possible because something other
than the currently-executing step can reach the window.

Browsers run headed, which is what makes the handoff honest rather than
simulated: when automation pauses, the operator clicks in the window that is
already on screen. No frame streaming, no input forwarding, no co-browsing
server, all of which the brief puts out of scope.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from cua.session.state import (
    ControlViolation,
    IllegalTransition,
    can_transition,
    control_for,
    is_terminal,
)
from cua.surface.base import Surface
from cua.surface.web import BrowserSession
from cua.types import ControlOwner, RunState


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StateChange:
    """One entry in a session's history.

    Kept because "who was in control when this happened" is an audit question
    in a regulated setting, and reconstructing it after the fact from
    interleaved logs is exactly the sort of thing that turns out to be
    impossible when it matters.
    """

    at: datetime
    frm: RunState
    to: RunState
    control: ControlOwner
    reason: str = ""


@dataclass
class Session:
    """One live browser, plus who is allowed to drive it."""

    id: str
    surface: Surface
    browser: BrowserSession
    base_url: str
    tenant: str = "base"
    state: RunState = RunState.PENDING
    run_id: str | None = None
    created_at: datetime = field(default_factory=_now)
    history: list[StateChange] = field(default_factory=list)

    #: Set when an operator finishes and hands control back. The automation
    #: thread blocks on this rather than polling, so a paused run costs
    #: nothing while it waits.
    _resume: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _guarded: "GuardedSurface | None" = field(default=None, repr=False)

    # --- control ---------------------------------------------------------

    @property
    def controlled(self) -> "GuardedSurface":
        """The surface to hand to an engine: refuses to act out of turn."""
        if self._guarded is None:
            self._guarded = GuardedSurface(self, self.surface)
        return self._guarded

    @property
    def control(self) -> ControlOwner:
        return control_for(self.state)

    def assert_control(self, owner: ControlOwner = ControlOwner.AUTOMATION) -> None:
        """Refuse to act unless the caller holds the token.

        Called at the top of every action the replay and discovery engines
        take. Cheap, and it is the mechanism that makes "automation must not
        act while a human is driving" a property of the system rather than a
        convention people remember.
        """
        if self.control is not owner:
            raise ControlViolation(owner, self.control, self.state)

    # --- transitions -----------------------------------------------------

    def transition(self, to: RunState, reason: str = "") -> RunState:
        with self._lock:
            if not can_transition(self.state, to):
                raise IllegalTransition(self.state, to)
            frm, self.state = self.state, to
            self.history.append(
                StateChange(_now(), frm, to, control_for(to), reason)
            )
            return to

    def start(self, run_id: str | None = None) -> None:
        self.run_id = run_id or self.run_id
        self.transition(RunState.RUNNING, "run started")

    def escalate(self, reason: str) -> None:
        """Stop and ask for a person. Valid from RUNNING or VERIFYING."""
        self._resume.clear()
        self.transition(RunState.AWAITING_HUMAN, reason)

    def grant(self, to: ControlOwner = ControlOwner.HUMAN) -> None:
        """Hand the live session to an operator."""
        if to is not ControlOwner.HUMAN:
            raise ValueError("only a human can be granted an open intervention")
        self.transition(RunState.HUMAN_CONTROL, "operator took control")

    def release(self, note: str = "") -> None:
        """Operator is done. Control returns, but trust does not yet.

        Goes to VERIFYING rather than RUNNING on purpose. The human is the
        higher authority and we never correct what they did -- but we do
        re-check where they left us before acting on the assumption that the
        expected screen is still in front of us.
        """
        self.transition(RunState.VERIFYING, note or "operator released control")
        self._resume.set()

    def resume(self, reason: str = "state re-verified") -> None:
        self.transition(RunState.RUNNING, reason)

    def finish(self, state: RunState, reason: str = "") -> None:
        if not is_terminal(state):
            raise ValueError(f"{state} is not a terminal state")
        self.transition(state, reason)

    def abort(self, reason: str = "cancelled by operator") -> None:
        self.transition(RunState.ABORTED, reason)
        self._resume.set()

    # --- waiting ---------------------------------------------------------

    def wait_for_release(self, timeout: float | None = None) -> bool:
        """Block the automation while a human works. True if control returned."""
        return self._resume.wait(timeout)

    # --- housekeeping ----------------------------------------------------

    @property
    def is_finished(self) -> bool:
        return is_terminal(self.state)

    def close(self) -> None:
        """Release the browser.

        Closing the browser closes its pages, so the page is not closed
        separately: doing both made teardown hang for minutes, because closing
        a page runs its unload handlers and waits on connections the browser
        is about to drop anyway.
        """
        self.browser.stop()


@dataclass
class GuardedSurface:
    """The session's surface with the control token actually enforced.

    Without this the token is documentation. ``assert_control`` has to be
    called by something, and "every caller remembers to call it" is exactly
    the kind of rule that holds until the one path that forgets. Wrapping the
    surface makes acting while a human holds the session impossible rather
    than discouraged, which is what the brief's "a way to know who is (or
    should be) in control" is worth having.

    Only ``act`` is guarded. Observing and resolving are read-only, and the
    operator console wants to look at the page precisely while a person is
    driving it -- a guard there would block the one view that matters during a
    handoff.
    """

    session: "Session"
    inner: Surface

    def observe(self, *args, **kwargs):
        return self.inner.observe(*args, **kwargs)

    def resolve(self, *args, **kwargs):
        return self.inner.resolve(*args, **kwargs)

    def act(self, action):
        self.session.assert_control(ControlOwner.AUTOMATION)
        return self.inner.act(action)

    def screenshot(self, *args, **kwargs):
        return self.inner.screenshot(*args, **kwargs)

    def close(self) -> None:
        self.inner.close()

    def __getattr__(self, name: str):
        # Everything a particular surface offers beyond the protocol --
        # page_source, the human-action watcher -- passes straight through, so
        # wrapping a surface never costs it a capability.
        if name.startswith("_") or name == "inner":
            # Guard against recursing forever when `inner` itself is missing,
            # which is what a dunder probe during copy or pickle looks like.
            raise AttributeError(name)
        return getattr(self.inner, name)


class SessionManager:
    """Holds live sessions so they can be reached by id.

    Thread-safe because the operator console runs on its own thread and reads
    the same sessions the automation thread is driving.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def create(
        self,
        base_url: str,
        *,
        tenant: str = "base",
        headless: bool | None = None,
        screenshot_dir: str | Path | None = None,
        session_id: str | None = None,
    ) -> Session:
        browser = BrowserSession(base_url, headless=headless)
        surface = browser.start(screenshot_dir=screenshot_dir)
        session = Session(
            id=session_id or uuid4().hex[:12],
            surface=surface,
            browser=browser,
            base_url=base_url,
            tenant=tenant,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                raise KeyError(f"no session {session_id!r}") from None

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def awaiting_human(self) -> list[Session]:
        """What the operator console shows: runs that have stopped for a person."""
        return [s for s in self.all() if s.state is RunState.AWAITING_HUMAN]

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        for session in self.all():
            self.close(session.id)

    def __iter__(self) -> Iterator[Session]:
        return iter(self.all())

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
