"""Trace to capability.

Built on hand-made traces, because the questions are about the distiller's
rules rather than about the browser: what survives pruning, what becomes a
parameter, what the model is allowed to change afterwards. The live
end-to-end -- real app, real synthesis, real artifact, validated -- lives in
``test_distill_live.py``.
"""

from __future__ import annotations

import pytest

from cua.artifact.models import AppRef
from cua.artifact.validate import validate_or_raise
from cua.discovery import NothingToDistil, ToolCall, ModelReply, distil, prune
from cua.discovery.annotate import ANNOTATE_TOOL, annotate
from cua.discovery.annotate import apply as apply_annotation
from cua.discovery.distill import parameterise, slug_from_goal
from cua.discovery.trace import DiscoveryTrace, TraceRecord
from cua.locators import Locator, RoleNameSpec
from cua.primitives import Action
from cua.types import ActionType, RiskLevel, Sensitivity

GOAL = "look up member 10001 and read their current savings balance"


def button(name: str) -> Locator:
    return Locator(primary=RoleNameSpec(role="button", name=name))


def record(
    seq: int,
    tool: str,
    *,
    action: Action | None,
    intent: str = "do a thing",
    ok: bool = True,
    before: str = "h1",
    after: str | None = "h2",
    landmark_before: str | None = "heading:Member Search",
    landmark_after: str | None = "heading:Member Details",
    value: str | None = None,
) -> TraceRecord:
    return TraceRecord(
        seq=seq,
        tool=tool,
        intent=intent,
        action=action,
        ok=ok,
        tree_before=before,
        tree_after=after,
        landmark_before=landmark_before,
        landmark_after=landmark_after,
        value=value,
        target=action.target.describe() if action and action.target else None,
    )


@pytest.fixture
def trace() -> DiscoveryTrace:
    """A run with one wrong turn, one dead click, and the real path."""
    navigate = Action(
        type=ActionType.NAVIGATE, args={"path": "/members/search"}
    )
    typing = Action(
        type=ActionType.TYPE,
        target=Locator(primary=RoleNameSpec(role="textbox", name="Member ID")),
        args={"value": "10001"},
    )
    submit = Action(type=ActionType.CLICK, target=button("Search"))
    extract = Action(
        type=ActionType.EXTRACT,
        target=Locator(primary=RoleNameSpec(role="cell", name="Balance")),
        args={"into": "savings_balance", "transform": "money"},
    )
    dead = Action(type=ActionType.CLICK, target=button("Reports"))

    return DiscoveryTrace(
        goal=GOAL,
        inputs={"member_id": "10001"},
        model="test-model",
        run_id="run-1",
        terminal="done",
        summary="Read the savings balance.",
        outputs={"savings_balance": "4,210.33"},
        records=[
            record(
                1, "navigate", action=navigate,
                intent="Open the member search screen",
                landmark_before="heading:Home", landmark_after="heading:Member Search",
            ),
            # A refused action: it never happened.
            record(2, "navigate", action=None, ok=False, after=None),
            # A click that changed nothing: it did nothing.
            record(
                3, "click", action=dead, before="h2", after="h2",
                landmark_before="heading:Member Search",
                landmark_after="heading:Member Search",
            ),
            record(
                4, "type", action=typing,
                intent="Enter the member number into the search field",
                before="h2", after="h3",
                landmark_before="heading:Member Search",
                landmark_after="heading:Member Search",
            ),
            record(
                5, "click", action=submit, intent="Submit the search",
                before="h3", after="h4",
                landmark_before="heading:Member Search",
                landmark_after="heading:Member Details",
            ),
            record(
                6, "extract", action=extract, intent="Read the savings balance",
                before="h4", after="h4", value="4,210.33",
                landmark_before="heading:Member Details",
                landmark_after="heading:Member Details",
            ),
            record(7, "done", action=None, after=None),
        ],
    )


# --- pass one: pruning ----------------------------------------------------


def test_the_wrong_turns_are_dropped(trace):
    kept = prune(trace)
    assert [r.seq for r in kept] == [1, 4, 5, 6]


def test_a_refused_action_never_happened(trace):
    assert all(r.ok for r in prune(trace))


def test_a_click_that_changed_nothing_is_not_a_step(trace):
    """Mechanical, not a judgement. Whatever the model believed at the time,
    an action that left the screen identical did nothing."""
    kept = prune(trace)
    assert all(r.target != 'button "Reports"' for r in kept)


def test_an_extract_survives_although_it_changes_nothing(trace):
    """The rule that looks right and is wrong: "keep what changed the screen"
    throws away every extraction, which is the one thing the caller asked for.
    An artifact that navigates beautifully and returns nothing passes its own
    tests."""
    kept = prune(trace)
    assert any(r.action.type is ActionType.EXTRACT for r in kept)


def test_the_distillation_is_visible_in_the_provenance(trace):
    """Seven actions recorded, four kept. Saying so in the document is what
    makes it obvious the artifact is a distillation rather than a
    transcript."""
    artifact = distil(trace)
    assert artifact.capability.provenance.step_count_raw == 7
    assert artifact.capability.provenance.step_count_final == 4


# --- pass two: parameters -------------------------------------------------


def test_the_value_used_becomes_a_parameter(trace):
    artifact = distil(trace)
    typed = next(s for s in artifact.steps if s.action is ActionType.TYPE)
    assert typed.args["value"] == "{{ inputs.member_id }}"


def test_a_parameter_is_substituted_inside_a_path():
    assert parameterise("/members/10001/detail", {"member_id": "10001"}) == (
        "/members/{{ inputs.member_id }}/detail"
    )


def test_a_very_short_value_is_not_substituted():
    """Replacing "10" everywhere it appears would corrupt far more than it
    parameterises."""
    assert parameterise("/members/10", {"n": "10"}) == "/members/10"


def test_an_identifier_stays_a_string(trace):
    """A member number with a leading zero is not the integer it parses to,
    and an identifier arithmetic can be done on is one waiting to be
    corrupted."""
    artifact = distil(trace)
    spec = artifact.inputs["member_id"]
    assert spec.type == "string"
    assert spec.pattern == "^[0-9]{5}$"


def test_an_input_nothing_uses_is_not_declared(trace):
    """A parameter in the signature that no step consumes is a lie about the
    contract."""
    trace.inputs["branch_code"] = "NORTHGATE"
    artifact = distil(trace)
    assert "branch_code" not in artifact.inputs


def test_outputs_default_to_pii(trace):
    """The safe direction to be wrong in. Over-redacting a branch code costs a
    reviewer one edit; under-redacting a name costs a disclosure."""
    artifact = distil(trace)
    assert artifact.outputs["savings_balance"].sensitivity is Sensitivity.PII
    assert artifact.outputs["savings_balance"].type == "money"


# --- what makes it replayable --------------------------------------------


def test_a_step_that_changes_the_screen_gets_a_checkpoint(trace):
    """Without one the engine clicks and hopes, and the failure surfaces three
    steps later where nothing explains it."""
    artifact = distil(trace)
    submit = next(s for s in artifact.steps if s.action is ActionType.CLICK)
    assert submit.checkpoint is not None
    assert submit.checkpoint.target.primary.name == "Member Details"


def test_a_step_that_stays_on_the_same_screen_gets_no_checkpoint(trace):
    artifact = distil(trace)
    typed = next(s for s in artifact.steps if s.action is ActionType.TYPE)
    assert typed.checkpoint is None


def test_success_requires_the_outputs_it_promised(trace):
    """A run that reaches the right screen and comes back without the number
    has not succeeded."""
    artifact = distil(trace)
    assert artifact.success.require_outputs == ["savings_balance"]
    assert artifact.success.checkpoint.target.primary.name == "Member Details"


def test_risk_is_inferred_by_the_engine_that_will_gate_it(trace):
    """So a reviewer reads the number the system will actually act on, rather
    than one produced by a second opinion that never runs again."""
    artifact = distil(trace)
    clicked = next(s for s in artifact.steps if s.action is ActionType.CLICK)
    assert clicked.risk is RiskLevel.CAUTION
    assert all(s.risk is not RiskLevel.IRREVERSIBLE for s in artifact.steps)


def test_the_result_is_always_a_draft(trace):
    """A capability that drives bank software is not something you merge
    because the pipeline was green."""
    assert distil(trace).capability.status == "draft"


def test_the_artifact_validates(trace):
    validate_or_raise(distil(trace, capability_id="member.read_savings_balance"))


def test_a_run_that_did_not_succeed_is_not_a_capability(trace):
    """Evidence, yes. A contract somebody can call, no."""
    trace.terminal = "stuck"
    with pytest.raises(NothingToDistil):
        distil(trace)


def test_a_goal_without_an_id_still_produces_a_legal_one():
    assert slug_from_goal(GOAL) == "discovered.member_savings_balance"


# --- pass three: annotation ----------------------------------------------


class Writer:
    """A model that returns whatever it was told to return."""

    def __init__(self, args: dict) -> None:
        self.args = args
        self.seen: list[str] = []

    def reply(self, *, system, messages, tools):
        self.seen.append(messages[0]["content"])
        return ModelReply(
            tool_calls=(ToolCall(id="a1", name=ANNOTATE_TOOL["name"], args=self.args),)
        )


def test_annotation_improves_the_prose(trace):
    artifact = distil(trace)
    writer = Writer(
        {
            "id": "member.read_savings_balance",
            "title": "Read a member's savings balance",
            "description": "Look up a member and return their savings balance.",
            "step_intents": [{"id": "s1", "intent": "Open the search screen"}],
        }
    )
    updated = apply_annotation(artifact, annotate(trace, artifact, writer))

    assert updated.capability.id == "member.read_savings_balance"
    assert updated.capability.title == "Read a member's savings balance"
    assert updated.steps[0].intent == "Open the search screen"


def test_the_model_cannot_change_what_the_capability_does(trace):
    """Structural, not a matter of prompting. The writer is handed a draft and
    may return anything; only prose is read back out."""
    artifact = distil(trace)
    writer = Writer(
        {
            "id": "member.read_savings_balance",
            "title": "Innocent title",
            "description": "Innocent description.",
            "steps": [{"id": "s1", "action": "click", "args": {"value": "99999"}}],
            "inputs": {"member_id": {"type": "integer"}},
            "capability": {"status": "approved"},
        }
    )
    updated = apply_annotation(artifact, annotate(trace, artifact, writer))

    assert [s.action for s in updated.steps] == [s.action for s in artifact.steps]
    assert [s.args for s in updated.steps] == [s.args for s in artifact.steps]
    assert [s.risk for s in updated.steps] == [s.risk for s in artifact.steps]
    assert updated.inputs["member_id"].type == "string"
    assert updated.capability.status == "draft"


def test_an_illegal_capability_id_is_ignored_rather_than_crashing(trace):
    artifact = distil(trace)
    updated = apply_annotation(artifact, {"id": "Not A Valid Id!"})
    assert updated.capability.id == artifact.capability.id


def test_proposed_outcomes_detect_by_containment(trace):
    """The model's recollection of wording is approximate. An exact match that
    misses reports a business outcome as a crash, which is the conflation the
    error taxonomy exists to prevent."""
    artifact = distil(trace)
    updated = apply_annotation(
        artifact,
        {
            "outcomes": [
                {
                    "code": "MEMBER_NOT_FOUND",
                    "detect_text": "No records found",
                    "message": "No member exists with that number.",
                }
            ]
        },
    )
    outcome = updated.outcomes[0]
    assert outcome.code == "MEMBER_NOT_FOUND"
    assert outcome.detect.primary.name_contains == "No records found"
    assert outcome.detect.primary.name is None
    assert "Verify the wording" in outcome.detect.note


def test_a_malformed_outcome_is_dropped(trace):
    artifact = distil(trace)
    updated = apply_annotation(
        artifact, {"outcomes": [{"code": "", "detect_text": "x"}, {"code": "A"}]}
    )
    assert updated.outcomes == []


def test_the_writer_is_shown_the_discarded_actions_too(trace):
    """It is being asked what business outcomes it saw, and the refusals are
    where it saw them."""
    artifact = distil(trace)
    writer = Writer({"id": "a.b", "title": "t", "description": "d"})
    annotate(trace, artifact, writer)
    assert "INCLUDING WHAT WAS DISCARDED" in writer.seen[0]


def test_the_artifact_is_complete_before_annotation_runs(trace):
    """The property that keeps the second model call optional: every step
    already carries the intent written at the moment it was chosen."""
    artifact = distil(trace, capability_id="member.read_savings_balance")
    validate_or_raise(artifact)
    assert all(step.intent for step in artifact.steps)
