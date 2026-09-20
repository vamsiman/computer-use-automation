"""The artifact schema, its validator and its store.

No browser. These cover the properties that make an artifact trustworthy
before anything is ever executed: that it survives a round trip through YAML
unchanged, that it refuses to promise things it cannot deliver, and that
missing inputs are an error rather than a blank.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cua.artifact import (
    AppRef,
    Artifact,
    CapabilityMeta,
    CapabilityNotFound,
    CapabilityStore,
    Checkpoint,
    InputSpec,
    MissingInput,
    Outcome,
    OutputSpec,
    Recovery,
    Step,
    SuccessSpec,
    from_yaml,
    render,
    to_yaml,
    validate,
)
from cua.locators import Locator, RoleNameSpec, RowCellSpec
from cua.types import ActionType, RiskLevel, Sensitivity


def heading(name: str) -> Locator:
    return Locator(primary=RoleNameSpec(role="heading", name=name))


def build(**overrides) -> Artifact:
    """A minimal coherent artifact: one input in, one output out."""
    base = dict(
        capability=CapabilityMeta(
            id="member.read_savings_balance",
            version="1.0.0",
            title="Read a savings balance",
            app=AppRef(vendor="meridian", product="MemberConsole"),
        ),
        inputs={"member_id": InputSpec(type="string", pattern=r"^\d{5}$")},
        outputs={"savings_balance": OutputSpec(type="money", currency="USD")},
        steps=[
            Step(
                id="s1",
                intent="Enter the member number",
                action=ActionType.TYPE,
                target=Locator(primary=RoleNameSpec(role="textbox", name="Member ID")),
                args={"value": "{{ inputs.member_id }}"},
            ),
            Step(
                id="s2",
                intent="Read the savings balance",
                action=ActionType.EXTRACT,
                target=Locator(
                    primary=RowCellSpec(
                        table="", row_match="Savings", column="Balance"
                    )
                ),
                args={"into": "savings_balance"},
            ),
        ],
        success=SuccessSpec(require_outputs=["savings_balance"]),
    )
    base.update(overrides)
    return Artifact(**base)


# --- the contract --------------------------------------------------------


def test_a_minimal_artifact_is_coherent():
    assert validate(build()) == []


def test_signature_reads_like_a_callable():
    """What an agent sees when browsing the catalogue."""
    assert build().signature() == (
        "member.read_savings_balance(member_id: string) -> "
        "{savings_balance: money}"
    )


def test_new_artifacts_are_drafts_until_someone_approves_them():
    assert build().capability.status == "draft"
    assert not build().is_approved


def test_riskiest_step_summarises_the_whole_flow():
    artifact = build()
    assert artifact.riskiest() is RiskLevel.SAFE

    artifact.steps[1].risk = RiskLevel.IRREVERSIBLE
    artifact.steps[1].checkpoint = Checkpoint(target=heading("Done"))
    assert artifact.riskiest() is RiskLevel.IRREVERSIBLE


def test_sensitivity_is_declared_not_inferred():
    """Redaction reads this rather than guessing what looks secret."""
    artifact = build(
        outputs={"savings_balance": OutputSpec(type="money", sensitivity=Sensitivity.PII)}
    )
    assert artifact.sensitivity_of("savings_balance") is Sensitivity.PII


# --- serialisation -------------------------------------------------------


def test_yaml_round_trip_is_lossless():
    original = build()
    assert from_yaml(to_yaml(original)) == original


def test_yaml_keeps_declaration_order_for_readable_diffs():
    text = to_yaml(build())
    assert text.index("capability:") < text.index("inputs:") < text.index("steps:")


def test_locator_chains_survive_the_round_trip():
    artifact = build()
    artifact.steps[0].target = Locator(
        primary=RoleNameSpec(role="textbox", name="Member ID"),
        fallbacks=(RowCellSpec(table="", row_match="ID", column=1),),
    )
    restored = from_yaml(to_yaml(artifact))
    assert len(restored.steps[0].target.chain) == 2


# --- parameter interpolation ---------------------------------------------


def test_inputs_are_substituted_into_step_arguments():
    assert render("{{ inputs.member_id }}", {"member_id": "10002"}) == "10002"
    assert render(
        {"value": "id={{ inputs.member_id }}"}, {"member_id": "10002"}
    ) == {"value": "id=10002"}


def test_a_missing_input_raises_rather_than_substituting_nothing():
    """The invariant worth stating loudly.

    Substituting a blank would search for nothing and might well succeed at
    it. In a system that writes to bank records, inventing a value is the
    failure mode to engineer against.
    """
    with pytest.raises(MissingInput):
        render("{{ inputs.member_id }}", {})
    with pytest.raises(MissingInput):
        render("{{ inputs.member_id }}", {"member_id": None})


# --- what the validator refuses ------------------------------------------


def test_rejects_a_reference_to_an_undeclared_input():
    artifact = build()
    artifact.steps[0].args["value"] = "{{ inputs.branch_code }}"
    problems = [str(p) for p in validate(artifact)]
    assert any("branch_code" in p for p in problems)


def test_rejects_a_step_with_no_intent():
    with pytest.raises(ValidationError):
        Step(id="s1", intent="   ", action=ActionType.CLICK, target=heading("x"))


def test_rejects_an_output_no_step_produces():
    artifact = build(outputs={"member_name": OutputSpec(type="string")})
    problems = [str(p) for p in validate(artifact)]
    assert any("no step extracts it" in p for p in problems)


def test_rejects_extracting_into_an_undeclared_output():
    artifact = build()
    artifact.steps[1].args["into"] = "account_number"
    problems = [str(p) for p in validate(artifact)]
    assert any("not a declared output" in p for p in problems)


def test_rejects_require_outputs_naming_something_undeclared():
    artifact = build(success=SuccessSpec(require_outputs=["nonexistent"]))
    problems = [str(p) for p in validate(artifact)]
    assert any("not a declared output" in p for p in problems)


def test_rejects_an_action_that_needs_a_target_and_has_none():
    artifact = build()
    artifact.steps[0].target = None
    problems = [str(p) for p in validate(artifact)]
    assert any("needs a target" in p for p in problems)


def test_rejects_a_discovery_signal_recorded_as_a_step():
    """`done` and `stuck` are things the model says, not things replay does."""
    artifact = build()
    artifact.steps[0].action = ActionType.DONE
    problems = [str(p) for p in validate(artifact)]
    assert any("discovery signal" in p for p in problems)


def test_rejects_an_irreversible_step_with_no_checkpoint():
    """Exactly the class of step where 'did that work?' must have an answer."""
    artifact = build()
    artifact.steps[1].risk = RiskLevel.IRREVERSIBLE
    problems = [str(p) for p in validate(artifact)]
    assert any("must declare a checkpoint" in p for p in problems)


def test_a_recovery_must_carry_a_budget():
    """No default, because a recovery without a budget is an infinite loop
    waiting for a bad day and the right number depends on the condition."""
    with pytest.raises(ValidationError):
        Recovery(code="X", detect=heading("X"), strategy="dismiss")


def test_rejects_a_dismiss_recovery_with_nothing_to_dismiss_with():
    artifact = build(
        recoveries=[
            Recovery(
                code="NOTICE",
                detect=heading("System Notice"),
                strategy="dismiss",
                max_occurrences=2,
            )
        ]
    )
    problems = [str(p) for p in validate(artifact)]
    assert any("dismiss needs dismiss_via" in p for p in problems)


def test_rejects_resume_from_pointing_at_no_step():
    artifact = build(
        recoveries=[
            Recovery(
                code="SESSION_EXPIRED",
                detect=heading("Sign In"),
                strategy="reauthenticate_then_resume",
                max_occurrences=1,
                resume_from="s99",
            )
        ]
    )
    problems = [str(p) for p in validate(artifact)]
    assert any("is not a step id" in p for p in problems)


def test_rejects_a_code_declared_as_both_an_outcome_and_a_recovery():
    """It cannot be a final answer and something we quietly handle. Outcomes
    are evaluated first, so the recovery would be dead code."""
    artifact = build(
        outcomes=[Outcome(code="SESSION_EXPIRED", detect=heading("Sign In"))],
        recoveries=[
            Recovery(
                code="SESSION_EXPIRED",
                detect=heading("Sign In"),
                strategy="reauthenticate_then_resume",
                max_occurrences=1,
                resume_from="s1",
            )
        ],
    )
    problems = [str(p) for p in validate(artifact)]
    assert any("cannot be both" in p for p in problems)


def test_rejects_lowercase_outcome_codes():
    artifact = build(outcomes=[Outcome(code="not_found", detect=heading("x"))])
    problems = [str(p) for p in validate(artifact)]
    assert any("SHOUTING_CASE" in p for p in problems)


def test_rejects_an_override_that_does_not_name_its_tenant():
    artifact = build()
    artifact.capability.extends = "member.read_savings_balance@1.0.0"
    problems = [str(p) for p in validate(artifact)]
    assert any("must name the tenant" in p for p in problems)


def test_rejects_an_override_of_a_different_capability():
    artifact = build()
    artifact.capability.extends = "member.open_subaccount@1.0.0"
    artifact.capability.tenant = "cu-riverbend"
    problems = [str(p) for p in validate(artifact)]
    assert any("different id" in p for p in problems)


def test_ids_and_versions_are_checked_at_construction():
    meta = dict(
        title="x", app=AppRef(vendor="v", product="p"), version="1.0.0"
    )
    with pytest.raises(ValidationError):
        CapabilityMeta(id="MemberRead", **meta)
    with pytest.raises(ValidationError):
        CapabilityMeta(id="member.read", **{**meta, "version": "1.0"})


def test_extends_must_pin_a_version():
    with pytest.raises(ValidationError):
        CapabilityMeta(
            id="member.read",
            version="1.0.0",
            title="x",
            app=AppRef(vendor="v", product="p"),
            extends="member.read",
        )


# --- the store -----------------------------------------------------------


def test_save_then_load_returns_an_equal_artifact(tmp_path):
    store = CapabilityStore(tmp_path)
    original = build()
    store.save(original)
    assert store.load("member.read_savings_balance") == original


def test_save_refuses_an_incoherent_artifact(tmp_path):
    artifact = build()
    artifact.steps[0].args["value"] = "{{ inputs.nope }}"
    with pytest.raises(Exception):
        CapabilityStore(tmp_path).save(artifact)


def test_latest_version_is_resolved_by_semver_not_by_file_time(tmp_path):
    """A checkout reorders timestamps. An automation layer that picks a
    different capability version depending on when the repo was cloned is not
    one anybody should trust."""
    store = CapabilityStore(tmp_path)
    for version in ("1.0.0", "1.10.0", "1.9.0"):
        artifact = build()
        artifact.capability.version = version
        store.save(artifact)

    assert store.versions("member.read_savings_balance") == ["1.0.0", "1.9.0", "1.10.0"]
    assert store.load("member.read_savings_balance").capability.version == "1.10.0"


def test_loading_something_that_is_not_there_says_so(tmp_path):
    with pytest.raises(CapabilityNotFound):
        CapabilityStore(tmp_path).load("member.nothing")


def test_catalogue_lists_the_latest_of_each_capability(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save(build())
    second = build()
    second.capability.id = "member.open_subaccount"
    store.save(second)

    listed = sorted(a.capability.id for a in store.catalogue())
    assert listed == ["member.open_subaccount", "member.read_savings_balance"]


def test_a_corrupt_artifact_does_not_take_the_catalogue_down(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save(build())
    broken = store.dir_for("member.broken")
    broken.mkdir(parents=True)
    (broken / "1.0.0.yaml").write_text("this: is not: an artifact", encoding="utf-8")

    listed = [a.capability.id for a in store.catalogue()]
    assert listed == ["member.read_savings_balance"]


def test_tenant_overrides_are_stored_beside_the_base(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save(build())

    override = build()
    override.capability.tenant = "cu-riverbend"
    override.capability.extends = "member.read_savings_balance@1.0.0"
    path = store.save(override)

    assert path.parent.name == "tenants"
    assert path.name == "cu-riverbend.yaml"
    assert store.load_tenant_override(
        "member.read_savings_balance", "cu-riverbend"
    ) is not None
