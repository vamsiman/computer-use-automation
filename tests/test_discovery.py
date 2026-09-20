"""The discovery loop and the locator synthesis it depends on.

No browser and no model. The loop takes a ``ModelClient``, so a scripted
client can drive it through every path that matters -- a refusal, a stale ref,
a policy denial, a model going in circles -- deterministically and in
milliseconds. What a real model adds is judgement about which control to click,
and that is the one thing these tests are not trying to prove.

The synthesis tests use hand-built trees mirroring the target app, because the
question they ask is a pure one: given this tree and this node, what is the
most durable way to describe it?
"""

from __future__ import annotations

import pytest

from cua.discovery import (
    DiscoveryAgent,
    DiscoveryLimits,
    DiscoveryTrace,
    ModelReply,
    ToolCall,
    UnusableTarget,
    action_from_tool_call,
    synthesize,
)
from cua.discovery.tools import TOOLS, TOOL_NAMES
from cua.locators import LabelProximitySpec, RoleNameSpec, RowCellSpec
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.primitives import A11yNode, ActResult, Snapshot
from cua.types import ActionType, LocatorStrategy, RiskLevel

HERE = "http://localhost:5000/members/search"


def node(role, name="", *, box=None, value=None, ref=None, children=()):
    return A11yNode(
        role=role, name=name, box=box, value=value, ref=ref, children=list(children)
    )


@pytest.fixture
def search_tree():
    """The search screen. The member-id field has NO accessible name -- which
    is the whole reason the fallback chain exists."""
    return node(
        "document",
        "Member Search",
        children=[
            node("heading", "Member Search", box=(10, 20, 200, 20), ref="f0n1"),
            node(
                "form",
                "",
                box=(10, 90, 300, 80),
                ref="f0n7",
                children=[
                    node("text", "Member ID:", box=(10, 100, 80, 18), ref="f0n2"),
                    node("textbox", "", box=(100, 100, 120, 20), ref="f0n3"),
                    node("button", "Search", box=(240, 100, 60, 22), ref="f0n6"),
                ],
            ),
        ],
    )


@pytest.fixture
def grid_tree():
    """The accounts grid: header row is ordinary cells, no <th> anywhere."""
    return node(
        "document",
        "Member Details",
        children=[
            node("heading", "Member Details", box=(10, 10, 200, 20), ref="f0n1"),
            node(
                "table",
                "",
                box=(10, 60, 400, 80),
                ref="f0n10",
                children=[
                    node(
                        "row",
                        box=(10, 60, 400, 20),
                        ref="f0n11",
                        children=[
                            node("cell", "Type", box=(10, 60, 130, 20), ref="f0n12"),
                            node("cell", "Number", box=(140, 60, 130, 20), ref="f0n13"),
                            node("cell", "Balance", box=(280, 60, 130, 20), ref="f0n14"),
                        ],
                    ),
                    node(
                        "row",
                        box=(10, 80, 400, 20),
                        ref="f0n15",
                        children=[
                            node("cell", "Savings", box=(10, 80, 130, 20), ref="f0n16"),
                            node("cell", "S-0001", box=(140, 80, 130, 20), ref="f0n17"),
                            node("cell", "4,821.55", box=(280, 80, 130, 20), ref="f0n18"),
                        ],
                    ),
                    node(
                        "row",
                        box=(10, 100, 400, 20),
                        ref="f0n19",
                        children=[
                            node("cell", "Checking", box=(10, 100, 130, 20), ref="f0n20"),
                            node("cell", "C-0002", box=(140, 100, 130, 20), ref="f0n21"),
                            node("cell", "310.00", box=(280, 100, 130, 20), ref="f0n22"),
                        ],
                    ),
                ],
            ),
        ],
    )


def find(tree: A11yNode, ref: str) -> A11yNode:
    return next(n for n in tree.walk() if n.ref == ref)


def snapshot_of(tree: A11yNode, location: str = HERE) -> Snapshot:
    return Snapshot(location=location, tree=tree)


# --- locator synthesis ----------------------------------------------------


def test_a_named_control_records_on_role_and_name(search_tree):
    locator = synthesize(search_tree, find(search_tree, "f0n6"))
    assert isinstance(locator.primary, RoleNameSpec)
    assert (locator.primary.role, locator.primary.name) == ("button", "Search")
    assert locator.confidence == 1.0


def test_an_unnamed_field_falls_to_its_label(search_tree):
    """The member-id field has no accessible name, so tier 0 is unavailable.
    This is the case the whole tiered design exists for, and it is worth
    proving the synthesiser reaches for the label rather than giving up."""
    locator = synthesize(search_tree, find(search_tree, "f0n3"))
    assert locator.primary.strategy is LocatorStrategy.LABEL_PROXIMITY
    assert locator.primary.label == "Member ID:"


def test_the_synthesised_label_rule_always_states_its_role(search_tree):
    """Left implicit, the rule defaults to interactive controls -- correct for
    typing, wrong for reading a value beside a label. Stating the role is what
    stops a locator that works everywhere except where it matters."""
    locator = synthesize(search_tree, find(search_tree, "f0n3"))
    assert locator.primary.role == "textbox"


def test_a_grid_cell_records_as_a_row_and_a_column(grid_tree):
    """Not a path. A tenant reordering its columns breaks a path and does not
    break "the Balance on the Savings row"."""
    locator = synthesize(grid_tree, find(grid_tree, "f0n18"))
    row_cell = next(
        (s for s in locator.chain if isinstance(s, RowCellSpec)), None
    )
    assert row_cell is not None
    assert (row_cell.row_match, row_cell.column) == ("Savings", "Balance")


def test_a_cell_is_never_identified_by_its_own_value(grid_tree):
    """A locator matching the row by the balance it contains would only ever
    find the answer we already had."""
    locator = synthesize(grid_tree, find(grid_tree, "f0n18"))
    row_cell = next(s for s in locator.chain if isinstance(s, RowCellSpec))
    assert row_cell.row_match != "4,821.55"


def test_every_spec_in_the_chain_actually_resolves(search_tree, grid_tree):
    """The rule that makes a discovered artifact trustworthy: each rule in the
    chain was executed against the live tree and found this exact node. Nothing
    is written down hopefully."""
    from cua.surface.resolve import match_spec

    for tree in (search_tree, grid_tree):
        for target in tree.walk():
            if target.ref is None:
                continue
            locator = synthesize(tree, target)
            if locator is None:
                continue
            for spec in locator.chain:
                matches = match_spec(tree, spec)
                assert len(matches) == 1, (spec, target.ref)
                assert matches[0] is target, (spec, target.ref)


def test_the_chain_is_ordered_by_tier(grid_tree):
    from cua.types import LOCATOR_TIERS

    locator = synthesize(grid_tree, find(grid_tree, "f0n18"))
    tiers = [LOCATOR_TIERS.index(s.strategy) for s in locator.chain]
    assert tiers == sorted(tiers)


def test_two_identical_cells_are_still_told_apart_structurally():
    """A path is the weakest rule in the vocabulary and it is not nothing: two
    empty cells in a row are genuinely distinguishable by position. It is
    recorded at low confidence, which is the honest answer -- brittle, but
    verified, and better than refusing to record a step that works."""
    tree = node(
        "document",
        children=[
            node("table", children=[
                node("row", children=[node("cell", ""), node("cell", "")]),
            ]),
        ],
    )
    second = tree.children[0].children[0].children[1]
    locator = synthesize(tree, second)
    assert locator.primary.strategy is LocatorStrategy.REGION_PATH
    assert locator.primary.path.endswith("cell[1]")
    assert locator.confidence < 0.7


def test_a_node_nothing_in_the_vocabulary_describes_is_refused():
    """Two unnamed text nodes with no region above them. Returning a locator
    here would mean a discovery run that looks successful and an artifact that
    picks the wrong one in production, so the synthesiser says so and the model
    picks something else."""
    tree = node("document", children=[node("text", ""), node("text", "")])
    assert synthesize(tree, tree.children[0]) is None


def test_the_note_explains_why_the_chain_looks_like_that(search_tree):
    locator = synthesize(search_tree, find(search_tree, "f0n3"))
    assert "accessible name" in locator.note


# --- the tool surface -----------------------------------------------------


def test_the_tools_mirror_the_action_vocabulary():
    """The model cannot express a step replay would not know how to perform,
    because both sides read from the same enum rather than agreeing by
    convention."""
    executable = {a.value for a in ActionType} - {"done", "stuck"}
    assert executable | {"done", "stuck"} == set(TOOL_NAMES)


def test_every_tool_demands_an_intent():
    """One sentence from the model, and the artifact becomes reviewable by
    someone who does not read locators. Asked at the moment of the decision, it
    is also the only version of it that is honest."""
    for tool in TOOLS:
        assert "intent" in tool["input_schema"]["required"], tool["name"]


def test_a_tool_call_becomes_an_action_with_a_semantic_target(search_tree):
    action = action_from_tool_call(
        "type",
        {"ref": "f0n3", "value": "10001", "intent": "Enter the member number"},
        snapshot_of(search_tree),
    )
    assert action.type is ActionType.TYPE
    assert action.args["value"] == "10001"
    assert action.intent == "Enter the member number"
    assert action.target.primary.strategy is LocatorStrategy.LABEL_PROXIMITY


def test_the_ref_is_used_and_then_thrown_away(search_tree):
    """A ref is an index into one page load. Recording one would produce an
    artifact that works exactly once."""
    action = action_from_tool_call(
        "click", {"ref": "f0n6", "intent": "Submit"}, snapshot_of(search_tree)
    )
    assert "f0n6" not in action.target.describe()
    assert "ref" not in action.args


def test_a_stale_ref_is_refused_in_words_the_model_can_act_on(search_tree):
    with pytest.raises(UnusableTarget) as excinfo:
        action_from_tool_call(
            "click", {"ref": "f9n99", "intent": "Submit"}, snapshot_of(search_tree)
        )
    assert "not on the current screen" in str(excinfo.value)


def test_extract_defaults_to_trimming(search_tree):
    action = action_from_tool_call(
        "extract",
        {"ref": "f0n1", "into": "title", "intent": "Read the heading"},
        snapshot_of(search_tree),
    )
    assert action.args["transform"] == "trim"


def test_a_terminal_signal_is_not_an_action(search_tree):
    with pytest.raises(UnusableTarget):
        action_from_tool_call(
            "done", {"summary": "finished", "intent": "x"}, snapshot_of(search_tree)
        )


# --- the loop -------------------------------------------------------------


class ScriptedClient:
    """A model whose mind is already made up.

    Every branch of the loop can be driven this way, which is what makes the
    loop testable at all: the expensive, non-deterministic part is behind a
    seam with two methods.
    """

    def __init__(self, *calls: ToolCall) -> None:
        self.calls = list(calls)
        self.turns: list[list[dict]] = []

    def reply(self, *, system, messages, tools) -> ModelReply:
        # A copy, not the list itself. The agent keeps appending to the same
        # list, so storing the reference would mean every recorded turn was
        # really the final state of the conversation.
        self.turns.append(list(messages))
        if not self.calls:
            return ModelReply(text="I have nothing further.")
        return ModelReply(tool_calls=(self.calls.pop(0),))


class FakeSurface:
    """A two-screen application: search, then details."""

    def __init__(self, tree: A11yNode, location: str = HERE) -> None:
        self.tree = tree
        self.location = location
        self.acted: list = []
        self.next_tree: A11yNode | None = None
        self.next_location: str | None = None

    def observe(self) -> Snapshot:
        return Snapshot(location=self.location, tree=self.tree)

    def act(self, action) -> ActResult:
        self.acted.append(action)
        if self.next_tree is not None:
            self.tree = self.next_tree
            self.next_tree = None
        if self.next_location is not None:
            self.location = self.next_location
            self.next_location = None
        value = "4,821.55" if action.type is ActionType.EXTRACT else None
        return ActResult(ok=True, tier=0, value=value, observed=self.observe())

    def screenshot(self, path=None) -> str:
        return path or "shot.png"

    def close(self) -> None:
        pass


def call(name: str, **args) -> ToolCall:
    args.setdefault("intent", "do the thing")
    return ToolCall(id=f"t{abs(hash(name)) % 1000}", name=name, args=args)


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=("http://localhost:5000",),
                paths=("/", "/members/**"),
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


def agent(surface, client, engine, **kwargs) -> DiscoveryAgent:
    return DiscoveryAgent(surface, client, policy=engine, **kwargs)


def test_a_run_records_what_it_did_and_what_it_read(search_tree, grid_tree, engine):
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        call("type", ref="f0n3", value="10001", intent="Enter the member number"),
        call("click", ref="f0n6", intent="Submit the search"),
        call("extract", ref="f0n18", into="savings_balance", intent="Read the balance"),
        call("done", summary="Read the savings balance."),
    )
    surface.next_tree = None
    trace = _run_two_screens(surface, client, engine, grid_tree)

    assert trace.terminal == "done"
    assert trace.outputs == {"savings_balance": "4,821.55"}
    assert [r.tool for r in trace.records] == ["type", "click", "extract", "done"]


def _run_two_screens(surface, client, engine, second_tree):
    """Swap the screen after the click, the way a real submit would."""
    original_act = surface.act

    def act(action):
        if action.type is ActionType.CLICK:
            surface.next_tree = second_tree
            surface.next_location = "http://localhost:5000/members/10001"
        return original_act(action)

    surface.act = act
    return agent(surface, client, engine).run("read a balance", {"member_id": "10001"})


def test_the_recorded_target_is_semantic_not_a_ref(search_tree, engine):
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        call("click", ref="f0n6", intent="Submit the search"),
        call("done", summary="done"),
    )
    trace = agent(surface, client, engine).run("search")
    assert "Search" in trace.records[0].target
    assert "f0n6" not in trace.records[0].target


def test_a_policy_denial_stops_the_action_and_is_told_to_the_model(
    search_tree, engine
):
    """The refusal goes back into the conversation, so the model can route
    around the boundary rather than hammering at it -- and it stays in the
    transcript, where a reviewer can see the system held the line."""
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        call("navigate", path="/debug/expire", intent="Expire the session"),
        call("done", summary="gave up on that"),
    )
    trace = agent(surface, client, engine).run("try something off-limits")

    assert surface.acted == [], "the denied action must not have run"
    assert trace.records[0].ok is False
    assert trace.records[0].policy_rule.startswith("allowlist")
    last_turn = client.turns[-1]
    assert "not performed" in str(last_turn[-1])


def test_a_risky_step_stops_the_run_when_there_is_nobody_to_approve(
    search_tree, engine
):
    """Discovery has no licence to open an account because it was exploring."""
    tree = node(
        "document",
        children=[node("button", "Confirm", box=(10, 10, 80, 20), ref="f0n1")],
    )
    surface = FakeSurface(tree)
    client = ScriptedClient(call("click", ref="f0n1", intent="Confirm the account"))
    trace = agent(surface, client, engine).run("open an account")

    assert trace.terminal == "needs_approval"
    assert surface.acted == []


def test_an_approver_can_let_a_risky_step_through(engine):
    tree = node(
        "document",
        children=[node("button", "Confirm", box=(10, 10, 80, 20), ref="f0n1")],
    )
    surface = FakeSurface(tree)
    client = ScriptedClient(
        call("click", ref="f0n1", intent="Confirm the account"),
        call("done", summary="opened"),
    )
    trace = agent(
        surface, client, engine, approve=lambda action, decision: True
    ).run("open an account")

    assert trace.terminal == "done"
    assert len(surface.acted) == 1


def test_a_stale_ref_does_not_end_the_run(search_tree, engine):
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        call("click", ref="gone", intent="Click something that moved"),
        call("click", ref="f0n6", intent="Submit the search"),
        call("done", summary="recovered"),
    )
    trace = agent(surface, client, engine).run("search")

    assert trace.terminal == "done"
    assert trace.records[0].ok is False
    assert len(surface.acted) == 1


def test_going_in_circles_is_noticed(search_tree, engine):
    """An action that changes nothing will not change anything the third time
    either. Without this the loop burns its whole budget being polite."""
    surface = FakeSurface(search_tree)
    client = ScriptedClient(*[call("click", ref="f0n6", intent="Submit") for _ in range(10)])
    trace = agent(
        surface, client, engine, limits=DiscoveryLimits(repeat_limit=2)
    ).run("search")

    assert trace.terminal == "stuck_detected"
    assert len(trace.records) < 10


def test_the_step_budget_ends_the_run(search_tree, engine):
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        *[call("type", ref="f0n3", value=str(i), intent="Type") for i in range(20)]
    )
    trace = agent(
        surface, client, engine, limits=DiscoveryLimits(max_steps=3, repeat_limit=99)
    ).run("search")

    assert trace.terminal == "step_budget"
    assert len(trace.records) == 3


def test_a_model_that_only_talks_ends_the_run(search_tree, engine):
    """It is being asked to operate an application, not to describe one."""
    surface = FakeSurface(search_tree)
    trace = agent(surface, ScriptedClient(), engine).run("search")
    assert trace.terminal == "error"


def test_the_model_sees_the_tree_and_the_goal_and_the_inputs(search_tree, engine):
    surface = FakeSurface(search_tree)
    client = ScriptedClient(call("done", summary="nothing to do"))
    agent(surface, client, engine).run("read a balance", {"member_id": "10001"})

    opening = client.turns[0][0]["content"]
    assert "GOAL: read a balance" in opening
    assert "member_id = '10001'" in opening
    assert "textbox" in opening, "the tree itself must be in the prompt"
    assert "<html" not in opening, "raw markup is never shown to the model"


# --- the trace ------------------------------------------------------------


def test_an_action_that_changed_nothing_is_not_effective(search_tree, engine):
    """The first pruning rule, and it is a fact about the trace rather than a
    judgement: an action that left the screen identical did nothing, whatever
    the model believed at the time."""
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        call("click", ref="f0n6", intent="Submit"),
        call("done", summary="done"),
    )
    trace = agent(surface, client, engine).run("search")
    assert trace.records[0].changed_the_screen is False
    assert trace.effective() == []


def test_a_trace_round_trips_through_disk(tmp_path, search_tree, engine):
    """It is the raw material distillation works from, and evidence in its own
    right, so it has to survive being written down."""
    surface = FakeSurface(search_tree)
    client = ScriptedClient(
        call("type", ref="f0n3", value="10001", intent="Enter the member number"),
        call("done", summary="done"),
    )
    trace = agent(surface, client, engine).run("read a balance", {"member_id": "10001"})

    path = trace.save(tmp_path / "trace.json")
    reloaded = DiscoveryTrace.load(path)
    assert reloaded.goal == trace.goal
    assert [r.target for r in reloaded.records] == [r.target for r in trace.records]


# --- a locator may not be defined by the data it reads --------------------
#
# Found by the first genuine discovery run, not by these tests. The model
# pointed at the balance cell, every rule verified against the live tree, and
# the chain that came out described the number by the number: primary
# `cell "4,210.33"`, fallback `the cell below "1,287.50"`. Both resolve
# perfectly at record time and on exactly one member, so the first replay with
# a different input reported itself degraded. Verification cannot catch this --
# at record time a circular locator is a correct locator.


def test_reading_a_value_never_records_the_value_as_the_locator(grid_tree):
    """`cell "4,821.55"` finds this balance and no other member's.

    Worse than useless at the end of a chain, too: on a later run it either
    fails, or quietly resolves to some *other* row that happens to hold the
    number we remembered. So it is discarded rather than demoted.
    """
    balance = find(grid_tree, "f0n18")
    locator = synthesize(grid_tree, balance, reading=True)

    assert locator is not None
    described = [str(spec.model_dump()) for spec in locator.chain]
    assert not any("4,821.55" in text for text in described), described


def test_reading_a_value_is_not_anchored_to_the_cell_beside_it(grid_tree):
    """"The cell below 310.00" is not a label, it is another datum.

    It moves with exactly the data the locator was supposed to be independent
    of, which is the same circularity one step removed.
    """
    balance = find(grid_tree, "f0n18")
    locator = synthesize(grid_tree, balance, reading=True)

    labels = [
        spec.label
        for spec in locator.chain
        if isinstance(spec, LabelProximitySpec)
    ]
    assert not any(label in ("310.00", "S-0001", "C-0002") for label in labels)


def test_reading_a_value_lands_on_the_rule_that_generalises(grid_tree):
    """"The Balance on the Savings row" is the description that survives a
    different member, a reordered column and a renamed table."""
    locator = synthesize(grid_tree, find(grid_tree, "f0n18"), reading=True)

    assert isinstance(locator.primary, RowCellSpec)
    assert (locator.primary.row_match, locator.primary.column) == (
        "Savings",
        "Balance",
    )


def test_clicking_a_control_still_records_its_name(grid_tree):
    """The rule is about reading, not about tables.

    Clicking the cell that says "Savings" is a perfectly good thing to
    describe by its name -- the name is what the control is called, not what
    the run came to find out.
    """
    locator = synthesize(grid_tree, find(grid_tree, "f0n16"))
    assert isinstance(locator.primary, RoleNameSpec)
    assert locator.primary.name == "Savings"


def test_a_value_outside_a_table_is_described_by_where_it_sits(grid_tree):
    """A number loose on the page, with only its own text to name it.

    There is still a structural answer -- where it sits inside a region we
    found semantically -- and that is what gets recorded. What must not
    survive is the rule that names the number after the number. If nothing at
    all were left, `synthesize` would return `None` and the model would be
    told to pick something else, which is the honest answer: "we cannot say
    where this is, only what it says" is not a locator.
    """
    lone = node(
        "document",
        "Report",
        children=[node("text", "4,821.55", box=(10, 10, 60, 18), ref="f0n1")],
    )
    reading = synthesize(lone, find(lone, "f0n1"), reading=True)
    assert reading.primary.strategy is LocatorStrategy.REGION_PATH
    assert not any(
        "4,821.55" in str(spec.model_dump()) for spec in reading.chain
    )
    # ...and it is named perfectly well when it is a thing to click.
    assert synthesize(lone, find(lone, "f0n1")).primary.name == "4,821.55"
