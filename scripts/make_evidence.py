"""Produce the evidence bundles the deliverable calls for.

Every run here is real: a browser, the live target app, the real replay
engine. Nothing is staged.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, ".")
os.environ["CUA_HEADLESS"] = "1"

from werkzeug.serving import make_server  # noqa: E402

from cua.artifact.overrides import for_tenant  # noqa: E402
from cua.artifact.store import CapabilityStore  # noqa: E402
from cua.evidence import EvidenceConfig, Recorder  # noqa: E402
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

ROOT = Path("evidence/runs")
CREDENTIALS = Credentials(user=APP_USER, password=APP_PASS)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(tenant: str = "base"):
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


def go(surface, path):
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": path}))


class Desk(Handoff):
    """The operator, on the automation's thread (Playwright owns that thread)."""

    def __init__(self, *args, do=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.do = do

    def _wait(self) -> bool:
        take_control(self.session)
        if self.do:
            self.do(self.session.surface)
        hand_back(self.session, "handled at the desk")
        return True


def record(name, url, artifact, inputs, *, arm=None, handoff=False, tenant=None):
    """One real replay, with its evidence bundle named for what it shows."""
    manager = SessionManager()
    session = manager.create(url, headless=True)
    try:
        go(session.surface, "/debug/reset")
        authenticate(session.surface, CREDENTIALS)
        if arm:
            go(session.surface, arm)
            authenticate(session.surface, CREDENTIALS)
        run = Session(
            id=name, surface=session.surface, browser=session.browser, base_url=url
        )
        run.start(run_id=name)

        recorder = Recorder.start(
            artifact=artifact,
            inputs=inputs,
            run_id=name,
            tenant=tenant,
            config=EvidenceConfig(root=ROOT),
        )
        escalate = None
        if handoff:
            from cua.locators import Locator, RoleNameSpec

            ack = Locator(primary=RoleNameSpec(role="button", name="Acknowledge"))
            escalate = Desk(
                run,
                registry=InterventionRegistry(),
                recorder=recorder,
                inputs=inputs,
                capability=artifact.ref,
                do=lambda s: s.act(Action(type=ActionType.CLICK, target=ack)),
            )

        result = ReplayEngine(
            run.controlled,
            artifact,
            policy=policy_for(url),
            recorder=recorder,
            session_id=run.id,
            escalate=escalate,
            reauthenticate=lambda: authenticate(session.surface, CREDENTIALS),
        ).run(inputs)

        recorder.finish(result)
        print(f"  {name:<28} {result.kind:<16} {getattr(result, 'outputs', '')}")
        return result
    finally:
        manager.close_all()


def main() -> int:
    seed.seed()
    exceptional.reset_all()
    if ROOT.exists():
        for child in ROOT.iterdir():
            if child.is_dir() and not child.name.startswith("2026"):
                shutil.rmtree(child)

    library = CapabilityStore()
    artifact = library.load("member.read_savings_balance")

    server, url = serve("base")
    rb_server, rb_url = serve("riverbend")
    try:
        print("evidence runs:")
        record("01-success", url, artifact, {"member_id": "10002"})
        record("02-business-outcome-not-found", url, artifact, {"member_id": "99999"})
        record("03-business-outcome-permission", url, artifact, {"member_id": "10003"})
        record("04-recovery-interstitial", url, artifact, {"member_id": "10004"})
        record(
            "05-recovery-session-expired",
            url,
            artifact,
            {"member_id": "10001"},
            arm="/debug/expire",
        )
        record(
            "06-escalation-handoff",
            url,
            artifact,
            {"member_id": "10005"},
            handoff=True,
        )
        record("07-failure-no-savings-row", url, artifact, {"member_id": "10006"})
        record("08-contract-refused", url, artifact, {"member_id": "abc"})
        record(
            "09-tenant-riverbend-unprepared",
            rb_url,
            artifact,
            {"member_id": "10001"},
            tenant="riverbend (no override)",
        )
        record(
            "10-tenant-riverbend-overridden",
            rb_url,
            for_tenant(library, artifact, "cu-riverbend"),
            {"member_id": "10001"},
            tenant="cu-riverbend",
        )
    finally:
        server.shutdown()
        rb_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
