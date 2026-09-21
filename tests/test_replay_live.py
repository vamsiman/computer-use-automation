"""Replay against the real browser and the real application.

The unit tests prove the engine's decisions. This proves the thing those
decisions are about: that the reference capability, written once, runs against
a live legacy app with a *different* member number and comes back with that
member's balance. Record once, replay many, with nothing in the loop that could
have worked it out on the day.

The tier log is checked here too, because this is the only place it means
anything. Against a hand-built tree every locator resolves however the fake was
written; against real markup, the search field genuinely has no accessible name
and the fallback genuinely earns its cost.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.artifact.store import CapabilityStore
from cua.evidence import EvidenceConfig, Recorder
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.replay import BusinessOutcome, Failure, ReplayEngine, Success
from cua.session import Credentials, authenticate
from cua.surface.web import BrowserSession
from cua.types import ActionType, FailureCategory, LocatorStrategy, RiskLevel
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser

CAPABILITY = "member.read_savings_balance"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_app():
    from werkzeug.serving import make_server

    seed.seed()
    exceptional.reset_all()

    port = _free_port()
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def surface(live_app):
    session = BrowserSession(live_app, headless=True)
    surf = session.start()
    try:
        authenticate(surf, Credentials(user=APP_USER, password=APP_PASS))
        yield surf
    finally:
        surf.close()
        session.stop()


@pytest.fixture(scope="module")
def policy(live_app) -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(live_app,),
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


@pytest.fixture
def artifact():
    return CapabilityStore().load(CAPABILITY)


def replay_for(surface, artifact, policy, member_id, **kwargs):
    return ReplayEngine(surface, artifact, policy=policy, **kwargs).run(
        {"member_id": member_id}
    )


# --- the whole point ------------------------------------------------------


def test_a_capability_recorded_once_runs_with_a_different_member(
    surface, artifact, policy
):
    """10001 was the member the capability was written against. 10002 is not,
    and the run has no model in it to work out the difference."""
    result = replay_for(surface, artifact, policy, "10002")

    assert isinstance(result, Success), getattr(result, "observed", result)
    assert result.outputs["member_name"] == "Marcus Ellery"
    assert result.outputs["savings_balance"] == "982.14"


def test_the_tier_log_says_how_each_control_was_found(surface, artifact, policy):
    """Against real markup this is a measurement rather than a restatement of
    how a fixture was written."""
    result = replay_for(surface, artifact, policy, "10001")
    assert isinstance(result, Success)

    strategy = {entry.step_id: entry.strategy for entry in result.tier_log}
    # The member-id field's label is a bare span in the neighbouring cell, so
    # role+name cannot describe it and the second rule does the work.
    assert strategy["s2"] == LocatorStrategy.LABEL_PROXIMITY.value
    # The search button is a real submit input with a value, so it can.
    assert strategy["s3"] == LocatorStrategy.ROLE_NAME.value
    # The accounts grid has no <th> at all; the row/column rule reconstructs it.
    assert strategy["s5"] == LocatorStrategy.ROW_CELL.value


def test_a_healthy_run_reports_nothing_degraded(surface, artifact, policy):
    """Every step on its own primary rule, against the application this
    capability was written for.

    This is what makes the tier log a drift signal rather than decoration.
    The artifact once carried an aspirational primary on s2 that could never
    resolve, so every successful run reported itself degraded -- and a warning
    that is always on is one nobody reads. The degradation that *should* show
    up is in ``test_tenant_live.py``, where the same artifact meets a
    deployment that renamed the field.
    """
    result = replay_for(surface, artifact, policy, "10001")

    assert result.degraded_steps == ()
    assert {e.step_id for e in result.tier_log} >= {"s2", "s3"}
    assert all(entry.tier == 0 for entry in result.tier_log), result.tier_log


# --- the other three results ---------------------------------------------


def test_no_such_member_is_a_business_outcome(surface, artifact, policy):
    """The application answered correctly. Nothing is broken, and raising an
    incident for this is how an automation cries wolf."""
    result = replay_for(surface, artifact, policy, "99999")

    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.ok is False


def test_a_member_with_no_savings_row_is_a_failure(surface, artifact, policy):
    """Nothing declared covers it and nothing is blocking the screen. The
    automation has met a world it was not told about, which is a failure and
    not an answer."""
    result = replay_for(surface, artifact, policy, "10006")

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.LOCATOR_UNRESOLVED
    assert result.step_id == "s5"
    assert "Savings" in result.expected or "Savings" in result.observed


def test_a_bad_input_never_reaches_the_browser(surface, artifact, policy):
    result = replay_for(surface, artifact, policy, "abc")

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.CONTRACT
    assert result.steps_executed == 0


# --- evidence -------------------------------------------------------------


def test_a_replay_leaves_a_complete_bundle(surface, artifact, policy, tmp_path):
    import json

    recorder = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10001"},
        config=EvidenceConfig(root=tmp_path / "runs"),
        mode="replay",
    )
    result = ReplayEngine(
        surface, artifact, policy=policy, recorder=recorder
    ).run({"member_id": "10001"})

    assert isinstance(result, Success)
    # Posix separators on purpose: this string is written into a JSON
    # deliverable that gets read on machines other than the one that ran.
    assert result.evidence_ref == recorder.dir.as_posix()
    assert (recorder.dir / "result.json").exists()

    written = json.loads((recorder.dir / "result.json").read_text(encoding="utf-8"))
    # The outputs are declared pii, so the bundle carries tokens rather than
    # the balance itself.
    assert written["outputs"]["savings_balance"].startswith("[pii:")
    assert "982.14" not in json.dumps(written)

    kinds = [
        json.loads(line)["kind"]
        for line in recorder.log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert kinds.count("policy") == len(artifact.steps)
    assert "step" in kinds
