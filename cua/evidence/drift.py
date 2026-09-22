"""Reading the signal every run has been writing and nobody has been reading.

Every replay records which locator rule resolved each step, whether it was the
step's own primary or a fallback, how long it took, and which recoveries
fired. On successful runs too, deliberately — a capability sliding from its
primary rule to its third fallback is degrading weeks before it breaks, and
nobody reads the logs of runs that worked.

That data has existed since the replay engine did. What has not existed is
anything that reads it across runs, which makes the drift signal real in
principle and invisible in practice. This module is the reader.

It is a report, not a monitor. There is no daemon, no threshold, no alerting
integration — the brief is explicit that building scaling infrastructure is
not what is being rewarded. What matters is that the shape of the answer is
available from the evidence already on disk, which is the claim the design has
been making.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ROOT = Path("evidence/runs")


@dataclass
class StepHistory:
    """How one step of one capability has been resolving, over time."""

    capability: str
    step_id: str
    intent: str = ""
    #: (run id, strategy, tier, degraded), oldest first.
    resolutions: list[tuple[str, str, int, bool]] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)
    ambiguous: int = 0

    @property
    def runs(self) -> int:
        return len(self.resolutions)

    @property
    def degraded_runs(self) -> int:
        return sum(1 for _, _, _, degraded in self.resolutions if degraded)

    @property
    def strategies(self) -> list[str]:
        seen: list[str] = []
        for _, strategy, _, _ in self.resolutions:
            if strategy not in seen:
                seen.append(strategy)
        return seen

    @property
    def worsening(self) -> bool:
        """Is this step resolving on a lower rule than it used to?

        Compared first-to-last rather than by a trend line. With a handful of
        runs a trend is noise, and the question that matters is the blunt one:
        does it resolve worse now than it did when it was recorded?
        """
        if len(self.resolutions) < 2:
            return False
        return self.resolutions[-1][2] > self.resolutions[0][2]

    @property
    def verdict(self) -> str:
        if self.worsening:
            return "WORSENING"
        if self.ambiguous:
            return "AMBIGUOUS"
        if self.degraded_runs == self.runs and self.runs:
            return "always degraded"
        if self.degraded_runs:
            return "sometimes degraded"
        return "healthy"


@dataclass
class Report:
    steps: dict[tuple[str, str], StepHistory] = field(default_factory=dict)
    recoveries: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    runs: int = 0
    results: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def concerning(self) -> list[StepHistory]:
        """What somebody should actually look at."""
        return [
            history
            for history in self.steps.values()
            if history.verdict in ("WORSENING", "AMBIGUOUS", "always degraded")
        ]


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def bundles(root: Path) -> Iterator[tuple[str, dict, dict]]:
    """Every run bundle under ``root``, oldest first.

    Sorted by name because run ids lead with a UTC timestamp, so the
    filesystem ordering is chronological without having to open anything.
    """
    if not root.is_dir():
        return
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        result = _load(directory / "result.json")
        meta = _load(directory / "meta.json")
        if result is None:
            continue
        yield directory.name, result, meta or {}


def collect(root: Path | str = DEFAULT_ROOT) -> Report:
    report = Report()
    for run_id, result, meta in bundles(Path(root)):
        capability = result.get("capability") or "(unknown)"
        report.runs += 1
        report.results[result.get("kind") or _kind_of(result)] += 1

        for code in result.get("recoveries") or ():
            name, times = _split_recovery(str(code))
            report.recoveries[name] += times

        for entry in result.get("tier_log") or ():
            key = (capability, entry.get("step_id") or "?")
            history = report.steps.get(key)
            if history is None:
                history = StepHistory(
                    capability=capability,
                    step_id=entry.get("step_id") or "?",
                    intent=entry.get("intent") or "",
                )
                report.steps[key] = history
            history.resolutions.append(
                (
                    run_id,
                    # A step that failed every tier has no strategy at all,
                    # and "None" in a drift report reads like a bug in the
                    # report rather than a fact about the run.
                    str(entry.get("strategy") or "unresolved"),
                    int(entry.get("tier") or 0),
                    bool(entry.get("degraded")),
                )
            )
            if int(entry.get("match_count") or 1) > 1:
                # The dangerous one. A locator that used to identify one
                # control and now matches several still resolves, still
                # returns a value, and is no longer pointing at anything in
                # particular.
                history.ambiguous += 1
    return report


#: Recoveries are recorded as "SYSTEM_NOTICEx2" -- the code and how many
#: times it fired in that run. Counting the string would count a recovery
#: that fired twice as one event, which is exactly the number that matters.
_RECOVERY = re.compile(r"^(?P<code>.+?)x(?P<times>\d+)$")


def _split_recovery(raw: str) -> tuple[str, int]:
    match = _RECOVERY.match(raw)
    if not match:
        return raw, 1
    return match.group("code"), int(match.group("times"))


def _kind_of(result: dict) -> str:
    """Older bundles do not name their result type; infer it."""
    if result.get("category"):
        return "Failure"
    if result.get("code"):
        return "BusinessOutcome"
    if result.get("reason"):
        return "Escalated"
    return "Success"


def render(report: Report) -> str:
    """The report a person reads. Plain text on purpose."""
    if not report.runs:
        return "no evidence bundles found -- run something first"

    lines = [
        f"{report.runs} run(s)",
        "  " + ", ".join(f"{n}x {kind}" for kind, n in sorted(report.results.items())),
        "",
    ]

    concerning = report.concerning
    if concerning:
        lines.append("NEEDS A LOOK")
        for history in concerning:
            lines.append(
                f"  {history.capability}  {history.step_id}  {history.verdict}"
            )
            lines.append(f"      {history.intent}")
            lines.append(
                f"      resolved on: {' -> '.join(history.strategies)}"
                f"   ({history.degraded_runs}/{history.runs} runs degraded)"
            )
            if history.ambiguous:
                lines.append(
                    f"      {history.ambiguous} run(s) matched more than one "
                    "control -- the locator still resolves and no longer "
                    "means one thing"
                )
        lines.append("")

    lines.append("EVERY STEP")
    for (capability, step_id), history in sorted(report.steps.items()):
        lines.append(
            f"  {capability:<38} {step_id:<4} "
            f"{'/'.join(history.strategies):<34} "
            f"{history.degraded_runs}/{history.runs} degraded   "
            f"{history.verdict}"
        )

    if report.recoveries:
        lines.append("")
        lines.append("RECOVERIES THAT FIRED")
        for code, count in sorted(report.recoveries.items()):
            lines.append(f"  {code:<24} {count} time(s) across {report.runs} runs")

    return "\n".join(lines)
