"""Record-once / replay-many computer-use automation.

An LLM discovers how to complete a goal against a live surface once; that run
is distilled into a typed, versioned capability artifact; the artifact then
replays deterministically with no model in the decision loop.
"""

from cua.locators import (
    AnchorOffsetSpec,
    LabelProximitySpec,
    Locator,
    RegionPathSpec,
    Resolution,
    RoleNameSpec,
    RowCellSpec,
)
from cua.primitives import A11yNode, Action, ActResult, Snapshot
from cua.types import (
    LOCATOR_TIERS,
    TERMINAL_ACTIONS,
    TERMINAL_STATES,
    ActionType,
    ControlOwner,
    FailureCategory,
    LocatorStrategy,
    RiskLevel,
    RunState,
    Sensitivity,
)

__version__ = "0.1.0"

__all__ = [
    "A11yNode",
    "ActResult",
    "Action",
    "ActionType",
    "AnchorOffsetSpec",
    "ControlOwner",
    "FailureCategory",
    "LOCATOR_TIERS",
    "LabelProximitySpec",
    "Locator",
    "LocatorStrategy",
    "RegionPathSpec",
    "Resolution",
    "RiskLevel",
    "RoleNameSpec",
    "RowCellSpec",
    "RunState",
    "Sensitivity",
    "Snapshot",
    "TERMINAL_ACTIONS",
    "TERMINAL_STATES",
    "__version__",
]
