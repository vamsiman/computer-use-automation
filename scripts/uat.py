"""Manual acceptance runs, with the operator console alongside.

    python scripts/uat.py --case handoff

Every case starts the target application, the operator console, and a headed
browser, then replays the capability against a trigger chosen to produce one
particular kind of answer. The browser window that opens is the live session.

    --list      show the cases
    --case X    run one
    --headless  no visible browser (not much use for the handoff cases)

The handoff case additionally reports what the human-action watcher saw at
each stage, because a live run found it capturing nothing while the automation
thread was parked -- which no test covers, since the test operator clicks
synchronously on the automation's own thread.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from cua.artifact.overrides import for_tenant  # noqa: E402
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

APP_PORT = int(os.environ.get("UAT_APP_PORT", 5000))
CONSOLE_PORT = int(os.environ.get("UAT_CONSOLE_PORT", 5100))

os.environ.setdefault("CUA_HANDOFF_TIMEOUT", "1800")


#: name -> (member id, what to arm first, what you should see, needs a person)
CASES: dict[str, tuple[str | None, str | None, str, bool]] = {
    "success": ("10002", None, "Success, balance 982.14", False),
    "not-found": ("99999", None, "BusinessOutcome MEMBER_NOT_FOUND -- an answer, not a failure", False),
    "permission": ("10003", None, "BusinessOutcome PERMISSION_DENIED", False),
    "recovery": ("10004", None, "Success, after dismissing a declared notice", False),
    "expired": ("10001", "/debug/expire", "Success, after re-authenticating mid-flow", False),
    "slow": ("10001", "/debug/slow?ms=5000&path=/members/10001", "Success, after waiting out a real stall", False),
    "failure": ("10006", None, "Failure LOCATOR_UNRESOLVED -- no savings row exists", False),
    "contract": ("abc", None, "Failure CONTRACT, 0 steps -- refused before the browser moved", False),
    "handoff": ("10005", None, "Escalated, then YOU take over in the console", True),
    "ask": (None, None, "The run asks the console for a member id, as a typed field", True),
    "tenant": ("10001", None, "Success on riverbend with NO override -- s2 DEGRADED", False),
    "tenant-fixed": ("10001", None, "Same deployment, override applied -- s2 back on tier 0", False),
}


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


class ReportingHandoff(Handoff):
    """A handoff that says what the watcher knew, and when.

    Instrumentation rather than production code: the question is whether a
    real mouse click, made while this thread is parked, survives to the drain.
    """

    def _start_watching(self, intervention):
        super()._start_watching(intervention)
        say(f"  [watch] supported={self.watcher.supported} "
            f"watching={self.watcher.watching}")
        self._dump("after watch_start")

    def _settle(self, intervention, came_back):
        say(f"  [watch] woke up, came_back={came_back}")
        self._dump("before drain")
        result = super()._settle(intervention, came_back)
        say(f"  [watch] drained {len(intervention.human_actions)} record(s)")
        for action in intervention.human_actions:
            say(f"          {action.describe()}  ref={action.ref}")
        return result

    def _dump(self, stage: str) -> None:
        page = getattr(self.session.surface, "page", None)
        if page is None:
            return
        probe = """() => {
            let s = null;
            try { s = {flag: sessionStorage.getItem('__cua_watch'),
                       log: sessionStorage.getItem('__cua_watch_log')}; }
            catch (e) { s = {error: String(e)}; }
            return {url: location.href,
                    installed: !!window.__cuaWatchInstalled,
                    api: typeof window.__cuaWatch, store: s};
        }"""
        for i, frame in enumerate(page.frames):
            try:
                d = frame.evaluate(probe)
            except Exception as exc:
                say(f"  [watch] {stage} frame[{i}] probe failed: {exc}")
                continue
            store = d["store"] or {}
            log = store.get("log")
            say(f"  [watch] {stage} frame[{i}] {d['url']}")
            say(f"          installed={d['installed']} api={d['api']} "
                f"flag={store.get('flag')!r} log={'(empty)' if not log else log[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="handoff")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    if args.list:
        say("cases:\n")
        for name, (member, arm, expect, manual) in CASES.items():
            flag = "  [needs you]" if manual else ""
            say(f"  {name:<12} member={member or '(none)':<7} {expect}{flag}")
        return 0

    if args.case not in CASES:
        say(f"unknown case {args.case!r}; try --list")
        return 2

    member, arm, expect, manual = CASES[args.case]
    tenant = "riverbend" if args.case.startswith("tenant") else "base"

    seed.seed()
    exceptional.reset_all()
    os.environ["TENANT"] = tenant

    url = f"http://localhost:{APP_PORT}"
    app_server = make_server("127.0.0.1", APP_PORT, create_app(), threaded=True)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()

    manager = SessionManager()
    registry = InterventionRegistry()
    handoffs: dict[str, Handoff] = {}
    console = ConsoleServer(manager, registry, handoffs, port=CONSOLE_PORT)
    console.start()

    say("=" * 70)
    say(f"  case      {args.case}   (tenant: {tenant})")
    say(f"  expect    {expect}")
    say(f"  app       {url}")
    say(f"  console   {console.url}")
    say("=" * 70)

    session = manager.create(url, headless=args.headless)
    credentials = Credentials(user=APP_USER, password=APP_PASS)
    authenticate(session.surface, credentials)

    if arm:
        say(f"arming {arm}")
        session.surface.act(Action(type=ActionType.NAVIGATE, args={"path": arm}))
        if "expire" in arm:
            say("  (session dropped; the run must notice and recover)")
        else:
            authenticate(session.surface, credentials)

    run = Session(
        id=args.case, surface=session.surface, browser=session.browser, base_url=url
    )
    manager._sessions[run.id] = run
    run.start(run_id=args.case)
    watch_for_a_closed_window(session, run)

    library = CapabilityStore()
    artifact = library.load("member.read_savings_balance")
    if args.case == "tenant":
        # A deployment with no override of its own: the capability meeting a
        # differently-worded screen it was never prepared for.
        artifact = for_tenant(library, artifact, "cu-riverbend-unprepared")
    elif args.case == "tenant-fixed":
        artifact = for_tenant(library, artifact, "cu-riverbend")

    inputs = {} if member is None else {"member_id": member}
    recorder = Recorder.start(
        artifact=artifact,
        inputs=inputs,
        config=EvidenceConfig(root=Path("evidence/runs")),
    )
    handoff = ReportingHandoff(
        run,
        registry=registry,
        recorder=recorder,
        inputs=inputs,
        capability=artifact.ref,
    )
    handoffs[run.id] = handoff

    if manual:
        def guide():
            for _ in range(240):
                if run.state.value == "awaiting_human":
                    break
                time.sleep(0.5)
            else:
                return
            say("")
            say("*" * 70)
            say("  THE RUN HAS STOPPED AND IS WAITING FOR YOU")
            say("")
            say(f"  Console:  {console.url}")
            if args.case == "handoff":
                say("")
                say("  1. in the CONSOLE press 'Take control'")
                say("  2. in the BROWSER window press 'Acknowledge'")
                say("     (aim carefully -- clicking the dialog text does nothing)")
                say("  3. in the CONSOLE press 'Hand back'")
                say("")
                say("  Try it the other way once too: hand back WITHOUT")
                say("  acknowledging. The run re-checks, finds the hold still")
                say("  there, and stops again instead of reporting success.")
            else:
                say("")
                say("  The run needs a member id. The console shows a typed")
                say("  field for it -- try 'abc' first and watch the contract")
                say("  refuse it, then '10002'.")
            say("*" * 70)
            try:
                webbrowser.open(console.url)
            except Exception:
                pass

        threading.Thread(target=guide, daemon=True).start()

    try:
        result = ReplayEngine(
            run.controlled,
            artifact,
            policy=policy_for(url),
            recorder=recorder,
            session_id=run.id,
            escalate=handoff,
            reauthenticate=lambda: authenticate(session.surface, credentials),
        ).run(inputs)

        say("")
        say("=" * 70)
        say(f"  RESULT    {type(result).__name__}")
        for field in ("code", "message", "category", "reason", "observed", "expected"):
            value = getattr(result, field, None)
            if value:
                say(f"  {field:<9} {value}")
        if getattr(result, "outputs", None):
            for name, value in result.outputs.items():
                say(f"  output    {name} = {value}")
        say(f"  steps     {result.steps_executed}")
        for entry in result.tier_log:
            mark = "   <-- DEGRADED" if entry.degraded else ""
            say(f"    {entry.step_id}  {entry.strategy}{mark}")
        if result.recoveries:
            say(f"  recovered {', '.join(result.recoveries)}")
        for item in handoff.history:
            say(f"  handoff   {item.state}, re-verified={item.verified}, "
                f"{len(item.human_actions)} human action(s)")
        say(f"  evidence  {recorder.dir}")
        say(f"  expected  {expect}")
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
