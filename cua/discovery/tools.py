"""The vocabulary offered to the discovery model.

One tool per :class:`~cua.types.ActionType`, plus the two terminal signals.
The set is closed and it is the same set replay can execute, which is the
property that matters: **the model cannot express a step that replay would not
know how to perform.** Discovery and replay share the action vocabulary rather
than agreeing on one by convention, so there is no class of artifact that
records cleanly and then cannot be run.

Every tool takes an ``intent``. It costs the model one short sentence and it
buys the artifact its reviewability -- a person auditing a capability that
opens accounts should not have to decode locators to find out what it does.
Asking for it at the moment of the decision is also the only time it is
honest: a description written afterwards by a second model call is a guess
about what the first one was thinking.

Targets are named by ``ref``, the marker the accessibility tree carries for
each node. That is the one thing a model can do reliably against a page it has
never seen. A ref is worthless by the next page load, so nothing records one:
:mod:`cua.discovery.locate` turns it into a verified semantic locator before
the action is taken, and that is what reaches the trace.
"""

from __future__ import annotations

from typing import Any

from cua.discovery.locate import synthesize
from cua.primitives import Action, Snapshot
from cua.types import ActionType

REF_PARAM = {
    "type": "string",
    "description": (
        "The ref marker of the target control, exactly as shown after '#' in "
        "the tree, e.g. 'f0n17'."
    ),
}

INTENT_PARAM = {
    "type": "string",
    "description": (
        "One plain sentence saying what this step is for, written for a person "
        "reviewing the recorded capability later. 'Enter the member number "
        "into the search field', not 'type f0n17'."
    ),
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {**properties, "intent": INTENT_PARAM},
            "required": [*required, "intent"],
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "navigate",
        "Go to a path within the application, e.g. '/members/search'.",
        {"path": {"type": "string", "description": "Application-relative path."}},
        ["path"],
    ),
    _tool(
        "click",
        "Click a control: a button, a link, a checkbox.",
        {"ref": REF_PARAM},
        ["ref"],
    ),
    _tool(
        "type",
        (
            "Type into a text field. Replaces whatever is already there. Use the "
            "exact value you were given; never invent one."
        ),
        {"ref": REF_PARAM, "value": {"type": "string"}},
        ["ref", "value"],
    ),
    _tool(
        "select",
        "Choose an option in a dropdown by its visible label.",
        {"ref": REF_PARAM, "value": {"type": "string"}},
        ["ref", "value"],
    ),
    _tool(
        "press_key",
        "Press a single key, e.g. 'Enter' or 'Escape'.",
        {"key": {"type": "string"}, "ref": REF_PARAM},
        ["key"],
    ),
    _tool(
        "extract",
        (
            "Read a value off the screen and record it as a named output of this "
            "capability. Use this for every value the caller asked for -- a value "
            "you only mention in your reply is not returned to anyone."
        ),
        {
            "ref": REF_PARAM,
            "into": {
                "type": "string",
                "description": (
                    "Output name, lowercase with underscores, e.g. "
                    "'savings_balance'."
                ),
            },
            "transform": {
                "type": "string",
                "enum": ["none", "money", "integer", "trim"],
                "description": "How to normalise the raw text. Default 'trim'.",
            },
        },
        ["ref", "into"],
    ),
    _tool(
        "wait_for",
        "Wait until a control appears, when a page is still settling.",
        {"ref": REF_PARAM, "timeout_ms": {"type": "integer"}},
        ["ref"],
    ),
    _tool(
        "done",
        (
            "The goal has been achieved. Call this once every value asked for has "
            "been extracted."
        ),
        {
            "summary": {
                "type": "string",
                "description": "What was accomplished, in one or two sentences.",
            }
        },
        ["summary"],
    ),
    _tool(
        "stuck",
        (
            "Stop. Use this when the goal cannot be achieved from here -- the "
            "application is refusing, asking for something you were not given, or "
            "you have run out of things to try. Stopping is a correct answer; "
            "guessing at a value is not."
        ),
        {
            "reason": {
                "type": "string",
                "description": "What blocked you, concretely.",
            }
        },
        ["reason"],
    ),
]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)

#: Signals rather than steps. They end the loop and never reach an artifact.
TERMINAL_TOOLS = frozenset({"done", "stuck"})


class UnusableTarget(ValueError):
    """The model pointed at something we cannot describe semantically.

    Either the ref is stale -- the page moved under it -- or no rule in the
    locator vocabulary identifies that node unambiguously. Both are told to the
    model plainly so it can choose something else. The alternative, recording a
    locator we could not verify, buys a discovery run that appears to succeed
    and an artifact that fails in production.
    """


def node_by_ref(snapshot: Snapshot, ref: str) -> Any:
    return next((n for n in snapshot.tree.walk() if n.ref == ref), None)


def action_from_tool_call(
    name: str, args: dict[str, Any], snapshot: Snapshot
) -> Action:
    """Turn one model tool call into an executable, recordable action."""
    if name not in TOOL_NAMES:
        raise UnusableTarget(f"no such tool: {name}")
    if name in TERMINAL_TOOLS:
        raise UnusableTarget(f"{name} is a signal, not an action")

    action_type = ActionType(name)
    intent = str(args.get("intent", "")).strip()
    payload = {k: v for k, v in args.items() if k not in {"ref", "intent"}}

    ref = args.get("ref")
    target = None
    if ref:
        node = node_by_ref(snapshot, str(ref))
        if node is None:
            raise UnusableTarget(
                f"ref {ref!r} is not on the current screen -- the page has "
                "changed since you last saw it. Look again and pick a ref from "
                "the tree above."
            )
        # Reading a value has a stricter idea of what a description may be
        # made of than clicking a button does.
        target = synthesize(
            snapshot.tree, node, reading=action_type is ActionType.EXTRACT
        )
        if target is None:
            raise UnusableTarget(
                f"the {node.role} at {ref!r} cannot be identified reliably "
                "enough to record: nothing distinguishes it from its "
                "neighbours. Try a different control."
            )
    elif action_type not in (ActionType.NAVIGATE, ActionType.PRESS_KEY):
        raise UnusableTarget(f"{name} needs a ref")

    if action_type is ActionType.EXTRACT:
        payload.setdefault("transform", "trim")

    return Action(type=action_type, target=target, args=payload, intent=intent)
