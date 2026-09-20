"""What the person did, while they were the one doing it.

A handoff is the only moment in a run when the automation is not the thing
acting, and without this module it would be the only gap in the evidence: a
step that failed, a silence, and then a step that worked, with nothing in the
record to say why. In a regulated back office that gap is the whole problem --
"the automation paused and somebody sorted it out" is not an audit trail.

So the window is watched for the duration of the handoff and every click,
change and edit comes back as a typed record, described in exactly the
vocabulary the automation uses, because it is produced by the same code (see
``surface/describe.js``). The human's actions and the automation's steps can
be read against each other in one log.

Two things this deliberately is not:

* **Not a way to check the human.** Nothing here compares what the person did
  against what the automation would have done, and nothing rejects a handoff
  because the clicks looked wrong. The human is the higher authority. What
  guards the resume is re-verifying the *world* afterwards, which is the
  engine's job, not this module's.
* **Not keylogging.** Values arrive already masked where the surface masks
  them, and everything here goes through the run's redactor before it is
  written, so a person typing into a field the artifact declares ``secret``
  does not make it any less secret.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Watchable(Protocol):
    """A surface that can report what a human did to it.

    Kept out of the ``Surface`` protocol on purpose. Observing, resolving and
    acting are what every surface must do; watching a person is a capability
    some can offer and others cannot, and writing it into the core protocol
    would mean a desktop driver had to fake it to compile. Asked for by
    duck-typing, absent without consequence.
    """

    def watch_start(self) -> None: ...

    def watch_drain(self) -> list[dict]: ...

    def watch_stop(self) -> list[dict]: ...


@dataclass(frozen=True)
class HumanAction:
    """One thing a person did to the live session."""

    at: str
    kind: str
    role: str
    name: str
    value: str | None = None
    ref: str | None = None
    location: str = ""

    @classmethod
    def from_raw(cls, raw: dict) -> "HumanAction":
        return cls(
            at=str(raw.get("at", "")),
            kind=str(raw.get("kind", "")),
            role=str(raw.get("role", "")),
            name=str(raw.get("name", "")),
            value=raw.get("value"),
            ref=raw.get("ref"),
            location=str(raw.get("location", "")),
        )

    def describe(self) -> str:
        """One line, in the form the step log already uses."""
        what = f"{self.role} {self.name!r}" if self.name else self.role
        if self.value:
            return f"{self.kind} {what} = {self.value!r}"
        return f"{self.kind} {what}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HumanWatcher:
    """Turns a watchable surface on and off around a handoff.

    Holds everything drained so far, because a person navigating mid-handoff
    is normal and the records have to be collected as they go rather than all
    at the end.
    """

    def __init__(self, surface: Any) -> None:
        self.surface = surface
        self.supported = all(
            callable(getattr(surface, name, None))
            for name in ("watch_start", "watch_drain", "watch_stop")
        )
        self._actions: list[HumanAction] = []
        self._seen: set[str] = set()
        self.watching = False

    @property
    def actions(self) -> tuple[HumanAction, ...]:
        return tuple(self._actions)

    def start(self) -> bool:
        """Returns False when the surface cannot be watched.

        A surface that cannot report human actions does not stop a handoff --
        it just means the log says so, which is a better answer than silently
        recording nothing and letting a reader assume the person did nothing.
        """
        if not self.supported:
            return False
        self.surface.watch_start()
        self.watching = True
        return True

    def drain(self) -> tuple[HumanAction, ...]:
        """Collect what has happened since the last drain."""
        if not self.watching:
            return ()
        return self._absorb(self.surface.watch_drain())

    def stop(self) -> tuple[HumanAction, ...]:
        """Collect the rest and stop watching. Returns everything from the handoff."""
        if not self.watching:
            return self.actions
        self._absorb(self.surface.watch_stop())
        self.watching = False
        return self.actions

    def _absorb(self, raw: Sequence[dict] | None) -> tuple[HumanAction, ...]:
        fresh: list[HumanAction] = []
        for record in raw or ():
            # Frames of one origin share the tab's storage, so the same
            # record can come back from more than one of them. Dropping
            # duplicates here is cheaper than trying to pick the one frame
            # that owns the log while pages are navigating underneath us.
            marker = str(record.get("id") or "")
            if marker and marker in self._seen:
                continue
            if marker:
                self._seen.add(marker)
            fresh.append(HumanAction.from_raw(record))

        fresh.sort(key=lambda action: action.at)
        self._actions.extend(fresh)
        return tuple(fresh)
