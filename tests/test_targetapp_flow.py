"""The stand-in console has to actually work before we automate it.

These also pin the hostile markup in place. If someone later "tidies up" the
search field by adding a label, or gives the accounts grid real ``<th>``
headers, the surface stops exercising the locator fallback chain and the whole
demonstration quietly becomes easier than the problem it claims to solve. The
markup assertions below are there to make that a test failure.
"""

import pytest

from targetapp import seed
from targetapp.app import APP_PASS, APP_USER, create_app


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    seed.seed()


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def signed_in(client):
    client.post("/", data={"user": APP_USER, "pwd": APP_PASS})
    return client


def text(response) -> str:
    return response.get_data(as_text=True)


# --- the happy path ------------------------------------------------------


def test_signin_rejects_bad_credentials(client):
    body = text(client.post("/", data={"user": APP_USER, "pwd": "wrong"}))
    assert "Invalid user ID or password." in body


def test_signin_then_home_serves_the_frame_shell(client):
    resp = client.post("/", data={"user": APP_USER, "pwd": APP_PASS})
    assert resp.status_code == 302
    body = text(client.get("/home"))
    assert 'name="mainFrame"' in body
    assert 'src="/members/search"' in body


def test_search_redirects_to_the_member_detail(signed_in):
    resp = signed_in.post("/members/search", data={"mbr": "10001"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/members/10001")


def test_member_detail_shows_the_savings_balance(signed_in):
    body = text(signed_in.get("/members/10001"))
    assert "Member Details" in body
    assert "Dana Whitfield" in body
    assert "Savings" in body
    assert "4,210.33" in body


def test_member_10006_has_no_savings_row(signed_in):
    """Exists, but the extract target is absent -- a different failure from
    'no such member', and the two must not be conflated."""
    body = text(signed_in.get("/members/10006"))
    assert "Grant Okonkwo" in body
    assert "Checking" in body
    assert "Savings" not in body


def test_subaccount_flow_reaches_confirmation_and_commits(signed_in):
    form = {
        "acct_type": "Savings",
        "deposit": "150.00",
        "purpose": "Vacation fund",
        "stmt_pref": "Electronic",
    }
    review = text(signed_in.post("/members/10002/subaccount/review", data=form))
    assert "Confirm Sub-Account" in review
    assert "150.00" in review
    assert "Review before confirming." in review

    done = text(signed_in.post("/members/10002/subaccount/commit", data=form))
    assert "Sub-Account Opened" in done
    assert "Account opened successfully." in done
    assert "SV-" in done


def test_subaccount_review_rejects_a_deposit_below_the_minimum(signed_in):
    body = text(
        signed_in.post(
            "/members/10002/subaccount/review",
            data={
                "acct_type": "Savings",
                "deposit": "5.00",
                "purpose": "x",
                "stmt_pref": "Paper",
            },
        )
    )
    assert "at least 25.00" in body
    assert "Confirm Sub-Account" not in body


def test_protected_pages_render_signin_inside_the_frame(client):
    """A dropped session shows sign-in *in the frame* rather than redirecting,
    which is what session-expiry detection keys off."""
    body = text(client.get("/members/search"))
    assert "Sign In" in body
    assert 'class="framed"' in body


# --- the markup stays hostile -------------------------------------------


def test_member_id_field_has_no_accessible_name(signed_in):
    """No label-for, no aria-label, no title, no placeholder. Role+name
    targeting must fail on this field so tier 2 is genuinely exercised."""
    body = text(signed_in.get("/members/search"))
    field_start = body.index('id="ctl00_ContentPlaceHolder1_txtMbrId"')
    tag = body[body.rindex("<input", 0, field_start) : body.index(">", field_start)]
    for attr in ("aria-label", "title=", "placeholder"):
        assert attr not in tag
    assert 'for="ctl00_ContentPlaceHolder1_txtMbrId"' not in body


def test_search_button_does_have_an_accessible_name(signed_in):
    """The contrast case: tier 1 has to work somewhere."""
    body = text(signed_in.get("/members/search"))
    assert 'type="submit" value="Search"' in body


def test_accounts_grid_has_no_table_headers(signed_in):
    """Header cells are <td>, so there is no column semantics to read and the
    balance has to be found structurally."""
    body = text(signed_in.get("/members/10001"))
    grid = body[body.index('id="ctl00_ContentPlaceHolder1_grdAccounts"') :]
    assert "<th" not in grid
    assert 'class="gridhdrcell"' in grid


def test_subaccount_link_is_a_javascript_postback(signed_in):
    """Cannot be followed as a URL; it has to be clicked."""
    body = text(signed_in.get("/members/10001"))
    assert "javascript:__doPostBack(" in body
    assert "lnkNewSub" in body


def test_no_test_ids_anywhere(signed_in):
    for path in ("/members/search", "/members/10001", "/members/10001/subaccount/new"):
        body = text(signed_in.get(path))
        assert "data-testid" not in body
        assert "data-test" not in body
