"""Every exceptional state has to be reachable on demand.

This file is the executable version of the trigger table in
``targetapp/README.md``. The replay engine's error-taxonomy tests will assert
that each of these maps to the right *result variant* -- a business outcome, a
recovery, an escalation or a hard failure. Here we only prove the app can
actually produce them, which is the prerequisite.

The distinctions being pinned down:

- a malformed id and an unknown id are different conditions, not one
- an unknown id is a legitimate answer, not a crash
- the known maintenance notice and the compliance hold render identically
  except for their name, so a detector cannot tell them apart by shape
- lockout survives a fresh session, so it is a hard failure rather than
  something to retry
"""

import pytest

from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    seed.seed()


@pytest.fixture
def app():
    app = create_app()
    app.config.update(TESTING=True)
    return app


@pytest.fixture(autouse=True)
def _clean_lockouts():
    exceptional.reset_all()
    yield
    exceptional.reset_all()


@pytest.fixture
def signed_in(app):
    client = app.test_client()
    client.post("/", data={"user": APP_USER, "pwd": APP_PASS})
    return client


def text(response) -> str:
    return response.get_data(as_text=True)


# --- declared business outcomes ------------------------------------------


def test_unknown_member_id_reports_no_records(signed_in):
    """A well-formed id that matches nothing. This is an answer about the
    world, not a malfunction."""
    body = text(signed_in.post("/members/search", data={"mbr": "99999"}))
    assert exceptional.MSG_NOT_FOUND in body
    assert 'role="alert"' in body


@pytest.mark.parametrize("bad", ["abc", "123", "", "1234567", "10a01"])
def test_malformed_member_id_reports_a_validation_error(signed_in, bad):
    """Distinct from not-found: the caller sent us garbage."""
    body = text(signed_in.post("/members/search", data={"mbr": bad}))
    assert exceptional.MSG_VALIDATION in body
    assert exceptional.MSG_NOT_FOUND not in body


def test_validation_runs_before_lookup(signed_in):
    """Order matters. A malformed id must never be reported as not-found."""
    body = text(signed_in.post("/members/search", data={"mbr": "abc"}))
    assert exceptional.MSG_VALIDATION in body


def test_restricted_member_reports_permission_denied(signed_in):
    body = text(signed_in.get("/members/10003"))
    assert exceptional.MSG_PERMISSION in body
    assert "Priya" not in body


def test_permission_denial_also_applies_when_reached_through_search(signed_in):
    resp = signed_in.post("/members/search", data={"mbr": "10003"})
    body = text(signed_in.get(resp.headers["Location"]))
    assert exceptional.MSG_PERMISSION in body


# --- recoverable conditions ----------------------------------------------


def test_notice_member_shows_an_interstitial_before_the_detail(signed_in):
    body = text(signed_in.get("/members/10004"))
    assert exceptional.NOTICE_TITLE in body
    assert f'aria-label="{exceptional.NOTICE_TITLE}"' in body
    assert 'value="Continue"' in body
    assert "Member Details" not in body


def test_dismissing_the_notice_reveals_the_detail(signed_in):
    signed_in.get("/members/10004")
    resp = signed_in.post("/members/10004/dismiss-notice")
    assert resp.status_code == 302
    body = text(signed_in.get("/members/10004"))
    assert "Member Details" in body
    assert "Tomas Lindqvist" in body
    assert exceptional.NOTICE_TITLE not in body


def test_slow_render_is_armed_once_and_then_clears(signed_in):
    signed_in.get("/debug/slow?ms=250")
    import time

    started = time.monotonic()
    signed_in.get("/members/10001")
    stalled = time.monotonic() - started

    started = time.monotonic()
    signed_in.get("/members/10001")
    normal = time.monotonic() - started

    assert stalled >= 0.25
    assert normal < 0.25


def test_expire_drops_the_session_and_signin_renders_in_the_frame(signed_in):
    assert "Member Details" in text(signed_in.get("/members/10001"))
    signed_in.get("/debug/expire")
    body = text(signed_in.get("/members/10001"))
    assert "Sign In" in body
    assert 'class="framed"' in body
    assert "Member Details" not in body


def test_signing_back_in_after_expiry_restores_access(signed_in):
    """The recovery has to be able to actually recover."""
    signed_in.get("/debug/expire")
    signed_in.post("/", data={"user": APP_USER, "pwd": APP_PASS})
    assert "Member Details" in text(signed_in.get("/members/10001"))


# --- the undeclared blocking state ---------------------------------------


def test_compliance_hold_blocks_the_detail(signed_in):
    body = text(signed_in.get("/members/10005"))
    assert exceptional.HOLD_TITLE in body
    assert 'value="Acknowledge"' in body
    assert "Member Details" not in body
    assert "22,047.19" not in body


def test_compliance_hold_is_not_distinguishable_from_the_notice_by_shape(
    signed_in, app
):
    """The load-bearing property of the escalation demo.

    Both render through the same template with the same roles and the same
    control layout. The only difference is the name. A recovery declared for
    `dialog "System Notice"` therefore cannot match a compliance hold by
    accident -- it has to be wrong on purpose, or escalate.
    """
    other = app.test_client()
    other.post("/", data={"user": APP_USER, "pwd": APP_PASS})

    notice = text(other.get("/members/10004"))
    hold = text(signed_in.get("/members/10005"))

    for shared in ('role="dialog"', "ctl00_ContentPlaceHolder1_btnDlgAction", "dlgtbl"):
        assert shared in notice
        assert shared in hold

    assert exceptional.NOTICE_TITLE not in hold
    assert exceptional.HOLD_TITLE not in notice


def test_acknowledging_the_hold_reveals_the_detail(signed_in):
    """What a human does during the handoff, after which the run resumes."""
    signed_in.get("/members/10005")
    signed_in.post("/members/10005/acknowledge-hold")
    body = text(signed_in.get("/members/10005"))
    assert "Member Details" in body
    assert "Aisha Bello" in body
    assert "22,047.19" in body


# --- hard failure ---------------------------------------------------------


def test_three_failed_signins_lock_the_account(app):
    client = app.test_client()
    for _ in range(exceptional.MAX_SIGNIN_ATTEMPTS):
        client.post("/", data={"user": APP_USER, "pwd": "wrong"})
    body = text(client.post("/", data={"user": APP_USER, "pwd": APP_PASS}))
    assert exceptional.MSG_LOCKED in body


def test_lockout_survives_a_fresh_session(app):
    """Not something a retry or a new browser gets around, which is what
    makes it a hard failure for the auth bootstrap rather than a recovery."""
    first = app.test_client()
    for _ in range(exceptional.MAX_SIGNIN_ATTEMPTS):
        first.post("/", data={"user": APP_USER, "pwd": "wrong"})

    second = app.test_client()
    body = text(second.post("/", data={"user": APP_USER, "pwd": APP_PASS}))
    assert exceptional.MSG_LOCKED in body


def test_debug_reset_clears_the_lockout(app):
    client = app.test_client()
    for _ in range(exceptional.MAX_SIGNIN_ATTEMPTS):
        client.post("/", data={"user": APP_USER, "pwd": "wrong"})
    client.get("/debug/reset")
    resp = client.post("/", data={"user": APP_USER, "pwd": APP_PASS})
    assert resp.status_code == 302


# --- the missing-extract-target case --------------------------------------


def test_member_without_savings_is_not_a_not_found(signed_in):
    """10006 exists and is viewable; the thing we came to read is absent.

    Three different conditions that a naive implementation collapses into one
    error: no such member, member you may not see, and member whose savings
    row does not exist.
    """
    body = text(signed_in.get("/members/10006"))
    assert "Member Details" in body
    assert "Grant Okonkwo" in body
    assert exceptional.MSG_NOT_FOUND not in body
    assert exceptional.MSG_PERMISSION not in body
    assert "Savings" not in body
