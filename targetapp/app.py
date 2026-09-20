"""A stand-in for a credit-union back-office console.

This is the *target* of the automation, not part of it. It exists to be awkward
in the ways real legacy bank software is awkward, because a system that only
works against clean markup proves nothing about the environment in the brief.

Deliberate hostility, all of it load-bearing for the locator strategy:

- the working area lives inside an ``<iframe>``, so a driver that only reads the
  top-level document sees an empty page
- layout is nested tables with spacer cells; there are no semantic containers
- control ids are ASP.NET WebForms noise (``ctl00_ContentPlaceHolder1_txtMbrId``)
- the member-id field has a bare ``<span>`` beside it instead of a real
  ``<label for>``, so its accessible name is empty and role+name targeting
  cannot find it
- the accounts grid uses ``<td>`` for its header row, so there is no column
  semantics to read
- the sub-account link is a ``javascript:__doPostBack(...)`` anchor, so it
  cannot be followed as a URL; it has to actually be clicked
- there is not a single test id anywhere

Some things are deliberately *not* hostile: page titles are real headings and
the search button is a real submit input. A surface where nothing resolved
semantically would make the accessibility-tree approach look wrong, and one
where everything did would make the fallback chain decoration. Real legacy apps
are a mix, so this one is too.
"""

from __future__ import annotations

import os
import time
from datetime import date
from functools import wraps

from flask import Flask, g, redirect, render_template, request, session, url_for

from targetapp import data, exceptional, tenants

# Shared with the automation's auth bootstrap, which reads the same variables.
# Demo credentials only; a real system would never hold these in code.
APP_USER = os.environ.get("CUA_APP_USER", "teller1")
APP_PASS = os.environ.get("CUA_APP_PASS", "demo-pass-2024")

ACCOUNT_TYPES = ["Savings", "Checking", "Money Market"]
STATEMENT_PREFS = ["Paper", "Electronic"]
MIN_DEPOSIT_CENTS = 2500


def _acct_cell(account, column: str) -> str:
    """Render one grid cell by column *name*.

    The grid is driven by the tenant's column list rather than a fixed row
    template, so a tenant that orders its columns differently is a config
    change here and a locator problem for the automation -- which is the
    point.
    """
    return {
        "Type": account["acct_type"],
        "Account No.": account["acct_number"],
        "Balance": data.money(account["balance_cents"]),
        "Opened": account["opened_at"],
    }.get(column, "")


def login_required(view):
    """Guard for everything inside the frame.

    A dropped session renders the sign-in page *in the frame* rather than
    redirecting the whole window, which is how these apps actually behave and
    is what the session-expiry recovery has to cope with.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return render_template("signin.html", framed=True, message=None)
        return view(*args, **kwargs)

    return wrapper


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "stand-in-console-not-a-real-secret"
    app.config["TENANT"] = tenants.current()

    @app.before_request
    def _open_db() -> None:
        g.db = data.connect()

    @app.before_request
    def _maybe_stall() -> None:
        """Honour a one-shot slow render armed by /debug/slow.

        One-shot rather than sticky, because the condition we want to
        reproduce is a transient stall that a bounded retry recovers from --
        not an app that is permanently down, which is a hard failure.
        """
        if request.path.startswith(("/static", "/debug")):
            return
        delay_ms = session.pop("slow_ms", 0)
        if delay_ms:
            time.sleep(min(int(delay_ms), 30000) / 1000)

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def _inject_tenant() -> dict[str, object]:
        return {
            "tenant": app.config["TENANT"],
            "money": data.money,
            "acct_cell": _acct_cell,
        }

    def _search_with_message(message: str, entered: str):
        """Bounce back to the search screen carrying a message.

        All three declared business outcomes -- not found, bad format,
        permission denied -- surface this way, as an alert on the search
        screen. Uniform on purpose: it lets an artifact declare all three
        detectors in the same shape, and it is what these apps actually do
        rather than routing you to a bespoke error page per condition.
        """
        return render_template("search.html", message=message, entered=entered)

    def _no_such_member(entered: str):
        return _search_with_message(exceptional.MSG_NOT_FOUND, entered)

    def _gate_member(member):
        """Interpose the exceptional states that guard a member record.

        Returns a response to send instead of the detail page, or None to
        carry on. Order matters: a permission denial is a final answer, while
        the two dialogs are things standing in front of a record we are
        otherwise allowed to see.
        """
        member_no = member["member_no"]
        flags = member["flags"]

        if flags == "restricted":
            return _search_with_message(exceptional.MSG_PERMISSION, member_no)

        if flags == "notice" and not session.get(f"notice_seen_{member_no}"):
            return render_template(
                "interstitial.html",
                title=exceptional.NOTICE_TITLE,
                body=exceptional.NOTICE_BODY,
                button=exceptional.NOTICE_BUTTON,
                action=f"/members/{member_no}/dismiss-notice",
            )

        if flags == "compliance_hold" and not session.get(f"hold_ack_{member_no}"):
            return render_template(
                "interstitial.html",
                title=exceptional.HOLD_TITLE,
                body=exceptional.HOLD_BODY,
                button=exceptional.HOLD_BUTTON,
                action=f"/members/{member_no}/acknowledge-hold",
            )

        return None

    @app.route("/", methods=["GET", "POST"])
    def signin():
        if request.method == "GET":
            if session.get("user"):
                return redirect(url_for("home"))
            return render_template("signin.html", framed=False, message=None)

        user = (request.form.get("user") or "").strip()

        # Checked before the credentials: once locked, a correct password does
        # not help. That is the point of a lockout, and it makes this a hard
        # failure for the auth bootstrap rather than something to retry.
        if exceptional.is_locked(user):
            return render_template(
                "signin.html", framed=False, message=exceptional.MSG_LOCKED
            )

        if user == APP_USER and request.form.get("pwd") == APP_PASS:
            exceptional.clear_signin_failures(user)
            session["user"] = APP_USER
            return redirect(url_for("home"))

        exceptional.record_signin_failure(user)
        message = (
            exceptional.MSG_LOCKED
            if exceptional.is_locked(user)
            else "Invalid user ID or password."
        )
        return render_template("signin.html", framed=False, message=message)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("signin"))

    @app.route("/home")
    def home():
        if not session.get("user"):
            return redirect(url_for("signin"))
        return render_template("shell.html", user=session["user"])

    @app.route("/members/search", methods=["GET", "POST"])
    @login_required
    def member_search():
        if request.method == "GET":
            return render_template("search.html", message=None, entered="")

        entered = (request.form.get("mbr") or "").strip()

        problem = exceptional.validate_member_id(entered)
        if problem:
            return _search_with_message(problem, entered)

        member = data.get_member(g.db, entered)
        if member is None:
            return _no_such_member(entered)
        return redirect(url_for("member_detail", member_no=member["member_no"]))

    @app.route("/members/<member_no>")
    @login_required
    def member_detail(member_no: str):
        member = data.get_member(g.db, member_no)
        if member is None:
            return _no_such_member(member_no)

        gated = _gate_member(member)
        if gated is not None:
            return gated

        return render_template(
            "detail.html",
            member=member,
            accounts=data.get_accounts(g.db, member_no),
        )

    @app.route("/members/<member_no>/dismiss-notice", methods=["POST"])
    @login_required
    def dismiss_notice(member_no: str):
        session[f"notice_seen_{member_no}"] = True
        return redirect(url_for("member_detail", member_no=member_no))

    @app.route("/members/<member_no>/acknowledge-hold", methods=["POST"])
    @login_required
    def acknowledge_hold(member_no: str):
        """Clearing a compliance hold is a person's decision.

        Nothing stops the automation pressing this button -- the app cannot
        tell who is clicking. Keeping it out of unattended reach is the
        automation's job, via the undeclared-state escalation path, which is
        exactly the seam this member is here to exercise.
        """
        session[f"hold_ack_{member_no}"] = True
        return redirect(url_for("member_detail", member_no=member_no))

    # --- deterministic triggers ------------------------------------------
    #
    # Conditions a real system produces by accident, exposed here as explicit
    # endpoints so a demo can summon them on cue. This is the payoff for
    # owning the target: session expiry and transient slowness are otherwise
    # untestable, and an error taxonomy you cannot demonstrate is a claim
    # rather than a result.

    @app.route("/debug/expire")
    def debug_expire():
        """Drop the session without signing out.

        The next framed request then renders sign-in *inside the frame*,
        which is how mid-flow expiry actually presents and what the
        reauthenticate-and-resume recovery keys off.
        """
        session.pop("user", None)
        return {"expired": True}

    @app.route("/debug/slow")
    def debug_slow():
        """Arm a one-shot stall on the next page render."""
        session["slow_ms"] = int(request.args.get("ms", 8000))
        return {"slow_ms": session["slow_ms"]}

    @app.route("/debug/reset")
    def debug_reset():
        """Clear lockouts, dismissals and arming flags so demos start clean."""
        exceptional.reset_all()
        session.clear()
        return {"reset": True}

    @app.route("/members/<member_no>/subaccount/new")
    @login_required
    def subaccount_new(member_no: str):
        member = data.get_member(g.db, member_no)
        if member is None:
            return _no_such_member(member_no)
        return render_template(
            "subaccount_new.html",
            member=member,
            acct_types=ACCOUNT_TYPES,
            stmt_prefs=STATEMENT_PREFS,
            form={},
            message=None,
        )

    @app.route("/members/<member_no>/subaccount/review", methods=["POST"])
    @login_required
    def subaccount_review(member_no: str):
        member = data.get_member(g.db, member_no)
        if member is None:
            return _no_such_member(member_no)

        form = {
            "acct_type": request.form.get("acct_type", ""),
            "deposit": request.form.get("deposit", ""),
            "purpose": request.form.get("purpose", ""),
            "stmt_pref": request.form.get("stmt_pref", ""),
        }
        cents = data.parse_money(form["deposit"])

        if form["acct_type"] not in ACCOUNT_TYPES:
            problem = "Select an account type."
        elif cents is None:
            problem = "Initial deposit must be a dollar amount."
        elif cents < MIN_DEPOSIT_CENTS:
            problem = "Initial deposit must be at least 25.00."
        elif not form["purpose"].strip():
            problem = "Purpose is required."
        elif form["stmt_pref"] not in STATEMENT_PREFS:
            problem = "Select a statement preference."
        else:
            problem = None

        if problem:
            return render_template(
                "subaccount_new.html",
                member=member,
                acct_types=ACCOUNT_TYPES,
                stmt_prefs=STATEMENT_PREFS,
                form=form,
                message=problem,
            )
        return render_template(
            "subaccount_review.html", member=member, form=form, cents=cents
        )

    @app.route("/members/<member_no>/subaccount/commit", methods=["POST"])
    @login_required
    def subaccount_commit(member_no: str):
        """The irreversible step. Once this returns, an account exists."""
        member = data.get_member(g.db, member_no)
        if member is None:
            return _no_such_member(member_no)

        acct_type = request.form.get("acct_type", "")
        cents = data.parse_money(request.form.get("deposit", "")) or 0
        acct_number = data.create_account(
            g.db, member_no, acct_type, cents, date.today().isoformat()
        )
        return render_template(
            "subaccount_done.html",
            member=member,
            acct_type=acct_type,
            acct_number=acct_number,
            cents=cents,
        )

    return app


app = create_app()
