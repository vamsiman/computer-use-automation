"""The raw record of a discovery run, kept deliberately separate from the artifact.

The brief is explicit that the capability must be decoupled from the model
transcript, and this is where that separation is made physical. A trace is
everything that happened, including the wrong turns; an artifact is the
distilled flow. Keeping them as two documents means the distillation is
*visible* -- ``step_count_raw`` against ``step_count_final`` says the model
wandered for nineteen actions and six survived -- and it means a capability can
be re-derived from the same trace if the distillation rules improve.

Each record carries the tree hash before and after the action. That is not
diagnostics: it is how the loop knows an action changed nothing, which is the
difference between a model making progress and a model stuck in a polite loop
clicking the same dead link.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.primitives import Action, Snapshot
from cua.surface.a11y import tree_hash as _tree_hash

#: How a run ended.
Terminal = Literal[
    "done",              # the model says the goal is met
    "stuck",             # the model says it cannot proceed
    "stuck_detected",    # the loop noticed it was going in circles
    "step_budget",
    "time_budget",
    "needs_approval",    # a risky step, with nobody configured to approve it
    "error",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tree_hash(snapshot: Snapshot) -> str:
    """Fingerprint of what is on screen, for one snapshot.

    Thin wrapper over the surface layer's hash, which already ignores refs and
    geometry -- both regenerate on every observation, and including either
    would make every screen look different from itself and destroy the stuck
    signal this exists to provide.
    """
    return _tree_hash(snapshot.tree)


class TraceRecord(BaseModel):
    """One turn of the loop."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    at: datetime = Field(default_factory=_now)
    tool: str
    intent: str = ""
    #: The executable action built from the tool call. None when the call was
    #: refused before it became one.
    action: Action | None = None
    #: What the model pointed at, kept for debugging the synthesis rules. It
    #: is meaningless outside this page load and never reaches an artifact.
    ref: str | None = None
    target: str | None = None
    #: Which tier resolved it when the action ran.
    tier: int | None = None
    ok: bool = True
    error: str | None = None
    value: str | None = None
    location_before: str | None = None
    location_after: str | None = None
    tree_before: str | None = None
    tree_after: str | None = None
    screenshot_ref: str | None = None
    #: Set when the policy engine refused or deferred the action.
    policy_rule: str | None = None

    @property
    def changed_the_screen(self) -> bool:
        return bool(self.tree_after and self.tree_after != self.tree_before)


class DiscoveryTrace(BaseModel):
    """Everything one discovery run did, in order."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    run_id: str | None = None
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    records: list[TraceRecord] = Field(default_factory=list)
    terminal: Terminal | None = None
    #: The model's own account of what it did, from ``done`` or ``stuck``.
    summary: str = ""
    #: Values the run extracted, by output name.
    outputs: dict[str, Any] = Field(default_factory=dict)

    def add(self, record: TraceRecord) -> TraceRecord:
        self.records.append(record)
        return record

    @property
    def next_seq(self) -> int:
        return len(self.records) + 1

    @property
    def succeeded(self) -> bool:
        return self.terminal == "done"

    def effective(self) -> list[TraceRecord]:
        """Actions that ran and changed something.

        The first pruning rule, kept here rather than in the distiller because
        it is a fact about the trace: an action that left the screen identical
        did nothing, whatever the model believed at the time.
        """
        return [r for r in self.records if r.ok and r.changed_the_screen]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "DiscoveryTrace":
        return cls.model_validate(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )
