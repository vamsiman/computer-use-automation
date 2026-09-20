"""The auth bootstrap against the real sign-in page.

Signing in is the one procedure the system cannot learn by itself, because
credentials are deliberately kept out of artifacts. That makes it worth
testing directly, including the two ways it can be refused -- rejected
credentials and a locked account -- since those mean different things to the
caller and only one of them is worth another try.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.session import (
    AuthFailed,
    AuthLocked,
    Credentials,
    SessionManager,
    authenticate,
    is_authenticated,
)
from cua.session.auth import MEMBER_CONSOLE
from cua.types import ControlOwner, RunState
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_app():
    from werkzeug.serving import make_server

    seed.seed()
    port = _free_port()
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture
def manager():
    mgr = SessionManager()
    try:
        yield mgr
    finally:
        mgr.close_all()


@pytest.fixture(autouse=True)
def _clean_lockouts():
    exceptional.reset_all()
    yield
    exceptional.reset_all()


@pytest.fixture
def session(manager, live_app):
    return manager.create(live_app, headless=True)


def good() -> Credentials:
    return Credentials(user=APP_USER, password=APP_PASS)


# --- the happy path ------------------------------------------------------


def test_authenticate_signs_in_and_reports_it_did_the_work(session):
    result = authenticate(session.surface, good())
    assert not result.already_authenticated
    assert result.attempts == 1
    assert session.surface.observe().find("heading", "Member Search")


def test_authenticate_is_idempotent(session):
    """The session-expiry recovery calls this again mid-run, so a second call
    on an already-authenticated session must be free rather than disruptive."""
    authenticate(session.surface, good())
    again = authenticate(session.surface, good())
    assert again.already_authenticated
    assert again.attempts == 0


def test_is_authenticated_tracks_the_session(session):
    assert not is_authenticated(session.surface)
    authenticate(session.surface, good())
    assert is_authenticated(session.surface)


def test_expiry_is_detected_anywhere_in_the_app(session):
    """Checked by the sign-in form appearing rather than by reaching a
    particular screen, because a session can drop anywhere."""
    from cua.primitives import Action
    from cua.types import ActionType

    authenticate(session.surface, good())
    session.surface.act(
        Action(type=ActionType.NAVIGATE, args={"path": "/members/10001"})
    )
    assert is_authenticated(session.surface)

    session.surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/debug/expire"}))
    session.surface.act(
        Action(type=ActionType.NAVIGATE, args={"path": "/members/10001"})
    )
    assert not is_authenticated(session.surface)

    authenticate(session.surface, good())
    assert is_authenticated(session.surface)


# --- the two refusals ----------------------------------------------------


def test_wrong_credentials_raise_auth_failed(session):
    with pytest.raises(AuthFailed):
        authenticate(session.surface, Credentials(user=APP_USER, password="nope"))


def test_a_locked_account_raises_auth_locked_not_auth_failed(session, live_app):
    """The distinction the caller needs.

    Rejected credentials might be worth one more try. A lockout never is --
    retrying is what caused it. The application reports lockout even for a
    correct password, so conflating the two would send a caller off retrying
    the one thing guaranteed not to help.
    """
    wrong = Credentials(user=APP_USER, password="nope")

    # Attempts before the last are ordinary rejections.
    for _ in range(exceptional.MAX_SIGNIN_ATTEMPTS - 1):
        with pytest.raises(AuthFailed):
            authenticate(session.surface, wrong)

    # The attempt that trips the lock reports the lock, not the rejection.
    with pytest.raises(AuthLocked):
        authenticate(session.surface, wrong)


def test_lockout_is_reported_even_with_the_right_password(session):
    """The reason the two exceptions have to be distinct.

    Once locked, the application reports lockout for a correct password too.
    A caller that saw AuthFailed here would go off retrying the one thing
    guaranteed not to help.
    """
    wrong = Credentials(user=APP_USER, password="nope")
    for _ in range(exceptional.MAX_SIGNIN_ATTEMPTS - 1):
        with pytest.raises(AuthFailed):
            authenticate(session.surface, wrong)
    with pytest.raises(AuthLocked):
        authenticate(session.surface, wrong)

    with pytest.raises(AuthLocked):
        authenticate(session.surface, good())


def test_a_failed_attempt_actually_reaches_the_server(session):
    """Guards the bug this exposed.

    Reading the error banner before submitting made a retry report failure
    without ever asking, so the server-side attempt counter never advanced.
    Three calls must produce three attempts, which is observable because the
    third one locks the account.
    """
    wrong = Credentials(user=APP_USER, password="nope")
    raised: list[str] = []
    for _ in range(exceptional.MAX_SIGNIN_ATTEMPTS):
        try:
            authenticate(session.surface, wrong)
        except (AuthFailed, AuthLocked) as exc:
            raised.append(type(exc).__name__)

    assert raised == ["AuthFailed", "AuthFailed", "AuthLocked"]
    assert exceptional.is_locked(APP_USER)


# --- credentials never leak ----------------------------------------------


def test_credentials_never_render_their_password(session):
    """Tracebacks, log lines and crash reports all call repr()."""
    creds = Credentials(user="teller1", password="super-secret-value")
    assert "super-secret-value" not in repr(creds)
    assert "super-secret-value" not in str(creds)
    assert "super-secret-value" not in f"{creds}"
    assert creds.password == "super-secret-value"


def test_a_typed_password_is_masked_in_the_observed_tree(session):
    """Snapshots are handed to a model and written to evidence, so the
    password must not be readable in one even while it is in the field."""
    from cua.primitives import Action
    from cua.types import ActionType

    session.surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/"}))
    session.surface.act(
        Action(
            type=ActionType.TYPE,
            target=MEMBER_CONSOLE.password_field,
            args={"value": "super-secret-value"},
        )
    )
    text = session.surface.observe().text_view()
    assert "super-secret-value" not in text
    assert "********" in text


# --- sessions and control ------------------------------------------------


def test_a_created_session_is_reachable_by_id(manager, live_app):
    session = manager.create(live_app, headless=True)
    assert manager.get(session.id) is session
    assert session.state is RunState.PENDING
    assert session.control is ControlOwner.NONE


def test_a_session_survives_the_function_that_authenticated_it(manager, live_app):
    """The architectural point: the browser outlives the call, so something
    else can reach it later."""

    def bootstrap() -> str:
        session = manager.create(live_app, headless=True)
        authenticate(session.surface, good())
        return session.id

    session_id = bootstrap()
    recovered = manager.get(session_id)
    assert is_authenticated(recovered.surface)
    assert recovered.surface.observe().find("heading", "Member Search")
