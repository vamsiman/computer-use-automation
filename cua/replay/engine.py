"""Running a capability with nothing in the loop that can improvise.

This is the production path, and its defining property is what it does *not*
import. No model, no client, no prompt. Determinism here is structural rather
than a discipline somebody has to maintain: there is nothing available to this
module that could make a different choice on a Tuesday.

The order of operations per step is the design, so it is worth stating plainly:

1. the contract is checked **once, before anything is touched**
2. policy is asked, **before** the action rather than after
3. the locator is resolved, and **which rule won is recorded**
4. the action is performed
5. declared **outcomes** are read off the result -- answers stop the run
6. the checkpoint is verified, which is what makes this a replay and not a
   re-enactment
7. only if the checkpoint does not hold are **recoveries** consulted

Step 7 is the subtle one. Recoveries describe things that have gone slightly
wrong, so looking for them on a healthy screen is how a detector meant for a
stalled page fires on a page that loaded perfectly and spends its budget doing
nothing. Outcomes are different: "no records found" renders on a page that is
working exactly as designed, so those are read every time.

And when nothing declared matches and something is blocking the screen, this
module does not decide. It escalates. Guessing at an unknown dialog is how an
automation clicks "Acknowledge" on a compliance hold.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from cua.artifact.models import (
    Artifact,
    Checkpoint,
    MissingInput,
    Recovery,
    Step,
    render,
)
from cua.evidence import Recorder, StepRecord
from cua.policy import Mode, PolicyContext, PolicyEngine, RequireApproval, load_engine
from cua.primitives import Action, Snapshot
from cua.replay.conditions import ConditionEvaluator, blocking
from cua.replay.result import (
    BusinessOutcome,
    Escalated,
    Failure,
    Result,
    Success,
    TierEntry,
)
from cua.surface.base import Surface, SurfaceError
from cua.types import ActionType, FailureCategory

from contextlib import nullcontext

#: How many actions a run may take before we assume the recoveries are feeding
#: each other. Per-code budgets stop one condition looping; this stops two of
#: them taking turns.
ACTION_MULTIPLIER = 4
ACTION_FLOOR = 10

POLL_MS = 150


class ContractError(ValueError):
    """The caller did not supply what the capability's signature asks for."""


def validate_inputs(artifact: Artifact, inputs: dict[str, Any]) -> list[str]:
    """Check the call against the signature. Nothing is corrected, ever.

    A missing member number is not defaulted, and an ill-typed one is not
    coerced -- both would mean acting on a value nobody supplied, in an
    application that writes to member records. This is also the cheapest check
    in the system, so it runs before a browser is opened rather than after four
    steps of real work.
    """
    problems: list[str] = []

    for name, spec in artifact.inputs.items():
        problems.extend(spec.problems(name, inputs.get(name)))

    unknown = set(inputs) - set(artifact.inputs)
    for name in sorted(unknown):
        # Rejected rather than ignored: an unrecognised name is far more often
        # a misspelling of a real parameter than a harmless extra, and
        # silently dropping it would run the capability without the value the
        # caller believed they had supplied.
        problems.append(
            f"unknown input {name!r}; this capability takes "
            f"{sorted(artifact.inputs) or 'no inputs'}"
        )
    return problems


def apply_transform(value: str | None, kind: str) -> Any:
    """Normalise a scraped string into the type the contract promises.

    A caller that receives ``'$4,210.33'`` has to parse it, and every caller
    will parse it slightly differently. Doing it here once is the difference
    between a typed output and a screen-scrape with good manners.
    """
    if value is None:
        return None
    text = value.strip()
    if kind == "money":
        cleaned = re.sub(r"[^\d.\-]", "", text)
        return cleaned or None
    if kind == "integer":
        digits = re.sub(r"[^\d\-]", "", text)
        return int(digits) if digits else None
    if kind == "none":
        return value
    return text


@dataclass
class _StepOutcome:
    """Internal: what the engine should do next."""

    result: Result | None = None
    goto: int | None = None
    advance: bool = True


#: Called when something undeclared blocks the run. Returns True if a person
#: dealt with it and the engine should re-verify and continue. Without one,
#: the run stops and says so.
#:
#: A handler may also offer ``resolved(verified: bool)``, which the engine
#: calls with the result of re-checking the step afterwards. That is how the
#: live handoff learns whether to put its session back into RUNNING or ask for
#: a person again: the engine knows whether the checkpoint held, and is the
#: only thing that can know, but it has no business knowing a session exists.
#: Optional, so a plain function remains a perfectly good handler.
EscalationHandler = Callable[[Step, str, Snapshot], bool]

#: Called by the SESSION_EXPIRED recovery. Kept as a callable so replay never
#: imports the auth module, and so credentials stay out of this file entirely.
Reauthenticator = Callable[[], None]


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        artifact: Artifact,
        *,
        policy: PolicyEngine | None = None,
        recorder: Recorder | None = None,
        reauthenticate: Reauthenticator | None = None,
        escalate: EscalationHandler | None = None,
        approve: Callable[[Step], bool] | None = None,
        allow_draft: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.surface = surface
        self.artifact = artifact
        self.policy = policy or load_engine()
        self.recorder = recorder
        self.reauthenticate = reauthenticate
        self.escalate = escalate
        self.approve = approve
        self.allow_draft = allow_draft
        self.session_id = session_id

    # --- the run ---------------------------------------------------------

    def run(self, inputs: dict[str, Any] | None = None) -> Result:
        inputs = dict(inputs or {})
        began = time.perf_counter()
        self.outputs: dict[str, Any] = {}
        self.tier_log: list[TierEntry] = []
        self.conditions = ConditionEvaluator(self.artifact)
        self.steps_executed = 0

        contract = self._check_contract(inputs)
        if contract is not None:
            return self._finish(contract, began)

        entry = self._check_entry()
        if entry is not None:
            return self._finish(entry, began)

        index = 0
        budget = len(self.artifact.steps) * ACTION_MULTIPLIER + ACTION_FLOOR

        while index < len(self.artifact.steps):
            if self.steps_executed >= budget:
                return self._finish(
                    self._failure(
                        self.artifact.steps[index],
                        FailureCategory.RECOVERY_EXHAUSTED,
                        expected="a run that makes progress",
                        observed=f"{self.steps_executed} actions without finishing",
                    ),
                    began,
                )

            step = self.artifact.steps[index]
            try:
                outcome = self._run_step(step, inputs)
            except SurfaceError as exc:
                return self._finish(
                    self._failure(
                        step,
                        FailureCategory.SURFACE_ERROR,
                        expected="a working surface",
                        observed=str(exc),
                    ),
                    began,
                )

            if outcome.result is not None:
                return self._finish(outcome.result, began)
            if outcome.goto is not None:
                index = outcome.goto
                continue
            if outcome.advance:
                index += 1

        return self._finish(self._succeed(), began)

    # --- one step --------------------------------------------------------

    def _run_step(self, step: Step, inputs: dict[str, Any]) -> _StepOutcome:
        self.steps_executed += 1

        try:
            args = render(step.args, inputs)
        except MissingInput as exc:
            return _StepOutcome(
                result=self._failure(
                    step,
                    FailureCategory.CONTRACT,
                    expected=f"a value for input {exc.args[0]!r}",
                    observed="nothing was supplied",
                )
            )

        action = Action(
            type=step.action, target=step.target, args=args, intent=step.intent
        )

        where = self.surface.observe()
        decision = self.policy.check(
            action,
            PolicyContext(
                location=where.location,
                risk=step.risk,
                mode=Mode.REPLAY,
                step_id=step.id,
            ),
        )
        if self.recorder:
            self.recorder.policy(
                decision,
                action=str(step.action),
                target=step.target.describe() if step.target else None,
            )

        if not decision.permitted:
            if isinstance(decision, RequireApproval):
                if self.approve is None or not self.approve(step):
                    # Not permitted *yet*. The run is paused for a person, not
                    # broken -- which is why this is an escalation and not a
                    # policy failure.
                    return _StepOutcome(
                        result=self._pause_for_approval(step, decision, where)
                    )
            else:
                return _StepOutcome(
                    result=self._failure(
                        step,
                        FailureCategory.POLICY,
                        expected="an action policy permits",
                        observed=decision.reason,
                        detail=decision.rule,
                    )
                )

        # --- resolve, and record which rule won --------------------------
        #
        # Resolved here rather than read back off the act, because the tier is
        # not a detail of how the click went: it is the drift signal, and it
        # has to be recorded whether the step then succeeds or not. It also
        # means an unresolvable target is diagnosed as exactly that instead of
        # arriving three lines later as a generic failure.
        if step.target is not None:
            resolution = self.surface.resolve(step.target)
            self.tier_log.append(
                TierEntry(
                    step_id=step.id,
                    intent=step.intent,
                    strategy=resolution.strategy.value if resolution.strategy else None,
                    tier=resolution.tier,
                    degraded=resolution.degraded,
                    match_count=resolution.match_count,
                )
            )
            if not resolution.resolved:
                return self._handle_trouble(
                    step,
                    self.surface.observe(),
                    f"unresolved: {step.target.describe()}",
                )

        # --- act ---------------------------------------------------------
        with self._evidence(step) as evidence:
            result = self.surface.act(action)
            evidence.ok = result.ok
            evidence.tier = result.tier
            evidence.error = result.error
            evidence.policy_rule = decision.rule

            if result.ok and step.action is ActionType.EXTRACT:
                name = str(args.get("into") or "value")
                value = apply_transform(
                    result.value, str(args.get("transform") or "trim")
                )
                self.outputs[name] = value
                evidence.extracted = {name: value}

            if self.recorder:
                evidence.screenshot = self.recorder.screenshot(self.surface)

        after = result.observed or self.surface.observe()

        # --- answers are read every time ---------------------------------
        detected = self.conditions.evaluate(after)
        if detected.outcome is not None:
            return _StepOutcome(result=self._business(detected.outcome, step))

        # The checkpoint is the authority on whether the step worked, not the
        # driver's return value. A slow legacy render makes the browser report
        # a timeout for a click that went through perfectly well, and trusting
        # the driver over the world turns a run that succeeded into a failure
        # report. Where there is no checkpoint the driver's word is all we
        # have -- which is itself the argument for declaring one.
        if step.checkpoint is not None:
            if self._verify(step.checkpoint):
                return _StepOutcome()
        elif result.ok:
            return _StepOutcome()

        # --- only now, when something is wrong ---------------------------
        return self._handle_trouble(step, after, result.error)

    def _handle_trouble(
        self, step: Step, snapshot: Snapshot, error: str | None
    ) -> _StepOutcome:
        detected = self.conditions.evaluate(snapshot)

        if detected.recovery is not None:
            recovery = detected.recovery
            count = self.conditions.record(recovery.code)
            if not self.conditions.within_budget(recovery):
                return _StepOutcome(
                    result=self._failure(
                        step,
                        FailureCategory.RECOVERY_EXHAUSTED,
                        expected=f"{recovery.code} at most "
                        f"{recovery.max_occurrences} time(s)",
                        observed=f"it occurred {count} times",
                        detail=recovery.code,
                    )
                )
            if self.recorder:
                self.recorder.recovery(
                    recovery.code, recovery.strategy, count, recovery.max_occurrences
                )
            return self._apply_recovery(recovery, step, count)

        obstruction = blocking(snapshot)
        if obstruction:
            # Something is standing in the way that nobody declared. This is
            # the compliance-hold case, and the correct response is to stop and
            # ask -- an automation that reasons its way through an unknown
            # dialog is an automation that will one day reason wrongly.
            return _StepOutcome(
                result=self._escalated(step, "an undeclared dialog is blocking the "
                                       "screen", snapshot, observed=obstruction)
            )

        category = (
            FailureCategory.LOCATOR_UNRESOLVED
            if error and "unresolved" in error
            else FailureCategory.CHECKPOINT_FAILED
        )
        return _StepOutcome(
            result=self._failure(
                step,
                category,
                expected=self._expectation(step),
                observed=error or self._describe(snapshot),
            )
        )

    # --- recoveries ------------------------------------------------------

    def _apply_recovery(
        self, recovery: Recovery, step: Step, count: int
    ) -> _StepOutcome:
        if recovery.strategy == "dismiss":
            if recovery.dismiss_via is None:
                return _StepOutcome(
                    result=self._failure(
                        step,
                        FailureCategory.RECOVERY_EXHAUSTED,
                        expected=f"{recovery.code} to declare how to dismiss it",
                        observed="no dismiss_via on the recovery",
                    )
                )
            self.surface.act(
                Action(type=ActionType.CLICK, target=recovery.dismiss_via)
            )
        elif recovery.strategy == "retry_with_backoff":
            time.sleep(recovery.backoff_ms * count / 1000)
        elif recovery.strategy == "reauthenticate_then_resume":
            if self.reauthenticate is None:
                return _StepOutcome(
                    result=self._escalated(
                        step,
                        "the session expired and no reauthentication is configured",
                        self.surface.observe(),
                    )
                )
            self.reauthenticate()
            target = recovery.resume_from or self.artifact.steps[0].id
            index = next(
                (i for i, s in enumerate(self.artifact.steps) if s.id == target), 0
            )
            return _StepOutcome(goto=index)

        # Re-verify rather than assume: the recovery did something to the
        # world, and whether it worked is a question about the world.
        if self._verify(step.checkpoint):
            return _StepOutcome()
        return _StepOutcome(advance=False)

    # --- preconditions ---------------------------------------------------

    def _check_entry(self) -> Result | None:
        """Are we looking at the screen this capability starts from?

        Cheap, and it turns the most confusing failure mode there is into a
        sentence. Started from the wrong screen, the first locator does not
        resolve and the run reports that the Member ID field is missing --
        which sends somebody looking for a broken locator when the locator is
        fine and the browser is simply somewhere else.

        Only checked when the artifact declares it. An artifact that begins
        with a navigate does not need one, because its first step puts the
        browser where it belongs.
        """
        checkpoint = self.artifact.preconditions.entry_checkpoint
        if checkpoint is None or self._verify(checkpoint):
            return None

        expected = checkpoint.target.describe()
        return Failure(
            capability=self.artifact.ref,
            category=FailureCategory.PRECONDITION,
            step_id=None,
            intent="start from the screen this capability expects",
            expected=expected,
            observed=self._describe(self.surface.observe()),
            detail=(
                "the capability did not run. Put the session on the expected "
                "screen first, or add a navigate step to the artifact."
            ),
        )

    # --- checkpoints -----------------------------------------------------

    def _verify(self, checkpoint: Checkpoint | None) -> bool:
        """Did the state we expected actually arrive?

        The difference between a replay and a re-enactment. Without this the
        engine clicks and hopes, and the eventual failure lands somewhere that
        cannot explain itself.
        """
        if checkpoint is None:
            return True

        deadline = time.monotonic() + checkpoint.timeout_ms / 1000
        while True:
            resolution = self.surface.resolve(checkpoint.target)
            found = resolution.resolved
            if found == (checkpoint.expect == "visible"):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_MS / 1000)

    # --- results ---------------------------------------------------------

    def _check_contract(self, inputs: dict[str, Any]) -> Result | None:
        if not self.artifact.is_approved and not self.allow_draft:
            return Failure(
                capability=self.artifact.ref,
                category=FailureCategory.POLICY,
                expected="an approved capability",
                observed=f"status is {self.artifact.capability.status!r}",
                detail=(
                    "a draft has not been read by a person yet; pass "
                    "allow_draft to run it anyway"
                ),
            )
        problems = validate_inputs(self.artifact, inputs)
        if problems and self._ask_for_missing(inputs, problems):
            problems = validate_inputs(self.artifact, inputs)
        if problems:
            return Failure(
                capability=self.artifact.ref,
                category=FailureCategory.CONTRACT,
                expected=self.artifact.signature(),
                observed="; ".join(problems),
            )
        return None

    def _ask_for_missing(
        self, inputs: dict[str, Any], problems: list[str]
    ) -> bool:
        """A value nobody supplied is a question, not a fault.

        Only when every problem is a *missing* required input, and only when
        the escalation handler can ask somebody. An ill-typed value is a
        different thing -- the caller believes they supplied it and they are
        wrong, and asking a person to retype it hides a bug in whatever
        called us.

        The value comes back through the capability's own ``InputSpec``, so
        the answer is checked exactly as an API caller's would be. Nothing
        here defaults, coerces or guesses; the alternative to asking is
        stopping, and inventing a member number in an application that writes
        to member records is not on the list.
        """
        ask = getattr(self.escalate, "ask", None)
        if not callable(ask):
            return False

        missing = [
            name
            for name, spec in self.artifact.inputs.items()
            if spec.required and inputs.get(name) in (None, "")
        ]
        if not missing or len(missing) != len(problems):
            return False

        supplied = ask({name: self.artifact.inputs[name] for name in missing})
        if not supplied:
            return False

        inputs.update(supplied)
        if self.recorder:
            self.recorder.event("inputs_supplied_by_operator", names=sorted(supplied))
        notify = getattr(self.escalate, "resolved", None)
        if callable(notify):
            notify(True)
        return True

    def _succeed(self) -> Result:
        if not self._verify(self.artifact.success.checkpoint):
            return Failure(
                capability=self.artifact.ref,
                category=FailureCategory.CHECKPOINT_FAILED,
                expected="the capability's success condition",
                observed=self._describe(self.surface.observe()),
            )
        missing = [
            name
            for name in self.artifact.success.require_outputs
            if self.outputs.get(name) in (None, "")
        ]
        if missing:
            # Reaching the right screen and coming back without the number is
            # not success, however clean the run looked.
            return Failure(
                capability=self.artifact.ref,
                category=FailureCategory.CHECKPOINT_FAILED,
                expected=f"outputs {missing}",
                observed=f"got {sorted(self.outputs)}",
            )
        return Success(capability=self.artifact.ref, outputs=dict(self.outputs))

    def _business(self, outcome, step: Step) -> Result:
        if self.recorder:
            self.recorder.outcome(outcome.code, outcome.message)
        return BusinessOutcome(
            capability=self.artifact.ref,
            code=outcome.code,
            message=outcome.message,
            partial_outputs=dict(self.outputs),
            step_id=step.id,
        )

    def _pause_for_approval(self, step: Step, decision, snapshot: Snapshot) -> Result:
        """A risky step nobody has agreed to. Paused, not failed."""
        if self.recorder:
            self.recorder.event(
                "awaiting_approval", step_id=step.id, reason=decision.reason
            )
        return Escalated(
            capability=self.artifact.ref,
            step_id=step.id,
            intent=step.intent,
            reason=decision.reason,
            observed=self._describe(snapshot),
            session_id=self.session_id,
            partial_outputs=dict(self.outputs),
        )

    def _escalated(
        self,
        step: Step,
        reason: str,
        snapshot: Snapshot,
        *,
        observed: str | None = None,
    ) -> Result | None:
        if self.escalate is not None and self.escalate(step, reason, snapshot):
            # A person dealt with it. Never take their word for it: re-verify.
            verified = self._verify(step.checkpoint)
            notify = getattr(self.escalate, "resolved", None)
            if callable(notify):
                notify(verified)
            if verified:
                return None
            reason = f"{reason} (and the step still did not verify afterwards)"

        if self.recorder:
            self.recorder.event(
                "escalated", step_id=step.id, reason=reason, observed=observed
            )
        return Escalated(
            capability=self.artifact.ref,
            intervention_id=self._intervention_id(),
            step_id=step.id,
            intent=step.intent,
            reason=reason,
            observed=observed or self._describe(snapshot),
            session_id=self.session_id,
            partial_outputs=dict(self.outputs),
        )

    def _intervention_id(self) -> str | None:
        """Name the open intervention, if the handler kept one.

        The caller of a replay that came back ``Escalated`` needs to be able
        to find the thing a person is being asked about, and the handler is
        the only party that knows its id.
        """
        current = getattr(self.escalate, "current", None)
        return getattr(current, "id", None)

    def _failure(
        self,
        step: Step,
        category: FailureCategory,
        *,
        expected: str,
        observed: str,
        detail: str | None = None,
    ) -> Result:
        if self.recorder:
            self.recorder.failure(
                self.surface, step_id=step.id, reason=observed, category=category
            )
        return Failure(
            capability=self.artifact.ref,
            category=category,
            step_id=step.id,
            intent=step.intent,
            expected=expected,
            observed=observed,
            detail=detail,
        )

    def _finish(self, result: Result, began: float) -> Result:
        from dataclasses import replace

        result = replace(
            result,
            capability=result.capability or self.artifact.ref,
            run_id=self.recorder.run_id if self.recorder else None,
            evidence_ref=str(self.recorder.dir) if self.recorder else None,
            steps_executed=self.steps_executed,
            duration_ms=int((time.perf_counter() - began) * 1000),
            tier_log=tuple(self.tier_log),
            recoveries=self.conditions.fired(),
        )
        if self.recorder:
            self.recorder.finish(result)
        return result

    # --- odds and ends ---------------------------------------------------

    def _evidence(self, step: Step):
        if self.recorder is None:
            return nullcontext(
                StepRecord(step.id, step.intent, str(step.action))
            )
        return self.recorder.step(
            step.id,
            step.intent,
            str(step.action),
            target=step.target.describe() if step.target else None,
        )

    @staticmethod
    def _expectation(step: Step) -> str:
        if step.checkpoint is not None:
            return f"{step.checkpoint.expect}: {step.checkpoint.target.describe()}"
        return f"the step to succeed: {step.intent}"

    @staticmethod
    def _describe(snapshot: Snapshot) -> str:
        heading = next(
            (n.name for n in snapshot.tree.walk() if n.role == "heading" and n.name),
            None,
        )
        where = snapshot.location
        return f"{heading!r} at {where}" if heading else f"at {where}"


def replay(
    surface: Surface,
    artifact: Artifact,
    inputs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Result:
    return ReplayEngine(surface, artifact, **kwargs).run(inputs)
