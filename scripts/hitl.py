"""Human-in-the-loop, narrated, at the speed of a person.

The other scripts run the system. This one *shows you the handoff*, which is a
different job: it prints every state change as it happens, says who holds the
session at each moment, and waits for you with no time pressure.

    python scripts/hitl.py

What you should see, in order:

    1. the automation drives itself to a compliance hold nobody declared
    2. it STOPS -- and refuses to click through it
    3. control is nobody's: the automation has stopped, you have not started
    4. you take control in the console; the automation is now *refused*
    5. you press Acknowledge in the browser with your own hand
    6. you hand back; the engine re-checks the screen for ITSELF
    7. only then does it carry on and finish

Step 4 is the one worth watching. While you hold the session the script tries
an automated click on your behalf and is refused by the control token -- not
discouraged, refused, with the exception printed.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from cua.artifact.store import CapabilityStore  # noqa: E402
from cua.evidence import EvidenceConfig, Recorder  # noqa: E402
from cua.locators import Locator, RoleNameSpec  # noqa: E402
from cua.operator import ConsoleServer  # noqa: E402
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy  # noqa: E402
from cua.primitives import Action  # noqa: E402
from cua.replay import ReplayEngine  # noqa: E402
from cua.session import (  # noqa: E402
    ControlViolation,
    Credentials,
    Handoff,
    InterventionRegistry,
    Session,
    SessionManager,
    authenticate,
)
from cua.types import ActionType, RiskLevel  # noqa: E402
from targetapp import exceptional, seed  # noqa: E402
from targetapp.app import APP_PASS, APP_USER, create_app  # noqa: E402

MEMBER = "10005"
ACKNOWLEDGE = Locator(primary=RoleNameSpec(role="button", name="Acknowledge"))
CREDENTIALS = Credentials(user=APP_USER, password=APP_PASS)

# No time pressure. A person reading a compliance hold is not a test.
os.environ["CUA_HANDOFF_TIMEOUT"] = "3600"

SHUTTING_DOWN = threading.Event()


def say(text: str = "") -> None:
    print(text, flush=True)


def banner(*lines: str) -> None:
    say("")
    say("+" + "-" * 68 + "+")
    for line in lines:
        say("| " + line.ljust(66) + " |")
    say("+" + "-" * 68 + "+")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def policy_for(url: str) -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(url,),
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


#: What each state means, said in words rather than in enum names.
MEANING = {
    "pending": "not started",
    "running": "THE AUTOMATION is driving",
    "awaiting_human": "NOBODY is driving -- it stopped and is asking for a person",
    "human_control": "YOU are driving. The automation is refused.",
    "verifying": "control is back, but the engine has not trusted it yet",
    "succeeded": "finished",
    "aborted": "cancelled",
    "failed": "failed",
    "business_outcome": "the application gave a definite answer",
}


def narrate(session, console_url: str) -> None:
    """Print every state change the moment it happens.

    Reading the history rather than hooking the transitions, because the point
    is that the state machine is observable from outside -- which is the same
    reason the console can exist at all.
    """
    seen = 0
    while not SHUTTING_DOWN.is_set():
        history = list(session.history)
        for change in history[seen:]:
            state = change.to.value
            say("")
            say(f"  >> {change.frm.value}  ->  {state}")
            say(f"     {MEANING.get(state, state)}")
            say(f"     control: {change.control.value}     ({change.reason})")
            if state == "awaiting_human":
                banner(
                    "THE RUN HAS STOPPED AND IS WAITING FOR YOU.",
                    "",
                    "It met a compliance hold that the capability does not",
                    "declare, so it refused to click through it.",
                    "",
                    f"CONSOLE:  {console_url}",
                    "",
                    "  1. press 'Take control' in the console",
                    "  2. press 'Acknowledge' in the BROWSER window",
                    "  3. press 'Hand back' in the console",
                    "",
                    "Take as long as you like. Nothing times out.",
                )
        seen = len(history)
        time.sleep(0.2)


class NarratedHandoff(Handoff):
    """The real handoff, with a running commentary."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refused: str | None = None
        self.proved_refusal = False

    def _wait(self) -> bool:
        # While the person holds the session, prove the control token is real
        # rather than decorative: try to act, and be refused.
        threading.Thread(target=self._prove_refusal, daemon=True).start()
        return super()._wait()

    def _prove_refusal(self) -> None:
        for _ in range(3600):
            if SHUTTING_DOWN.is_set():
                return
            if self.session.state.value == "human_control":
                break
            time.sleep(0.2)
        else:
            return
        try:
            # Deliberately through the guarded surface, which is what the
            # engine is handed. The raw surface would let this through, and
            # that difference is the whole point of the guard.
            self.session.controlled.act(
                Action(type=ActionType.CLICK, target=ACKNOWLEDGE)
            )
            self.refused = "NOT REFUSED -- the guard did not hold"
        except ControlViolation as exc:
            self.refused = str(exc)
            self.proved_refusal = True
        banner(
            "WHILE YOU HOLD THE SESSION, THE AUTOMATION TRIED TO ACT.",
            "",
            "It was refused:",
            f"  {self.refused[:62]}",
            "",
            "Not discouraged by a convention. Refused by the control token,",
            "which is checked on every single act.",
        )

    def _settle(self, intervention, came_back):
        result = super()._settle(intervention, came_back)
        actions = intervention.human_actions
        say("")
        say(f"  >> you handed the session back. Recorded {len(actions)} "
            f"human action(s):")
        for action in actions:
            say(f"       {action.describe()}")
        if not actions:
            say("       (none -- this is the known gap in human-action capture;")
            say("        the state machine below is unaffected)")
        return result

    def resolved(self, verified: bool) -> None:
        say("")
        say(f"  >> the engine re-checked the screen ITSELF: "
            f"{'it is where the step expected' if verified else 'NOT what it expected'}")
        if not verified:
            say("     so it stops and asks again, rather than taking your word")
        super().resolved(verified)


def main() -> int:
    seed.seed()
    exceptional.reset_all()

    port = free_port()
    app_server = make_server("127.0.0.1", port, create_app(), threaded=True)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"

    manager = SessionManager()
    registry = InterventionRegistry()
    handoffs: dict = {}
    console = ConsoleServer(manager, registry, handoffs, port=free_port())
    console.start()

    banner(
        "HUMAN IN THE LOOP -- a walkthrough",
        "",
        f"application  {url}",
        f"console      {console.url}",
        "",
        "A browser window is about to open. THAT WINDOW IS THE LIVE",
        "SESSION -- not a picture of one. When the run stops, you will",
        "act in it directly.",
    )

    session = manager.create(url, headless=False)
    authenticate(session.surface, CREDENTIALS)

    run = Session(
        id="hitl", surface=session.surface, browser=session.browser, base_url=url
    )
    manager._sessions[run.id] = run

    threading.Thread(target=narrate, args=(run, console.url), daemon=True).start()
    run.start(run_id="hitl")

    artifact = CapabilityStore().load("member.read_savings_balance")
    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": MEMBER},
        run_id="hitl-walkthrough",
        config=EvidenceConfig(root=Path("evidence/runs")),
    )
    handoff = NarratedHandoff(
        run,
        registry=registry,
        recorder=recorder,
        inputs={"member_id": MEMBER},
        capability=artifact.ref,
    )
    handoffs[run.id] = handoff

    say("")
    say(f"the automation is now driving, looking up member {MEMBER}...")
    try:
        webbrowser.open(console.url)
    except Exception:
        pass

    try:
        result = ReplayEngine(
            run.controlled,
            artifact,
            policy=policy_for(url),
            recorder=recorder,
            session_id=run.id,
            escalate=handoff,
        ).run({"member_id": MEMBER})

        recorder.finish(result)
        item = handoff.history[0] if handoff.history else None
        guard_note = (
            "refused the automation while you held it"
            if handoff.proved_refusal
            else "not exercised this run"
        )
        banner(
            "WHAT JUST HAPPENED",
            "",
            f"result            {type(result).__name__}",
            f"outputs           {getattr(result, 'outputs', {}) or '-'}",
            "",
            "the automation    drove to the hold, then STOPPED",
            "you               took the session and cleared the hold",
            "the engine        re-checked the screen before continuing",
            "",
            f"intervention      {item.state if item else '-'}, "
            f"re-verified={item.verified if item else '-'}",
            f"control token     {guard_note}",
            f"human actions     {len(item.human_actions) if item else 0} recorded",
            "",
            f"evidence          {recorder.dir}",
        )
        say("")
        say("states this run passed through, in order:")
        for change in run.history:
            say(f"   {change.frm.value:<16} -> {change.to.value:<16} "
                f"({change.control.value})")
        say("")
        return 0 if result.ok else 1
    except KeyboardInterrupt:
        say("\nstopping")
        return 1
    finally:
        SHUTTING_DOWN.set()
        manager.close_all()
        console.stop()
        app_server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
