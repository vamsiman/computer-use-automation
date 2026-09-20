"""Evidence capture against a real browser and the real target app.

The recorder tests use a fake surface, which proves the redaction rules and
the bundle layout. It cannot prove the two things that only a live page can
settle: that a screenshot of a framed legacy app actually writes a PNG, and
that ``page_source`` reaches inside the ``<iframe>`` where all the content
lives. A naive implementation calling ``page.content()`` returns the outer
frameset and nothing else -- which looks like a working capture right up to
the moment somebody needs it.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from cua.evidence import EvidenceConfig, Recorder, Redactor
from cua.locators import Locator, RoleNameSpec
from cua.primitives import Action
from cua.session import Credentials, authenticate
from cua.surface.web import BrowserSession
from cua.types import ActionType, Sensitivity
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
        authenticate(surf, Credentials(user=APP_USER, password=APP_PASS))
        yield surf
    finally:
        surf.close()
        session.stop()


@pytest.fixture
def on_member(surface):
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/10001"}))
    return surface


def recorder(tmp_path, **kwargs) -> Recorder:
    rules = {"member_name": Sensitivity.PII, "member_id": Sensitivity.INTERNAL}
    return Recorder.start(
        config=EvidenceConfig(root=tmp_path / "runs", **kwargs),
        redactor=Redactor(rules=rules, salt="live").with_secrets([APP_PASS]),
    )


def test_page_source_reaches_inside_the_frame(surface):
    """Everything an operator works in lives in ``mainFrame``. A capture that
    stops at the top-level document gets a nav menu and an empty frame tag --
    which looks like a working capture right up to the moment somebody needs
    it to explain a failure."""
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/home"}))
    source = surface.page_source()

    assert source.count("<!-- frame:") >= 2, "the inner frame was not captured"
    assert 'id="ctl00_mainFrame"' in source, "outer document missing"
    assert "Member Search" in source, "frame content missing"


def test_a_screenshot_of_the_live_page_is_a_real_png(tmp_path, on_member):
    rec = recorder(tmp_path)
    relative = rec.screenshot(on_member, label="001")
    assert relative == "steps/001.png"
    written = rec.dir / relative
    assert written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_failure_bundle_captures_the_framed_page(tmp_path, on_member):
    rec = recorder(tmp_path)
    captured = rec.failure(on_member, step_id="s5", reason="no savings row")

    assert set(captured) >= {"a11y", "page_source", "screenshot"}
    tree = (rec.dir / "failure" / "a11y.txt").read_text(encoding="utf-8")
    # The tree is what the system could actually see, which is the thing
    # worth handing to whoever diagnoses this later.
    assert "Member Details" in tree
    assert "/members/10001" in captured["location"]


def test_the_password_never_reaches_a_live_capture(tmp_path, surface):
    """The sign-in page is the one screen where a credential is genuinely in
    the markup. ``inject.js`` masks the field's value in the tree; the literal
    scrub is what covers the raw HTML underneath it."""
    surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/logout"}))
    rec = recorder(tmp_path)
    try:
        surface.act(
            Action(
                type=ActionType.TYPE,
                target=Locator(primary=RoleNameSpec(role="textbox", name="Password")),
                args={"value": APP_PASS},
            )
        )
        rec.failure(surface, step_id="s0", reason="sign-in captured")

        for path in rec.dir.rglob("*"):
            if path.is_file() and path.suffix != ".png":
                assert APP_PASS not in path.read_text(encoding="utf-8"), path
    finally:
        authenticate(surface, Credentials(user=APP_USER, password=APP_PASS))


def test_a_real_run_produces_a_readable_log(tmp_path, surface):
    rec = recorder(tmp_path)
    with rec.step("s1", "Open the member detail screen", "navigate") as step:
        result = surface.act(
            Action(type=ActionType.NAVIGATE, args={"path": "/members/10001"})
        )
        step.ok = result.ok
        step.tier = result.tier
    rec.finish({"status": "Success"})

    records = [
        json.loads(line)
        for line in rec.log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [r["kind"] for r in records]
    assert kinds == ["step", "finished"]
    assert records[0]["ok"] is True
