"""The seam between perceiving a surface and the recorded flow.

This is the abstraction the brief's heterogeneity question turns on. Discovery
and replay are written against this protocol and nothing else -- neither of
them imports Playwright, and neither of them knows a browser exists. A desktop
driver built on UI Automation is a third implementation of three methods, and
nothing above this line changes.

That is only true because of what crosses the boundary: an accessibility tree
and a locator chain, both of which mean the same thing on a web page and on a
native window. Had we chosen CSS selectors or pixel coordinates as the
currency, the seam would be decorative.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cua.locators import Locator, Resolution
from cua.primitives import Action, ActResult, Snapshot


@runtime_checkable
class Surface(Protocol):
    """Something an agent can observe and act upon."""

    def observe(self) -> Snapshot:
        """Capture the current state as an accessibility tree.

        Implementations should return a tree that is stable enough to hash:
        discovery uses successive snapshot hashes to notice that an action
        changed nothing, which is how a stuck loop is detected.
        """
        ...

    def resolve(self, locator: Locator) -> Resolution:
        """Find the control a locator describes.

        Must walk the locator's chain in order and report which tier
        succeeded. The tier is not diagnostic detail -- it is the signal that
        a capability is drifting, so it belongs in the return value rather
        than in a log line.
        """
        ...

    def act(self, action: Action) -> ActResult:
        """Perform one action. Never decides anything; just does as told."""
        ...

    def screenshot(self, path: str) -> str:
        """Capture evidence. Returns the path written."""
        ...

    def close(self) -> None:
        ...


class SurfaceError(RuntimeError):
    """The surface itself broke: a crashed browser, a failed navigation.

    Distinct from a locator that did not resolve or a checkpoint that did not
    hold, both of which are ordinary outcomes of driving a live application.
    """
