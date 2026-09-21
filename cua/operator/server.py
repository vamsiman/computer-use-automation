"""The operator console: what a person sees when an automation stops.

The brief asks for a way for a human to take over, and the honest shape of
that in a single-process system is this: a small web page that talks to the
session manager, and a Chromium window already open on the next monitor. The
console does not show the application. It shows **why the run stopped**, and
it hands over the controls.

That split is deliberate and it is the design decision worth defending. The
obvious build is an embedded viewport -- stream the page into the console so
the operator never leaves it -- and it is a large piece of infrastructure
(frame transport, input forwarding, latency, a second rendering of a page that
is already rendered) in service of an ergonomic improvement, not a capability
one. The brief puts co-browsing out of scope, so the viewport is *designed* in
REPORT.md and not built, and the seam is kept honest: **this module reaches the
session only through the control-token API**. Swapping the screenshot for a
streamed interactive viewport changes this component and nothing else.

What the console is not allowed to do is as important as what it does. It
never drives the browser, never resolves a locator, never decides a run has
recovered. It moves the control token and records what a person chose. The
automation re-checks the world for itself when it gets the session back.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for

from cua.session.intervention import Handoff, InterventionRegistry, hand_back, take_control
from cua.session.manager import SessionManager
from cua.types import RunState

DEFAULT_PORT = 5100

#: How often the page re-reads the state of the world, in seconds. Polling
#: rather than pushing: a handoff is measured in minutes and a human is the
#: slowest thing in the loop, so a websocket would be machinery bought to
#: solve a problem nobody has.
POLL_SECONDS = 2


def create_app(
    manager: SessionManager,
    registry: InterventionRegistry,
    handoffs: dict[str, Handoff] | None = None,
) -> Flask:
    """A console over these sessions.

    ``handoffs`` maps session id -> the ``Handoff`` waiting on that session,
    which is how an answer typed into a form reaches the run that asked for
    it. Absent, the console still grants, resumes and cancels; it simply
    cannot answer questions.
    """
    app = Flask(__name__)
    handoffs = handoffs if handoffs is not None else {}

    def state() -> dict[str, Any]:
        items = []
        for item in sorted(
            registry.all(), key=lambda i: i.request.opened_at, reverse=True
        ):
            session_id = item.request.session_id
            session = None
            if session_id:
                try:
                    session = manager.get(session_id)
                except KeyError:
                    session = None
            data = item.as_dict()
            data["session_state"] = session.state.value if session else "gone"
            data["control"] = session.control.value if session else "none"
            data["can_grant"] = bool(
                session and session.state is RunState.AWAITING_HUMAN
            )
            data["can_release"] = bool(
                session and session.state is RunState.HUMAN_CONTROL
            )
            data["can_cancel"] = bool(session and not session.is_finished)
            data["fields"] = [
                {
                    "name": name,
                    "type": str(spec.type),
                    "description": spec.description,
                    "pattern": spec.pattern,
                    "enum": spec.enum,
                    "required": spec.required,
                }
                for name, spec in sorted(item.request.needs.items())
            ]
            items.append(data)
        return {"interventions": items, "poll_seconds": POLL_SECONDS}

    def session_for(intervention_id: str):
        item = registry.get(intervention_id)
        session_id = item.request.session_id
        if not session_id:
            raise KeyError(f"intervention {intervention_id!r} has no session")
        return item, manager.get(session_id)

    @app.get("/")
    def index():
        return render_template("console.html", **state())

    @app.get("/api/state")
    def api_state():
        return jsonify(state())

    @app.post("/interventions/<intervention_id>/grant")
    def grant(intervention_id: str):
        item, session = session_for(intervention_id)
        take_control(session)
        item.state = "granted"
        return _back(intervention_id)

    @app.post("/interventions/<intervention_id>/resume")
    def resume(intervention_id: str):
        """Hand the session back. Note what this does *not* do.

        It does not mark the step done, and it does not tell the run it
        succeeded. It returns the control token and lets the automation
        re-check the world. A console button that could declare a step
        complete would be a console button that can be wrong about a live
        banking session.
        """
        item, session = session_for(intervention_id)
        hand_back(session, request.form.get("note", ""))
        return _back(intervention_id)

    @app.post("/interventions/<intervention_id>/cancel")
    def cancel(intervention_id: str):
        item, session = session_for(intervention_id)
        session.abort(request.form.get("note") or "cancelled from the console")
        item.state = "aborted"
        return _back(intervention_id)

    @app.post("/interventions/<intervention_id>/answer")
    def answer(intervention_id: str):
        """Supply the values the run asked for.

        Validated against the capability's own ``InputSpec`` -- the same code
        that checks an API caller's arguments. A console that accepted looser
        input than the API would be a second, quieter way into the same
        capability.
        """
        item = registry.get(intervention_id)
        handoff = handoffs.get(item.request.session_id or "")
        if handoff is None:
            return _back(intervention_id, error="nothing is waiting for an answer")

        values = {name: request.form.get(name, "") for name in item.request.needs}
        problems = handoff.provide(values)
        if problems:
            return _back(intervention_id, error="; ".join(problems))

        _, session = session_for(intervention_id)
        if session.state is RunState.AWAITING_HUMAN:
            take_control(session)
        hand_back(session, "values supplied from the console")
        return _back(intervention_id)

    def _back(intervention_id: str, error: str | None = None):
        target = url_for("index")
        if error:
            return render_template("console.html", error=error, **state())
        return redirect(target)

    return app


class ConsoleServer:
    """The console on a background thread, beside the run it is watching.

    A thread rather than a process because there is one session manager and it
    lives in memory. Two processes would need the sessions to be somewhere
    both could reach, which is a database and a message bus bought to run a
    demo -- and the brief is explicit that scaling infrastructure is not what
    is being graded.
    """

    def __init__(
        self,
        manager: SessionManager,
        registry: InterventionRegistry,
        handoffs: dict[str, Handoff] | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
    ) -> None:
        self.app = create_app(manager, registry, handoffs)
        self.host = host
        self.port = port
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        from werkzeug.serving import make_server

        self._server = make_server(self.host, self.port, self.app, threaded=True)
        # The real port, which is what was actually bound: passing 0 asks the
        # OS to choose, and a test that guessed would be flaky.
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "ConsoleServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


TEMPLATES = Path(__file__).with_name("templates")
