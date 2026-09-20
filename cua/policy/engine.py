"""The gate every action passes through before it happens.

Checked *before* acting, never after. An irreversible step that is refused
after it has run is not a guardrail, it is a log entry.

On the brief's question of what to do with the risky class -- block, require
confirmation, or flag -- this system requires confirmation. Blocking would
make the write capability useless for the only thing it exists to do, and
flagging is far too weak for something that opens an account. Requiring a
person to agree is the option that keeps the capability useful while keeping
the decision with a human, and it costs nothing extra to build because the
escalation machinery for taking over a live session already exists. Approval
is routed down exactly that path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin

from cua.locators import Locator, RoleNameSpec
from cua.policy.config import Policy
from cua.primitives import Action
from cua.types import ActionType, RiskLevel


class Mode(StrEnum):
    """Which engine is asking.

    Risk is read off the artifact during replay. During discovery nothing has
    classified the step yet, so it has to be guessed -- and guessed
    pessimistically, because the model is exploring an application it has not
    seen before.
    """

    REPLAY = "replay"
    DISCOVERY = "discovery"


@dataclass(frozen=True)
class Decision:
    permitted: bool
    reason: str = ""
    #: Which gate produced this, for the evidence log.
    rule: str = ""

    def __bool__(self) -> bool:
        return self.permitted


@dataclass(frozen=True)
class Allow(Decision):
    permitted: bool = True


@dataclass(frozen=True)
class Deny(Decision):
    permitted: bool = False


@dataclass(frozen=True)
class RequireApproval(Decision):
    """Not permitted *yet*. A person has to agree first."""

    permitted: bool = False
    risk: RiskLevel = RiskLevel.IRREVERSIBLE


@dataclass(frozen=True)
class PolicyContext:
    #: Where the browser is right now.
    location: str
    #: Declared risk of the step. None during discovery, where it is inferred.
    risk: RiskLevel | None = None
    mode: Mode = Mode.REPLAY
    step_id: str | None = None
    #: True once a human has agreed to this specific step.
    approved: bool = False


def target_label(locator: Locator | None) -> str:
    """Best-effort human name for what an action is aimed at."""
    if locator is None:
        return ""
    spec = locator.primary
    if isinstance(spec, RoleNameSpec):
        return (spec.name or spec.name_contains or "").lower()
    return locator.describe().lower()


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    # --- risk ------------------------------------------------------------

    def infer_risk(self, action: Action) -> RiskLevel:
        """Classify an action the artifact has not classified.

        Used during discovery. The bias is deliberate: a click whose control
        is called "Confirm" is treated as irreversible without waiting to find
        out, because the cost of being wrong in that direction is one
        approval prompt, and the cost of being wrong the other way is an
        account opened by accident.
        """
        if action.type in (
            ActionType.EXTRACT,
            ActionType.WAIT_FOR,
            ActionType.NAVIGATE,
        ):
            return RiskLevel.SAFE

        if action.type is ActionType.CLICK:
            label = target_label(action.target)
            if any(hint in label for hint in self.policy.risk.irreversible_hints):
                return RiskLevel.IRREVERSIBLE
            # Any other click may still submit something. Caution, not safe.
            return RiskLevel.CAUTION

        return RiskLevel.CAUTION

    # --- the gate --------------------------------------------------------

    def check(self, action: Action, context: PolicyContext) -> Decision:
        where = self._check_location(action, context)
        if not where.permitted:
            return where

        what = self._check_action_type(action)
        if not what.permitted:
            return what

        return self._check_risk(action, context)

    def _check_location(self, action: Action, context: PolicyContext) -> Decision:
        """Gate one: are we allowed to be doing anything here?

        For a navigation the destination is what matters. For everything else
        it is where we already are -- a click on a page outside the allowlist
        is exactly as much of a problem as navigating there would have been.
        """
        if action.type is ActionType.NAVIGATE:
            target = action.args.get("url") or action.args.get("path") or "/"
            url = urljoin(context.location or "", target)
            subject = "destination"
        else:
            url = context.location
            subject = "current page"

        if not self.policy.allowlist.permits_origin(url):
            return Deny(
                reason=f"{subject} {url!r} is not on an allowlisted origin",
                rule="allowlist.origins",
            )
        if not self.policy.allowlist.permits_path(url):
            return Deny(
                reason=f"{subject} {url!r} is not on an allowlisted path",
                rule="allowlist.paths",
            )
        return Allow(rule="allowlist")

    def _check_action_type(self, action: Action) -> Decision:
        if not self.policy.allowlist.permits_action(action.type):
            return Deny(
                reason=f"action {action.type} is not permitted",
                rule="allowlist.actions",
            )
        return Allow(rule="allowlist.actions")

    def _check_risk(self, action: Action, context: PolicyContext) -> Decision:
        level = context.risk if context.risk is not None else self.infer_risk(action)

        if self.policy.risk.is_blocked(level):
            return Deny(
                reason=f"{level} actions are blocked by policy",
                rule="risk.blocked",
            )

        if not self.policy.risk.needs_approval(level):
            return Allow(rule="risk")

        if context.approved:
            return Allow(reason=f"{level} step approved by an operator", rule="risk")

        described = target_label(action.target) or action.type
        return RequireApproval(
            reason=(
                f"{level} step ({described}) needs a person to agree; "
                f"unattended limit is {self.policy.risk.unattended_max}"
            ),
            rule="risk.unattended_max",
            risk=level,
        )


def load_engine(path=None) -> PolicyEngine:
    from cua.policy.config import DEFAULT_POLICY_PATH

    return PolicyEngine(Policy.load(path or DEFAULT_POLICY_PATH))
