"""Turning "that control, there" into a locator that will still work in March.

The discovery model points at a node by its reference -- ``#f0n17`` -- because
that is the one thing it can do reliably against a page it has never seen.
A reference is useless five minutes later: it is an index into one snapshot of
one page load. So the moment the model points at something, we work out how to
describe that same control *semantically*, and it is the description, never the
reference, that reaches the artifact.

The rule that makes this trustworthy: **every spec in the chain is verified
against the live tree before it is written down.** A spec that does not resolve
back to the node the model actually pointed at is discarded rather than
recorded hopefully. So a discovered artifact contains only locators that were
executed, in order, against the real application -- which is a much stronger
claim than one where a second model call invents plausible selectors after the
fact and nothing ever checks them.

Doing it here rather than in distillation is deliberate. This is the only
moment the live tree and the model's choice exist together; ten minutes later
in a distillation pass there is nothing left to verify against.
"""

from __future__ import annotations

from cua.locators import (
    LabelProximitySpec,
    Locator,
    LocatorSpec,
    RegionPathSpec,
    RoleNameSpec,
    RowCellSpec,
)
from cua.primitives import A11yNode
from cua.surface.resolve import match_spec, walk
from cua.types import LocatorStrategy

#: Roles worth anchoring a structural path to. A path from the document root
#: breaks the moment anything above it shifts; one anchored to a region we
#: found semantically only breaks if that region's own internals change.
REGION_ROLES = frozenset({"form", "table", "main", "region", "navigation", "dialog"})

#: Roles whose text can serve as a label for a neighbouring control.
LABEL_ROLES = frozenset({"text", "cell", "label", "columnheader"})

#: How much to trust a chain, by the tier its primary sits on.
CONFIDENCE_BY_STRATEGY = {
    LocatorStrategy.ROLE_NAME: 1.0,
    LocatorStrategy.LABEL_PROXIMITY: 0.85,
    LocatorStrategy.ROW_CELL: 0.8,
    LocatorStrategy.REGION_PATH: 0.6,
    LocatorStrategy.ANCHOR_OFFSET: 0.3,
}

#: Directions tried when looking for the label belonging to a control, in the
#: order legacy forms actually lay them out.
LABEL_DIRECTIONS = ("right", "below")


def _text(node: A11yNode) -> str:
    return (node.name or node.value or "").strip()


def index_of(nodes: list[A11yNode], node: A11yNode) -> int | None:
    """Position of *this* node, by identity.

    ``list.index`` would compare by value, and these are Pydantic models: two
    empty cells in a table are equal to each other without being the same cell.
    Every index in this module has to mean "the one the model pointed at".
    """
    for i, candidate in enumerate(nodes):
        if candidate is node:
            return i
    return None


def parent_map(tree: A11yNode) -> dict[int, A11yNode]:
    """Child id -> parent. The tree is one-directional, and we need to climb."""
    parents: dict[int, A11yNode] = {}
    for node in walk(tree):
        for child in node.children:
            parents[id(child)] = node
    return parents


def ancestors(node: A11yNode, parents: dict[int, A11yNode]) -> list[A11yNode]:
    """Nearest first, up to the root."""
    chain = []
    current = parents.get(id(node))
    while current is not None:
        chain.append(current)
        current = parents.get(id(current))
    return chain


def verifies(tree: A11yNode, spec: LocatorSpec, node: A11yNode) -> bool:
    """Does this spec find exactly the node we meant?

    Identity, not equality: two cells reading "0.00" are equal and are not the
    same cell, and a locator that resolves to the wrong one of them is the
    failure this whole module exists to avoid.
    """
    matches = match_spec(tree, spec)
    return len(matches) == 1 and matches[0] is node


def _role_name_spec(tree: A11yNode, node: A11yNode) -> RoleNameSpec | None:
    """Tier 0: the control says what it is and what it is called."""
    if not node.name:
        return None
    same = [
        n
        for n in walk(tree)
        if n.role == node.role and n.name == node.name
    ]
    nth = index_of(same, node)
    if nth is None:
        return None
    spec = RoleNameSpec(role=node.role, name=node.name, nth=nth)
    return spec if verifies(tree, spec, node) else None


def _label_spec(tree: A11yNode, node: A11yNode) -> LabelProximitySpec | None:
    """Tier 1: "the field beside the words 'Member ID:'".

    The role is always stated explicitly. Left out, the rule defaults to
    interactive controls -- right when the next thing to do is type, wrong when
    the next thing to do is read a value sitting beside a label, and the sort of
    default that produces a locator which works everywhere except where it
    matters.
    """
    candidates = [
        n
        for n in walk(tree)
        if n is not node and n.role in LABEL_ROLES and _text(n) and len(_text(n)) < 40
    ]
    # Nearest label first: on a form laid out in a table, the closest text to
    # the left of a field is almost always its label.
    for direction in LABEL_DIRECTIONS:
        for label in candidates:
            spec = LabelProximitySpec(
                label=_text(label), direction=direction, role=node.role
            )
            if verifies(tree, spec, node):
                return spec
    return None


def _row_cell_spec(
    tree: A11yNode, node: A11yNode, parents: dict[int, A11yNode]
) -> RowCellSpec | None:
    """Tier 3: "the Balance on the Savings row".

    Only meaningful for a cell, and worth trying before a structural path
    because it survives a tenant reordering its columns, which a path does not.
    """
    if node.role != "cell":
        return None
    row = parents.get(id(node))
    if row is None or row.role != "row":
        return None
    table = parents.get(id(row))
    if table is None or table.role != "table":
        return None

    rows = [c for c in table.children if c.role == "row"]
    if len(rows) < 2 or rows[0] is row:
        return None
    cells = [c for c in row.children if c.role in ("cell", "columnheader")]
    header = [c for c in rows[0].children if c.role in ("cell", "columnheader")]
    index = index_of(cells, node)
    if index is None:
        return None

    column = _text(header[index]) if index < len(header) else index
    # Identify the row by some *other* cell's text. Using this cell's own value
    # would produce a locator that only finds the answer we already knew.
    row_key = next(
        (_text(c) for c in cells if c is not node and _text(c)), None
    )
    if row_key is None or not column:
        return None

    spec = RowCellSpec(table="", row_match=row_key, column=column)
    return spec if verifies(tree, spec, node) else None


def _region_path_spec(
    tree: A11yNode, node: A11yNode, parents: dict[int, A11yNode]
) -> RegionPathSpec | None:
    """Tier 2: a structural path, scoped to a region found semantically."""
    for ancestor in ancestors(node, parents):
        region = _region_name(tree, ancestor)
        if region is None:
            continue
        path = _path_between(ancestor, node, parents)
        if path is None:
            continue
        spec = RegionPathSpec(region=region, path=path)
        if verifies(tree, spec, node):
            return spec
    return None


def _region_name(tree: A11yNode, node: A11yNode) -> str | None:
    """How to name a region: by its own name if it has one, else by index."""
    if node.name:
        return node.name
    if node.role not in REGION_ROLES:
        return None
    same = [n for n in walk(tree) if n.role == node.role]
    index = index_of(same, node)
    return f"{node.role}[{index}]" if index is not None else None


def _path_between(
    ancestor: A11yNode, node: A11yNode, parents: dict[int, A11yNode]
) -> str | None:
    steps: list[str] = []
    current = node
    while current is not ancestor:
        parent = parents.get(id(current))
        if parent is None:
            return None
        siblings = [c for c in parent.children if c.role == current.role]
        index = index_of(siblings, current)
        if index is None:
            return None
        steps.append(f"{current.role}[{index}]")
        current = parent
    return "/".join(reversed(steps)) if steps else None


def synthesize(tree: A11yNode, node: A11yNode) -> Locator | None:
    """Build a verified, tiered locator for one node.

    Returns ``None`` when nothing in the vocabulary can identify this node
    unambiguously -- which is a real answer, and better than a locator that
    resolves to something else on a later run. The caller reports the control
    as untargetable and the model picks something else.
    """
    parents = parent_map(tree)
    candidates: list[LocatorSpec | None] = [
        _role_name_spec(tree, node),
        _label_spec(tree, node),
        _region_path_spec(tree, node, parents),
        _row_cell_spec(tree, node, parents),
    ]
    specs = [spec for spec in candidates if spec is not None]
    if not specs:
        return None

    # Tier order is the resolution order, so the chain has to be sorted by it
    # rather than by the order we happened to try things.
    from cua.types import LOCATOR_TIERS

    specs.sort(key=lambda s: LOCATOR_TIERS.index(s.strategy))
    primary, *fallbacks = specs

    return Locator(
        primary=primary,
        fallbacks=tuple(fallbacks),
        confidence=CONFIDENCE_BY_STRATEGY.get(primary.strategy, 0.5),
        note=_note(primary, fallbacks, node),
    )


def _note(primary: LocatorSpec, fallbacks: list[LocatorSpec], node: A11yNode) -> str:
    """Why this chain looks the way it does, for whoever reviews the artifact."""
    if primary.strategy is LocatorStrategy.ROLE_NAME:
        head = "Resolves on role and accessible name."
    else:
        head = (
            f"No usable accessible name on this {node.role}, so the primary is "
            f"{primary.strategy.value}."
        )
    if not fallbacks:
        return head + " No verified fallback -- this chain is one rule deep."
    tiers = ", ".join(spec.strategy.value for spec in fallbacks)
    return f"{head} Verified fallbacks: {tiers}."
