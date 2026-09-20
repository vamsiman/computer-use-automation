"""The observe / decide / act loop. The only place a model is in the loop at all.

This is the expensive half of the system and it runs once per capability. Its
job is not to complete a task -- a person could do that faster -- but to
produce a recording good enough that nothing has to think again. Everything
here bends toward that: the model points at named controls because those are
recordable, every action is policy-checked before it happens because a
discovery run drives the same live application as production, and the loop
stops itself rather than waiting to be stopped.

The model sits behind a two-method seam. The loop takes a ``ModelClient`` and
knows nothing about the Anthropic SDK, which is what lets the whole loop be
tested against a scripted client, live, in CI, with no API key -- proving
everything except the model's judgement. That is worth arranging: the parts
most likely to break are the ones a fake model exercises perfectly well.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from cua.discovery.prompt import SYSTEM, render_goal, render_refusal, render_screen
from cua.discovery.tools import (
    TERMINAL_TOOLS,
    TOOLS,
    UnusableTarget,
    action_from_tool_call,
)
from cua.discovery.trace import DiscoveryTrace, TraceRecord, tree_hash
from cua.evidence import Recorder, StepRecord
from cua.policy import Mode, PolicyContext, PolicyEngine, RequireApproval, load_engine
from cua.policy.engine import Decision, target_label
from cua.primitives import Action, Snapshot
from cua.surface.base import Surface, SurfaceError
from cua.types import ActionType

DEFAULT_MODEL = "claude-sonnet-5"


def model_name() -> str:
    return os.environ.get("CUA_MODEL") or DEFAULT_MODEL


# --- the seam -------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelReply:
    tool_calls: tuple[ToolCall, ...] = ()
    text: str = ""

    @property
    def call(self) -> ToolCall | None:
        """The one action this turn. Extra calls are ignored on purpose.

        A model that emits three tool calls in a turn has decided what to do
        after the second one without seeing what the first one did. Taking only
        the first keeps every decision grounded in an observation, which is the
        property that makes the recording worth anything.
        """
        return self.tool_calls[0] if self.tool_calls else None


class ModelClient(Protocol):
    def reply(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> ModelReply: ...


class AnthropicClient:
    """The real model, and the only module in the system that talks to one."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 1024,
    ) -> None:
        import anthropic

        self.model = model or model_name()
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    def reply(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> ModelReply:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        calls = tuple(
            ToolCall(id=block.id, name=block.name, args=dict(block.input or {}))
            for block in response.content
            if block.type == "tool_use"
        )
        text = " ".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        return ModelReply(tool_calls=calls, text=text)


# --- budgets --------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryLimits:
    """Every way the loop is allowed to end by itself.

    A loop that only stops when the model says so is a loop that runs until
    somebody notices the bill. Each of these has a different failure behind it:
    a model working steadily but slowly, a model looping, a model having a very
    expensive conversation with a page that never changes.
    """

    max_steps: int = 40
    max_seconds: float = 300.0
    #: Identical action, identical screen, this many times running.
    repeat_limit: int = 3
    #: How often the same screen may be revisited before we call it a cycle.
    revisit_limit: int = 5
    #: Consecutive surface errors before giving up.
    error_limit: int = 3


Approver = Callable[[Action, Decision], bool]


class DiscoveryAgent:
    """Drives one goal against one live surface, once."""

    def __init__(
        self,
        surface: Surface,
        client: ModelClient,
        *,
        policy: PolicyEngine | None = None,
        recorder: Recorder | None = None,
        limits: DiscoveryLimits | None = None,
        approve: Approver | None = None,
        model: str | None = None,
    ) -> None:
        self.surface = surface
        self.client = client
        self.policy = policy or load_engine()
        self.recorder = recorder
        self.limits = limits or DiscoveryLimits()
        #: How a risky step gets agreed to. With nobody to ask, the run stops
        #: rather than proceeding -- discovery has no licence to open an
        #: account just because it was exploring.
        self.approve = approve
        self.model = model or getattr(client, "model", model_name())

    # --- the loop --------------------------------------------------------

    def run(self, goal: str, inputs: dict[str, Any] | None = None) -> DiscoveryTrace:
        inputs = dict(inputs or {})
        trace = DiscoveryTrace(
            goal=goal,
            inputs=inputs,
            model=self.model,
            run_id=self.recorder.run_id if self.recorder else None,
        )

        snapshot = self.surface.observe()
        messages: list[dict] = [
            {
                "role": "user",
                "content": f"{render_goal(goal, inputs)}\n\n{render_screen(snapshot)}",
            }
        ]

        began = time.monotonic()
        seen: Counter[str] = Counter([tree_hash(snapshot)])
        repeats = 0
        errors = 0
        last_signature: tuple | None = None

        while True:
            if len(trace.records) >= self.limits.max_steps:
                return self._end(trace, "step_budget", "Step budget exhausted.")
            if time.monotonic() - began > self.limits.max_seconds:
                return self._end(trace, "time_budget", "Time budget exhausted.")

            reply = self.client.reply(
                system=SYSTEM, messages=messages, tools=TOOLS
            )
            call = reply.call
            if call is None:
                # No tool call means the model answered in prose. It is being
                # asked to operate an application, not to describe one.
                return self._end(
                    trace, "error", reply.text or "The model took no action."
                )

            messages.append(self._assistant_turn(reply, call))

            if call.name in TERMINAL_TOOLS:
                summary = str(
                    call.args.get("summary") or call.args.get("reason") or ""
                )
                trace.add(
                    TraceRecord(
                        seq=trace.next_seq,
                        tool=call.name,
                        intent=summary,
                        location_before=snapshot.location,
                        tree_before=tree_hash(snapshot),
                    )
                )
                return self._end(trace, call.name, summary)  # type: ignore[arg-type]

            record = TraceRecord(
                seq=trace.next_seq,
                tool=call.name,
                intent=str(call.args.get("intent", "")),
                ref=call.args.get("ref"),
                location_before=snapshot.location,
                tree_before=tree_hash(snapshot),
            )

            # --- build the action ---------------------------------------
            try:
                action = action_from_tool_call(call.name, call.args, snapshot)
            except UnusableTarget as exc:
                record.ok = False
                record.error = str(exc)
                trace.add(record)
                messages.append(self._tool_result(call, render_refusal(str(exc))))
                continue

            record.target = (
                action.target.describe() if action.target is not None else None
            )

            # --- ask permission before acting, never after ---------------
            decision = self.policy.check(
                action,
                PolicyContext(
                    location=snapshot.location,
                    mode=Mode.DISCOVERY,
                    step_id=f"a{record.seq}",
                ),
            )
            record.policy_rule = decision.rule
            if self.recorder:
                self.recorder.policy(
                    decision, action=str(action.type), target=record.target
                )

            if not decision.permitted:
                if isinstance(decision, RequireApproval):
                    if self.approve is None or not self.approve(action, decision):
                        record.ok = False
                        record.error = decision.reason
                        trace.add(record)
                        return self._end(trace, "needs_approval", decision.reason)
                    decision = self.policy.check(
                        action,
                        PolicyContext(
                            location=snapshot.location,
                            mode=Mode.DISCOVERY,
                            step_id=f"a{record.seq}",
                            approved=True,
                        ),
                    )
                else:
                    record.ok = False
                    record.error = decision.reason
                    trace.add(record)
                    messages.append(
                        self._tool_result(call, render_refusal(decision.reason))
                    )
                    continue

            # --- act -----------------------------------------------------
            signature = (call.name, record.ref, record.tree_before)
            repeats = repeats + 1 if signature == last_signature else 0
            last_signature = signature

            try:
                result = self._act(action, record)
                errors = 0
            except SurfaceError as exc:
                errors += 1
                record.ok = False
                record.error = str(exc)
                trace.add(record)
                if errors >= self.limits.error_limit:
                    return self._end(trace, "error", str(exc))
                messages.append(self._tool_result(call, render_refusal(str(exc))))
                continue

            snapshot = result.observed or self.surface.observe()
            record.location_after = snapshot.location
            record.tree_after = tree_hash(snapshot)
            trace.add(record)

            if record.ok and action.type is ActionType.EXTRACT:
                name = str(action.args.get("into") or "value")
                trace.outputs[name] = record.value

            # --- notice when nothing is happening ------------------------
            if repeats >= self.limits.repeat_limit:
                return self._end(
                    trace,
                    "stuck_detected",
                    f"The same {call.name} left the screen unchanged "
                    f"{repeats + 1} times running.",
                )
            seen[record.tree_after] += 1
            if seen[record.tree_after] > self.limits.revisit_limit:
                return self._end(
                    trace,
                    "stuck_detected",
                    "The run keeps returning to the same screen.",
                )

            note = None if record.ok else f"That did not work: {record.error}"
            messages.append(
                self._tool_result(call, render_screen(snapshot, note))
            )

    # --- pieces ----------------------------------------------------------

    def _act(self, action: Action, record: TraceRecord):
        """Perform one action, with the evidence record open around it."""
        step = (
            self.recorder.step(
                f"a{record.seq}",
                record.intent or str(action.type),
                str(action.type),
                target=record.target,
            )
            if self.recorder
            else nullcontext(StepRecord(f"a{record.seq}", record.intent, str(action.type)))
        )
        with step as evidence:
            result = self.surface.act(action)
            record.ok = result.ok
            record.tier = result.tier
            record.error = result.error
            record.value = result.value

            evidence.ok = result.ok
            evidence.tier = result.tier
            evidence.error = result.error
            evidence.policy_rule = record.policy_rule
            if result.value is not None and action.type is ActionType.EXTRACT:
                evidence.extracted = {
                    str(action.args.get("into") or "value"): result.value
                }
            if self.recorder:
                record.screenshot_ref = self.recorder.screenshot(self.surface)
                evidence.screenshot = record.screenshot_ref
        return result

    @staticmethod
    def _assistant_turn(reply: ModelReply, call: ToolCall) -> dict:
        content: list[dict] = []
        if reply.text:
            content.append({"type": "text", "text": reply.text})
        content.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.args,
            }
        )
        return {"role": "assistant", "content": content}

    @staticmethod
    def _tool_result(call: ToolCall, body: str) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": body,
                }
            ],
        }

    def _end(self, trace: DiscoveryTrace, terminal: str, summary: str) -> DiscoveryTrace:
        from datetime import datetime, timezone

        trace.terminal = terminal  # type: ignore[assignment]
        trace.summary = summary
        trace.finished_at = datetime.now(timezone.utc)
        if self.recorder:
            self.recorder.event(
                "discovery_finished",
                terminal=terminal,
                summary=summary,
                steps=len(trace.records),
                effective=len(trace.effective()),
            )
        return trace


def discover(
    surface: Surface,
    goal: str,
    inputs: dict[str, Any] | None = None,
    *,
    client: ModelClient | None = None,
    **kwargs: Any,
) -> DiscoveryTrace:
    """Run discovery with the real model unless another client is supplied."""
    return DiscoveryAgent(surface, client or AnthropicClient(), **kwargs).run(
        goal, inputs
    )
