"""Does the human-action watcher see a real mouse click?

The live handoff demo recorded every state transition and zero human actions.
Every test that ever captured one used a scripted operator clicking
synchronously on the automation's own thread, so a real mouse has never
actually been tried.

This strips everything else away. No replay engine, no state machine, no
threading: open the hold screen, start the watcher, wait for a person to
click Acknowledge with their actual mouse, and report exactly what each
frame knows at each stage.

Run it, click the Acknowledge button in the window that opens, and send back
`diagnose-watch.txt`.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from cua.primitives import Action  # noqa: E402
from cua.session import Credentials, SessionManager, authenticate  # noqa: E402
from cua.session.human import HumanWatcher  # noqa: E402
from cua.types import ActionType  # noqa: E402
from targetapp import exceptional, seed  # noqa: E402
from targetapp.app import APP_PASS, APP_USER, create_app  # noqa: E402

REPORT = Path("diagnose-watch.txt")
LINES: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    LINES.append(text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: Asked of every frame, at every stage. Deliberately raw: what the page
#: itself believes, not what the Python side thinks it arranged.
PROBE = """() => {
    let store = null, err = null;
    try {
        store = {
            flag: window.sessionStorage.getItem('__cua_watch'),
            log: window.sessionStorage.getItem('__cua_watch_log'),
        };
    } catch (e) { err = String(e); }
    return {
        url: window.location.href,
        installed: !!window.__cuaWatchInstalled,
        api: typeof window.__cuaWatch,
        active: window.__cuaWatch ? window.__cuaWatch.active() : null,
        storage: store,
        storageError: err,
        origin: window.location.origin,
    };
}"""


def probe(surface, stage: str) -> None:
    say(f"\n--- {stage} ---")
    for index, frame in enumerate(surface.page.frames):
        try:
            data = frame.evaluate(PROBE)
        except Exception as exc:
            say(f"  frame[{index}]  PROBE FAILED: {exc}")
            continue
        say(f"  frame[{index}] {data['url']}")
        say(f"      origin={data['origin']}")
        say(f"      listeners installed={data['installed']}  "
            f"window.__cuaWatch={data['api']}  active={data['active']}")
        if data["storageError"]:
            say(f"      sessionStorage ERROR: {data['storageError']}")
        else:
            store = data["storage"] or {}
            say(f"      sessionStorage flag={store.get('flag')!r}")
            log = store.get("log")
            say(f"      sessionStorage log={'(empty)' if not log else log[:300]}")


def main() -> int:
    seed.seed()
    exceptional.reset_all()

    port = free_port()
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{port}"

    manager = SessionManager()
    session = manager.create(url, headless=False)
    surface = session.surface

    try:
        authenticate(surface, Credentials(user=APP_USER, password=APP_PASS))
        say(f"app on {url}")

        # Straight to the compliance hold, no replay engine involved.
        surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/10005"}))
        snapshot = surface.observe()
        say(f"on screen: {snapshot.modal_text!r}")
        if not snapshot.modal_text:
            say("!! expected the Compliance Hold and did not find it; stopping")
            return 1

        probe(surface, "BEFORE watch_start()")

        watcher = HumanWatcher(surface)
        say(f"\nwatcher.supported = {watcher.supported}")
        started = watcher.start()
        say(f"watcher.start() -> {started}")

        probe(surface, "AFTER watch_start()")

        say("")
        say("=" * 68)
        say("  CLICK THE 'Acknowledge' BUTTON IN THE BROWSER WINDOW NOW.")
        say("  (the Chromium window that just opened, not your normal browser)")
        say("=" * 68)

        # Poll for the click having landed, so no keyboard input is needed.
        deadline = time.monotonic() + 180
        clicked = False
        while time.monotonic() < deadline:
            if surface.page.is_closed():
                say("")
                say("!! the browser window was closed -- stopping")
                break
            try:
                current = surface.observe()
            except Exception:
                time.sleep(0.5)
                continue
            if not current.modal_text:
                clicked = True
                break
            time.sleep(0.5)

        say(f"\nclick detected: {clicked}")
        if not clicked:
            say("(timed out waiting - if you did click, that itself is a finding)")

        probe(surface, "AFTER the human's click")

        drained = watcher.drain()
        say(f"\nwatcher.drain() returned {len(drained)} record(s)")
        for action in drained:
            say(f"  {action.describe()}   ref={action.ref}  at={action.at}")

        final = watcher.stop()
        say(f"watcher.stop() total = {len(final)} record(s)")
        for action in final:
            say(f"  {action.describe()}")

        probe(surface, "AFTER watch_stop()")

        say("")
        say("=" * 68)
        say("  VERDICT: " + (
            "watcher captured the click" if final
            else "watcher captured NOTHING - the bug reproduces"
        ))
        say("=" * 68)
        return 0
    finally:
        REPORT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        print(f"\nwritten to {REPORT.resolve()}", flush=True)
        manager.close_all()
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
