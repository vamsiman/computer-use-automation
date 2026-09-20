"""The guardrails.

No browser: the policy engine is a pure decision function, which is the point
of it being a separate layer. It answers "may this happen?" without knowing
how anything is performed, so it can be exercised exhaustively and cheaply.

The property worth stating: every check happens *before* the action. A refusal
issued afterwards is not a guardrail, it is a log entry.
"""

from __future__ import annotations

import pytest

from cua.locators import Locator, LabelProximitySpec, RoleNameSpec
from cua.policy import (
    Allow,
    Allowlist,
    Deny,
    Mode,
    Policy,
    PolicyContext,
    PolicyEngine,
    RequireApproval,
    RiskPolicy,
    glob_to_regex,
    load_engine,
    origin_of,
    risk_rank,
)
from cua.primitives import Action
from cua.types import ActionType, RiskLevel

HERE = "http://localhost:5000/members/10001"


def button(name: str) -> Locator:
    return Locator(primary=RoleNameSpec(role="button", name=name))


def click(name: str) -> Action:
    return Action(type=ActionType.CLICK, target=button(name))


def navigate(path: str) -> Action:
    return Action(type=ActionType.NAVIGATE, args={"path": path})


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=("http://localhost:5000",),
                paths=("/", "/home", "/members/**"),
                actions=frozenset(
                    {
                        ActionType.NAVIGATE,
                        ActionType.CLICK,
                        ActionType.TYPE,
                        ActionType.EXTRACT,
                    }
                ),
            ),
            risk=RiskPolicy(unattended_max=RiskLevel.CAUTION),
        )
    )


def ctx(location: str = HERE, **kwargs) -> PolicyContext:
    return PolicyContext(location=location, **kwargs)


# --- path globbing --------------------------------------------------------


def test_single_star_stops_at_a_separator():
    """`fnmatch` would not, and that difference is a security property: a rule
    meant to permit one level would silently permit everything below it."""
    pattern = glob_to_regex("/members/*")
    assert pattern.match("/members/10001")
    assert not pattern.match("/members/10001/subaccount/new")


def test_double_star_crosses_separators():
    pattern = glob_to_regex("/members/**")
    assert pattern.match("/members/10001")
    assert pattern.match("/members/10001/subaccount/new")


def test_globs_are_anchored_at_both_ends():
    pattern = glob_to_regex("/home")
    assert pattern.match("/home")
    assert not pattern.match("/homepage")
    assert not pattern.match("/admin/home")


def test_origin_ignores_path_and_case():
    assert origin_of("HTTP://LocalHost:5000/members/1") == "http://localhost:5000"
    assert origin_of("/relative/path") == ""


def test_risk_is_ordered():
    assert risk_rank(RiskLevel.SAFE) < risk_rank(RiskLevel.CAUTION)
    assert risk_rank(RiskLevel.CAUTION) < risk_rank(RiskLevel.IRREVERSIBLE)


# --- gate one: where ------------------------------------------------------


def test_navigation_is_judged_on_its_destination(engine):
    assert engine.check(navigate("/members/search"), ctx()).permitted
    assert not engine.check(navigate("/debug/expire"), ctx()).permitted


def test_an_off_origin_navigation_is_denied(engine):
    decision = engine.check(
        Action(type=ActionType.NAVIGATE, args={"url": "https://evil.example/"}), ctx()
    )
    assert isinstance(decision, Deny)
    assert decision.rule == "allowlist.origins"


def test_other_actions_are_judged_on_where_we_already_are(engine):
    """A click on a page outside the allowlist is exactly as much of a problem
    as navigating there would have been. Checking only navigations would leave
    the gate open to anything that arrived by a redirect or a form post."""
    decision = engine.check(click("Go"), ctx("http://localhost:5000/debug/expire"))
    assert isinstance(decision, Deny)
    assert "current page" in decision.reason


def test_relative_navigation_is_resolved_before_judging(engine):
    decision = engine.check(navigate("../../debug/expire"), ctx())
    assert not decision.permitted


# --- gate two: what -------------------------------------------------------


def test_an_action_type_outside_the_allowlist_is_denied(engine):
    decision = engine.check(
        Action(type=ActionType.PRESS_KEY, args={"key": "Enter"}), ctx()
    )
    assert isinstance(decision, Deny)
    assert decision.rule == "allowlist.actions"


def test_the_two_gates_are_independent(engine):
    """A permitted action on a forbidden page, and a forbidden action on a
    permitted page, are both refused."""
    assert not engine.check(click("Search"), ctx("http://localhost:5000/debug/x")).permitted
    assert not engine.check(
        Action(type=ActionType.PRESS_KEY, args={"key": "Enter"}), ctx()
    ).permitted


# --- gate three: risk -----------------------------------------------------


def test_safe_and_caution_run_unattended(engine):
    assert engine.check(click("Search"), ctx(risk=RiskLevel.SAFE)).permitted
    assert engine.check(click("Search"), ctx(risk=RiskLevel.CAUTION)).permitted


def test_an_irreversible_step_needs_a_person(engine):
    decision = engine.check(click("Confirm"), ctx(risk=RiskLevel.IRREVERSIBLE))
    assert isinstance(decision, RequireApproval)
    assert decision.risk is RiskLevel.IRREVERSIBLE
    assert not decision.permitted, "not permitted *yet* is still not permitted"


def test_approval_unlocks_that_step(engine):
    decision = engine.check(
        click("Confirm"), ctx(risk=RiskLevel.IRREVERSIBLE, approved=True)
    )
    assert isinstance(decision, Allow)
    assert "approved by an operator" in decision.reason


def test_a_blocked_risk_level_cannot_be_approved_around():
    """A read-only deployment should be read-only, whatever an operator
    clicks."""
    engine = PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=("http://localhost:5000",),
                paths=("/members/**",),
                actions=frozenset({ActionType.CLICK}),
            ),
            risk=RiskPolicy(blocked=frozenset({RiskLevel.IRREVERSIBLE})),
        )
    )
    decision = engine.check(
        click("Confirm"), ctx(risk=RiskLevel.IRREVERSIBLE, approved=True)
    )
    assert isinstance(decision, Deny)
    assert decision.rule == "risk.blocked"


def test_location_is_checked_before_risk(engine):
    """Order matters for the message the operator sees. Being asked to approve
    an irreversible action on a page we should never have reached would bury
    the actual problem."""
    decision = engine.check(
        click("Confirm"),
        ctx("http://localhost:5000/debug/x", risk=RiskLevel.IRREVERSIBLE),
    )
    assert isinstance(decision, Deny)
    assert decision.rule.startswith("allowlist")


# --- risk inference, for discovery ---------------------------------------


def test_discovery_infers_irreversible_from_the_control_name(engine):
    """Nothing has classified the step yet, so the guess leans to caution.
    Being wrong this way costs an approval prompt; being wrong the other way
    opens an account."""
    for name in ("Confirm", "Submit Transfer", "Delete Member", "Post Entry"):
        assert engine.infer_risk(click(name)) is RiskLevel.IRREVERSIBLE, name


def test_discovery_treats_an_ordinary_click_as_caution_not_safe(engine):
    """Any click may submit something. Safe would be an assumption we have no
    basis for on an application the model has not seen before."""
    assert engine.infer_risk(click("Search")) is RiskLevel.CAUTION


def test_reading_the_screen_is_safe(engine):
    assert engine.infer_risk(Action(type=ActionType.EXTRACT)) is RiskLevel.SAFE
    assert engine.infer_risk(navigate("/members/search")) is RiskLevel.SAFE


def test_typing_is_caution(engine):
    action = Action(
        type=ActionType.TYPE,
        target=Locator(primary=LabelProximitySpec(label="Member ID:")),
        args={"value": "10001"},
    )
    assert engine.infer_risk(action) is RiskLevel.CAUTION


def test_inference_only_applies_when_the_artifact_has_not_spoken(engine):
    """A declared risk always wins. The artifact has been reviewed; the guess
    has not."""
    decision = engine.check(
        click("Confirm"), ctx(risk=RiskLevel.SAFE, mode=Mode.DISCOVERY)
    )
    assert decision.permitted


def test_inference_is_used_when_no_risk_is_declared(engine):
    decision = engine.check(click("Confirm"), ctx(mode=Mode.DISCOVERY))
    assert isinstance(decision, RequireApproval)


# --- the shipped policy ---------------------------------------------------


def test_the_checked_in_policy_loads():
    engine = load_engine()
    assert engine.policy.risk.unattended_max is RiskLevel.CAUTION
    assert ActionType.CLICK in engine.policy.allowlist.actions


def test_the_shipped_policy_refuses_the_debug_endpoints():
    """Those exist so a demo can summon failure states. A run that could
    expire its own session or slow its own page down would make the error
    taxonomy untestable in the one way that matters."""
    engine = load_engine()
    for path in ("/debug/expire", "/debug/slow?ms=8000", "/debug/reset"):
        assert not engine.check(navigate(path), ctx()).permitted, path


def test_the_shipped_policy_allows_the_read_capability_path():
    engine = load_engine()
    assert engine.check(navigate("/members/search"), ctx()).permitted
    assert engine.check(
        navigate("/members/10001/subaccount/new"), ctx()
    ).permitted


def test_decisions_are_falsy_when_refused(engine):
    """So `if not engine.check(...)` reads correctly at every call site."""
    assert engine.check(click("Search"), ctx(risk=RiskLevel.SAFE))
    assert not engine.check(navigate("/debug/expire"), ctx())
    assert not engine.check(click("Confirm"), ctx(risk=RiskLevel.IRREVERSIBLE))
