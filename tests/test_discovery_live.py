"""The discovery loop against the real browser and the real application.

No API key. The operator below reads the same rendered accessibility tree the
model would and follows a fixed plan, which leaves exactly one thing unproven:
the model's judgement about which control to click. Everything else -- frame
walking, ref resolution, locator synthesis against real markup, policy checks,
the trace, the evidence bundle -- is exercised end to end against the live app.

That division is deliberate. The parts most likely to break are the parts a
fake model exercises perfectly well, and they should not be untestable in CI
because one of them happens to sit next to an LLM.

The strong assertion here is the last one: the locators this run *synthesised*
are fed straight back to the resolver on a fresh page load and must still find
the same controls. A discovery run that cannot replay is the failure this whole
design exists to prevent, and it is cheap to check at the moment of recording.
"""

from __future__ import annotations

import re
import socket
import threading

import pytest

from cua.discovery import DiscoveryAgent, DiscoveryLimits, ModelReply, ToolCall
from cua.evidence import EvidenceConfig, Recorder
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.session import Credentials, authenticate
from cua.surface.web import BrowserSession
from cua.types import ActionType, LocatorStrategy, RiskLevel
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser

GOAL = "look up member 10001 and read their current savings balance"

#: ``role "name" = 'value' #ref`` -- one line of the rendered tree.
LINE = re.compile(r'^\s*(?P<role>\S+)(?:\s+"(?P<name>[^"]*)")?.*?#(?P<ref>\S+)\s*$')


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
def browser(live_app):
    session = BrowserSession(live_app, headless=True)
    surface = session.start()
    try:
        authenticate(surface, Credentials(user=APP_USER, password=APP_PASS))
        yield surface
    finally:
        surface.close()
        session.stop()


@pytest.fixture
def engine(live_app) -> PolicyEngine:
    # The live fixture binds a random port, so the shipped policy.yaml cannot
    # match it. Same rules, this origin.
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(live_app,),
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


# --- a model stand-in that actually reads the screen ----------------------


def rows(screen: str):
    for line in screen.splitlines():
        match = LINE.match(line)
        if match:
            yield match.group("role"), match.group("name") or "", match.group("ref")


def by_role_name(role: str, name: str | None = None, nth: int = 0):
    """Point at the nth control of a role, optionally by name."""

    def pick(screen: str) -> str | None:
        found = [
            ref
            for r, n, ref in rows(screen)
            if r == role and (name is None or name.lower() in n.lower())
        ]
        return found[nth] if nth < len(found) else None

    return pick


def after_label(label: str, offset: int = 1):
    """Point at the control that follows a piece of text on screen.

    How a person reads a form: the thing after the words "Name:" is the name.
    """

    def pick(screen: str) -> str | None:
        # Collapse the label/cell pairs first. Legacy table markup renders the
        # same text twice -- once as the layout cell, once as the text node
        # inside it -- and counting along without noticing lands on the
        # duplicate rather than on the value. A person reading the screen sees
        # one "Name:", so the stand-in should too.
        listed: list[tuple[str, str, str]] = []
        for row in rows(screen):
            if listed and row[1].strip() and row[1].strip() == listed[-1][1].strip():
                continue
            listed.append(row)

        for i, (_role, name, _ref) in enumerate(listed):
            if name.strip().lower() == label.lower() and i + offset < len(listed):
                return listed[i + offset][2]
        return None

    return pick


class RuleBasedOperator:
    """Follows a fixed plan, reading refs off the screen it is shown.

    It is given no more information than the model gets: the rendered tree,
    turn by turn. If the application's markup changes shape, this stops working
    in exactly the way a model would be confused by it, which is the property
    that makes it a useful stand-in rather than a puppet.
    """

    model = "rule-based-operator"

    def __init__(self, plan: list[dict]) -> None:
        self.plan = list(plan)
        self.screens: list[str] = []

    def reply(self, *, system, messages, tools) -> ModelReply:
        screen = _last_screen(messages)
        self.screens.append(screen)

        if not self.plan:
            return ModelReply(
                tool_calls=(
                    ToolCall(id="t-done", name="done", args={
                        "summary": "Read the savings balance.",
                        "intent": "Finish",
                    }),
                )
            )

        step = dict(self.plan.pop(0))
        pick = step.pop("pick", None)
        if pick is not None:
            ref = pick(screen)
            if ref is None:
                return ModelReply(
                    tool_calls=(
                        ToolCall(id="t-stuck", name="stuck", args={
                            "reason": f"could not find the control for {step}",
                            "intent": "Stop",
                        }),
                    )
                )
            step["ref"] = ref
        name = step.pop("tool")
        return ModelReply(
            tool_calls=(ToolCall(id=f"t{len(self.screens)}", name=name, args=step),)
        )


def _last_screen(messages: list[dict]) -> str:
    content = messages[-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("content", "") for block in content if isinstance(block, dict)
    )


READ_BALANCE_PLAN = [
    {
        "tool": "navigate",
        "path": "/members/search",
        "intent": "Open the member search screen",
    },
    {
        "tool": "type",
        "pick": by_role_name("textbox"),
        "value": "10001",
        "intent": "Enter the member number into the search field",
    },
    {
        "tool": "click",
        "pick": by_role_name("button", "Search"),
        "intent": "Submit the search",
    },
    {
        "tool": "extract",
        "pick": after_label("Name:"),
        "into": "member_name",
        "intent": "Read the member's name from the detail header",
    },
    {
        "tool": "extract",
        "pick": after_label("Savings", offset=2),
        "into": "savings_balance",
        "transform": "money",
        "intent": "Read the savings balance from the accounts grid",
    },
    {"tool": "done", "summary": "Read the savings balance.", "intent": "Finish"},
]


@pytest.fixture(scope="module")
def discovered(request, live_app):
    """One real discovery run, shared by the assertions below."""
    from werkzeug.serving import make_server  # noqa: F401  (fixture ordering)

    session = BrowserSession(live_app, headless=True)
    surface = session.start()
    tmp = request.getfixturevalue("tmp_path_factory").mktemp("discovery")
    try:
        authenticate(surface, Credentials(user=APP_USER, password=APP_PASS))
        engine = PolicyEngine(
            Policy(
                allowlist=Allowlist(
                    origins=(live_app,),
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
        recorder = Recorder.start(
            config=EvidenceConfig(root=tmp / "runs"),
            mode="discovery",
            goal=GOAL,
            model="rule-based-operator",
        )
        agent = DiscoveryAgent(
            surface,
            RuleBasedOperator(READ_BALANCE_PLAN),
            policy=engine,
            recorder=recorder,
            limits=DiscoveryLimits(max_steps=12),
        )
        trace = agent.run(GOAL, {"member_id": "10001"})
        yield trace, surface, recorder
    finally:
        surface.close()
        session.stop()


def test_the_run_reaches_the_goal(discovered):
    trace, _surface, _rec = discovered
    assert trace.terminal == "done", trace.summary
    assert all(r.ok for r in trace.records), [r.error for r in trace.records if not r.ok]


def test_it_read_the_values_it_was_asked_for(discovered):
    trace, _surface, _rec = discovered
    assert trace.outputs["member_name"] == "Dana Whitfield"
    assert "4,210.33" in trace.outputs["savings_balance"]


def test_the_trace_records_semantic_targets_not_refs(discovered):
    """The ref was how the model pointed. It is not how the step is recorded,
    because a ref is an index into one page load."""
    trace, _surface, _rec = discovered
    acted = [r for r in trace.records if r.target]
    assert acted
    for record in acted:
        assert not re.search(r"\bf\d+n\d+\b", record.target), record.target


def test_the_unnamed_search_field_was_recorded_by_its_label(discovered):
    """Against the real markup, not a hand-built tree. This field's label is a
    bare span in the neighbouring table cell, so tier 0 genuinely cannot
    describe it -- which is why the fallback chain is not decoration."""
    trace, _surface, _rec = discovered
    typed = next(r for r in trace.records if r.tool == "type")
    assert "Member ID" in typed.target
    assert typed.target.startswith("control right of"), typed.target


def test_the_tier_that_resolved_is_recorded_for_every_action(discovered):
    trace, _surface, _rec = discovered
    acted = [r for r in trace.records if r.target]
    assert all(r.tier is not None for r in acted)
    # The search button has a real accessible name, so it must resolve at the
    # top tier. If this ever degrades, the app changed and we want to know.
    clicked = next(r for r in trace.records if r.tool == "click")
    assert clicked.tier == 0


def test_the_synthesised_locators_still_resolve_on_a_fresh_page(discovered):
    """The claim discovery is making: these steps can be replayed. Checked by
    replaying the locators themselves against a newly loaded screen -- if a
    synthesised locator only works on the snapshot it was born from, the
    artifact is worthless and this is the cheapest place to find out."""
    trace, surface, _rec = discovered
    from cua.primitives import Action

    surface.act(Action(type=ActionType.NAVIGATE, args={"path": "/members/search"}))
    fresh = surface.observe()

    typed = next(r for r in trace.records if r.tool == "type")
    assert typed.action is not None
    assert not re.search(r"f\d+n\d+", typed.action.target.describe())

    # Re-run synthesis against the new page and confirm we would record the
    # same description -- i.e. the description is a property of the screen, not
    # of the snapshot it was born from.
    from cua.discovery.locate import synthesize

    field = next(
        n for n in fresh.tree.walk() if n.role == "textbox" and not n.name
    )
    again = synthesize(fresh.tree, field)
    assert again is not None
    assert again.describe() == typed.target


def test_the_run_left_an_evidence_bundle(discovered):
    trace, _surface, recorder = discovered
    import json

    records = [
        json.loads(line)
        for line in recorder.log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [r["kind"] for r in records]
    assert "policy" in kinds, "every action is policy-checked before it happens"
    assert "step" in kinds
    assert kinds[-1] == "discovery_finished"
    assert trace.run_id == recorder.run_id


def test_a_refused_navigation_does_not_happen_live(browser, engine):
    """The debug endpoints exist so a demo can summon failure states. A run
    able to expire its own session would make the error taxonomy untestable in
    the one way that matters."""
    operator = RuleBasedOperator(
        [
            {"tool": "navigate", "path": "/debug/expire", "intent": "Expire"},
            {"tool": "stuck", "reason": "not allowed", "intent": "Stop"},
        ]
    )
    agent = DiscoveryAgent(browser, operator, policy=engine)
    trace = agent.run("try to expire the session")

    assert trace.records[0].ok is False
    assert trace.records[0].policy_rule.startswith("allowlist")
    assert "Sign In" not in browser.observe().text_view()


# --- distillation, end to end --------------------------------------------


def test_the_run_distils_into_a_capability_that_validates(discovered):
    """The whole point of the run: not that it worked, but that what it leaves
    behind is a contract somebody else can call."""
    from cua.artifact.models import AppRef
    from cua.artifact.validate import validate_or_raise
    from cua.discovery import distil

    trace, _surface, _rec = discovered
    artifact = distil(
        trace,
        capability_id="member.read_savings_balance",
        app=AppRef(vendor="meridian", product="MemberConsole"),
    )
    validate_or_raise(artifact)

    assert artifact.capability.status == "draft"
    assert artifact.success.require_outputs == ["member_name", "savings_balance"]
    assert artifact.capability.provenance.step_count_raw >= len(artifact.steps)


def test_the_member_number_became_a_parameter(discovered):
    """Before this pass the artifact is a recording of one answer. After it,
    it is a function."""
    from cua.discovery import distil

    trace, _surface, _rec = discovered
    artifact = distil(trace, capability_id="member.read_savings_balance")
    typed = next(s for s in artifact.steps if s.action is ActionType.TYPE)

    assert typed.args["value"] == "{{ inputs.member_id }}"
    assert "10001" not in str(typed.args)
    assert artifact.inputs["member_id"].pattern == "^[0-9]{5}$"


def test_every_distilled_locator_resolves_on_a_freshly_loaded_screen(discovered):
    """The claim the artifact makes, checked against the live application.

    Each step's locator is resolved on a page loaded after the run finished,
    in order, driving the flow as replay would. A capability whose locators
    only work on the snapshot they were born from is the failure this design
    exists to prevent, and this is where it would show.
    """
    from cua.discovery import distil
    from cua.primitives import Action

    trace, surface, _rec = discovered
    artifact = distil(trace, capability_id="member.read_savings_balance")

    tiers: dict[str, int] = {}
    strategies: dict[str, LocatorStrategy] = {}
    for step in artifact.steps:
        if step.target is None:
            surface.act(Action(type=step.action, args=step.args))
            continue
        resolution = surface.resolve(step.target)
        assert resolution.resolved, f"{step.id} ({step.intent}) did not resolve"
        tiers[step.id] = resolution.tier
        strategies[step.id] = resolution.strategy
        if step.action is ActionType.TYPE:
            surface.act(
                Action(type=step.action, target=step.target, args={"value": "10001"})
            )
        elif step.action is ActionType.CLICK:
            surface.act(Action(type=step.action, target=step.target))

    # Tier is an index into each locator's own chain, so "tier 0" means the
    # primary rule still works -- healthy, not necessarily semantic. The
    # cross-locator claim is about which *strategy* won.
    assert all(tier == 0 for tier in tiers.values()), tiers
    assert strategies["s2"] is LocatorStrategy.LABEL_PROXIMITY, (
        "this field has no accessible name, so the label is doing the work"
    )
    assert strategies["s3"] is LocatorStrategy.ROLE_NAME


def test_the_capability_survives_being_written_down(discovered, tmp_path):
    from cua.artifact.store import CapabilityStore
    from cua.discovery import distil

    trace, _surface, _rec = discovered
    artifact = distil(trace, capability_id="member.read_savings_balance")

    store = CapabilityStore(tmp_path)
    store.save(artifact)
    reloaded = store.load("member.read_savings_balance")

    assert reloaded.ref == artifact.ref
    assert [s.intent for s in reloaded.steps] == [s.intent for s in artifact.steps]
    assert reloaded.steps[1].target.describe() == artifact.steps[1].target.describe()
