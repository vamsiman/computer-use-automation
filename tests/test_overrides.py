"""Tenant overrides: one capability, two deployments.

The unit half. The measurement that makes it worth having -- one artifact
replayed against both tenants, with the tier log showing the degradation and
its repair -- is in ``test_tenant_live.py``.
"""

from __future__ import annotations

import pytest

from cua.artifact.overrides import OverrideError, for_tenant, merge
from cua.artifact.store import CapabilityStore
from cua.types import LocatorStrategy

TENANT = "cu-riverbend"


@pytest.fixture
def library():
    return CapabilityStore()


@pytest.fixture
def base(library):
    return library.load("member.read_savings_balance")


# --- what an override does -----------------------------------------------


def test_the_shipped_override_changes_one_thing(library, base):
    """Riverbend differs in a field's wording. That is the entire document.

    Worth asserting as a count rather than a description: an override that
    quietly grows is an override that has become a fork, and the day it does
    the base's fixes stop reaching it.
    """
    merged = for_tenant(library, base, TENANT)

    differing = [
        step.id
        for step, original in zip(merged.steps, base.steps)
        if step.target != original.target
    ]
    assert differing == ["s2"]
    assert merged.steps[1].target.primary.label == "Member Number:"


def test_every_tenant_answers_to_the_same_signature(library, base):
    """The strongest rule here.

    Two deployments of one capability have to be interchangeable to a caller.
    If they are not, the catalogue is lying about at least one of them, and an
    agent that read the contract for one tenant would call the other wrongly.
    """
    merged = for_tenant(library, base, TENANT)

    assert merged.signature() == base.signature()
    assert merged.capability.id == base.capability.id
    assert list(merged.inputs) == list(base.inputs)
    assert list(merged.outputs) == list(base.outputs)


def test_the_override_records_what_it_extends(library, base):
    merged = for_tenant(library, base, TENANT)
    assert merged.capability.tenant == TENANT
    assert merged.capability.extends == "member.read_savings_balance@1.0.0"


def test_merging_does_not_touch_the_base(library, base):
    before = base.steps[1].target.primary.model_dump()
    for_tenant(library, base, TENANT)
    assert base.steps[1].target.primary.model_dump() == before


def test_a_tenant_with_no_override_gets_the_base(library, base):
    """Not an error, and the interesting case: this is the capability meeting
    a deployment nobody prepared it for, which is what the fallback chain is
    for."""
    merged = for_tenant(library, base, "cu-somewhere-else")

    assert merged.capability.tenant == "cu-somewhere-else"
    assert merged.steps[1].target == base.steps[1].target


def test_the_step_that_needed_no_override(library, base):
    """Riverbend moves Balance from the third column to the second, and s5 is
    unchanged -- it is keyed on the column *name*, so it follows the move.

    This is the argument for row_cell outranking a structural path, stated as
    a test: `row[1]/cell[2]` would have resolved here too, and read the
    account number.
    """
    merged = for_tenant(library, base, TENANT)
    assert merged.steps[4].target == base.steps[4].target
    assert merged.steps[4].target.primary.strategy is LocatorStrategy.ROW_CELL
    assert merged.steps[4].target.primary.column == "Balance"


# --- what an override may not do -----------------------------------------


def test_a_tenant_may_not_add_a_step(base):
    """Describing a screen differently is configuration. Needing a different
    sequence of actions is a different capability, and letting an override
    grow one would mean the signature a caller read no longer describes what
    runs."""
    with pytest.raises(OverrideError, match="not in the base"):
        merge(base, {"capability": {"tenant": TENANT}, "steps": [{"id": "s99"}]})


def test_a_tenant_may_not_change_what_a_step_does(base):
    with pytest.raises(OverrideError, match="may only vary"):
        merge(
            base,
            {
                "capability": {"tenant": TENANT},
                "steps": [{"id": "s5", "action": "click"}],
            },
        )


def test_a_tenant_may_not_change_the_contract(base):
    with pytest.raises(OverrideError, match="same signature"):
        merge(
            base,
            {
                "capability": {"tenant": TENANT},
                "inputs": {"member_id": {"type": "integer"}},
            },
        )


def test_an_override_must_name_its_tenant(base):
    """Without it there is nothing to tell two overrides apart, and a merged
    artifact that does not know which deployment it describes is worse than
    no override at all."""
    with pytest.raises(OverrideError, match="must name its tenant"):
        merge(base, {"steps": [{"id": "s2"}]})


def test_an_override_step_must_say_which_step(base):
    with pytest.raises(OverrideError, match="name the id"):
        merge(base, {"capability": {"tenant": TENANT}, "steps": [{"target": {}}]})


def test_the_merged_document_is_validated(base):
    """The override is not checked; the result is. A sparse step has no intent
    and no action and could not be an Artifact if it tried -- requiring it to
    be would mean copying both out of the base into every override, which is
    the duplication the whole mechanism exists to avoid."""
    with pytest.raises(Exception):
        merge(
            base,
            {
                "capability": {"tenant": TENANT},
                "steps": [{"id": "s2", "target": {"primary": {"strategy": "nonsense"}}}],
            },
        )


def test_a_tenant_may_vary_its_recoveries(base):
    """Allowed, and it should be: what a deployment interrupts you with is a
    property of the deployment, not of the task."""
    merged = merge(
        base,
        {
            "capability": {"tenant": TENANT},
            "recoveries": [
                {
                    "code": "BRANCH_NOTICE",
                    "detect": {
                        "primary": {
                            "strategy": "role_name",
                            "role": "dialog",
                            "name": "Branch Notice",
                        }
                    },
                    "strategy": "dismiss",
                    "dismiss_via": {
                        "primary": {
                            "strategy": "role_name",
                            "role": "button",
                            "name": "OK",
                        }
                    },
                    "max_occurrences": 1,
                }
            ],
        },
    )
    assert [r.code for r in merged.recoveries] == ["BRANCH_NOTICE"]
    assert merged.signature() == base.signature()
