"""Stand the whole stack up and leave it running, for a person to look at.

Starts the target application, opens a headed browser, replays the capability
against member 10005 -- who is flagged for compliance review, which nothing in
the artifact declares -- and stops. The browser window stays on the hold
screen; the operator console lists the paused run.

From there it is a real handoff: take control in the console, press
Acknowledge in the browser window, hand back, and watch the run re-verify the
screen for itself and finish.

**Which thread owns what.** Playwright's sync API belongs to the thread that
created the page, so the browser is created *and* driven on one thread and
everything else arranges itself around that. Here the automation runs on the
main thread and the console serves from another, which is the production shape
too -- it works because the console never touches the browser. It reads session
state and moves the control token; the only things that ever act on the page
are the automation and a person's actual mouse.

An earlier version of this script created the browser on the main thread and
replayed from a worker. It failed at step one with a greenlet error, which is
a good demonstration of why the rule is worth writing down.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from cua.artifact.store import CapabilityStore  # noqa: E402
from cua.evidence import EvidenceConfig, Recorder  # noqa: E402
from cua.operator import ConsoleServer  # noqa: E402
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy  # noqa: E402
from cua.replay import ReplayEngine  # noqa: E402
from cua.session import (  # noqa: E402
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

APP_PORT = int(os.environ.get("DEMO_APP_PORT", 5000))
CONSOLE_PORT = int(os.environ.get("DEMO_CONSOLE_PORT", 5100))
MEMBER = os.environ.get("DEMO_MEMBER", "10005")

# A person needs longer to read a screen than a test does.
os.environ.setdefault("CUA_HANDOFF_TIMEOUT", "1800")


def say(text: str = "") -> None:
    print(text, flush=True)


def policy_for(url: str) -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(url, f"http://127.0.0.1:{APP_PORT}"),
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


def watch_for_a_closed_window(session, run, console=None) -> None:
    """Notice when the person shuts the browser, and stop.

    Without this, closing the window leaves the run parked on its handoff
    timeout -- half an hour of a terminal that looks frozen and is not. The
    session is gone the moment the window is, so the honest response is to
    abort the run and say so.

    ``page.is_closed()`` reads a local flag rather than talking to the
    browser, so it is safe to poll from a thread that does not own the
    Playwright connection.
    """
    page = getattr(session.surface, "page", None)
    if page is None:
        return

    def poll() -> None:
        while True:
            try:
                closed = page.is_closed()
            except Exception:
                closed = True
            if closed:
                say("")
                say("!! the browser window was closed -- abandoning the run")
                try:
                    if not run.is_finished:
                        run.abort("the operator closed the browser window")
                except Exception:
                    pass
                return
            time.sleep(1)

    threading.Thread(target=poll, daemon=True).start()


def main() -> int:
    seed.seed()
    exceptional.reset_all()

    url = f"http://localhost:{APP_PORT}"
    app_server = make_server("127.0.0.1", APP_PORT, create_app(), threaded=True)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    say(f"target application   {url}")

    manager = SessionManager()
    registry = InterventionRegistry()
    handoffs: dict[str, Handoff] = {}
    console = ConsoleServer(manager, registry, handoffs, port=CONSOLE_PORT)
    say(f"operator console     {console.start()}")

    # Headed on purpose. The window that opens IS the live session -- that is
    # what makes the handoff real rather than simulated.
    session = manager.create(url, headless=False)
    credentials = Credentials(user=APP_USER, password=APP_PASS)
    authenticate(session.surface, credentials)
    say("signed in")

    run = Session(
        id="demo", surface=session.surface, browser=session.browser, base_url=url
    )
    manager._sessions[run.id] = run
    run.start(run_id="demo")
    watch_for_a_closed_window(session, run)

    artifact = CapabilityStore().load("member.read_savings_balance")
    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": MEMBER},
        config=EvidenceConfig(root=Path("evidence/runs")),
    )
    handoff = Handoff(
        run,
        registry=registry,
        recorder=recorder,
        inputs={"member_id": MEMBER},
        capability=artifact.ref,
    )
    handoffs[run.id] = handoff

    say(f"\nreplaying {artifact.capability.id} with member_id={MEMBER}")
    say("the browser window that just opened is the live session\n")

    def announce() -> None:
        """Tell the person what to do once the run has actually stopped.

        On its own thread because the automation thread is about to block
        inside the handoff, which is the entire point of the demo.
        """
        for _ in range(240):
            if run.state.value == "awaiting_human":
                break
            time.sleep(0.5)
        else:
            return
        say("=" * 70)
        say("  The run stopped.")
        say("")
        say(f"  Member {MEMBER} is flagged for compliance review, and nothing in")
        say("  the capability declares that state. So it did not guess -- it")
        say("  asked, and it is holding the session open while it waits.")
        say("")
        say(f"  Console:  {console.url}")
        say("")
        say("  1. press 'Take control' in the console")
        say("  2. press 'Acknowledge' in the browser window")
        say("  3. press 'Hand back' in the console")
        say("")
        say("  The engine re-checks the screen for itself before continuing.")
        say("  Hand back without acknowledging and it will stop and ask again.")
        say("=" * 70)
        try:
            webbrowser.open(console.url)
        except Exception:
            pass

    threading.Thread(target=announce, daemon=True).start()

    try:
        # On this thread, because this thread owns the browser.
        result = ReplayEngine(
            run.controlled,
            artifact,
            policy=policy_for(url),
            recorder=recorder,
            session_id=run.id,
            escalate=handoff,
            reauthenticate=lambda: authenticate(session.surface, credentials),
        ).run({"member_id": MEMBER})

        say("")
        say("=" * 70)
        say(f"  {type(result).__name__}   {getattr(result, 'outputs', '') or ''}")
        say(f"  session state  {run.state.value}")
        for entry in result.tier_log:
            mark = "  <- degraded" if entry.degraded else ""
            say(f"    {entry.step_id}  {entry.strategy}{mark}")
        if handoff.history:
            item = handoff.history[0]
            say(f"  intervention   {item.state}, re-verified: {item.verified}")
            for action in item.human_actions:
                say(f"    human: {action.describe()}")
        say(f"  evidence       {recorder.dir}")
        say("=" * 70)
        recorder.finish(result)
        say("\nstack still up -- ctrl-c to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("\nstopping")
    finally:
        manager.close_all()
        console.stop()
        app_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
