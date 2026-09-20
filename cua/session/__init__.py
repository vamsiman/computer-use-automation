"""Live sessions: the browser, who may drive it, and how to sign in."""

from cua.session.auth import (
    MEMBER_CONSOLE,
    AuthError,
    AuthFailed,
    AuthLocked,
    AuthProfile,
    AuthResult,
    AuthUnavailable,
    Credentials,
    authenticate,
    is_authenticated,
)
from cua.session.manager import Session, SessionManager, StateChange
from cua.session.state import (
    CONTROL_BY_STATE,
    LEGAL_TRANSITIONS,
    ControlViolation,
    IllegalTransition,
    can_transition,
    control_for,
    is_terminal,
)

__all__ = [
    "AuthError",
    "AuthFailed",
    "AuthLocked",
    "AuthProfile",
    "AuthResult",
    "AuthUnavailable",
    "CONTROL_BY_STATE",
    "ControlViolation",
    "Credentials",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "MEMBER_CONSOLE",
    "Session",
    "SessionManager",
    "StateChange",
    "authenticate",
    "can_transition",
    "control_for",
    "is_authenticated",
    "is_terminal",
]
