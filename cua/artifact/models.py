"""The capability artifact: what a discovery run distils into.

This is the contract the whole system turns on, so it is worth being explicit
about what it is *not*. It is not a transcript of what the model did, and it is
not a cached answer. It is a parameterised description of a flow: the ordered
steps, how each control is identified, the typed inputs a caller supplies, the
typed outputs they get back, and the conditions that mean something other than
success happened.

Two properties drove every decision here.

**It has to be reviewable by a person.** A capability that opens accounts in a
bank is not something you merge because the tests pass. So every step carries
an ``intent`` in plain language, outcomes and recoveries are declared in the
document rather than buried in engine code, and the whole thing is YAML in git
where it shows up as a diff.

**It has to be callable by an agent.** The calling side needs a signature, not
a step list -- what it must supply, what it gets back, and what "no such
member" looks like as a result rather than as a crash.

Those two audiences want the same document, which is the useful constraint.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cua.locators import Locator
from cua.types import ActionType, RiskLevel, Sensitivity

SCHEMA_VERSION = "1.0"

CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
EXTENDS_RE = re.compile(r"^[a-z][a-z0-9_.]*@\d+\.\d+\.\d+$")
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: ``{{ inputs.member_id }}`` inside a step argument.
INPUT_REF_RE = re.compile(r"\{\{\s*inputs\.([a-z][a-z0-9_]*)\s*\}\}")

#: Actions that cannot run without something to act on.
NEEDS_TARGET = frozenset(
    {
        ActionType.CLICK,
        ActionType.TYPE,
        ActionType.SELECT,
        ActionType.EXTRACT,
        ActionType.WAIT_FOR,
    }
)


ParamKind = Literal["string", "integer", "number", "boolean", "money", "date"]

#: Post-processing applied to an extracted string before it is returned.
TransformKind = Literal["none", "money", "integer", "trim"]

ArtifactStatus = Literal["draft", "approved"]

RecoveryStrategy = Literal[
    "dismiss", "retry_with_backoff", "reauthenticate_then_resume"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- the agent-facing contract -------------------------------------------


class InputSpec(_Model):
    """One typed parameter the caller supplies per invocation."""

    type: ParamKind
    description: str = ""
    required: bool = True
    #: Validated before the browser is even opened.
    pattern: str | None = None
    enum: list[str] | None = None
    #: Drives redaction. Declared here rather than guessed from the value,
    #: because guessing what looks sensitive is how leaks happen.
    sensitivity: Sensitivity = Sensitivity.INTERNAL

    @field_validator("pattern")
    @classmethod
    def _compilable(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    def problems(self, name: str, value: Any) -> list[str]:
        """Why this value is not acceptable for this parameter.

        Lives on the spec so there is exactly one answer to the question. A
        caller's arguments are checked by it before a browser is opened, and
        an operator typing a value into the console is checked by the same
        code -- otherwise the console would be a second, laxer front door into
        the same capability, which is the sort of asymmetry nobody notices
        until it matters.
        """
        if value is None or value == "":
            return [f"required input {name!r} was not supplied"] if self.required else []

        text = str(value)
        found = []
        if self.pattern and not re.fullmatch(self.pattern, text):
            found.append(f"input {name!r} = {text!r} does not match {self.pattern}")
        if self.enum and text not in self.enum:
            found.append(f"input {name!r} = {text!r} is not one of {self.enum}")
        return found


class OutputSpec(_Model):
    """One typed value the caller gets back."""

    type: ParamKind
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    currency: str | None = None


class AppRef(_Model):
    """Which application this capability was recorded against.

    ``version_range`` exists so an artifact can say what it was built for.
    Hundreds of tenants run the same vendor product at different versions, and
    a capability recorded on 4.2 quietly failing on 5.0 is worse than one that
    refuses to run.
    """

    vendor: str
    product: str
    version_range: str = "*"


class Provenance(_Model):
    """Where this artifact came from.

    ``step_count_raw`` against ``step_count_final`` is the visible evidence
    that the artifact is a distillation and not a transcript: the model
    wandered for nineteen actions, six of them survived.
    """

    discovered_at: datetime | None = None
    model: str | None = None
    run_id: str | None = None
    step_count_raw: int | None = None
    step_count_final: int | None = None
    #: Who approved it, if anyone has.
    approved_by: str | None = None
    approved_at: datetime | None = None


class CapabilityMeta(_Model):
    id: str
    version: str
    title: str
    #: Written for the calling agent, not for a human reading the file. This
    #: is what an agent sees when deciding whether this capability does what
    #: it needs.
    description: str = ""
    app: AppRef
    #: None means this is a base artifact usable by any tenant on this product.
    tenant: str | None = None
    #: ``capability.id@1.2.0`` -- set only on a tenant override document.
    extends: str | None = None
    #: Unattended replay is gated on this. A freshly distilled artifact is a
    #: draft until a person has read it.
    status: ArtifactStatus = "draft"
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("id")
    @classmethod
    def _dotted_lowercase(cls, value: str) -> str:
        if not CAPABILITY_ID_RE.match(value):
            raise ValueError(
                f"capability id {value!r} must be dotted lowercase, e.g. "
                "'member.read_savings_balance'"
            )
        return value

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not SEMVER_RE.match(value):
            raise ValueError(f"version {value!r} must be semver, e.g. '1.0.0'")
        return value

    @field_validator("extends")
    @classmethod
    def _extends_pins_a_version(cls, value: str | None) -> str | None:
        if value is not None and not EXTENDS_RE.match(value):
            raise ValueError(
                f"extends {value!r} must pin a version, e.g. 'member.read@1.0.0'"
            )
        return value

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


# --- the flow -------------------------------------------------------------


class Checkpoint(_Model):
    """A condition asserted to confirm we actually reached a state.

    The brief's glossary puts this well: a checkpoint is what stops you
    assuming the click worked. It is also where waiting happens, which is why
    the timeout lives here rather than in the driver -- how long a step is
    allowed to take is a property of the flow, not of the browser.
    """

    target: Locator
    expect: Literal["visible", "absent"] = "visible"
    timeout_ms: int = Field(default=5000, ge=0, le=120_000)


class Step(_Model):
    id: str
    #: Plain language, and not optional. It costs one line and buys three
    #: things: a reviewer can audit the flow without decoding locators, a
    #: failure can say "while entering the member ID" instead of "at s2", and
    #: any future bounded recovery has something to ground itself on.
    intent: str
    action: ActionType
    target: Locator | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    #: Evaluated by the policy engine, which never has to understand the flow.
    risk: RiskLevel = RiskLevel.SAFE
    checkpoint: Checkpoint | None = None

    @field_validator("id")
    @classmethod
    def _step_id(cls, value: str) -> str:
        if not STEP_ID_RE.match(value):
            raise ValueError(f"step id {value!r} must be lowercase, e.g. 's2'")
        return value

    @field_validator("intent")
    @classmethod
    def _intent_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("every step needs an intent a reviewer can read")
        return value

    def input_refs(self) -> set[str]:
        """Input names this step interpolates."""
        return set(iter_input_refs(self.args))


class SuccessSpec(_Model):
    """What it means for this capability to have worked."""

    checkpoint: Checkpoint | None = None
    #: Outputs that must be present for the run to count as successful. A
    #: capability that reaches the right screen but comes back without the
    #: number it promised has not succeeded.
    require_outputs: list[str] = Field(default_factory=list)


class Outcome(_Model):
    """A declared business result that is not success and not a failure.

    "No such member" belongs here. The brief calls conflating this with a
    crash the most common design mistake in the problem, so the distinction is
    made in the document rather than in code: each capability declares the
    answers it knows how to give, with a stable code the caller can branch on.
    """

    code: str
    detect: Locator
    #: False would mean "note it and carry on"; in practice these stop the run.
    terminal: bool = True
    message: str | None = None


class Recovery(_Model):
    """A known condition the replay engine handles and keeps going.

    The caller never hears about these -- they show up in the evidence log and
    nowhere else. That is the point: dismissing a maintenance notice is not
    news.
    """

    code: str
    detect: Locator
    strategy: RecoveryStrategy
    #: Required, with no default. A recovery without a budget is an infinite
    #: loop waiting for a bad day, and the sensible number depends entirely on
    #: the condition, so there is no honest default to pick.
    max_occurrences: int = Field(ge=1)
    #: For ``dismiss``.
    dismiss_via: Locator | None = None
    #: For ``reauthenticate_then_resume`` -- the step id to resume from.
    resume_from: str | None = None
    #: For ``retry_with_backoff``.
    backoff_ms: int = Field(default=500, ge=0)


class Preconditions(_Model):
    """What must already be true before step one.

    ``authenticated`` is the load-bearing one, and it is a precondition rather
    than a set of recorded steps on purpose. Signing in is a separate
    bootstrap that reads credentials from the environment, so credentials are
    never an artifact input and are therefore *structurally incapable* of
    ending up in a saved artifact. That is a stronger guarantee than redacting
    them well.
    """

    authenticated: bool = True
    #: A path to start from, when the application has one worth naming.
    entry_point: str | None = None
    #: The screen the capability expects to be looking at, asserted before the
    #: first step.
    #:
    #: A path cannot carry this on its own. In a frameset application the
    #: address bar reads the same on every screen, so the URL says nothing
    #: about where the run actually is -- and a capability whose entry
    #: condition is a URL would be checking something the application does not
    #: bother to change. The screen is named the way everything else here
    #: names screens: by what is on it.
    entry_checkpoint: Checkpoint | None = None


# --- the document ---------------------------------------------------------


class Artifact(_Model):
    """A versioned, reviewable, agent-invocable capability."""

    schema_version: str = SCHEMA_VERSION
    capability: CapabilityMeta
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    preconditions: Preconditions = Field(default_factory=Preconditions)
    steps: list[Step] = Field(default_factory=list)
    success: SuccessSpec = Field(default_factory=SuccessSpec)
    outcomes: list[Outcome] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)

    # --- convenience used by the store, catalog and replay engine ---

    @property
    def ref(self) -> str:
        return self.capability.ref

    @property
    def is_approved(self) -> bool:
        return self.capability.status == "approved"

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def extracted_outputs(self) -> set[str]:
        """Output names some step actually produces."""
        return {
            str(step.args["into"])
            for step in self.steps
            if step.action is ActionType.EXTRACT and step.args.get("into")
        }

    def referenced_inputs(self) -> set[str]:
        refs: set[str] = set()
        for step in self.steps:
            refs |= step.input_refs()
        return refs

    def riskiest(self) -> RiskLevel:
        order = [RiskLevel.SAFE, RiskLevel.CAUTION, RiskLevel.IRREVERSIBLE]
        return max((s.risk for s in self.steps), key=order.index, default=RiskLevel.SAFE)

    def signature(self) -> str:
        """One-line typed signature, for the capability catalogue.

        This is the form an agent would see when discovering what it can call.
        """
        args = ", ".join(
            f"{name}: {spec.type}" + ("" if spec.required else " = None")
            for name, spec in self.inputs.items()
        )
        returns = ", ".join(f"{n}: {s.type}" for n, s in self.outputs.items())
        return f"{self.capability.id}({args}) -> {{{returns}}}"

    def sensitivity_of(self, name: str) -> Sensitivity:
        """Classification for a declared input or output, for redaction."""
        if name in self.inputs:
            return self.inputs[name].sensitivity
        if name in self.outputs:
            return self.outputs[name].sensitivity
        return Sensitivity.INTERNAL


# --- parameter interpolation ---------------------------------------------


def iter_input_refs(value: Any) -> Iterator[str]:
    """Every ``{{ inputs.x }}`` name appearing anywhere inside a value."""
    if isinstance(value, str):
        yield from INPUT_REF_RE.findall(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_input_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_input_refs(item)


class MissingInput(KeyError):
    """A template referenced an input the caller did not supply.

    Deliberately an error rather than a blank. Substituting an empty string
    for a member id would search for nothing and quite possibly succeed at it;
    inventing a value in a system that writes to bank records is the failure
    mode worth engineering against.
    """


def render(value: Any, inputs: dict[str, Any]) -> Any:
    """Substitute supplied inputs into a step argument."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in inputs or inputs[name] is None:
                raise MissingInput(name)
            return str(inputs[name])

        return INPUT_REF_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render(v, inputs) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [render(v, inputs) for v in value]
    return value
