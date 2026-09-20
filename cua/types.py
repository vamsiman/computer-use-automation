"""Core enumerations shared across every layer of the system.

These live in one place because they form the vocabulary that the artifact
schema, the surface drivers, the replay engine and the operator console all
have to agree on. If a value is not here, it is not part of the contract.
"""

from __future__ import annotations

from enum import StrEnum


class ActionType(StrEnum):
    """The complete vocabulary of things the system can do to a surface.

    Deliberately small. Discovery exposes exactly this set to the model, and
    replay can execute exactly this set -- so an artifact can never contain a
    step that replay does not know how to perform.
    """

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS_KEY = "press_key"
    EXTRACT = "extract"
    WAIT_FOR = "wait_for"

    # Terminal signals available to the discovery loop only. These are not
    # executable steps and never appear in a recorded artifact.
    DONE = "done"
    STUCK = "stuck"


#: Actions the discovery model may emit that are signals rather than steps.
TERMINAL_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.DONE, ActionType.STUCK}
)


class RiskLevel(StrEnum):
    """How dangerous a step is, independent of what the step actually does.

    Tagged per step in the artifact so the policy engine can make decisions
    without understanding banking or the flow it is guarding.
    """

    #: Read-only or trivially reversible. Runs unattended.
    SAFE = "safe"
    #: Mutates state but is undoable. Allowed unattended only if policy says so.
    CAUTION = "caution"
    #: Cannot be undone (submits a transaction, opens an account). Always gated.
    IRREVERSIBLE = "irreversible"


class Sensitivity(StrEnum):
    """Data classification, used to drive redaction declaratively.

    The evidence recorder reads this off the artifact's input/output
    declarations rather than pattern-matching values that look secret.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    #: Personally identifiable. Masked in logs and never written to artifacts.
    PII = "pii"
    #: Credentials and tokens. Never logged, never persisted, in any form.
    SECRET = "secret"


class LocatorStrategy(StrEnum):
    """How a control is identified, ordered most to least semantic.

    Replay walks a locator's chain in this order and records which tier
    actually resolved. A capability that drifts from ROLE_NAME toward
    ANCHOR_OFFSET is degrading before it breaks -- that is the drift signal.
    """

    #: Accessibility role + accessible name. Survives markup churn. Preferred.
    ROLE_NAME = "role_name"
    #: "the field immediately right of the label 'Member ID:'".
    LABEL_PROXIMITY = "label_proximity"
    #: Structural path scoped to a named region, for table-soup legacy markup.
    REGION_PATH = "region_path"
    #: A cell picked by matching its row and naming its column.
    ROW_CELL = "row_cell"
    #: Pixel offset from a text anchor. Last resort; always flagged low confidence.
    ANCHOR_OFFSET = "anchor_offset"


#: Resolution order. Index position doubles as the tier number reported in logs.
#: Most durable first. This is the order a synthesised chain is built in; a
#: stored locator is always walked in the order it was written down.
#:
#: ``row_cell`` sits above ``region_path`` deliberately. "The Balance on the
#: Savings row" survives a tenant that reorders its columns; "row 1, cell 2"
#: does not -- and it does not fail either, it quietly reads the wrong column
#: and returns a number that looks like an answer. A rule that can be
#: confidently wrong belongs below one that can only be right or absent.
LOCATOR_TIERS: tuple[LocatorStrategy, ...] = (
    LocatorStrategy.ROLE_NAME,
    LocatorStrategy.LABEL_PROXIMITY,
    LocatorStrategy.ROW_CELL,
    LocatorStrategy.REGION_PATH,
    LocatorStrategy.ANCHOR_OFFSET,
)


class ControlOwner(StrEnum):
    """Who currently holds the right to act on a live session.

    The brief requires "a way to know who is (or should be) in control".
    Exactly one owner at any instant; every act() asserts ownership first.
    """

    AUTOMATION = "automation"
    HUMAN = "human"
    #: Paused mid-handoff. Nobody may act until control is granted or resumed.
    NONE = "none"


class RunState(StrEnum):
    """Lifecycle of a single discovery or replay run."""

    PENDING = "pending"
    RUNNING = "running"
    #: Escalated. An intervention request is open and awaiting an operator.
    AWAITING_HUMAN = "awaiting_human"
    #: Operator granted control and is driving the live session by hand.
    HUMAN_CONTROL = "human_control"
    #: Control returned. Re-checking the world before trusting it again.
    VERIFYING = "verifying"

    # Terminal states.
    SUCCEEDED = "succeeded"
    #: Finished cleanly on a declared business outcome (e.g. no such member).
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"
    #: A human cancelled the run.
    ABORTED = "aborted"


TERMINAL_STATES: frozenset[RunState] = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.BUSINESS_OUTCOME,
        RunState.FAILED,
        RunState.ABORTED,
    }
)


class FailureCategory(StrEnum):
    """Why a run failed hard.

    Distinct from a business outcome, which is an expected answer, and from a
    recoverable condition, which never reaches the caller at all.
    """

    #: Every tier of the locator chain failed to resolve.
    LOCATOR_UNRESOLVED = "locator_unresolved"
    #: Acted, but the expected post-state never materialised.
    CHECKPOINT_FAILED = "checkpoint_failed"
    #: A declared recovery kept firing past its budget.
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    #: Blocked by the allowlist or by risk policy before acting.
    POLICY = "policy"
    #: Caller broke the capability's contract (missing or ill-typed input).
    CONTRACT = "contract"
    #: The capability was started from the wrong place. Distinct from a failed
    #: checkpoint: nothing went wrong during the run, the run should not have
    #: begun here.
    PRECONDITION = "precondition"
    #: The surface itself broke (browser crash, navigation error).
    SURFACE_ERROR = "surface_error"
    #: Wall-clock or step budget exhausted.
    TIMEOUT = "timeout"
