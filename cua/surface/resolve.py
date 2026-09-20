"""Turning a locator chain into a node, and reporting how hard that was.

Everything here is a pure function of an accessibility tree. No browser, no
Playwright, no I/O -- which means the interesting logic is unit-testable
against hand-built trees, and a desktop driver gets it for free by producing
trees of the same shape.

The chain is tried in order, most semantic first, and the tier that succeeded
comes back with the result. That number is the point of the whole design. A
step that resolved on role+name last month and resolves on a pixel offset today
is drifting, and it says so before it breaks.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from cua.locators import (
    AnchorOffsetSpec,
    LabelProximitySpec,
    Locator,
    LocatorSpec,
    RegionPathSpec,
    Resolution,
    RoleNameSpec,
    RowCellSpec,
)
from cua.primitives import A11yNode
from cua.types import LocatorStrategy

#: Roles a label can plausibly be labelling.
CONTROL_ROLES = frozenset(
    {"textbox", "combobox", "listbox", "checkbox", "radio", "button", "link"}
)

#: Roles that can act as a visible text anchor.
ANCHOR_ROLES = frozenset({"text", "label", "cell", "columnheader", "heading"})

#: Pixel slack when measuring the *gap* between a label and its control.
#: Legacy table layouts are full of spacer cells and borders, and being strict
#: about a few pixels of padding would fail tier 2 for reasons that have
#: nothing to do with the page.
ALIGN_TOLERANCE = 6

#: How much two boxes must overlap on the cross axis to count as being on the
#: same line. A fraction rather than a pixel slack, and this is not a detail:
#: applying pixel tolerance to overlap merges adjacent form rows. Stacked rows
#: sit a few pixels apart, so any fixed slack lets the field from the row above
#: qualify as "beside" this label -- and since it is the same distance away
#: horizontally, it can win outright. That is how a password ends up typed
#: into a user-id field, with every individual action reporting success.
OVERLAP_MIN_FRACTION = 0.5

_PATH_STEP = re.compile(r"^([a-z_]+)(?:\[(\d+)\])?$")


def walk(node: A11yNode) -> Iterable[A11yNode]:
    yield from node.walk()


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _label_key(text: str | None) -> str:
    """Compare label text ignoring the trailing colon typographers add."""
    return _norm(text).rstrip(":").strip()


def _text_of(node: A11yNode) -> str:
    return node.name or node.value or ""


def _box(node: A11yNode) -> tuple[int, int, int, int] | None:
    return node.box


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    """Do two spans share enough of their extent to be on the same line?

    Measured as a fraction of the shorter span, so a tall layout cell and a
    short input still line up, while two neighbouring form rows do not.
    """
    overlap = min(a1, b1) - max(a0, b0)
    shorter = min(a1 - a0, b1 - b0)
    if shorter <= 0:
        return overlap >= 0
    return overlap >= shorter * OVERLAP_MIN_FRACTION


# --- tier 1: role + accessible name --------------------------------------


def match_role_name(tree: A11yNode, spec: RoleNameSpec) -> list[A11yNode]:
    role = _norm(spec.role)
    matches = []
    for node in walk(tree):
        if _norm(node.role) != role:
            continue
        if spec.name is not None:
            if _norm(node.name) != _norm(spec.name):
                continue
        else:
            if _norm(spec.name_contains) not in _norm(node.name):
                continue
        matches.append(node)
    return matches


# --- tier 2: the control next to a piece of visible text ------------------


def _find_anchors(tree: A11yNode, label: str) -> list[A11yNode]:
    key = _label_key(label)
    exact = [
        n
        for n in walk(tree)
        if _norm(n.role) in ANCHOR_ROLES and _label_key(_text_of(n)) == key
    ]
    if exact:
        return exact
    return [
        n
        for n in walk(tree)
        if _norm(n.role) in ANCHOR_ROLES and key and key in _label_key(_text_of(n))
    ]


def _directional_distance(
    anchor: tuple[int, int, int, int],
    cand: tuple[int, int, int, int],
    direction: str,
) -> float | None:
    """Distance from anchor to candidate in the given direction, or None.

    Encodes the reasoning a person does without thinking: the field for
    "Member ID:" is the box immediately to its right, on the same line.
    """
    ax, ay, aw, ah = anchor
    cx, cy, cw, ch = cand

    if direction == "right":
        if not _overlaps(ay, ay + ah, cy, cy + ch):
            return None
        gap = cx - (ax + aw)
        return gap if gap >= -ALIGN_TOLERANCE else None

    if direction == "left":
        if not _overlaps(ay, ay + ah, cy, cy + ch):
            return None
        gap = ax - (cx + cw)
        return gap if gap >= -ALIGN_TOLERANCE else None

    if direction == "below":
        if not _overlaps(ax, ax + aw, cx, cx + cw):
            return None
        gap = cy - (ay + ah)
        return gap if gap >= -ALIGN_TOLERANCE else None

    if direction == "above":
        if not _overlaps(ax, ax + aw, cx, cx + cw):
            return None
        gap = ay - (cy + ch)
        return gap if gap >= -ALIGN_TOLERANCE else None

    return None


def match_label_proximity(
    tree: A11yNode, spec: LabelProximitySpec
) -> list[A11yNode]:
    anchors = _find_anchors(tree, spec.label)
    if not anchors:
        return []

    if spec.direction == "self":
        return anchors

    wanted = _norm(spec.role) if spec.role else None
    candidates = [
        n
        for n in walk(tree)
        if _box(n) is not None
        and (_norm(n.role) == wanted if wanted else _norm(n.role) in CONTROL_ROLES)
    ]

    # The same label text usually matches both the text node and the layout
    # cell wrapping it. Prefer the tighter anchor when distances tie: the
    # narrow one is the label, the wide one is scaffolding that happens to
    # contain it, and the narrow one reasons about position more precisely.
    best: tuple[float, int, A11yNode] | None = None
    for anchor in anchors:
        abox = _box(anchor)
        if abox is None:
            continue
        anchor_area = abox[2] * abox[3]
        for cand in candidates:
            if cand is anchor:
                continue
            distance = _directional_distance(abox, _box(cand), spec.direction)
            if distance is None:
                continue
            key = (distance, anchor_area)
            if best is None or key < best[:2]:
                best = (distance, anchor_area, cand)

    return [best[2]] if best else []


# --- tier 3a: a structural path inside a named region ---------------------


def _children_by_role(node: A11yNode, role: str) -> list[A11yNode]:
    return [c for c in node.children if _norm(c.role) == role]


def _find_region(tree: A11yNode, region: str) -> A11yNode | None:
    step = _PATH_STEP.match(region.strip())
    if step and step.group(2) is not None:
        role, index = step.group(1), int(step.group(2))
        found = [n for n in walk(tree) if _norm(n.role) == role]
        return found[index] if index < len(found) else None

    key = _norm(region)
    for node in walk(tree):
        if _norm(node.name) == key:
            return node
    return None


def match_region_path(tree: A11yNode, spec: RegionPathSpec) -> list[A11yNode]:
    """Walk a path of ``role[index]`` steps relative to a located region.

    The path is expressed on the accessibility tree rather than on DOM tags.
    That keeps even the structural fallback surface-agnostic -- ``row[1]/cell[2]``
    means something on a native table too, where ``tr``/``td`` would not.

    Scoping to a region matters: an absolute path from the document root
    breaks when anything above it shifts, while a path anchored to a region we
    found semantically only breaks if that region's internals change.
    """
    current = _find_region(tree, spec.region)
    if current is None:
        return []

    for raw in spec.path.strip("/").split("/"):
        step = _PATH_STEP.match(raw.strip())
        if step is None:
            return []
        role = step.group(1)
        index = int(step.group(2)) if step.group(2) is not None else 0
        siblings = _children_by_role(current, role)
        if index >= len(siblings):
            return []
        current = siblings[index]

    return [current]


# --- tier 3b: a cell picked by its row and column -------------------------


def _row_cells(row: A11yNode) -> list[A11yNode]:
    return [c for c in row.children if _norm(c.role) in ("cell", "columnheader")]


def _column_index(header: A11yNode, column: str | int) -> int | None:
    if isinstance(column, int):
        cells = _row_cells(header)
        return column if 0 <= column < len(cells) else None
    key = _norm(column)
    for i, cell in enumerate(_row_cells(header)):
        if _norm(_text_of(cell)) == key:
            return i
    return None


def match_row_cell(tree: A11yNode, spec: RowCellSpec) -> list[A11yNode]:
    """Find "the balance on the savings row".

    The grid this targets has no ``<th>`` anywhere -- its header row is
    ordinary cells with a class on them -- so there is no column semantics to
    read. We reconstruct it the way a person does: find the table whose first
    row carries the heading we want, count along to that column, then find the
    row whose text identifies it.

    Locating the table by the heading it contains, rather than by an id, is
    deliberate. Generated ids like ``ctl00_ContentPlaceHolder1_grdAccounts``
    are exactly the thing that changes between vendor versions.
    """
    tables = [n for n in walk(tree) if _norm(n.role) == "table"]
    if not tables:
        return []

    hint = _norm(spec.table)
    row_key = _norm(spec.row_match)
    matches: list[A11yNode] = []

    for table in tables:
        rows = _children_by_role(table, "row")
        if len(rows) < 2:
            continue

        header, body = rows[0], rows[1:]
        column_at = _column_index(header, spec.column)
        if column_at is None:
            continue

        if hint:
            table_text = " ".join(
                _text_of(n) for n in table.walk() if _text_of(n)
            ).lower()
            if _norm(table.name) != hint and hint not in table_text:
                continue

        for row in body:
            cells = _row_cells(row)
            if column_at >= len(cells):
                continue
            if any(row_key in _norm(_text_of(c)) for c in cells):
                matches.append(cells[column_at])

    return matches


# --- tier 4: a pixel offset from a text anchor ----------------------------


def _area(node: A11yNode) -> int:
    box = _box(node)
    return box[2] * box[3] if box else 0


def match_anchor_offset(tree: A11yNode, spec: AnchorOffsetSpec) -> list[A11yNode]:
    """Last resort: the thing at a fixed offset from a piece of text.

    Recorded so a flow through a canvas-drawn or image-mapped control is still
    replayable at all. An offset encodes where something sat once rather than
    what it is, so resolving here is always reported as degraded.
    """
    anchors = _find_anchors(tree, spec.anchor_text)
    if not anchors:
        return []

    abox = _box(anchors[0])
    if abox is None:
        return []

    px, py = abox[0] + spec.dx, abox[1] + spec.dy
    hits = [
        n
        for n in walk(tree)
        if (b := _box(n)) is not None
        and b[0] <= px <= b[0] + b[2]
        and b[1] <= py <= b[1] + b[3]
    ]
    if not hits:
        return []
    return [min(hits, key=_area)]


# --- the chain ------------------------------------------------------------

_MATCHERS = {
    LocatorStrategy.ROLE_NAME: match_role_name,
    LocatorStrategy.LABEL_PROXIMITY: match_label_proximity,
    LocatorStrategy.REGION_PATH: match_region_path,
    LocatorStrategy.ROW_CELL: match_row_cell,
    LocatorStrategy.ANCHOR_OFFSET: match_anchor_offset,
}


def match_spec(tree: A11yNode, spec: LocatorSpec) -> list[A11yNode]:
    return _MATCHERS[spec.strategy](tree, spec)


def resolve_in_tree(tree: A11yNode, locator: Locator) -> Resolution:
    """Try each rule in the chain and report which one worked.

    ``nth`` is honoured only on role+name, where ordering is meaningful.
    Ambiguity is reported rather than silently resolved: ``match_count > 1``
    means the rule did not identify one control, and a caller that cares about
    correctness should treat that as a weaker result than a unique match even
    though we do return the first.
    """
    for tier, spec in enumerate(locator.chain):
        matches = match_spec(tree, spec)
        if not matches:
            continue

        index = spec.nth if isinstance(spec, RoleNameSpec) else 0
        if index >= len(matches):
            continue

        chosen = matches[index]
        return Resolution(
            resolved=True,
            tier=tier,
            strategy=spec.strategy,
            handle=chosen,
            match_count=len(matches),
            detail=f"{chosen.role} {chosen.name!r}" if chosen.name else chosen.role,
        )

    return Resolution(
        resolved=False,
        detail=f"no tier resolved {locator.describe()}",
    )


def tried_strategies(locator: Locator) -> Sequence[LocatorStrategy]:
    return [spec.strategy for spec in locator.chain]
