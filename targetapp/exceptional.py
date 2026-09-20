"""The ways this console refuses to cooperate.

The brief is explicit that the hard part of replay is not layout drift -- these
UIs are stable -- but the runtime conditions that legitimately occur: validation
errors, "record not found", permission denials, unexpected dialogs, session
expiry, transient slowness. A capability that only works on the happy path is
not useful in production.

So every one of those has to be reachable here, deterministically, on demand.
That is the whole reason we own the target instead of pointing at a public demo
site: you cannot ask someone else's server to expire your session on cue.

The exact message strings below are part of the contract. Capability artifacts
declare detectors against them, so changing the wording here is a breaking
change for every artifact recorded against this app -- which is itself a
faithful reproduction of what vendor upgrades do to real automation.
"""

from __future__ import annotations

import re

# --- messages the automation detects on -----------------------------------

MSG_NOT_FOUND = "No records found."
MSG_VALIDATION = "Member ID must be 5 digits."
MSG_PERMISSION = "You are not authorized to view this member."
MSG_LOCKED = "Account locked after 3 failed attempts. Contact your administrator."

#: A *known* interstitial. Artifacts declare a recovery that dismisses it.
NOTICE_TITLE = "System Notice"
NOTICE_BODY = (
    "Scheduled maintenance is planned for Sunday 02:00-04:00 ET. "
    "Member servicing will be unavailable during this window."
)
NOTICE_BUTTON = "Continue"

#: A blocking state artifacts do NOT declare. Deliberately titled differently
#: from the notice above: a recovery written for "System Notice" must not
#: accidentally swallow a compliance hold, because the two mean very different
#: things and only one of them is safe to click through unattended.
HOLD_TITLE = "Compliance Hold"
HOLD_BODY = (
    "This member is flagged for compliance review. Contact the compliance "
    "desk before servicing this account."
)
HOLD_BUTTON = "Acknowledge"

MEMBER_ID_RE = re.compile(r"^\d{5}$")

#: Failed sign-ins before the account locks.
MAX_SIGNIN_ATTEMPTS = 3

# Module-level because lockout has to survive across sessions -- a caller who
# keeps retrying with fresh cookies should still find the account locked.
_failed_signins: dict[str, int] = {}


def validate_member_id(raw: str) -> str | None:
    """Return a validation message, or None when the input is well formed.

    Note this is *format* validation only. A well-formed id that matches no
    member is a different condition ("no records found") and the two must not
    be collapsed: one means the caller sent us garbage, the other is a
    legitimate answer about the world.
    """
    if not MEMBER_ID_RE.match((raw or "").strip()):
        return MSG_VALIDATION
    return None


def record_signin_failure(user: str) -> None:
    _failed_signins[user] = _failed_signins.get(user, 0) + 1


def clear_signin_failures(user: str) -> None:
    _failed_signins.pop(user, None)


def is_locked(user: str) -> bool:
    return _failed_signins.get(user, 0) >= MAX_SIGNIN_ATTEMPTS


def reset_all() -> None:
    """Clear lockout state. Used by /debug/reset so demos start clean."""
    _failed_signins.clear()
