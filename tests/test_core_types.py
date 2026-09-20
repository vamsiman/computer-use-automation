"""Smoke tests for the shared vocabulary.

These are cheap but load-bearing: every other module agrees on these types,
so a silent change here is a contract break everywhere.
"""

import json

import pytest
from pydantic import ValidationError

from cua import (
    LOCATOR_TIERS,
    A11yNode,
    Action,
    ActionType,
    LabelProximitySpec,
    Locator,
    LocatorStrategy,
    RoleNameSpec,
    RowCellSpec,
    Snapshot,
)


def test_locator_tiers_cover_every_strategy():
    """The resolution order must not silently omit a strategy."""
    assert set(LOCATOR_TIERS) == set(LocatorStrategy)


def test_locator_chain_is_primary_then_fallbacks_in_order():
    loc = Locator(
        primary=RoleNameSpec(role="textbox", name="Member ID"),
        fallbacks=(
            LabelProximitySpec(label="Member ID:", direction="right"),
            RowCellSpec(table="search", row_match="Member", column=1),
        ),
    )
    assert [s.strategy for s in loc.chain] == [
        LocatorStrategy.ROLE_NAME,
        LocatorStrategy.LABEL_PROXIMITY,
        LocatorStrategy.ROW_CELL,
    ]


def test_locator_survives_json_roundtrip():
    """Artifacts are stored as YAML/JSON; the discriminated union must survive."""
    loc = Locator(
        primary=RoleNameSpec(role="button", name_contains="Search"),
        fallbacks=(LabelProximitySpec(label="Search", direction="self"),),
        confidence=0.8,
    )
    restored = Locator.model_validate(json.loads(loc.model_dump_json()))
    assert restored == loc
    assert isinstance(restored.primary, RoleNameSpec)


def test_role_name_requires_exactly_one_name_rule():
    with pytest.raises(ValidationError):
        RoleNameSpec(role="button")
    with pytest.raises(ValidationError):
        RoleNameSpec(role="button", name="Go", name_contains="Go")


def test_unknown_locator_strategy_is_rejected():
    with pytest.raises(ValidationError):
        Locator.model_validate({"primary": {"strategy": "xpath", "path": "//a"}})


def test_a11y_tree_renders_as_indented_text():
    tree = A11yNode(
        role="document",
        children=[
            A11yNode(role="heading", name="Member Search"),
            A11yNode(role="textbox", name="Member ID", ref="n4"),
        ],
    )
    rendered = tree.to_text()
    assert rendered.splitlines() == [
        "document",
        '  heading "Member Search"',
        '  textbox "Member ID" #n4',
    ]


def test_snapshot_find_filters_by_role_and_name():
    snap = Snapshot(
        location="/members/search",
        tree=A11yNode(
            role="document",
            children=[
                A11yNode(role="button", name="Search"),
                A11yNode(role="button", name="Clear"),
            ],
        ),
    )
    assert len(snap.find("button")) == 2
    assert [n.name for n in snap.find("button", "Clear")] == ["Clear"]


def test_snapshot_text_view_announces_a_blocking_modal():
    """The discovery model has to be able to see that it is blocked."""
    snap = Snapshot(
        location="/members/12345",
        tree=A11yNode(role="document"),
        modal_text="System Notice",
    )
    assert "MODAL OPEN: System Notice" in snap.text_view()


def test_action_rejects_unknown_fields():
    """Actions are a closed contract between discovery, artifact and replay."""
    with pytest.raises(ValidationError):
        Action(type=ActionType.CLICK, selector="#go")
