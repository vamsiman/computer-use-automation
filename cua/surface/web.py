"""The browser implementation of ``Surface``.

This is the only module in the system that knows Playwright exists. Discovery
and replay talk to the protocol in ``base.py``; swapping this for a desktop
driver is a matter of producing the same trees and honouring the same actions.

Browsers run headed by default, which looks like a debugging convenience and
is actually an architectural commitment: the escalation path requires a human
to take over *the session the automation was already using*, and the cheapest
honest way to do that is to leave the window on screen and stop driving it.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, Frame, Page, sync_playwright

from cua.locators import Locator, Resolution
from cua.primitives import A11yNode, Action, ActResult, Snapshot
from cua.surface import a11y
from cua.surface.base import SurfaceError
from cua.surface.resolve import resolve_in_tree
from cua.types import ActionType

_SCRIPT = (Path(__file__).with_name("inject.js")).read_text(encoding="utf-8")

#: How long to let a navigation settle before carrying on regardless. Short on
#: purpose: a page that is still loading is a condition the replay engine's
#: checkpoint and retry logic is meant to notice, not something to paper over
#: with a long blocking wait here.
SETTLE_MS = 2500

#: Grace period before we start waiting for a load to finish.
#:
#: Clicking an ``<a href="javascript:...">`` returns before the script has run,
#: so the navigation it triggers has not begun yet. Asking Playwright to wait
#: for a load at that instant finds nothing in flight and returns immediately,
#: leaving the very next observation describing the page we just left. The
#: grace gives such a navigation time to start so there is something to wait
#: for.
#:
#: This narrows the window rather than closing it. The real guarantee that we
#: are looking at the state we expect is the checkpoint the replay engine
#: asserts after each step, which waits and retries on its own budget.
SETTLE_GRACE_MS = 120


def headless_default() -> bool:
    return os.environ.get("CUA_HEADLESS", "").lower() in ("1", "true", "yes")


class WebSurface:
    """Observe and act on a browser page."""

    def __init__(
        self,
        page: Page,
        base_url: str,
        screenshot_dir: str | Path | None = None,
    ) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self._frames: list[Frame] = []
        self._shot_seq = 0

    # --- observation -----------------------------------------------------

    def observe(self, screenshot: bool = False) -> Snapshot:
        """Build one tree spanning every frame on the page."""
        self._frames = list(self.page.frames)
        trees: dict[int, A11yNode] = {}
        location = self.page.url

        for index, frame in enumerate(self._frames):
            try:
                raw = frame.evaluate(_SCRIPT, f"f{index}")
            except Exception:
                # Frames detach mid-navigation. A frame we cannot read is
                # simply absent from this observation rather than an error:
                # the next one will pick it up.
                continue
            trees[index] = a11y.document_from_raw(
                raw, role="document" if index == 0 else "frame"
            )
            if index == 0 and raw.get("location"):
                # Read the location from inside the document rather than from
                # `page.url`, which lags after a javascript: navigation and
                # would have us report the page we just left.
                location = raw["location"]

        root = trees.get(0)
        if root is None:
            raise SurfaceError("could not read the main frame")

        for index, frame in enumerate(self._frames):
            if index == 0 or index not in trees:
                continue
            host_ref = self._host_ref(frame)
            if host_ref:
                a11y.splice_frame(root, host_ref, trees[index])
            else:
                root.children.append(trees[index])

        shot = None
        if screenshot and self.screenshot_dir:
            shot = self.screenshot()

        return Snapshot(
            location=location,
            tree=root,
            screenshot_ref=shot,
            modal_text=self._modal_text(root),
        )

    def _host_ref(self, frame: Frame) -> str | None:
        """The ref of the iframe element this frame lives in."""
        try:
            element = frame.frame_element()
            return element.get_attribute("data-cua-ref")
        except Exception:
            return None

    @staticmethod
    def _modal_text(tree: A11yNode) -> str | None:
        """Surface a blocking dialog in the header of the text view.

        Worth calling out separately because a dialog is not just another
        node: it means the rest of the tree is unreachable, and an agent that
        misses that will keep trying to click through it.
        """
        for node in tree.walk():
            if node.role == "dialog":
                return node.name or "dialog"
        return None

    # --- resolution ------------------------------------------------------

    def resolve(self, locator: Locator, snapshot: Snapshot | None = None) -> Resolution:
        snap = snapshot or self.observe()
        return resolve_in_tree(snap.tree, locator)

    def _element_for(self, node: A11yNode):
        """Turn a tree node back into something clickable."""
        if not node.ref:
            raise SurfaceError("node has no ref; cannot act on it")
        index = a11y.frame_index_of(node.ref)
        if index is None or index >= len(self._frames):
            raise SurfaceError(f"stale node ref {node.ref}")
        frame = self._frames[index]
        return frame.locator(f'[data-cua-ref="{node.ref}"]')

    # --- action ----------------------------------------------------------

    def act(self, action: Action) -> ActResult:
        try:
            return self._act(action)
        except SurfaceError as exc:
            return ActResult(ok=False, error=str(exc))
        except Exception as exc:  # playwright errors
            return ActResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def _act(self, action: Action) -> ActResult:
        if action.type is ActionType.NAVIGATE:
            return self._navigate(action)

        if action.type is ActionType.PRESS_KEY:
            self.page.keyboard.press(action.args.get("key", "Enter"))
            self._settle()
            return ActResult(ok=True)

        if action.type is ActionType.WAIT_FOR:
            return self._wait_for(action)

        if action.target is None:
            raise SurfaceError(f"{action.type} requires a target")

        snapshot = self.observe()
        resolution = self.resolve(action.target, snapshot)
        if not resolution.resolved:
            return ActResult(
                ok=False,
                error=f"unresolved: {action.target.describe()}",
                observed=snapshot,
            )

        node: A11yNode = resolution.handle  # type: ignore[assignment]

        if action.type is ActionType.EXTRACT:
            # Read straight off the tree we already have. The value is right
            # there, and a second round trip would only invite the page to
            # change underneath us between observing and reading.
            value = node.value or node.name
            return ActResult(ok=True, tier=resolution.tier, value=value)

        element = self._element_for(node)

        if action.type is ActionType.CLICK:
            element.click(timeout=SETTLE_MS)
        elif action.type is ActionType.TYPE:
            element.fill(str(action.args.get("value", "")), timeout=SETTLE_MS)
        elif action.type is ActionType.SELECT:
            element.select_option(
                label=str(action.args.get("value", "")), timeout=SETTLE_MS
            )
        else:
            raise SurfaceError(f"unsupported action {action.type}")

        self._settle()
        return ActResult(ok=True, tier=resolution.tier)

    def _navigate(self, action: Action) -> ActResult:
        target = action.args.get("path") or action.args.get("url") or "/"
        url = target if urlparse(target).scheme else urljoin(self.base_url + "/", target.lstrip("/"))
        self.page.goto(url, wait_until="domcontentloaded")
        self._settle()
        return ActResult(ok=True)

    def _wait_for(self, action: Action) -> ActResult:
        if action.target is None:
            raise SurfaceError("wait_for requires a target")
        deadline = time.monotonic() + int(action.args.get("timeout_ms", 5000)) / 1000
        last: Snapshot | None = None
        while time.monotonic() < deadline:
            last = self.observe()
            resolution = resolve_in_tree(last.tree, action.target)
            if resolution.resolved:
                return ActResult(ok=True, tier=resolution.tier, observed=last)
            time.sleep(0.2)
        return ActResult(
            ok=False,
            error=f"timed out waiting for {action.target.describe()}",
            observed=last,
        )

    def _settle(self) -> None:
        time.sleep(SETTLE_GRACE_MS / 1000)
        for state in ("domcontentloaded", "networkidle"):
            try:
                self.page.wait_for_load_state(state, timeout=SETTLE_MS)
            except Exception:
                # A page still loading is information, not an error. The
                # checkpoint that follows will decide whether it matters.
                pass

    # --- evidence --------------------------------------------------------

    def screenshot(self, path: str | None = None) -> str:
        if path is None:
            if self.screenshot_dir is None:
                raise SurfaceError("no screenshot directory configured")
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            self._shot_seq += 1
            path = str(self.screenshot_dir / f"{self._shot_seq:03d}.png")
        self.page.screenshot(path=path, full_page=False)
        return path

    def page_source(self) -> str:
        """Raw markup of the main document and every frame inside it.

        Not part of the Surface protocol, and deliberately so: a desktop
        driver has no such thing, and requiring one would make the protocol
        describe a browser rather than a surface. The recorder asks for it by
        duck-typing and does without when it is absent, which is the right
        shape for a capability only some surfaces have.

        Captured on failure only. Markup carries values the accessibility tree
        elides -- hidden fields, full account numbers, view state -- so it is
        the richest thing here and separately gated in the evidence config.
        """
        parts = []
        for frame in self.page.frames:
            try:
                parts.append(f"<!-- frame: {frame.url} -->\n{frame.content()}")
            except Exception:
                # A frame that has navigated out from under us is not worth
                # failing an evidence capture over.
                continue
        return "\n\n".join(parts)

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass


_DRIVER_LOCK = threading.Lock()
_DRIVER = None


def _driver():
    """The single Playwright driver for this process.

    One driver, many browsers. Starting ``sync_playwright()`` per session
    looks harmless and is not: each start spawns its own node driver and
    installs its own event loop, and a process that accumulates several ends
    up deadlocked with orphaned browsers still running. That is not a
    theoretical risk -- it is what happened here the first time the session
    manager created a browser per run.

    The session manager exists precisely to hold many concurrent sessions, so
    the driver has to be shared and the browsers kept separate.
    """
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is None:
            _DRIVER = sync_playwright().start()
        return _DRIVER


def shutdown_driver() -> None:
    """Stop the shared driver. For process teardown and test fixtures."""
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.stop()
            except Exception:
                pass
            _DRIVER = None


class BrowserSession:
    """Owns a browser for the lifetime of a session.

    Exists so the browser outlives any single function call. That is the
    change that makes a human handoff possible at all: something other than
    the currently-running step has to be able to reach the window.
    """

    def __init__(self, base_url: str, headless: bool | None = None) -> None:
        self.base_url = base_url
        self.headless = headless_default() if headless is None else headless
        self._browser: Browser | None = None

    def start(self, screenshot_dir: str | Path | None = None) -> WebSurface:
        self._browser = _driver().chromium.launch(headless=self.headless)
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        return WebSurface(page, self.base_url, screenshot_dir)

    def stop(self) -> None:
        """Close this session's browser, leaving the shared driver running."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

    def __enter__(self) -> WebSurface:
        self._surface = self.start()
        return self._surface

    def __exit__(self, *exc) -> None:
        self.stop()
