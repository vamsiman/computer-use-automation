"""Checks the type system cannot express.

Pydantic guarantees the document is well formed. These rules guarantee it is
*coherent*: that it promises nothing it cannot deliver and asks for nothing it
did not declare.

The bias throughout is to fail loudly at load time. An artifact is loaded once
and replayed many times, often unattended, against systems that move money.
Every rule below describes something that would otherwise surface as a
confident wrong answer somewhere further downstream -- which, in this problem,
is the expensive kind of bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from cua.artifact.models import Artifact
from cua.types import ActionType, RiskLevel


@dataclass(frozen=True)
class Problem:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class InvalidArtifact(ValueError):
    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        joined = "\n  ".join(str(p) for p in problems)
        super().__init__(f"artifact is not coherent:\n  {joined}")


def validate(artifact: Artifact) -> list[Problem]:
    """Return every coherence problem. Empty means the artifact is sound."""
    problems: list[Problem] = []
    problems += _check_steps(artifact)
    problems += _check_inputs(artifact)
    problems += _check_outputs(artifact)
    problems += _check_outcomes(artifact)
    problems += _check_recoveries(artifact)
    problems += _check_tenancy(artifact)
    return problems


def validate_or_raise(artifact: Artifact) -> Artifact:
    problems = validate(artifact)
    if problems:
        raise InvalidArtifact(problems)
    return artifact


# --- steps ----------------------------------------------------------------


def _check_steps(artifact: Artifact) -> list[Problem]:
    problems: list[Problem] = []

    if not artifact.steps:
        problems.append(Problem("steps", "a capability with no steps does nothing"))

    seen: set[str] = set()
    for step in artifact.steps:
        where = f"steps.{step.id}"

        if step.id in seen:
            problems.append(Problem(where, "duplicate step id"))
        seen.add(step.id)

        if step.action in (ActionType.DONE, ActionType.STUCK):
            problems.append(
                Problem(
                    where,
                    f"{step.action} is a discovery signal, not a replayable step",
                )
            )

        needs_target = step.action in {
            ActionType.CLICK,
            ActionType.TYPE,
            ActionType.SELECT,
            ActionType.EXTRACT,
            ActionType.WAIT_FOR,
        }
        if needs_target and step.target is None:
            problems.append(Problem(where, f"{step.action} needs a target"))

        if step.action is ActionType.NAVIGATE and not (
            step.args.get("path") or step.args.get("url")
        ):
            problems.append(Problem(where, "navigate needs args.path or args.url"))

        if step.action is ActionType.TYPE and "value" not in step.args:
            problems.append(Problem(where, "type needs args.value"))

        if step.action is ActionType.EXTRACT and not step.args.get("into"):
            problems.append(
                Problem(where, "extract needs args.into naming a declared output")
            )

        if step.action is ActionType.SELECT and "value" not in step.args:
            problems.append(Problem(where, "select needs args.value"))

        # An irreversible step with no checkpoint cannot be verified after the
        # fact, and this is precisely the class of step where "did that
        # actually work?" has to have an answer.
        if step.risk is RiskLevel.IRREVERSIBLE and step.checkpoint is None:
            problems.append(
                Problem(where, "an irreversible step must declare a checkpoint")
            )

    return problems


# --- inputs ---------------------------------------------------------------


def _check_inputs(artifact: Artifact) -> list[Problem]:
    problems: list[Problem] = []
    declared = set(artifact.inputs)

    for step in artifact.steps:
        for name in sorted(step.input_refs()):
            if name not in declared:
                problems.append(
                    Problem(
                        f"steps.{step.id}",
                        f"references undeclared input {name!r}",
                    )
                )

    # An input nobody uses is usually a distillation bug -- a literal that was
    # parameterised but never substituted back in. Worth surfacing, but not
    # worth refusing to run over, so the caller decides.
    unused = declared - artifact.referenced_inputs()
    for name in sorted(unused):
        problems.append(Problem(f"inputs.{name}", "declared but never used"))

    return problems


# --- outputs --------------------------------------------------------------


def _check_outputs(artifact: Artifact) -> list[Problem]:
    problems: list[Problem] = []
    declared = set(artifact.outputs)
    produced = artifact.extracted_outputs()

    for name in sorted(declared - produced):
        problems.append(
            Problem(
                f"outputs.{name}",
                "promised to the caller but no step extracts it",
            )
        )

    for name in sorted(produced - declared):
        problems.append(
            Problem(
                "steps",
                f"a step extracts into {name!r}, which is not a declared output",
            )
        )

    for name in artifact.success.require_outputs:
        if name not in declared:
            problems.append(
                Problem(
                    "success.require_outputs",
                    f"{name!r} is not a declared output",
                )
            )

    return problems


# --- outcomes and recoveries ---------------------------------------------


def _check_outcomes(artifact: Artifact) -> list[Problem]:
    problems: list[Problem] = []
    seen: set[str] = set()
    for outcome in artifact.outcomes:
        if outcome.code in seen:
            problems.append(
                Problem(f"outcomes.{outcome.code}", "duplicate outcome code")
            )
        seen.add(outcome.code)
        if not outcome.code.isupper():
            problems.append(
                Problem(
                    f"outcomes.{outcome.code}",
                    "outcome codes are SHOUTING_CASE; callers branch on them",
                )
            )
    return problems


def _check_recoveries(artifact: Artifact) -> list[Problem]:
    problems: list[Problem] = []
    step_ids = {step.id for step in artifact.steps}
    outcome_codes = {outcome.code for outcome in artifact.outcomes}

    for recovery in artifact.recoveries:
        where = f"recoveries.{recovery.code}"

        # A condition cannot be both a final answer and something we quietly
        # handle. If it is declared twice the artifact does not know what it
        # means, and outcomes are evaluated first, so the recovery would be
        # dead code.
        if recovery.code in outcome_codes:
            problems.append(
                Problem(where, "also declared as an outcome; it cannot be both")
            )

        if recovery.strategy == "dismiss" and recovery.dismiss_via is None:
            problems.append(Problem(where, "dismiss needs dismiss_via"))

        if recovery.strategy == "reauthenticate_then_resume":
            if not recovery.resume_from:
                problems.append(
                    Problem(where, "reauthenticate_then_resume needs resume_from")
                )
            elif recovery.resume_from not in step_ids:
                problems.append(
                    Problem(
                        where,
                        f"resume_from {recovery.resume_from!r} is not a step id",
                    )
                )

    return problems


# --- tenancy --------------------------------------------------------------


def _check_tenancy(artifact: Artifact) -> list[Problem]:
    problems: list[Problem] = []
    meta = artifact.capability

    if meta.extends and not meta.tenant:
        problems.append(
            Problem(
                "capability.extends",
                "an override document must name the tenant it is for",
            )
        )

    if meta.extends and meta.extends.split("@")[0] == meta.id:
        return problems

    if meta.extends:
        problems.append(
            Problem(
                "capability.extends",
                f"overrides {meta.extends} but has a different id {meta.id!r}; "
                "a tenant variant is the same capability, specialised",
            )
        )

    return problems
