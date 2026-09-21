"""The reference artifact is checked against the real app, not just parsed.

An artifact that validates but whose locators do not resolve is worse than no
artifact: it looks like a working capability right up until someone invokes it.
So every target, checkpoint, outcome detector and recovery detector in the
shipped artifact gets resolved against the live target app here, and the tier
each one lands on is asserted.

Asserting the *tier* rather than merely "it resolved" is the point. Tiers are
how drift is meant to be visible, and a test that only checks resolution would
stay green while a capability quietly degraded from semantic targeting to
counting cells.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.artifact import CapabilityStore, validate
from cua.locators import LabelProximitySpec, Locator, RoleNameSpec
from cua.primitives import Action
from cua.surface.web import BrowserSession
from cua.types import LocatorStrategy, ActionType
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser

CAPABILITY_ID = "member.read_savings_balance"


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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def surface(live_app):
    session = BrowserSession(live_app, headless=True)
    surf = session.start()
    surf.act(Action(type=ActionType.NAVIGATE, args={"path": "/"}))
    surf.act(
        Action(
            type=ActionType.TYPE,
            target=Locator(
                primary=LabelProximitySpec(label="User ID:", direction="right")
            ),
            args={"value": APP_USER},
        )
    )
    surf.act(
        Action(
            type=ActionType.TYPE,
            target=Locator(
                primary=LabelProximitySpec(label="Password:", direction="right")
            ),
            args={"value": APP_PASS},
        )
    )
    surf.act(
        Action(
            type=ActionType.CLICK,
            target=Locator(primary=RoleNameSpec(role="button", name="Sign In")),
        )
    )
    try:
        yield surf
    finally:
        surf.close()
        session.stop()


@pytest.fixture(scope="module")
def artifact():
    return CapabilityStore().load(CAPABILITY_ID)


def goto(surface, path: str):
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": path}))
    return surface.observe()


def test_reference_artifact_is_coherent(artifact):
    assert validate(artifact) == []
    assert artifact.capability.status == "approved"


def test_search_screen_locators_resolve_at_expected_tiers(surface, artifact):
    snap = goto(surface, "/members/search")

    checkpoint = surface.resolve(artifact.step("s1").checkpoint.target, snap)
    assert checkpoint.resolved and checkpoint.tier == 0

    # The interesting one. This field has no accessible name at all, so
    # role+name cannot reach it and the capability leads with the label rule
    # instead -- on its own primary, not degraded.
    #
    # An aspirational `role_name` primary used to sit above it, on the theory
    # that if the vendor ever labelled the field properly we would resolve a
    # tier better for free. It had to go: it never resolved, so every healthy
    # run reported this step degraded, and a drift signal that is always on is
    # not a signal. A chain holds rules that have been seen to work.
    field = surface.resolve(artifact.step("s2").target, snap)
    assert field.resolved
    assert field.tier == 0
    assert field.strategy is LocatorStrategy.LABEL_PROXIMITY
    assert not field.degraded

    button = surface.resolve(artifact.step("s3").target, snap)
    assert button.resolved and button.tier == 0


def test_detail_screen_locators_resolve(surface, artifact):
    snap = goto(surface, "/members/10001")

    arrival = surface.resolve(artifact.step("s3").checkpoint.target, snap)
    assert arrival.resolved and arrival.tier == 0

    name = surface.resolve(artifact.step("s4").target, snap)
    assert name.resolved
    assert name.handle.name == "Dana Whitfield"

    balance = surface.resolve(artifact.step("s5").target, snap)
    assert balance.resolved
    assert balance.handle.name == "4,210.33"

    success = surface.resolve(artifact.success.checkpoint.target, snap)
    assert success.resolved


def test_every_declared_outcome_detector_fires_on_its_trigger(surface, artifact):
    """Each declared business outcome must be detectable, or the capability
    will report a crash where it promised an answer."""
    detectors = {o.code: o.detect for o in artifact.outcomes}

    def search(member_id: str):
        goto(surface, "/members/search")
        surface.act(
            Action(
                type=ActionType.TYPE,
                target=artifact.step("s2").target,
                args={"value": member_id},
            )
        )
        surface.act(
            Action(type=ActionType.CLICK, target=artifact.step("s3").target)
        )
        return surface.observe()

    snap = search("99999")
    assert surface.resolve(detectors["MEMBER_NOT_FOUND"], snap).resolved

    snap = search("abc")
    assert surface.resolve(detectors["VALIDATION_ERROR"], snap).resolved

    snap = goto(surface, "/members/10003")
    assert surface.resolve(detectors["PERMISSION_DENIED"], snap).resolved


def test_outcome_detectors_do_not_fire_on_a_normal_screen(surface, artifact):
    """A detector that matches everything is worse than none: it would turn
    every successful run into a business outcome."""
    snap = goto(surface, "/members/10001")
    for outcome in artifact.outcomes:
        assert not surface.resolve(outcome.detect, snap).resolved, outcome.code


def test_known_interstitial_is_detected_and_dismissible(surface, artifact):
    recovery = next(r for r in artifact.recoveries if r.code == "SYSTEM_NOTICE")

    snap = goto(surface, "/members/10004")
    assert surface.resolve(recovery.detect, snap).resolved

    dismissed = surface.act(
        Action(type=ActionType.CLICK, target=recovery.dismiss_via)
    )
    assert dismissed.ok
    assert surface.observe().find("heading", "Member Details")


def test_the_dismiss_recovery_does_not_match_a_compliance_hold(surface, artifact):
    """The load-bearing safety property of the whole error taxonomy.

    The compliance hold renders through the same template as the maintenance
    notice: same role, same button id, same layout. Only the name differs. If
    the recovery matched on being a dialog rather than on being *that* dialog,
    replay would click Acknowledge on a compliance flag unattended and report
    success. It must escalate instead.
    """
    recovery = next(r for r in artifact.recoveries if r.code == "SYSTEM_NOTICE")

    snap = goto(surface, "/members/10005")
    assert snap.modal_text == "Compliance Hold", "the hold should be blocking"
    assert not surface.resolve(recovery.detect, snap).resolved

    # And nothing else in the artifact claims to understand this state, which
    # is what makes it an escalation rather than a silent mishandling.
    for outcome in artifact.outcomes:
        assert not surface.resolve(outcome.detect, snap).resolved
    for other in artifact.recoveries:
        assert not surface.resolve(other.detect, snap).resolved


def test_session_expiry_recovery_detector_fires(surface, artifact):
    recovery = next(r for r in artifact.recoveries if r.code == "SESSION_EXPIRED")

    goto(surface, "/debug/expire")
    snap = goto(surface, "/members/10001")
    assert surface.resolve(recovery.detect, snap).resolved

    # Put the session back for any later test in this module.
    goto(surface, "/")
    surface.act(
        Action(
            type=ActionType.TYPE,
            target=Locator(
                primary=LabelProximitySpec(label="User ID:", direction="right")
            ),
            args={"value": APP_USER},
        )
    )
    surface.act(
        Action(
            type=ActionType.TYPE,
            target=Locator(
                primary=LabelProximitySpec(label="Password:", direction="right")
            ),
            args={"value": APP_PASS},
        )
    )
    surface.act(
        Action(
            type=ActionType.CLICK,
            target=Locator(primary=RoleNameSpec(role="button", name="Sign In")),
        )
    )
    assert goto(surface, "/members/10001").find("heading", "Member Details")
