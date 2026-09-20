"""How a control is identified on a surface.

This module is shared deliberately: the artifact schema *stores* locators and
the surface driver *resolves* them, and they must mean exactly the same thing.

The central idea is that a locator is not one rule but an ordered chain of
them, most semantic first. Replay tries each tier in turn and reports which
one actually worked. That report is what makes drift observable: a step that
used to resolve on role+name and now only resolves on a pixel offset is
telling you the surface moved underneath it, well before it breaks outright.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.types import LocatorStrategy


class _Spec(BaseModel):
    """Base for every targeting rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RoleNameSpec(_Spec):
    """Tier 1 -- accessibility role plus accessible name.

    The preferred rule everywhere. It is what a screen reader would use, it
    ignores markup structure entirely, and the equivalent exists on desktop
    surfaces, which is what lets one artifact schema span web and native.
    """

    strategy: Literal[LocatorStrategy.ROLE_NAME] = LocatorStrategy.ROLE_NAME
    role: str
    #: Exact accessible name. Mutually exclusive with ``name_contains``.
    name: str | None = None
    #: Substring match, for names carrying volatile text (counts, dates).
    name_contains: str | None = None
    #: Disambiguates when several nodes match. Kept explicit, never implicit.
    nth: int = 0

    @model_validator(mode="after")
    def _exactly_one_name_rule(self) -> RoleNameSpec:
        if (self.name is None) == (self.name_contains is None):
            raise ValueError("set exactly one of 'name' or 'name_contains'")
        return self


class LabelProximitySpec(_Spec):
    """Tier 2 -- the control sitting next to a piece of visible text.

    Legacy markup routinely puts a bare ``<span>Member ID:</span>`` beside an
    input with no programmatic association between them, so the accessible
    name comes back empty. Humans read that layout fine; this rule encodes
    the same visual reasoning.
    """

    strategy: Literal[LocatorStrategy.LABEL_PROXIMITY] = (
        LocatorStrategy.LABEL_PROXIMITY
    )
    label: str
    direction: Literal["right", "below", "left", "above", "self"] = "right"
    role: str | None = None


class RegionPathSpec(_Spec):
    """Tier 3 -- a structural path, scoped to a named region.

    Scoping matters. An absolute path from the document root breaks when
    anything above shifts; a path anchored to a region we located
    semantically only breaks if that region's internals change.
    """

    strategy: Literal[LocatorStrategy.REGION_PATH] = LocatorStrategy.REGION_PATH
    #: Semantically-identified container the path is relative to.
    region: str
    #: e.g. "table[0]/tr[1]/td[2]/input[0]".
    path: str


class RowCellSpec(_Spec):
    """Tier 3 -- a table cell chosen by row content and column heading.

    The natural way to express "the balance on the savings row" without
    depending on savings being the second row this time.
    """

    strategy: Literal[LocatorStrategy.ROW_CELL] = LocatorStrategy.ROW_CELL
    #: Region or accessible name of the table.
    table: str
    #: Text identifying the row, matched against its cells.
    row_match: str
    #: Column heading, or a zero-based index when the table has no headings.
    column: str | int


class AnchorOffsetSpec(_Spec):
    """Tier 4 -- a pixel offset from a text anchor. Last resort.

    Recorded so that a flow through a canvas-rendered or image-mapped control
    is still replayable, but offsets encode where something sat once rather
    than what it is, so resolving here is always reported as degraded.
    """

    strategy: Literal[LocatorStrategy.ANCHOR_OFFSET] = (
        LocatorStrategy.ANCHOR_OFFSET
    )
    #: Visible text used as the origin point.
    anchor_text: str
    dx: int = 0
    dy: int = 0


LocatorSpec = Annotated[
    Union[
        RoleNameSpec,
        LabelProximitySpec,
        RegionPathSpec,
        RowCellSpec,
        AnchorOffsetSpec,
    ],
    Field(discriminator="strategy"),
]


class Locator(BaseModel):
    """An ordered chain of targeting rules for one control."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: LocatorSpec
    #: Tried in order if the primary misses. May be empty.
    fallbacks: tuple[LocatorSpec, ...] = ()
    #: Recorded at discovery time. Low confidence gates unattended replay.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Free-text note from discovery about why this control was chosen.
    note: str | None = None

    @property
    def chain(self) -> tuple[LocatorSpec, ...]:
        """Every rule to try, in resolution order."""
        return (self.primary, *self.fallbacks)

    def describe(self) -> str:
        """One-line human-readable form, for logs and failure messages."""
        spec = self.primary
        match spec:
            case RoleNameSpec():
                name = spec.name if spec.name is not None else f"~{spec.name_contains}"
                return f'{spec.role} "{name}"'
            case LabelProximitySpec():
                return f'control {spec.direction} of "{spec.label}"'
            case RegionPathSpec():
                return f"{spec.region}/{spec.path}"
            case RowCellSpec():
                return f'{spec.table}[row~"{spec.row_match}"].{spec.column}'
            case AnchorOffsetSpec():
                return f'offset ({spec.dx},{spec.dy}) from "{spec.anchor_text}"'
        return spec.strategy


class Resolution(BaseModel):
    """The outcome of resolving a locator against a live surface.

    ``tier`` is the payload that matters downstream: tier 0 means the
    preferred semantic rule still works, anything higher means we fell back
    and the capability is drifting.
    """

    model_config = ConfigDict(frozen=True)

    resolved: bool
    tier: int | None = None
    strategy: LocatorStrategy | None = None
    #: Opaque handle the surface driver uses to act. Never serialised.
    handle: object | None = Field(default=None, exclude=True, repr=False)
    #: How many nodes matched. >1 means the rule was ambiguous.
    match_count: int = 0
    detail: str | None = None

    @property
    def degraded(self) -> bool:
        """True when we resolved, but not on the preferred rule."""
        return self.resolved and (self.tier or 0) > 0
