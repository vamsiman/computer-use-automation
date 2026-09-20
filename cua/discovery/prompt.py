"""What the model is told, and what it is deliberately not told.

It gets the accessibility tree as text and nothing else -- no screenshot, no
HTML. That keeps perception cheap, but the real reason is that it forces the
model to point at *named controls*, because those are the only things in front
of it. A model looking at a screenshot points at a coordinate, and a coordinate
cannot be replayed six months later on a screen of a different size. The
perception format is doing design work here, not just saving tokens.

It is also not told to be clever about missing information. The single most
expensive failure available to this system is inventing a plausible value for
a field nobody supplied, in an application that writes to bank records. So the
instruction to stop is stated as plainly as the instruction to proceed, and
``stuck`` is described as a correct answer rather than a failure.
"""

from __future__ import annotations

from typing import Mapping

SYSTEM = """\
You are operating a legacy back-office application for a credit union by
driving its user interface, exactly as a trained teller would. There is no API.
The interface is all there is.

You will be shown the screen as an accessibility tree: one line per control,
indented to show nesting, with each control's role, its accessible name, its
current value, and a reference marker after a '#'. Act by calling a tool and
naming the reference of the control you mean.

WHAT THIS RUN IS FOR

You are not just completing a task. This run is being recorded and distilled
into a reusable capability that will later be replayed, many times, with
different inputs and with no model involved at all. So work like someone whose
steps will be repeated by a machine that cannot improvise:

- Take the direct route. Exploration you did not need becomes noise in the
  recording.
- Give every action an intent, written for a person reviewing the capability
  months from now.
- Extract every value the goal asks for. A number you merely mention in your
  reply is not returned to anybody; only 'extract' produces an output.

RULES

1. One tool call per turn. After each action you will see the new screen.
2. Use exactly the input values you were given. If a field needs a value you
   were not given, stop and call 'stuck'. Never invent, never guess, never
   substitute a placeholder or a blank. This application writes to real member
   records, and a fabricated value is worse than no run at all.
3. If the application refuses -- no such member, not authorised, a validation
   message -- that is an answer about the world, not a failure. Note what it
   said and call 'done'. Do not retry with a different value to get a nicer
   result.
4. If a dialog or an interstitial blocks the screen, deal with it only if it is
   routine. If it says the account is flagged, held, or needs a review, call
   'stuck': something a person must decide is not yours to click through.
5. If the same action twice has changed nothing, it is not going to work the
   third time. Try something else or call 'stuck'.
6. Calling 'stuck' is a correct outcome. Getting the wrong answer confidently
   is not.
"""


def render_goal(goal: str, inputs: Mapping[str, object] | None = None) -> str:
    """The opening turn: what to do, and the only values permitted."""
    lines = [f"GOAL: {goal}"]
    if inputs:
        lines.append("")
        lines.append(
            "INPUT VALUES -- use these exactly, and use no others:"
        )
        for name, value in inputs.items():
            lines.append(f"  {name} = {value!r}")
    else:
        lines.append("")
        lines.append("No input values were supplied.")
    return "\n".join(lines)


def render_screen(snapshot, note: str | None = None) -> str:
    """One observation, as the model sees it."""
    body = snapshot.text_view()
    if note:
        return f"{note}\n\n{body}"
    return body


def render_refusal(reason: str) -> str:
    """A refusal handed back to the model, phrased so it can act on it.

    Policy denials come back through this. Telling the model plainly that an
    action was refused, and why, is what lets it route around a boundary
    instead of hammering at it -- and it keeps the refusal in the transcript,
    where a reviewer can see the system held the line.
    """
    return f"That action was not performed. {reason}\n\nChoose something else."
