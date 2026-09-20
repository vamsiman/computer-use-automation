"""Locator resolution, tested against hand-built trees.

No browser here on purpose. The resolver is a pure function of an
accessibility tree, so the interesting behaviour -- which tier wins, what
happens when the preferred one misses, whether ambiguity is reported -- can be
pinned down without launching anything.

The trees below mirror the real target app: a search field with no accessible
name sitting beside a bare label, a submit button that does have one, and an
accounts grid whose header row is ordinary cells.
"""

import pytest

from cua.locators import (
    AnchorOffsetSpec,
    LabelProximitySpec,
    Locator,
    RegionPathSpec,
    RoleNameSpec,
    RowCellSpec,
)
from cua.primitives import A11yNode
from cua.surface.a11y import tree_hash
from cua.surface.resolve import resolve_in_tree
from cua.types import LocatorStrategy


def node(role, name="", *, box=None, value=None, ref=None, children=()):
    return A11yNode(
        role=role,
        name=name,
        box=box,
        value=value,
        ref=ref,
        children=list(children),
    )


@pytest.fixture
def search_tree():
    """The search screen. The member-id field has NO accessible name."""
    return node(
        "document",
        "Member Search",
        children=[
            node("heading", "Member Search", box=(10, 20, 200, 20), ref="f0n1"),
            node("text", "Member ID:", box=(10, 100, 80, 18), ref="f0n2"),
            node("textbox", "", box=(100, 100, 120, 20), ref="f0n3"),
            node("text", "Branch:", box=(10, 140, 80, 18), ref="f0n4"),
            node("textbox", "", box=(100, 140, 120, 20), ref="f0n5"),
            node("button", "Search", box=(240, 100, 60, 22), ref="f0n6"),
        ],
    )


@pytest.fixture
def detail_tree():
    """Member details, including the grid with no column headers."""
    header = node(
        "row",
        children=[
            node("cell", "Type"),
            node("cell", "Account No."),
            node("cell", "Balance"),
            node("cell", "Opened"),
        ],
    )
    savings = node(
        "row",
        children=[
            node("cell", "Savings", ref="f1n10"),
            node("cell", "SV-4471902"),
            node("cell", "4,210.33", ref="f1n12"),
            node("cell", "2019-03-14"),
        ],
    )
    checking = node(
        "row",
        children=[
            node("cell", "Checking"),
            node("cell", "CK-8820355"),
            node("cell", "1,287.50", ref="f1n16"),
            node("cell", "2019-03-14"),
        ],
    )
    return node(
        "document",
        children=[
            node("heading", "Member Details", box=(10, 20, 200, 20)),
            node("table", "", children=[header, savings, checking]),
            node("link", "Open Sub-Account", box=(10, 400, 130, 18)),
        ],
    )


# --- tier 1 ---------------------------------------------------------------


def test_role_name_resolves_at_tier_zero(search_tree):
    result = resolve_in_tree(
        search_tree, Locator(primary=RoleNameSpec(role="button", name="Search"))
    )
    assert result.resolved
    assert result.tier == 0
    assert result.strategy is LocatorStrategy.ROLE_NAME
    assert not result.degraded


def test_role_name_contains_matches_partial_names(search_tree):
    result = resolve_in_tree(
        search_tree,
        Locator(primary=RoleNameSpec(role="heading", name_contains="Search")),
    )
    assert result.resolved
    assert result.handle.name == "Member Search"


def test_role_name_cannot_find_the_unnamed_field(search_tree):
    """The premise of the whole fallback chain.

    Two textboxes, neither with an accessible name. Role+name has nothing to
    work with, and quietly returning the first one would be worse than
    failing -- on this app that is the difference between searching for a
    member and typing into the branch filter.
    """
    result = resolve_in_tree(
        search_tree,
        Locator(primary=RoleNameSpec(role="textbox", name="Member ID")),
    )
    assert not result.resolved


def test_ambiguity_is_reported_not_hidden(search_tree):
    result = resolve_in_tree(
        search_tree, Locator(primary=RoleNameSpec(role="textbox", name=""))
    )
    assert result.resolved
    assert result.match_count == 2


def test_nth_disambiguates_explicitly(search_tree):
    result = resolve_in_tree(
        search_tree, Locator(primary=RoleNameSpec(role="textbox", name="", nth=1))
    )
    assert result.handle.ref == "f0n5"


# --- tier 2 ---------------------------------------------------------------


def test_label_proximity_finds_the_field_right_of_its_label(search_tree):
    result = resolve_in_tree(
        search_tree,
        Locator(primary=LabelProximitySpec(label="Member ID:", direction="right")),
    )
    assert result.resolved
    assert result.handle.ref == "f0n3"


def test_label_proximity_ignores_the_trailing_colon(search_tree):
    result = resolve_in_tree(
        search_tree,
        Locator(primary=LabelProximitySpec(label="Member ID", direction="right")),
    )
    assert result.handle.ref == "f0n3"


def test_label_proximity_respects_the_row_it_is_on(search_tree):
    """The Branch field is also to the right of something. Vertical alignment
    is what stops the two labels claiming each other's control."""
    result = resolve_in_tree(
        search_tree,
        Locator(primary=LabelProximitySpec(label="Branch:", direction="right")),
    )
    assert result.handle.ref == "f0n5"


def test_chain_falls_through_to_tier_one_and_says_so(search_tree):
    """The headline behaviour: role+name misses, the label rule catches it,
    and the result records that we are now a tier worse than ideal."""
    locator = Locator(
        primary=RoleNameSpec(role="textbox", name="Member ID"),
        fallbacks=(LabelProximitySpec(label="Member ID:", direction="right"),),
    )
    result = resolve_in_tree(search_tree, locator)

    assert result.resolved
    assert result.tier == 1
    assert result.strategy is LocatorStrategy.LABEL_PROXIMITY
    assert result.degraded
    assert result.handle.ref == "f0n3"


def test_label_proximity_self_returns_the_label(search_tree):
    result = resolve_in_tree(
        search_tree,
        Locator(primary=LabelProximitySpec(label="Member ID:", direction="self")),
    )
    assert result.handle.ref == "f0n2"


# --- tier 3 ---------------------------------------------------------------


def test_row_cell_finds_the_balance_on_the_savings_row(detail_tree):
    result = resolve_in_tree(
        detail_tree,
        Locator(
            primary=RowCellSpec(table="", row_match="Savings", column="Balance")
        ),
    )
    assert result.resolved
    assert result.handle.name == "4,210.33"


def test_row_cell_picks_the_right_row_not_just_the_first(detail_tree):
    result = resolve_in_tree(
        detail_tree,
        Locator(
            primary=RowCellSpec(table="", row_match="Checking", column="Balance")
        ),
    )
    assert result.handle.name == "1,287.50"


def test_row_cell_survives_a_reordered_grid():
    """A tenant that swaps its columns round is a config change for them and
    a targeting problem for us. Counting from the heading rather than from a
    fixed index is what absorbs it."""
    header = node(
        "row",
        children=[
            node("cell", "Balance"),
            node("cell", "Type"),
            node("cell", "Opened"),
        ],
    )
    row = node(
        "row",
        children=[
            node("cell", "982.14"),
            node("cell", "Savings"),
            node("cell", "2021-07-02"),
        ],
    )
    tree = node("document", children=[node("table", children=[header, row])])

    result = resolve_in_tree(
        tree,
        Locator(primary=RowCellSpec(table="", row_match="Savings", column="Balance")),
    )
    assert result.handle.name == "982.14"


def test_row_cell_by_column_index_when_there_are_no_headings(detail_tree):
    result = resolve_in_tree(
        detail_tree,
        Locator(primary=RowCellSpec(table="", row_match="Savings", column=2)),
    )
    assert result.handle.name == "4,210.33"


def test_region_path_walks_the_tree_not_the_markup(detail_tree):
    """`row[1]/cell[2]` is expressed on the accessibility tree, so the
    structural fallback still means something on a surface that has no
    <tr> or <td> at all."""
    result = resolve_in_tree(
        detail_tree,
        Locator(primary=RegionPathSpec(region="table[0]", path="row[1]/cell[2]")),
    )
    assert result.resolved
    assert result.handle.name == "4,210.33"


def test_region_path_gives_up_rather_than_guessing(detail_tree):
    result = resolve_in_tree(
        detail_tree,
        Locator(primary=RegionPathSpec(region="table[0]", path="row[9]/cell[0]")),
    )
    assert not result.resolved


# --- tier 4 ---------------------------------------------------------------


def test_anchor_offset_resolves_and_is_always_degraded(search_tree):
    locator = Locator(
        primary=RoleNameSpec(role="textbox", name="Member ID"),
        fallbacks=(AnchorOffsetSpec(anchor_text="Member ID:", dx=120, dy=4),),
    )
    result = resolve_in_tree(search_tree, locator)

    assert result.resolved
    assert result.strategy is LocatorStrategy.ANCHOR_OFFSET
    assert result.degraded
    assert result.handle.ref == "f0n3"


# --- the chain as a whole -------------------------------------------------


def test_chain_reports_failure_when_every_tier_misses(search_tree):
    locator = Locator(
        primary=RoleNameSpec(role="button", name="Delete"),
        fallbacks=(LabelProximitySpec(label="Nothing Here:", direction="right"),),
    )
    result = resolve_in_tree(search_tree, locator)

    assert not result.resolved
    assert result.tier is None
    assert "Delete" in result.detail


def test_tiers_are_tried_in_order(search_tree):
    """If a later tier could also match, the earlier one must still win --
    otherwise the tier number stops meaning anything."""
    locator = Locator(
        primary=RoleNameSpec(role="button", name="Search"),
        fallbacks=(LabelProximitySpec(label="Member ID:", direction="right"),),
    )
    result = resolve_in_tree(search_tree, locator)
    assert result.tier == 0
    assert result.handle.ref == "f0n6"


# --- tree hashing ---------------------------------------------------------


def test_tree_hash_ignores_refs_and_geometry(search_tree):
    """Refs are regenerated on every observation and boxes drift by a pixel.
    Either would make every snapshot look new and destroy stuck detection."""
    moved = search_tree.model_copy(deep=True)
    for i, child in enumerate(moved.children):
        child.ref = f"f0n{100 + i}"
        if child.box:
            child.box = (child.box[0] + 1, child.box[1], child.box[2], child.box[3])

    assert tree_hash(moved) == tree_hash(search_tree)


def test_tree_hash_changes_when_content_changes(search_tree):
    changed = search_tree.model_copy(deep=True)
    changed.children[0].name = "Member Details"
    assert tree_hash(changed) != tree_hash(search_tree)


def test_tree_hash_changes_when_a_field_value_changes(search_tree):
    typed = search_tree.model_copy(deep=True)
    typed.children[2].value = "10001"
    assert tree_hash(typed) != tree_hash(search_tree)
