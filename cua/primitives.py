"""What the system perceives and what it does, independent of any surface.

Nothing here knows that a browser exists. That is the point: these are the
types crossing the seam described in the brief's 3.7, between "how we
perceive and act on a surface" and "the recorded flow". A desktop driver
would produce and consume exactly these.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from cua.locators import Locator
from cua.types import ActionType


def _now() -> datetime:
    return datetime.now(timezone.utc)


class A11yNode(BaseModel):
    """One node of an accessibility tree.

    This is the primary perception format. Browsers build this tree for
    screen readers and operating systems expose the same shape for native
    windows, which is why it is the representation worth standardising on --
    it survives the move from a modern web app to a frameset to a desktop
    app, where raw markup does not.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    name: str = ""
    #: Current value of an input-like control.
    value: str | None = None
    #: Driver-local identifier, used to act on this node within one snapshot.
    ref: str | None = None
    #: Bounding box (x, y, w, h). Only populated when tier-4 targeting needs it.
    box: tuple[int, int, int, int] | None = None
    disabled: bool = False
    children: list["A11yNode"] = Field(default_factory=list)

    def walk(self) -> Iterator["A11yNode"]:
        """Depth-first traversal, including self."""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_text(self, indent: int = 0, max_depth: int = 40) -> str:
        """Compact text rendering, which is what the discovery model sees.

        Feeding the model text rather than a screenshot keeps perception
        cheap and deterministic, and means the thing it points at is a named
        control we can record semantically rather than a pixel coordinate.
        """
        if indent > max_depth:
            return ""
        parts = [self.role]
        if self.name:
            parts.append(f'"{self.name}"')
        if self.value:
            parts.append(f"= {self.value!r}")
        if self.disabled:
            parts.append("(disabled)")
        if self.ref:
            parts.append(f"#{self.ref}")
        lines = ["  " * indent + " ".join(parts)]
        lines.extend(
            rendered
            for child in self.children
            if (rendered := child.to_text(indent + 1, max_depth))
        )
        return "\n".join(lines)


class Snapshot(BaseModel):
    """Everything observed about a surface at one instant."""

    model_config = ConfigDict(extra="forbid")

    #: Where we are. A URL for web; a window identity for desktop.
    location: str
    tree: A11yNode
    #: Path to the screenshot taken alongside this observation. Evidence only
    #: -- perception reads the tree, never the pixels.
    screenshot_ref: str | None = None
    #: Text of any modal currently blocking interaction, if the driver can see one.
    modal_text: str | None = None
    taken_at: datetime = Field(default_factory=_now)

    def find(self, role: str, name: str | None = None) -> list[A11yNode]:
        """Nodes matching a role and, optionally, an exact accessible name."""
        return [
            n
            for n in self.tree.walk()
            if n.role == role and (name is None or n.name == name)
        ]

    def text_view(self) -> str:
        """The rendering handed to the discovery model."""
        header = f"location: {self.location}"
        if self.modal_text:
            header += f"\nMODAL OPEN: {self.modal_text}"
        return f"{header}\n\n{self.tree.to_text()}"


class Action(BaseModel):
    """A single thing to do to a surface.

    The same type is emitted by the discovery model, recorded into an
    artifact, and executed by replay -- so anything discovery can do is by
    construction something replay can reproduce.
    """

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    #: Required for everything except NAVIGATE and the terminal signals.
    target: Locator | None = None
    #: Action-specific payload: ``path`` for navigate, ``value`` for type,
    #: ``into`` for extract, and so on.
    args: dict[str, Any] = Field(default_factory=dict)
    #: Why the model chose this. Carried into the artifact as the step intent.
    intent: str = ""


class ActResult(BaseModel):
    """What happened when an action was executed."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    #: Which locator tier resolved the target, if there was one. 0 is best.
    tier: int | None = None
    #: Value read back, for EXTRACT.
    value: str | None = None
    #: Populated when ``ok`` is False. Human-readable, safe to log.
    error: str | None = None
    #: Observation taken after acting, when the driver captured one.
    observed: Snapshot | None = None


A11yNode.model_rebuild()
