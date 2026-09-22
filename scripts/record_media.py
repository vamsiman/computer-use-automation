"""Record the system doing its job, for people who will not run it.

Produces, under ``evidence/media/``:

* ``*.webm``  -- full video of the browser session for each scenario
* ``*.gif``   -- the same run as an inline-viewable animation, built from the
                 per-step screenshots the recorder already captures
* ``console-*.png`` -- the operator console with a real intervention open

The handoff recording has a scripted operator standing in for a mouse, and
says so on the page it is recorded from. That is an honest substitution --
the pause, the control token, the re-verification and the evidence are all
the production path -- but it is a substitution, and a recording that implied
a person was sitting there would be a lie about what was demonstrated.
"""

from __future__ import annotations

import shutil
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from cua.artifact.overrides import for_tenant  # noqa: E402
from cua.artifact.store import CapabilityStore  # noqa: E402
from cua.evidence import EvidenceConfig, Recorder  # noqa: E402
from cua.locators import Locator, RoleNameSpec  # noqa: E402
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
    hand_back,
    take_control,
)
from cua.types import ActionType, RiskLevel  # noqa: E402
from targetapp import exceptional, seed  # noqa: E402
from targetapp.app import APP_PASS, APP_USER, create_app  # noqa: E402

MEDIA = Path("evidence/media")
ACKNOWLEDGE = Locator(primary=RoleNameSpec(role="button", name="Acknowledge"))
CREDENTIALS = Credentials(user=APP_USER, password=APP_PASS)

#: Slow the automation down. Replay at full speed is a blur of three page
#: loads, and a recording nobody can follow demonstrates nothing.
BEAT = 1.2

#: The console reads sessions out of a manager. It has to be the same one
#: the runs register into, or it lists an intervention whose session is gone.
CONSOLE_MANAGER = SessionManager()


def say(text: str = "") -> None:
    print(text, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(tenant: str = "base"):
    import os

    previous = os.environ.get("TENANT")
    os.environ["TENANT"] = tenant
    try:
        port = free_port()
        server = make_server("127.0.0.1", port, create_app(), threaded=True)
    finally:
        if previous is None:
            os.environ.pop("TENANT", None)
        else:
            os.environ["TENANT"] = previous
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


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


class PacedEngine(ReplayEngine):
    """The real engine, with a pause between steps so a viewer can follow."""

    def _run_step(self, step, inputs):
        time.sleep(BEAT)
        return super()._run_step(step, inputs)


class RecordedOperator(Handoff):
    """Stands in for a mouse, on the thread that owns the browser."""

    def _wait(self) -> bool:
        take_control(self.session)
        time.sleep(BEAT * 2)  # a person reading the hold
        self.session.surface.act(
            Action(type=ActionType.CLICK, target=ACKNOWLEDGE)
        )
        time.sleep(BEAT)
        hand_back(self.session, "acknowledged at the desk")
        return True


def gif_from(steps_dir: Path, out: Path, ms: int = 1400) -> bool:
    """Build an inline-viewable animation from the run's own screenshots.

    The screenshots are evidence the recorder captures anyway, so the GIF
    costs nothing extra and shows exactly the frames the run recorded --
    which is a more honest artefact than a video of a browser somebody could
    have driven by hand.
    """
    frames = sorted(steps_dir.glob("*.png"))
    if not frames:
        return False
    images = [Image.open(f).convert("P", palette=Image.ADAPTIVE) for f in frames]
    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=ms,
        loop=0,
        optimize=True,
    )
    return True


def record(name: str, url: str, artifact, inputs, *, handoff=False,
           console=None, manager_registry=None) -> None:
    videos = MEDIA / f"_raw_{name}"
    manager = SessionManager()
    session = manager.create(url, headless=True, video_dir=videos)
    registry, handoffs = manager_registry or (InterventionRegistry(), {})
    try:
        authenticate(session.surface, CREDENTIALS)
        run = Session(
            id=name, surface=session.surface, browser=session.browser, base_url=url
        )
        manager._sessions[run.id] = run
        CONSOLE_MANAGER._sessions[run.id] = run
        run.start(run_id=name)

        recorder = Recorder.start(
            artifact=artifact,
            inputs=inputs,
            run_id=f"media-{name}",
            config=EvidenceConfig(root=Path("evidence/runs")),
        )
        escalate = None
        if handoff:
            escalate = RecordedOperator(
                run,
                registry=registry,
                recorder=recorder,
                inputs=inputs,
                capability=artifact.ref,
            )
            handoffs[run.id] = escalate

        shot = None
        if console is not None and handoff:
            shot = threading.Thread(
                target=capture_console, args=(console, run, name), daemon=True
            )
            shot.start()

        result = PacedEngine(
            run.controlled,
            artifact,
            policy=policy_for(url),
            recorder=recorder,
            session_id=run.id,
            escalate=escalate,
        ).run(inputs)
        recorder.finish(result)
        if shot:
            shot.join(timeout=30)

        say(f"  {name:<24} {type(result).__name__:<16} {getattr(result, 'outputs', '')}")
        gif = MEDIA / f"{name}.gif"
        if gif_from(recorder.steps_dir, gif):
            say(f"  {'':<24} gif  -> {gif}")
    finally:
        manager.close_all()

    # Playwright names videos by an internal id; give it the scenario's name.
    for video in sorted(videos.glob("*.webm")):
        target = MEDIA / f"{name}.webm"
        shutil.move(str(video), target)
        say(f"  {'':<24} webm -> {target}")
        break
    shutil.rmtree(videos, ignore_errors=True)


def capture_console(console, run, name: str) -> None:
    """Photograph the console while an intervention is genuinely open.

    A second browser, because the first one is the live session and pointing
    it at the console would be a different demonstration entirely.
    """
    for _ in range(120):
        if run.state.value in ("awaiting_human", "human_control"):
            break
        time.sleep(0.25)
    else:
        return
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(console.url, wait_until="load")
            page.wait_for_timeout(500)
            out = MEDIA / f"console-{name}.png"
            page.screenshot(path=str(out), full_page=True)
            say(f"  {'':<24} console screenshot -> {out}")
            browser.close()
    except Exception as exc:
        say(f"  console screenshot failed: {exc}")


def main() -> int:
    seed.seed()
    exceptional.reset_all()
    MEDIA.mkdir(parents=True, exist_ok=True)

    library = CapabilityStore()
    artifact = library.load("member.read_savings_balance")

    base_server, base_url = serve("base")
    rb_server, rb_url = serve("riverbend")
    registry = InterventionRegistry()
    handoffs: dict = {}
    console = ConsoleServer(CONSOLE_MANAGER, registry, handoffs, port=free_port())
    console.start()

    say("recording:")
    try:
        record("01-happy-path", base_url, artifact, {"member_id": "10002"})
        record("02-business-outcome", base_url, artifact, {"member_id": "99999"})

        # The console needs to see the same manager the run registers into.
        manager_holder = (registry, handoffs)
        record(
            "03-escalation-handoff",
            base_url,
            artifact,
            {"member_id": "10005"},
            handoff=True,
            console=console,
            manager_registry=manager_holder,
        )
        record("04-failure", base_url, artifact, {"member_id": "10006"})
        record("05-tenant-degraded", rb_url, artifact, {"member_id": "10001"})
        record(
            "06-tenant-repaired",
            rb_url,
            for_tenant(library, artifact, "cu-riverbend"),
            {"member_id": "10001"},
        )
    finally:
        console.stop()
        base_server.shutdown()
        rb_server.shutdown()

    say("")
    for item in sorted(MEDIA.iterdir()):
        size = item.stat().st_size / 1024
        say(f"  {item.name:<32} {size:8.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
