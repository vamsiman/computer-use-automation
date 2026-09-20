"""Turning a run into a capability.

A trace is what happened. An artifact is what should happen next time, and the
gap between the two is the whole value of the record-once idea. Three passes
close it:

**Prune.** Drop the wrong turns. The rule is mechanical rather than a
judgement: an action that left the screen identical did nothing, whatever the
model believed at the time, and an action that was refused never happened at
all. What survives is the path that actually got somewhere.

**Parameterise.** The member number the run happened to use becomes
``{{ inputs.member_id }}``. Without this a capability is a recording of one
answer; with it, it is a function.

**Annotate.** Optional, and deliberately so -- see :mod:`cua.discovery.annotate`.
Every action already carries the intent the model wrote at the moment it
decided, so the artifact is complete and reviewable before any second model
call happens. Annotation improves the prose; it is not load-bearing.

Everything produced here is ``status: draft``. A capability that drives bank
software is not something you merge because the pipeline was green, and draft
is what makes human approval a step rather than a suggestion.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from cua.artifact.models import (
    AppRef,
    Artifact,
    CapabilityMeta,
    Checkpoint,
    InputSpec,
    OutputSpec,
    Preconditions,
    Provenance,
    Step,
    SuccessSpec,
)
from cua.discovery.trace import DiscoveryTrace, TraceRecord
from cua.locators import Locator, RoleNameSpec
from cua.policy import PolicyEngine, load_engine
from cua.types import ActionType, RiskLevel, Sensitivity

#: Words that carry no meaning in a capability name.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "the", "for", "of", "to", "in", "on", "at", "by",
        "look", "up", "read", "get", "find", "their", "this", "that", "with",
        "please", "current", "me", "my", "its", "from", "then", "number",
    }
)

#: Values shorter than this are not substituted into templates. Replacing "10"
#: everywhere it appears in a path would corrupt more than it parameterises.
MIN_PARAM_LENGTH = 3

DEFAULT_CHECKPOINT_MS = 8000


class NothingToDistil(ValueError):
    """The run did not get far enough to be worth recording.

    A distinct answer from a failed run: a trace that ended in ``stuck`` may
    still be useful evidence, but it is not a capability, and producing an
    artifact from it would be manufacturing a contract nobody can honour.
    """


# --- pass one: prune ------------------------------------------------------


def keep(record: TraceRecord) -> bool:
    """Is this action part of the flow, or part of the searching?

    The one subtlety worth stating: an ``extract`` changes nothing on screen
    and is kept anyway. Reading a value is the entire point of a read
    capability, and a pruning rule phrased purely as "did the screen change"
    would throw away exactly the steps the caller asked for. It is the sort of
    rule that looks right, passes its own test, and produces artifacts that
    navigate beautifully and return nothing.
    """
    if not record.ok or record.action is None:
        return False
    if record.action.type is ActionType.EXTRACT:
        return True
    return record.changed_the_screen


def prune(trace: DiscoveryTrace) -> list[TraceRecord]:
    return [record for record in trace.records if keep(record)]


# --- pass two: parameterise ----------------------------------------------


def _infer(value: Any) -> tuple[str, str | None]:
    """Type and pattern for a supplied input value.

    Digits stay a string rather than becoming an integer. A member number with
    a leading zero is not the same as the integer it parses to, and an
    identifier that arithmetic can be done on is an identifier waiting to be
    corrupted.
    """
    text = str(value)
    if text.isdigit():
        return "string", f"^[0-9]{{{len(text)}}}$"
    if re.fullmatch(r"-?[\d,]+\.\d{2}", text):
        return "money", None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "date", None
    return "string", None


def parameterise(value: Any, inputs: dict[str, Any]) -> Any:
    """Replace supplied input values with their template references."""
    if not isinstance(value, str):
        return value
    out = value
    # Longest first, so an input whose value contains another is replaced whole.
    for name, supplied in sorted(
        inputs.items(), key=lambda kv: len(str(kv[1])), reverse=True
    ):
        literal = str(supplied)
        if len(literal) >= MIN_PARAM_LENGTH and literal in out:
            out = out.replace(literal, f"{{{{ inputs.{name} }}}}")
    return out


def _input_specs(trace: DiscoveryTrace, used: set[str]) -> dict[str, InputSpec]:
    specs: dict[str, InputSpec] = {}
    for name, value in trace.inputs.items():
        if name not in used:
            # Supplied but never typed anywhere. Declaring it would put a
            # parameter in the signature that no step consumes, which is a lie
            # about the contract.
            continue
        kind, pattern = _infer(value)
        specs[name] = InputSpec(
            type=kind,
            description=f"Supplied as {value!r} during discovery.",
            required=True,
            pattern=pattern,
            sensitivity=Sensitivity.INTERNAL,
        )
    return specs


def _output_specs(records: Iterable[TraceRecord]) -> dict[str, OutputSpec]:
    """Outputs default to ``pii``, which is the safe direction to be wrong in.

    A value read out of a member's record is personal until somebody says
    otherwise. Over-redacting a branch code costs a reviewer one edit;
    under-redacting a name costs a disclosure, and the artifact is a draft
    awaiting review either way.
    """
    outputs: dict[str, OutputSpec] = {}
    for record in records:
        action = record.action
        if action is None or action.type is not ActionType.EXTRACT:
            continue
        name = str(action.args.get("into") or "value")
        transform = str(action.args.get("transform") or "trim")
        outputs[name] = OutputSpec(
            type="money" if transform == "money" else "string",
            description=f"Read from the screen during discovery ({record.intent}).",
            sensitivity=Sensitivity.PII,
            currency="USD" if transform == "money" else None,
        )
    return outputs


# --- checkpoints ----------------------------------------------------------


def _checkpoint_for(landmark: str | None) -> Checkpoint | None:
    """A landmark that changed becomes a thing to assert before continuing.

    This is what turns a recording into a replay: without it the engine clicks
    and hopes, and every later step fails somewhere confusing rather than here,
    where the intent says what was supposed to happen.
    """
    if not landmark or not landmark.startswith("heading:"):
        return None
    return Checkpoint(
        target=Locator(
            primary=RoleNameSpec(role="heading", name=landmark.split(":", 1)[1])
        ),
        expect="visible",
        timeout_ms=DEFAULT_CHECKPOINT_MS,
    )


# --- the whole thing ------------------------------------------------------


def slug_from_goal(goal: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-z]+", goal.lower())
        if word not in STOPWORDS and len(word) > 2
    ]
    slug = "_".join(words[:4]) or "capability"
    return f"discovered.{slug}"


def distil(
    trace: DiscoveryTrace,
    *,
    capability_id: str | None = None,
    version: str = "1.0.0",
    title: str | None = None,
    app: AppRef | None = None,
    tenant: str | None = None,
    entry_point: str | None = None,
    policy: PolicyEngine | None = None,
) -> Artifact:
    """Build a draft capability from one discovery run."""
    if not trace.succeeded:
        raise NothingToDistil(
            f"the run ended in {trace.terminal!r}, which is evidence but not a "
            "capability"
        )

    kept = prune(trace)
    if not kept:
        raise NothingToDistil("nothing in the run changed anything or read anything")

    policy = policy or load_engine()
    steps: list[Step] = []
    used_inputs: set[str] = set()
    previous_landmark = kept[0].landmark_before

    for index, record in enumerate(kept, start=1):
        action = record.action
        assert action is not None  # prune() guarantees it
        args = {
            key: parameterise(value, trace.inputs)
            for key, value in action.args.items()
        }
        used_inputs |= {
            name
            for name, value in trace.inputs.items()
            if any(
                f"inputs.{name}" in str(v) for v in args.values() if isinstance(v, str)
            )
        }

        checkpoint = None
        if record.landmark_after and record.landmark_after != previous_landmark:
            checkpoint = _checkpoint_for(record.landmark_after)
        previous_landmark = record.landmark_after or previous_landmark

        steps.append(
            Step(
                id=f"s{index}",
                intent=record.intent or f"{action.type} step {index}",
                action=action.type,
                target=action.target,
                args=args,
                # Inferred pessimistically by the same engine that will gate
                # the step at replay, so a reviewer is reading the number the
                # system will actually act on.
                risk=policy.infer_risk(action),
                checkpoint=checkpoint,
            )
        )

    outputs = _output_specs(kept)
    inputs = _input_specs(trace, used_inputs)

    success = SuccessSpec(
        checkpoint=_checkpoint_for(previous_landmark),
        require_outputs=sorted(outputs),
    )

    first_path = next(
        (
            str(record.action.args.get("path"))
            for record in kept
            if record.action is not None
            and record.action.type is ActionType.NAVIGATE
            and record.action.args.get("path")
        ),
        None,
    )

    return Artifact(
        capability=CapabilityMeta(
            id=capability_id or slug_from_goal(trace.goal),
            version=version,
            title=title or trace.goal[:80],
            description=(
                f"Discovered from the goal: {trace.goal}. "
                "Awaiting review -- this description was not written for a "
                "calling agent yet."
            ),
            app=app or AppRef(vendor="unknown", product="unknown"),
            tenant=tenant,
            # Never 'approved'. The whole point of distillation producing a
            # document is that a person reads it first.
            status="draft",
            provenance=Provenance(
                discovered_at=trace.started_at,
                model=trace.model,
                run_id=trace.run_id,
                step_count_raw=len(trace.records),
                step_count_final=len(steps),
            ),
        ),
        inputs=inputs,
        outputs=outputs,
        preconditions=Preconditions(
            authenticated=True,
            entry_point=parameterise(entry_point or first_path, trace.inputs),
            # Where the run was standing when it began. Discovery usually
            # starts somewhere convenient and never navigates there, so
            # without this the capability silently assumes a starting screen
            # it never recorded -- and fails at step one, blaming its first
            # locator, when it is replayed from anywhere else.
            entry_checkpoint=_checkpoint_for(kept[0].landmark_before),
        ),
        steps=steps,
        success=success,
    )


def summary(trace: DiscoveryTrace, artifact: Artifact) -> str:
    """One line a person can read after a discovery run."""
    raw = len(trace.records)
    final = len(artifact.steps)
    return (
        f"{artifact.capability.ref}: {raw} actions recorded, {final} kept, "
        f"{len(artifact.inputs)} input(s), {len(artifact.outputs)} output(s), "
        f"status {artifact.capability.status}"
    )
