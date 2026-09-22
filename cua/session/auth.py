"""Signing in, deliberately kept outside the artifact.

Authentication could have been discovered and recorded like any other flow.
It is not, and the reason is the strongest guarantee in the safety model:
credentials are never an artifact input, so they are *structurally incapable*
of ending up in a saved capability, a log line or an evidence bundle. There is
nothing to redact, which is better than redacting well. The cost is that
signing in is one thing the system cannot learn by itself.

The sign-in procedure is data rather than code. An ``AuthProfile`` names the
fields, the button and how to tell the three states apart, so a second
application -- or a tenant whose login page says "Operator ID" -- is a new
profile, not a new code path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

from cua.locators import LabelProximitySpec, Locator, RoleNameSpec
from cua.primitives import Action
from cua.surface.base import Surface
from cua.types import ActionType


class AuthError(RuntimeError):
    """Base for everything that can go wrong signing in."""


class AuthFailed(AuthError):
    """Credentials were rejected."""


class AuthLocked(AuthError):
    """The account is locked.

    A hard failure, not a recoverable condition. Retrying is what caused it,
    and retrying harder makes it worse -- a person has to unlock it.
    """


class AuthUnavailable(AuthError):
    """The sign-in page did not appear, or did not behave as the profile says."""


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "Credentials":
        user = os.environ.get("CUA_APP_USER", "teller1")
        password = os.environ.get("CUA_APP_PASS", "demo-pass-2024")
        return cls(user=user, password=password)

    def __repr__(self) -> str:
        # Not cosmetic. A dataclass repr containing a password ends up in
        # tracebacks, log lines and crash reports without anyone deciding it
        # should, which is precisely how regulated credentials leak.
        return f"Credentials(user={self.user!r}, password='***')"

    __str__ = __repr__


@dataclass(frozen=True)
class AuthProfile:
    """How to sign in to one application."""

    entry_path: str
    user_field: Locator
    password_field: Locator
    submit: Locator
    #: Visible once signed in.
    signed_in: Locator
    #: Visible when a sign-in is being asked for.
    sign_in_form: Locator
    #: Visible when the account has been locked out.
    locked: Locator
    #: Visible when credentials were wrong but the account is still usable.
    rejected: Locator


def _role(role: str, name: str) -> Locator:
    return Locator(primary=RoleNameSpec(role=role, name=name))


def _alert_containing(text: str) -> Locator:
    return Locator(primary=RoleNameSpec(role="alert", name_contains=text))


#: The stand-in console. Both credential fields have bare <span> labels with
#: no programmatic association, so they are only reachable by reasoning about
#: the label beside them -- the same tier-2 path the rest of the app needs.
MEMBER_CONSOLE = AuthProfile(
    entry_path="/",
    user_field=Locator(
        primary=RoleNameSpec(role="textbox", name="User ID"),
        fallbacks=(LabelProximitySpec(label="User ID:", direction="right"),),
    ),
    password_field=Locator(
        primary=RoleNameSpec(role="textbox", name="Password"),
        fallbacks=(LabelProximitySpec(label="Password:", direction="right"),),
    ),
    submit=_role("button", "Sign In"),
    #: Where a fresh sign-in lands. Not used to *decide* whether we are
    #: authenticated -- a session can drop anywhere, and this names one
    #: screen -- but it is the profile's statement of where the front door
    #: leads.
    signed_in=_role("heading", "Member Search"),
    sign_in_form=_role("heading", "Sign In"),
    locked=_alert_containing("Account locked"),
    rejected=_alert_containing("Invalid user ID or password"),
)


@dataclass(frozen=True)
class AuthResult:
    already_authenticated: bool
    attempts: int
    #: What the application said it was, if it said anything.
    #:
    #: Read here because this is the one screen we are guaranteed to look at.
    #: Legacy software announces its version on the sign-in page and nowhere
    #: else, and the bootstrap is the only part of a run that always sees it.
    #: A capability can then be refused against a version it was not recorded
    #: for, which is what ``AppRef.version_range`` has always promised and
    #: never delivered.
    app_version: str | None = None


#: How software names itself on a sign-in screen: "MemberConsole 4.3.1",
#: "Release 4.3.1 (build 9912)", "v4.3.1". Anything with a dotted number in
#: it will do; the parsing happens in :mod:`cua.artifact.compat`.
#: A dotted number anywhere in a node's text. Software announces itself
#: as "MemberConsole 4.3.1", "Release 4.3.1 (build 9912)", "v4.3.1"; the
#: number is the only part worth agreeing on.
_VERSION_HINT = re.compile(r"[0-9]+(?:\.[0-9]+)+")


def read_app_version(snapshot) -> str | None:
    """What the application says it is, from the sign-in screen.

    Returns the *shortest* piece of text carrying a version number. A
    legacy page is a nest of tables, so the same number appears on the
    little cell that holds it and again on every ancestor cell that
    swallowed it -- "Sign In MemberConsole 4.3.1 User ID: Password:" is a
    real node on this page. The shortest is the one that was put there to
    say the version.

    Best effort and deliberately so. A version we cannot find is not an
    error -- most applications never say -- and the compatibility check
    treats unknown as permitted.
    """
    found: list[str] = []
    for node in snapshot.tree.walk():
        text = (node.name or node.value or "").strip()
        if text and _VERSION_HINT.search(text):
            found.append(text)
    return min(found, key=len) if found else None


def _is_blank(snapshot) -> bool:
    """Is there a page here at all?

    This check exists because of a real bug. Authentication was decided by the
    *absence* of a sign-in form, which is sound while looking at the
    application and nonsense while looking at ``about:blank`` -- a fresh
    browser has no sign-in form either, and so reported itself as signed in.
    Every subsequent step then ran against a blank tab and failed for reasons
    that had nothing to do with the cause.

    Absence of evidence is not evidence of absence. A page with nothing on it
    tells us nothing about our session.
    """
    return (
        snapshot.location in ("", "about:blank")
        or not snapshot.tree.children
    )


def _looks_like_the_application(snapshot) -> bool:
    """Are we on a page this application rendered?

    Deliberately generic: a session can drop anywhere, so this cannot key on
    one particular screen. Every page of the console renders its title as a
    real heading, and nothing outside it does -- a JSON body from a debug
    endpoint has no heading, and neither does a blank tab. It is a weaker
    signal than naming a screen, and it is the strongest one that is true
    everywhere the question gets asked.
    """
    return any(
        node.role == "heading" and node.name for node in snapshot.tree.walk()
    )


def _recognised(surface: Surface, snapshot, profile: AuthProfile) -> bool:
    """Are we looking at something this profile knows how to reason about?

    Either the sign-in form or a screen of the application. Anything else --
    a blank tab, a JSON body, a proxy error -- is not evidence either way, and
    the only honest response is to go somewhere we understand before deciding.
    """
    if _is_blank(snapshot):
        return False
    return (
        surface.resolve(profile.sign_in_form, snapshot).resolved
        or _looks_like_the_application(snapshot)
    )


def is_authenticated(surface: Surface, profile: AuthProfile = MEMBER_CONSOLE) -> bool:
    """True when the current page is in the application and not asking us to
    sign in.

    Keyed on the sign-in form rather than on reaching a particular screen,
    because a session can drop anywhere in the app and the answer should be
    the same wherever that happens. A blank page is reported as *not*
    authenticated: we genuinely do not know, and guessing optimistically is
    what produced the bug described above.
    """
    snapshot = surface.observe()
    if _is_blank(snapshot):
        return False
    if surface.resolve(profile.sign_in_form, snapshot).resolved:
        return False
    # Confirmed positively, not inferred. A page that is neither the sign-in
    # form nor a screen of the application -- a JSON response from a debug
    # endpoint, an error page, anything at all -- says nothing about our
    # session, and reading it as "signed in" is the same mistake as reading a
    # blank tab that way.
    return _looks_like_the_application(snapshot)


def authenticate(
    surface: Surface,
    credentials: Credentials | None = None,
    profile: AuthProfile = MEMBER_CONSOLE,
) -> AuthResult:
    """Establish an authenticated session. Idempotent.

    Raises rather than returning a status, because every caller -- session
    setup and the session-expiry recovery alike -- has nothing useful to do
    with a failure except stop.
    """
    credentials = credentials or Credentials.from_env()

    snapshot = surface.observe()
    if not _recognised(surface, snapshot, profile):
        # Nowhere we recognise -- a blank tab, or a page that is neither the
        # sign-in form nor the application. Go to the entry point before
        # drawing any conclusion about whether we are signed in.
        surface.act(
            Action(type=ActionType.NAVIGATE, args={"path": profile.entry_path})
        )
        snapshot = surface.observe()

    if not surface.resolve(profile.sign_in_form, snapshot).resolved:
        # Already signed in, so the sign-in screen is not in front of us and
        # its version hint is not available. Not a problem: unknown is the
        # answer the compatibility check is built to tolerate.
        return AuthResult(already_authenticated=True, attempts=0)

    version = read_app_version(snapshot)

    # Note what is deliberately *not* here: a check for an error banner before
    # submitting. A message already on the form describes the previous
    # attempt, not this one, and reading it as our own result meant a retry
    # reported failure without ever submitting -- so the attempt counter never
    # advanced and a lockout could never be reached or reported. Refusals are
    # only interpreted after we have actually asked.

    typed_user = surface.act(
        Action(
            type=ActionType.TYPE,
            target=profile.user_field,
            args={"value": credentials.user},
            intent="Enter the operator user id",
        )
    )
    typed_password = surface.act(
        Action(
            type=ActionType.TYPE,
            target=profile.password_field,
            args={"value": credentials.password},
            intent="Enter the operator password",
        )
    )
    for result in (typed_user, typed_password):
        if not result.ok:
            raise AuthUnavailable(f"could not fill the sign-in form: {result.error}")

    submitted = surface.act(
        Action(
            type=ActionType.CLICK, target=profile.submit, intent="Submit the sign-in"
        )
    )
    if not submitted.ok:
        raise AuthUnavailable(f"could not submit the sign-in form: {submitted.error}")

    after = surface.observe()
    _raise_if_blocked(surface, after, profile)

    if surface.resolve(profile.sign_in_form, after).resolved:
        # Still looking at the form with no message we recognise.
        raise AuthUnavailable("sign-in did not complete and gave no reason")

    return AuthResult(
        already_authenticated=False, attempts=1, app_version=version
    )


def _raise_if_blocked(surface: Surface, snapshot, profile: AuthProfile) -> None:
    """Turn the two recognised refusals into distinct exceptions.

    Lockout is checked first: once locked, the application reports lockout
    rather than a bad password even when the password is right, and treating
    that as a credential problem would send a caller off retrying the one
    thing guaranteed not to help.
    """
    if surface.resolve(profile.locked, snapshot).resolved:
        raise AuthLocked("the operator account is locked; a person must clear it")
    if surface.resolve(profile.rejected, snapshot).resolved:
        raise AuthFailed("the operator credentials were rejected")


def profile_for_tenant(tenant: str, base: AuthProfile = MEMBER_CONSOLE) -> AuthProfile:
    """Per-tenant sign-in wording, should it ever differ.

    A placeholder with a real shape: tenants share this vendor product, so the
    override is expected to be one or two locators rather than a new profile.
    """
    return replace(base)
