"""One model call, outside the action loop, where the model is a writer.

The distinction this module exists to enforce: during discovery the model
*decides* things, and here it does not. It is handed a finished draft and asked
to improve the prose -- a name, a description written for the agent that will
call this capability, tidier step intents, and any business outcomes it noticed
while exploring. It cannot change a locator, an action, an argument or a risk
level, and that is not a matter of prompting. :func:`apply` copies across the
fields it is allowed to touch and ignores everything else, so a model that
returns a rewritten step list has simply wasted the tokens.

Worth being clear about why the call is optional. Every action already carries
the intent the model wrote at the moment it chose it, so the artifact is
complete and reviewable before this runs. Annotation makes it nicer to read. A
design where the only description of a flow comes from a second call, made
after the fact by a model reconstructing its own reasoning, has put something
load-bearing on a guess.
"""

from __future__ import annotations

import re
from typing import Any

from cua.artifact.models import Artifact, Outcome
from cua.discovery.trace import DiscoveryTrace
from cua.locators import Locator, RoleNameSpec

MAX_OUTCOMES = 6

ANNOTATE_TOOL: dict[str, Any] = {
    "name": "describe_capability",
    "description": (
        "Record the human-facing description of a capability that has just "
        "been distilled from a discovery run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Dotted lowercase identifier, noun then verb phrase, e.g. "
                    "'member.read_savings_balance'."
                ),
            },
            "title": {"type": "string", "description": "Short human title."},
            "description": {
                "type": "string",
                "description": (
                    "Written for the AI agent that will call this capability: "
                    "what it does, what it needs, what it returns, and whether "
                    "it changes anything. Two or three sentences."
                ),
            },
            "step_intents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "intent": {"type": "string"},
                    },
                    "required": ["id", "intent"],
                },
                "description": "Improved intent lines, by step id.",
            },
            "outcomes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "SHOUTING_SNAKE_CASE, e.g. MEMBER_NOT_FOUND.",
                        },
                        "detect_text": {
                            "type": "string",
                            "description": (
                                "Text that appears on screen when this outcome "
                                "occurs, as close to verbatim as you saw it."
                            ),
                        },
                        "message": {"type": "string"},
                    },
                    "required": ["code", "detect_text"],
                },
                "description": (
                    "Business outcomes you saw or were told about while "
                    "exploring: a refusal, a not-found, a validation message. "
                    "These are answers about the world, not failures. Do not "
                    "invent ones you did not see."
                ),
            },
        },
        "required": ["id", "title", "description"],
    },
}

SYSTEM = """\
You are documenting a capability that has just been recorded from a live run of
a back-office application. You are writing, not deciding: the steps, the
controls and the risk levels are already fixed and are not yours to change.

Write the description for the AI agent that will call this capability. It needs
to know what the capability does, what it must supply, what it gets back, and
whether anything is changed by running it. It does not need to know how the
screens are laid out.

Only report outcomes you actually saw in the run below. An invented outcome
becomes a detector that fires on the wrong screen, which is worse than having
none.
"""


def render_request(trace: DiscoveryTrace, artifact: Artifact) -> str:
    lines = [
        f"GOAL GIVEN: {trace.goal}",
        f"RESULT: {trace.terminal} -- {trace.summary}",
        "",
        "STEPS KEPT:",
    ]
    for step in artifact.steps:
        target = step.target.describe() if step.target else "-"
        lines.append(f"  {step.id}: {step.action} {target} -- {step.intent}")
    lines += ["", "VALUES READ:"]
    for name, value in trace.outputs.items():
        lines.append(f"  {name} = {value!r}")
    lines += ["", "EVERYTHING THE RUN DID, INCLUDING WHAT WAS DISCARDED:"]
    for record in trace.records:
        status = "ok" if record.ok else f"refused: {record.error}"
        lines.append(f"  {record.seq}. {record.tool} -- {record.intent} [{status}]")
    return "\n".join(lines)


def annotate(trace: DiscoveryTrace, artifact: Artifact, client) -> dict[str, Any]:
    """Ask the model to describe the capability. Returns its raw proposal."""
    reply = client.reply(
        system=SYSTEM,
        messages=[{"role": "user", "content": render_request(trace, artifact)}],
        tools=[ANNOTATE_TOOL],
    )
    call = reply.call
    if call is None or call.name != ANNOTATE_TOOL["name"]:
        return {}
    return dict(call.args)


def apply(artifact: Artifact, annotation: dict[str, Any]) -> Artifact:
    """Copy across the fields the model is allowed to influence, and no others.

    Structural, not a matter of prompting. The model is handed a draft and can
    return whatever it likes; only prose and proposed outcomes are read back
    out. Steps, targets, arguments and risk levels are untouched by
    construction, so a badly behaved annotation costs a rewrite of a sentence
    and never a change to what the capability does.
    """
    updated = artifact.model_copy(deep=True)

    proposed_id = str(annotation.get("id") or "").strip()
    if _is_valid_id(proposed_id):
        updated.capability.id = proposed_id
    if annotation.get("title"):
        updated.capability.title = str(annotation["title"])[:120]
    if annotation.get("description"):
        updated.capability.description = str(annotation["description"])

    by_id = {step.id: step for step in updated.steps}
    for entry in annotation.get("step_intents") or []:
        step = by_id.get(str(entry.get("id")))
        intent = str(entry.get("intent") or "").strip()
        if step is not None and intent:
            step.intent = intent

    updated.outcomes = _outcomes(annotation.get("outcomes") or [])
    return updated


def _is_valid_id(value: str) -> bool:
    from cua.artifact.models import CAPABILITY_ID_RE

    return bool(value) and bool(CAPABILITY_ID_RE.match(value))


def _outcomes(proposed: list[dict[str, Any]]) -> list[Outcome]:
    """Turn proposed outcomes into detectors, conservatively.

    Every one becomes a *contains* match on an ``alert``, never an exact one.
    The model's recollection of wording is approximate, and a detector that
    misses is a business outcome reported as a crash -- the exact conflation
    the error taxonomy exists to prevent.
    """
    outcomes: list[Outcome] = []
    seen: set[str] = set()
    for entry in proposed[:MAX_OUTCOMES]:
        code = re.sub(r"[^A-Z0-9_]", "", str(entry.get("code", "")).upper())
        text = str(entry.get("detect_text") or "").strip()
        if not code or not text or code in seen:
            continue
        seen.add(code)
        outcomes.append(
            Outcome(
                code=code,
                detect=Locator(
                    primary=RoleNameSpec(role="alert", name_contains=text[:60]),
                    confidence=0.6,
                    note=(
                        "Proposed by the annotation pass from what the run saw. "
                        "Verify the wording against the application before "
                        "approving."
                    ),
                ),
                terminal=True,
                message=str(entry.get("message") or "").strip() or None,
            )
        )
    return outcomes
