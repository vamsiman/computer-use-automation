"""The surface driver against the real browser and the real target app.

The resolver tests use hand-built trees; these prove the tree we build from an
actual page has the properties those tests assume. That distinction matters
most for the two things the design leans on hardest: that the working area
inside an iframe is perceivable at all, and that the search field genuinely
has no accessible name so tier 2 is doing real work rather than being
decoration.

Marked ``browser`` so the suite still runs somewhere without Chromium
installed.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.locators import (
    LabelProximitySpec,
    Locator,
    RoleNameSpec,
    RowCellSpec,
)
from cua.primitives import Action
from cua.surface.web import BrowserSession
from cua.types import ActionType, LocatorStrategy
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
        yield surf
    finally:
        surf.close()
        session.stop()


@pytest.fixture
def signed_in(surface):
    """Sign in and land on the framed shell."""
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/"}))
    snap = surface.observe()
    if snap.find("heading", "Sign In"):
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
    return surface


def search_for(surface, member_no: str):
    surface.act(
        Action(
            type=ActionType.TYPE,
            target=Locator(
                primary=LabelProximitySpec(label="Member ID:", direction="right")
            ),
            args={"value": member_no},
        )
    )
    return surface.act(
        Action(
            type=ActionType.CLICK,
            target=Locator(primary=RoleNameSpec(role="button", name="Search")),
        )
    )


# --- perception ----------------------------------------------------------


def test_signing_in_works_through_the_surface(signed_in):
    """Proves tier 2 can drive a real form end to end: neither credential
    field has an accessible name."""
    snap = signed_in.observe()
    assert snap.find("heading", "Member Search")


def test_observation_crosses_the_iframe_boundary(signed_in):
    """The whole working area lives in a frame. A driver that reads only the
    top document sees a nav menu and nothing else."""
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/home"}))
    snap = signed_in.observe()

    assert snap.find("link", "Member Search"), "top-level nav should be visible"
    assert snap.find("heading", "Member Search"), "framed content should be visible"

    roles = {n.role for n in snap.tree.walk()}
    assert "iframe" in roles
    assert "frame" in roles


def test_framed_nodes_carry_their_frame_index(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/home"}))
    snap = signed_in.observe()
    heading = snap.find("heading", "Member Search")[0]
    assert heading.ref.startswith("f1n")


def test_member_id_field_really_has_no_accessible_name(signed_in):
    """The premise of the fallback chain, checked against the real page
    rather than a fixture that assumes it."""
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    snap = signed_in.observe()
    textboxes = snap.find("textbox")
    assert textboxes
    assert all(not t.name for t in textboxes)


def test_search_button_does_have_one(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    assert signed_in.observe().find("button", "Search")


def test_a_field_value_never_becomes_its_name(signed_in):
    """A trap worth a test of its own.

    After a search the member-id field re-renders carrying the previous
    query. If `value` were treated as an accessible-name fallback, an unnamed
    field would acquire a name on the second render only -- so role+name
    targeting would start matching it *sometimes*, which is far worse than
    never matching at all.
    """
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    search_for(signed_in, "99999")

    snap = signed_in.observe()
    field = snap.find("textbox")[0]
    assert field.value == "99999"
    assert field.name == ""


def test_blocking_dialog_is_surfaced_in_the_text_view(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/10004"}))
    snap = signed_in.observe()

    assert snap.modal_text == "System Notice"
    assert "MODAL OPEN: System Notice" in snap.text_view()


# --- resolution against the live page ------------------------------------


def test_chain_degrades_to_tier_two_on_the_real_page(signed_in):
    """The headline behaviour, end to end: the preferred rule misses on a
    genuinely unnamed control and the label rule recovers it, reporting that
    we are a tier worse than ideal."""
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))

    locator = Locator(
        primary=RoleNameSpec(role="textbox", name="Member ID"),
        fallbacks=(LabelProximitySpec(label="Member ID:", direction="right"),),
    )
    result = signed_in.resolve(locator)

    assert result.resolved
    assert result.tier == 1
    assert result.strategy is LocatorStrategy.LABEL_PROXIMITY
    assert result.degraded


def test_unresolvable_locator_reports_failure_not_a_wrong_element(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    result = signed_in.resolve(
        Locator(primary=RoleNameSpec(role="button", name="Delete Member"))
    )
    assert not result.resolved


# --- acting --------------------------------------------------------------


def test_full_read_flow_extracts_the_savings_balance(signed_in):
    """Search, land on the detail screen, and pull the balance out of a grid
    that has no column headers."""
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    search_for(signed_in, "10001")

    snap = signed_in.observe()
    assert snap.find("heading", "Member Details")

    result = signed_in.act(
        Action(
            type=ActionType.EXTRACT,
            target=Locator(
                primary=RowCellSpec(table="", row_match="Savings", column="Balance")
            ),
            args={"into": "savings_balance"},
        )
    )
    assert result.ok
    assert result.value == "4,210.33"


def test_replaying_with_a_different_input_reads_a_different_balance(signed_in):
    """Parameterisation, proven at the surface layer: the same steps against
    a different member return that member's number, not a cached one."""
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    search_for(signed_in, "10002")

    result = signed_in.act(
        Action(
            type=ActionType.EXTRACT,
            target=Locator(
                primary=RowCellSpec(table="", row_match="Savings", column="Balance")
            ),
        )
    )
    assert result.value == "982.14"


def test_javascript_postback_link_has_to_be_clicked(signed_in):
    """Its href is a javascript: call, so there is no URL to shortcut to.
    Clicking is the only way through, which is the behaviour we want on a
    surface with no clean DOM."""
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/10001"}))
    result = signed_in.act(
        Action(
            type=ActionType.CLICK,
            target=Locator(
                primary=RoleNameSpec(role="link", name="Open Sub-Account")
            ),
        )
    )
    assert result.ok
    assert signed_in.observe().find("heading", "Open Sub-Account")


def test_extract_reports_the_tier_it_resolved_on(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/10001"}))
    result = signed_in.act(
        Action(
            type=ActionType.EXTRACT,
            target=Locator(
                primary=RoleNameSpec(role="cell", name="Nonexistent"),
                fallbacks=(
                    RowCellSpec(table="", row_match="Savings", column="Balance"),
                ),
            ),
        )
    )
    assert result.ok
    assert result.tier == 1


def test_acting_on_a_missing_target_fails_cleanly(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    result = signed_in.act(
        Action(
            type=ActionType.CLICK,
            target=Locator(primary=RoleNameSpec(role="button", name="Nope")),
        )
    )
    assert not result.ok
    assert "unresolved" in result.error


def test_wait_for_times_out_rather_than_hanging(signed_in):
    signed_in.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    result = signed_in.act(
        Action(
            type=ActionType.WAIT_FOR,
            target=Locator(primary=RoleNameSpec(role="heading", name="Nowhere")),
            args={"timeout_ms": 600},
        )
    )
    assert not result.ok
    assert "timed out" in result.error
