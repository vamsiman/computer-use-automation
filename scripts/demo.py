"""Stand the whole stack up and leave it running, for a person to look at.

Starts the target application, opens a headed browser, replays the capability
against member 10005 -- who is flagged for compliance review, which nothing in
the artifact declares -- and stops. The browser window stays on the hold
screen; the operator console lists the paused run.

From there it is a real handoff: take control in the console, press
Acknowledge in the browser window, hand back, and watch the run re-verify the
screen for itself and finish.
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
from cua.primitives import Action  # noqa: E402
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

APP_PORT = 5000
CONSOLE_PORT = 5100
MEMBER = os.environ.get("DEMO_MEMBER", "10005")

# A person needs longer than a test does.
os.environ.setdefault("CUA_HANDOFF_TIMEOUT", "1800")


def policy_for(url: str) -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(url, f"http://localhost:{APP_PORT}"),
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


def main() -> int:
    seed.seed()
    exceptional.reset_all()

    url = f"http://localhost:{APP_PORT}"
    app_server = make_server("127.0.0.1", APP_PORT, create_app(), threaded=True)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    print(f"target application   {url}")

    manager = SessionManager()
    registry = InterventionRegistry()
    handoffs: dict[str, Handoff] = {}
    console = ConsoleServer(manager, registry, handoffs, port=CONSOLE_PORT)
    print(f"operator console     {console.start()}")

    # Headed on purpose. The window that opens IS the live session -- that is
    # what makes the handoff real rather than simulated.
    session = manager.create(url, headless=False)
    authenticate(session.surface, Credentials(user=APP_USER, password=APP_PASS))

    run = Session(
        id="demo", surface=session.surface, browser=session.browser, base_url=url
    )
    manager._sessions[run.id] = run
    run.start(run_id="demo")

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

    print(f"\nreplaying {artifact.capability.id} with member_id={MEMBER}")
    print("the browser window is the live session\n")

    result_box: dict[str, object] = {}

    def drive():
        result_box["result"] = ReplayEngine(
            run.controlled,
            artifact,
            policy=policy_for(url),
            recorder=recorder,
            session_id=run.id,
            escalate=handoff,
            reauthenticate=lambda: authenticate(
                session.surface, Credentials(user=APP_USER, password=APP_PASS)
            ),
        ).run({"member_id": MEMBER})

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()

    time.sleep(6)
    if run.state.value == "awaiting_human":
        print("=" * 68)
        print("  The run stopped. Member 10005 is flagged for compliance review")
        print("  and nothing in the capability declares that state, so it did")
        print("  not guess -- it asked.")
        print()
        print(f"  Console:  {console.url}")
        print()
        print("  1. press 'Take control'")
        print("  2. press 'Acknowledge' in the browser window")
        print("  3. press 'Hand back'")
        print()
        print("  The engine re-checks the screen for itself before continuing.")
        print("=" * 68)
    try:
        webbrowser.open(console.url)
    except Exception:
        pass

    try:
        while worker.is_alive():
            time.sleep(1)
        result = result_box.get("result")
        print(f"\n{'=' * 68}")
        print(f"  {type(result).__name__}: {getattr(result, 'outputs', '')}")
        print(f"  session state: {run.state.value}")
        print(f"  evidence: {recorder.dir}")
        print("=" * 68)
        recorder.finish(result)
        print("\nstack still up; ctrl-c to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        manager.close_all()
        console.stop()
        app_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
