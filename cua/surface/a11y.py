"""Assembling one accessibility tree out of a page made of frames.

Kept free of Playwright so the assembly rules -- and the hash that stuck
detection depends on -- can be tested without a browser.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from cua.primitives import A11yNode

_REF = re.compile(r"^f(\d+)n(\d+)$")


def frame_index_of(ref: str) -> int | None:
    """Which frame a node ref belongs to.

    Refs carry their frame index (``f2n17``) so acting on a node needs nothing
    beyond the ref itself: no side table, and no ambiguity when two frames
    number their nodes the same way.
    """
    match = _REF.match(ref or "")
    return int(match.group(1)) if match else None


def node_from_raw(raw: dict[str, Any]) -> A11yNode:
    return A11yNode(
        role=raw.get("role", "generic"),
        name=raw.get("name") or "",
        value=raw.get("value"),
        ref=raw.get("ref"),
        box=tuple(raw["box"]) if raw.get("box") else None,
        disabled=bool(raw.get("disabled")),
        children=[node_from_raw(c) for c in raw.get("children", [])],
    )


def document_from_raw(raw: dict[str, Any], role: str = "document") -> A11yNode:
    return A11yNode(
        role=role,
        name=raw.get("title") or "",
        children=[node_from_raw(c) for c in raw.get("children", [])],
    )


def find_by_ref(tree: A11yNode, ref: str) -> A11yNode | None:
    return next((n for n in tree.walk() if n.ref == ref), None)


def splice_frame(tree: A11yNode, host_ref: str, subtree: A11yNode) -> bool:
    """Hang a child frame's tree off the iframe node that hosts it.

    Appending frames at the root would be easier and would lose the thing
    that matters: where the frame sits relative to everything else. Tier-2
    targeting reasons about which control is next to which label, and tier-3
    reasons about position within a region, so a frame dumped at the end of
    the tree would break both.
    """
    host = find_by_ref(tree, host_ref)
    if host is None:
        return False
    host.children.append(subtree)
    return True


def tree_hash(tree: A11yNode) -> str:
    """A stable fingerprint of what is on screen.

    Deliberately ignores refs and geometry. Refs are regenerated on every
    observation and boxes shift by a pixel for reasons nobody cares about, so
    including either would make every snapshot look different and defeat the
    purpose.

    Discovery compares successive hashes to notice that an action changed
    nothing -- an agent clicking a dead control in a loop looks exactly like
    an unchanged hash, and that is the cheapest reliable stuck signal we have.
    """
    digest = hashlib.sha256()
    for node in tree.walk():
        digest.update(f"{node.role}\x1f{node.name}\x1f{node.value or ''}\x1e".encode())
    return digest.hexdigest()[:16]


def count_nodes(tree: A11yNode) -> int:
    return sum(1 for _ in tree.walk())
