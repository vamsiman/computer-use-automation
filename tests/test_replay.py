"""The replay engine, against a surface that does as it is told.

A fake surface is the right instrument here. The questions are about the
engine's decisions -- when it stops, what it calls the stop, what it checks
before acting -- and a real browser would answer them slowly and with noise.
The live version, driving the real application through the reference
capability, is in ``test_replay_live.py``.

The tests are organised around the four results, because the four results are
the design. A business outcome reported as a failure and a failure reported as
a business outcome are both single-line mistakes that take a fortnight to
notice.
"""

from __future__ import annotations

import pytest

from cua.artifact.store import CapabilityStore
from cua.locators import Locator, RoleNameSpec
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.primitives import A11yNode, ActResult, Snapshot
from cua.replay import (
    BusinessOutcome,
    Escalated,
    Failure,
    ReplayEngine,
    Success,
    apply_transform,
    validate_inputs,
)
from cua.locators import Resolution
from cua.types import ActionType, FailureCategory, LocatorStrategy, RiskLevel

BASE = "http://localhost:5000"


def node(role, name="", *, children=(), value=None):
    return A11yNode(role=role, name=name, value=value, children=list(children))


def screen(heading: str, *extra, location: str = f"{BASE}/members/search", modal=None):
    return Snapshot(
        location=location,
        tree=node("document", heading, children=[node("heading", heading), *extra]),
        modal_text=modal,
    )


#: Real timeouts are for real pages. The value is not what these tests are
#: about, and eight seconds of polling per failed checkpoint turns a fast suite
#: into a slow one.
FAST_MS = 50


@pytest.fixture
def artifact():
    loaded = CapabilityStore().load("member.read_savings_balance")
    for step in loaded.steps:
        if step.checkpoint is not None:
            step.checkpoint.timeout_ms = FAST_MS
    if loaded.success.checkpoint is not None:
        loaded.success.checkpoint.timeout_ms = FAST_MS
    return loaded


@pytest.fixture
def engine_policy() -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(BASE,),
                paths=("/", "/home", "/members/**"),
                actions=frozenset(
                    {
                        ActionType.NAVIGATE,
                        ActionType.CLICK,
                        ActionType.TYPE,
                        ActionType.EXTRACT,
                        ActionType.WAIT_FOR,
                    }
                ),
            ),
            risk=RiskPolicy(unattended_max=RiskLevel.CAUTION),
        )
    )


#: Acts that make a legacy web app show a different page. Typing into a field
#: does not, and a fake that pretends otherwise lets a broken checkpoint look
#: like a working one.
PAGE_CHANGING = (ActionType.NAVIGATE, ActionType.CLICK)


class FakeSurface:
    """A scripted application: one screen, then the next.

    ``screens[0]`` is where we start; every navigate or click moves along one,
    and the last screen repeats. ``unresolvable`` names locator descriptions
    that should fail to resolve, which is how a member with no savings row is
    simulated.
    """

    def __init__(self, screens, *, unresolvable=(), values=None, advance_on=None):
        self.screens = list(screens)
        self.index = 0
        self.unresolvable = set(unresolvable)
        self.values = dict(values or {})
        self.advance_on = tuple(advance_on or PAGE_CHANGING)
        self.acted = []

    def observe(self) -> Snapshot:
        return self.screens[min(self.index, len(self.screens) - 1)]

    def resolve(self, locator) -> Resolution:
        """Walk the chain, like the real resolver does.

        Checking only the primary would make every locator whose primary is
        aspirational look broken -- which is most of the interesting ones in
        this application, and precisely the case the fallback chain exists for.
        Role+name is matched against the tree; the geometric and structural
        rules are taken on trust, since their real behaviour is tested against
        real markup elsewhere.
        """
        described = locator.describe()
        if any(bad in described for bad in self.unresolvable):
            return Resolution(resolved=False, detail=f"no tier resolved {described}")

        snapshot = self.observe()
        for tier, spec in enumerate(locator.chain):
            if isinstance(spec, RoleNameSpec) and not self._named(snapshot, spec):
                continue
            return Resolution(
                resolved=True,
                tier=tier,
                strategy=spec.strategy,
                handle=object(),
                match_count=1,
            )
        return Resolution(resolved=False, detail=f"no tier resolved {described}")

    @staticmethod
    def _named(snapshot, spec) -> bool:
        for candidate in snapshot.tree.walk():
            if candidate.role != spec.role:
                continue
            if spec.name is not None and candidate.name == spec.name:
                return True
            if spec.name_contains and spec.name_contains in candidate.name:
                return True
            if spec.name is None and not spec.name_contains:
                return True
        return False

    def act(self, action) -> ActResult:
        self.acted.append(action)
        if action.type in self.advance_on and self.index < len(self.screens) - 1:
            self.index += 1
        value = None
        if action.type is ActionType.EXTRACT:
            value = self.values.get(str(action.args.get("into")))
        return ActResult(ok=True, tier=0, value=value, observed=self.observe())

    def screenshot(self, path=None) -> str:
        return path or "shot.png"

    def close(self) -> None:
        pass


def run(artifact, surface, policy, **kwargs):
    inputs = kwargs.pop("inputs", {"member_id": "10001"})
    return ReplayEngine(surface, artifact, policy=policy, **kwargs).run(inputs)


# --- the contract, checked before anything is touched --------------------


def test_a_missing_input_fails_before_the_browser_is_used(artifact, engine_policy):
    """The cheapest check in the system runs first. Four steps of real work
    followed by "you did not supply a member number" is the wrong order."""
    surface = FakeSurface([screen("Member Search")])
    result = ReplayEngine(surface, artifact, policy=engine_policy).run({})

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.CONTRACT
    assert surface.acted == []


def test_an_ill_typed_input_is_refused_rather_than_coerced(artifact):
    problems = validate_inputs(artifact, {"member_id": "abc"})
    assert problems and "does not match" in problems[0]


def test_an_unknown_input_is_rejected_not_ignored(artifact):
    """Far more often a misspelling of a real parameter than a harmless extra.
    Dropping it silently runs the capability without the value the caller
    believed they had supplied."""
    problems = validate_inputs(artifact, {"member_id": "10001", "membr_id": "10002"})
    assert problems and "unknown input" in problems[0]


def test_a_missing_value_is_never_defaulted(artifact):
    assert validate_inputs(artifact, {"member_id": None})


def test_a_draft_capability_does_not_run_unattended(artifact, engine_policy):
    """Draft means no person has read it. The whole reason distillation
    produces a document is that somebody reads it first."""
    artifact.capability.status = "draft"
    result = ReplayEngine(
        FakeSurface([screen("Member Search")]), artifact, policy=engine_policy
    ).run({"member_id": "10001"})

    assert isinstance(result, Failure)
    assert "draft" in result.detail


def test_a_draft_runs_when_it_is_asked_for_explicitly(artifact, engine_policy):
    artifact.capability.status = "draft"
    surface = FakeSurface(
        HAPPY_PATH,
        values={"member_name": "Dana Whitfield", "savings_balance": "4,210.33"},
    )
    result = ReplayEngine(
        surface, artifact, policy=engine_policy, allow_draft=True
    ).run({"member_id": "10001"})
    assert isinstance(result, Success)


# --- success --------------------------------------------------------------


#: start on search, navigate stays on search, the click lands on details.
HAPPY_PATH = [
    screen("Member Search"),
    screen("Member Search"),
    screen("Member Details"),
]


@pytest.fixture
def happy_surface():
    return FakeSurface(
        HAPPY_PATH,
        values={"member_name": "Dana Whitfield", "savings_balance": "$4,210.33"},
    )


def test_a_clean_run_returns_typed_outputs(artifact, happy_surface, engine_policy):
    result = run(artifact, happy_surface, engine_policy)

    assert isinstance(result, Success)
    assert result.ok
    assert result.outputs["member_name"] == "Dana Whitfield"
    # Normalised here once, rather than by every caller slightly differently.
    assert result.outputs["savings_balance"] == "4210.33"


def test_the_supplied_value_is_what_gets_typed(artifact, happy_surface, engine_policy):
    run(artifact, happy_surface, engine_policy, inputs={"member_id": "10002"})
    typed = next(a for a in happy_surface.acted if a.type is ActionType.TYPE)
    assert typed.args["value"] == "10002"


def test_the_tier_that_resolved_is_logged_for_every_targeted_step(
    artifact, happy_surface, engine_policy
):
    """Collected on success too. Nobody reads the logs of runs that worked,
    which is exactly why the drift signal has to be in the result."""
    result = run(artifact, happy_surface, engine_policy)
    logged = {entry.step_id for entry in result.tier_log}
    targeted = {s.id for s in artifact.steps if s.target is not None}
    assert logged == targeted
    assert all(entry.strategy for entry in result.tier_log)


def test_reaching_the_right_screen_without_the_number_is_not_success(
    artifact, engine_policy
):
    surface = FakeSurface(
        HAPPY_PATH,
        values={"member_name": "Dana Whitfield"},  # no balance
    )
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Failure)
    assert "savings_balance" in result.expected


# --- business outcomes ----------------------------------------------------


def test_no_such_member_is_an_answer_not_a_failure(artifact, engine_policy):
    """The distinction the brief calls the most common design mistake. The
    system worked; the member does not exist."""
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Member Search", node("alert", "No records found")),
        ]
    )
    result = run(artifact, surface, engine_policy, inputs={"member_id": "99999"})

    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.ok is False, "a business outcome is not a balance"


def test_an_outcome_keeps_whatever_was_already_read(artifact, engine_policy):
    """The refusal lands partway through reading the record -- the second field
    is checked against a permission the first was not. A caller may well still
    want the member's name it did get."""
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Member Details"),
            screen("Member Details", node("alert", "not authorized")),
        ],
        values={"member_name": "Dana Whitfield"},
        advance_on=(ActionType.NAVIGATE, ActionType.CLICK, ActionType.EXTRACT),
    )
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, BusinessOutcome)
    assert result.code == "PERMISSION_DENIED"
    assert result.partial_outputs["member_name"] == "Dana Whitfield"


def test_an_outcome_is_read_even_on_a_screen_that_looks_healthy(
    artifact, engine_policy
):
    """"No records found" renders on a page working exactly as designed, so
    outcomes are evaluated after every action rather than only when something
    has gone wrong."""
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Member Search", node("alert", "must be 5 digits")),
        ]
    )
    result = run(artifact, surface, engine_policy)
    assert isinstance(result, BusinessOutcome)
    assert result.code == "VALIDATION_ERROR"


# --- recoveries -----------------------------------------------------------


def test_a_declared_interstitial_is_dismissed_and_never_mentioned(
    artifact, engine_policy
):
    """The caller does not hear about this. Dismissing a maintenance notice is
    not news -- but the count belongs in the result, because a recovery firing
    twice as often this month as last is how drift announces itself."""
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen(
                "System Notice", node("dialog", "System Notice"), modal="System Notice"
            ),
            screen("Member Details"),
        ],
        values={"member_name": "Dana Whitfield", "savings_balance": "4,210.33"},
    )
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Success)
    assert "SYSTEM_NOTICE" in " ".join(result.recoveries)


def test_the_dismiss_recovery_does_not_match_a_compliance_hold(
    artifact, engine_policy
):
    """The two dialogs are structurally identical and differ only by name, so
    a detector cannot be right by luck. One is noise; the other is a person's
    decision."""
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen(
                "Compliance Hold",
                node("dialog", "Compliance Hold"),
                modal="Account flagged - contact compliance",
            ),
        ]
    )
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Escalated)
    assert "compliance" in result.observed.lower()


def test_a_recovery_past_its_budget_becomes_a_failure(artifact, engine_policy):
    """A recovery without a budget is an infinite loop waiting for a bad day."""
    notice = screen(
        "System Notice", node("dialog", "System Notice"), modal="System Notice"
    )
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            notice,
            notice,
            notice,
            notice,
        ]
    )
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.RECOVERY_EXHAUSTED


def test_an_expired_session_is_re_authenticated_and_resumed(artifact, engine_policy):
    """Signing in is not a step in the artifact, so resuming has to call the
    bootstrap. Passed in as a callable, which is how replay avoids importing
    anything that has seen a credential."""
    calls = []
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Sign In", location=f"{BASE}/"),
            screen("Member Search"),
            screen("Member Details"),
        ],
        values={"member_name": "Dana Whitfield", "savings_balance": "4,210.33"},
    )
    result = ReplayEngine(
        surface,
        artifact,
        policy=engine_policy,
        reauthenticate=lambda: calls.append("signed in"),
    ).run({"member_id": "10001"})

    assert calls == ["signed in"]
    assert isinstance(result, (Success, Failure))
    assert "SESSION_EXPIRED" in " ".join(result.recoveries)


def test_an_expired_session_with_no_way_back_escalates(artifact, engine_policy):
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Sign In", location=f"{BASE}/"),
        ]
    )
    result = run(artifact, surface, engine_policy)
    assert isinstance(result, Escalated)


# --- failures and escalation ---------------------------------------------


def test_a_locator_that_no_longer_resolves_is_a_failure(artifact, engine_policy):
    """The member exists and has no savings row. Nothing declared covers it,
    and nothing is blocking the screen, so this is the automation meeting a
    world it was not told about -- a failure, not an outcome."""
    surface = FakeSurface(
        HAPPY_PATH,
        unresolvable=["Savings"],
        values={"member_name": "Dana Whitfield"},
    )
    result = run(artifact, surface, engine_policy, inputs={"member_id": "10006"})

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.LOCATOR_UNRESOLVED
    assert result.step_id == "s5"


def test_a_failure_says_what_it_expected_and_what_it_saw(artifact, engine_policy):
    """"Expected the Member Details heading, observed the search screen" is a
    diagnosis. "Step 4 failed" is a notification that somebody now has to go
    and reproduce it."""
    surface = FakeSurface([screen("Member Search")] * 3)
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Failure)
    assert result.expected and result.observed
    assert result.intent


def test_an_undeclared_dialog_escalates_rather_than_being_reasoned_about(
    artifact, engine_policy
):
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Whatever", node("dialog", "Something New"), modal="Something New"),
        ]
    )
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Escalated)
    assert result.step_id == "s3"
    assert result.intent


def test_a_human_who_clears_the_block_lets_the_run_continue(artifact, engine_policy):
    """Section 3.6's contract: control comes back and the engine re-verifies
    rather than taking anyone's word for it."""
    handled = []
    screens = [
        screen("Member Search"),
        screen("Member Search"),
        screen("Whatever", node("dialog", "Something New"), modal="Something New"),
        screen("Member Details"),
    ]
    surface = FakeSurface(
        screens, values={"member_name": "Dana", "savings_balance": "4,210.33"}
    )

    def escalate(step, reason, snapshot):
        handled.append(step.id)
        surface.index = 3  # the operator dealt with the dialog
        return True

    result = ReplayEngine(
        surface, artifact, policy=engine_policy, escalate=escalate
    ).run({"member_id": "10001"})

    assert handled == ["s3"]
    assert isinstance(result, Success)


def test_the_run_is_never_resumed_on_the_humans_word_alone(artifact, engine_policy):
    """The human is the higher authority and the world is still the world. An
    escalation handler that says "done" without the screen changing must not
    produce a successful run."""
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Whatever", node("dialog", "Something New"), modal="Something New"),
        ]
    )
    result = ReplayEngine(
        surface, artifact, policy=engine_policy, escalate=lambda *a: True
    ).run({"member_id": "10001"})

    assert isinstance(result, Escalated)
    assert "did not verify" in result.reason


def test_a_policy_denial_stops_the_step(artifact, engine_policy):
    locked = PolicyEngine(
        Policy(
            allowlist=Allowlist(origins=(BASE,), paths=("/nowhere",), actions=frozenset()),
            risk=RiskPolicy(),
        )
    )
    surface = FakeSurface([screen("Member Search")])
    result = run(artifact, surface, locked)

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.POLICY
    assert surface.acted == []


def test_an_irreversible_step_without_approval_pauses(artifact, engine_policy):
    """Paused for a person, not broken -- which is why it is an escalation and
    not a policy failure."""
    artifact.steps[2].risk = RiskLevel.IRREVERSIBLE
    surface = FakeSurface(HAPPY_PATH)
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Escalated)
    assert result.step_id == "s3"


# --- transforms -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind,expected",
    [
        ("  $4,210.33 ", "money", "4210.33"),
        ("-$12.00", "money", "-12.00"),
        ("  Dana Whitfield ", "trim", "Dana Whitfield"),
        ("  x  ", "none", "  x  "),
        ("12 accounts", "integer", 12),
        (None, "money", None),
    ],
)
def test_transforms(raw, kind, expected):
    assert apply_transform(raw, kind) == expected


# --- determinism ----------------------------------------------------------


def test_replay_imports_no_model_client():
    """Determinism by construction rather than by discipline. There is nothing
    available to this package that could make a different choice on a
    Tuesday."""
    import pkgutil

    import cua.replay

    banned = ("anthropic", "openai", "cua.discovery")
    for module in pkgutil.iter_modules(cua.replay.__path__):
        source = (
            __import__("pathlib")
            .Path(cua.replay.__path__[0], f"{module.name}.py")
            .read_text(encoding="utf-8")
        )
        for name in banned:
            assert f"import {name}" not in source, (module.name, name)


# --- the retry strategy, where it genuinely applies -----------------------


def test_retry_with_backoff_waits_for_a_declared_wait_screen(artifact, engine_policy):
    """The reference capability declares no SLOW_LOAD recovery, because a
    stall has nothing of its own to detect and a checkpoint already polls to a
    deadline. The strategy is still in the vocabulary for the applications
    that do render an explicit wait screen -- where there is something real to
    detect -- so it is proved here against one.
    """
    from cua.artifact.models import Recovery

    artifact.recoveries = [
        Recovery(
            code="PLEASE_WAIT",
            detect=Locator(
                primary=RoleNameSpec(role="heading", name="Please Wait")
            ),
            strategy="retry_with_backoff",
            max_occurrences=2,
            backoff_ms=1,
        )
    ]
    surface = FakeSurface(
        [
            screen("Member Search"),
            screen("Member Search"),
            screen("Please Wait"),
            screen("Member Details"),
        ],
        values={"member_name": "Dana Whitfield", "savings_balance": "4,210.33"},
        advance_on=(ActionType.NAVIGATE, ActionType.CLICK, ActionType.WAIT_FOR),
    )

    # The wait screen clears on its own after a few looks, the way one does.
    original = surface.observe
    looks = {"count": 0}

    def observe():
        if surface.index == 2:
            looks["count"] += 1
            if looks["count"] > 3:
                surface.index = 3
        return original()

    surface.observe = observe
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Success)
    assert "PLEASE_WAIT" in " ".join(result.recoveries)


def test_a_step_with_a_checkpoint_trusts_the_checkpoint_over_the_driver(
    artifact, engine_policy
):
    """A slow render makes the browser report a timeout for a click that went
    through. The world is the authority on whether the step worked."""
    surface = FakeSurface(HAPPY_PATH, values={"member_name": "D", "savings_balance": "1.00"})
    original_act = surface.act

    def act(action):
        result = original_act(action)
        if action.type is ActionType.CLICK:
            return ActResult(ok=False, error="TimeoutError: waiting for navigation")
        return result

    surface.act = act
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Success), getattr(result, "observed", result)


def test_a_step_without_a_checkpoint_has_only_the_drivers_word(
    artifact, engine_policy
):
    """Which is the argument for declaring one."""
    surface = FakeSurface(HAPPY_PATH)
    original_act = surface.act

    def act(action):
        if action.type is ActionType.TYPE:  # s2 has no checkpoint
            return ActResult(ok=False, error="element is not editable")
        return original_act(action)

    surface.act = act
    result = run(artifact, surface, engine_policy)

    assert isinstance(result, Failure)
    assert result.step_id == "s2"
