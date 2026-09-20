"""The run's account of itself.

The brief asks for evidence, and the reason is not that screenshots look good
in a demo. In a regulated back office, "the automation opened this account" is
a claim somebody will eventually have to substantiate -- to an auditor, to a
member, occasionally to a regulator -- using whatever was written down at the
time. So the bundle is built to answer the questions that get asked
afterwards: what did it do, what did it see when it decided to, who was in
control, and how sure was it of what it was pointing at.

That last one is why **the resolved locator tier is a first-class field** in
every step record rather than a debug line. A capability that quietly slid
from tier 0 to tier 3 six weeks ago is still passing its tests and is already
broken; the tier log is the only place that shows up before it does.

One structural rule holds the safety story together: every byte written here
goes through :class:`~cua.evidence.redact.Redactor`. There is no second path
to disk, which is what makes "nothing sensitive reaches the log" a property of
the design rather than a promise about everyone's future diligence.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from cua.evidence.redact import Redactor

DEFAULT_ROOT = Path("evidence") / "runs"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def jsonable(value: Any) -> Any:
    """Flatten to JSON-compatible types, before redaction rather than after.

    Redaction has to run over plain strings, dicts and lists or it will walk
    straight past a sensitive value hiding inside a model instance.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return str(value)


@dataclass(frozen=True)
class EvidenceConfig:
    """What may be *kept*, as opposed to what may be written.

    Screenshots are the honest weak point of this design. Redaction works on
    structured records, and a PNG is not one: a balance rendered on screen is
    in the pixels whatever the artifact declared about it. Rather than pretend
    otherwise, image capture is a switch, so a deployment handling real member
    data can turn it off and still get a complete structured log.
    """

    root: Path = DEFAULT_ROOT
    #: Per-step and failure PNGs. Off means no image is ever written.
    screenshots: bool = True
    #: The extra dump taken at the point of failure: tree, page source.
    failure_bundle: bool = True
    #: Raw page source inside that bundle. Separately gated because markup
    #: carries values the accessibility tree elides -- full account numbers in
    #: hidden fields, for one.
    page_source: bool = True

    @classmethod
    def from_env(cls) -> "EvidenceConfig":
        return cls(
            root=Path(os.environ.get("CUA_EVIDENCE_ROOT", str(DEFAULT_ROOT))),
            screenshots=_flag("CUA_EVIDENCE_SCREENSHOTS", True),
            failure_bundle=_flag("CUA_EVIDENCE_FAILURE_BUNDLE", True),
            page_source=_flag("CUA_EVIDENCE_PAGE_SOURCE", True),
        )

    def as_record(self) -> dict[str, Any]:
        """Written into ``meta.json`` so an absence is explainable.

        A reader finding no screenshots should be able to tell that retention
        was switched off, rather than concluding the run never took any.
        """
        return {
            "screenshots": self.screenshots,
            "failure_bundle": self.failure_bundle,
            "page_source": self.page_source,
        }


@dataclass
class StepRecord:
    """One executed step, as it will appear in ``run.jsonl``.

    Mutable on purpose: the recorder hands it out at the start of a step and
    the engine fills in what it learns -- which tier resolved, what came back
    -- while the step is still running. The alternative is assembling the
    record afterwards from variables, which is how half of it goes missing on
    the path that raised.
    """

    step_id: str
    intent: str
    action: str
    target: str | None = None
    #: Which locator tier resolved the target. 0 is best; a rising number is
    #: the drift signal.
    tier: int | None = None
    degraded: bool = False
    ok: bool = True
    error: str | None = None
    #: Values this step read, keyed by declared output name -- so the
    #: classification map applies to them by name.
    extracted: dict[str, Any] = field(default_factory=dict)
    #: Declared business outcome detected while running this step, if any.
    outcome: str | None = None
    #: Which policy rule spoke, from the engine's Decision.
    policy_rule: str | None = None
    screenshot: str | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        record = {
            "step_id": self.step_id,
            "intent": self.intent,
            "action": self.action,
            "target": self.target,
            "tier": self.tier,
            "degraded": self.degraded,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
        }
        if self.error:
            record["error"] = self.error
        if self.extracted:
            record["extracted"] = self.extracted
        if self.outcome:
            record["outcome"] = self.outcome
        if self.policy_rule:
            record["policy_rule"] = self.policy_rule
        if self.screenshot:
            record["screenshot"] = self.screenshot
        record.update(self.extra)
        return record


def new_run_id(capability_id: str | None = None) -> str:
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    slug = (capability_id or "run").replace(".", "-")
    return f"{stamp}-{slug}-{uuid4().hex[:6]}"


class Recorder:
    """Writes one run's evidence bundle.

    Layout, under ``evidence/runs/<run_id>/``::

        run.jsonl      one record per event, in the order they happened
        steps/NNN.png  a frame per step, when retention is on
        failure/       tree, page source and image, on failure only
        result.json    the typed result the caller received
        meta.json      capability, tenant, inputs, timings, retention flags

    ``run.jsonl`` is line-delimited rather than one JSON document because a
    run that dies mid-step should still leave a readable log up to the point
    it died, and that is precisely the run whose log matters most.
    """

    def __init__(
        self,
        run_id: str,
        directory: Path,
        redactor: Redactor,
        config: EvidenceConfig,
        meta: dict[str, Any],
    ) -> None:
        self.run_id = run_id
        self.dir = directory
        self.redactor = redactor
        self.config = config
        self.started_at = _now()
        self._meta = meta
        self._seq = 0
        self._shot_seq = 0
        self.dir.mkdir(parents=True, exist_ok=True)
        self._write_meta()

    # --- lifecycle -------------------------------------------------------

    @classmethod
    def start(
        cls,
        *,
        artifact=None,
        inputs: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        mode: str = "replay",
        tenant: str | None = None,
        model: str | None = None,
        goal: str | None = None,
        config: EvidenceConfig | None = None,
        redactor: Redactor | None = None,
    ) -> "Recorder":
        config = config or EvidenceConfig.from_env()
        capability_id = artifact.capability.id if artifact else None

        if redactor is None:
            redactor = (
                Redactor.for_artifact(artifact) if artifact else Redactor.empty()
            )
        if inputs:
            # Bind before anything is written, so the very first record is
            # already covered.
            redactor = redactor.bind(inputs)

        run_id = run_id or new_run_id(capability_id)
        meta: dict[str, Any] = {
            "run_id": run_id,
            "mode": mode,
            "tenant": tenant,
            "model": model,
            "goal": goal,
            "inputs": jsonable(dict(inputs or {})),
            "retention": config.as_record(),
        }
        if artifact is not None:
            meta["capability"] = {
                "id": artifact.capability.id,
                "version": artifact.capability.version,
                "status": artifact.capability.status,
                "title": artifact.capability.title,
                "tenant": artifact.capability.tenant,
            }
            meta["schema_version"] = artifact.schema_version
        return cls(run_id, config.root / run_id, redactor, config, meta)

    @property
    def log_path(self) -> Path:
        return self.dir / "run.jsonl"

    @property
    def steps_dir(self) -> Path:
        return self.dir / "steps"

    @property
    def failure_dir(self) -> Path:
        return self.dir / "failure"

    # --- the one path to disk -------------------------------------------

    def _write_json(self, path: Path, payload: Any) -> Path:
        safe = self.redactor.structure(jsonable(payload))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(safe, indent=2, sort_keys=False), encoding="utf-8"
        )
        return path

    def _write_text(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.text(content), encoding="utf-8")
        return path

    def _write_meta(self) -> None:
        self._write_json(
            self.dir / "meta.json",
            {**self._meta, "started_at": self.started_at},
        )

    def learn(self, values: Mapping[str, Any]) -> None:
        """Take note of a sensitive value the run has just read.

        Declared classification protects a value arriving under its own name.
        But the moment a step extracts a member's name, that same string
        starts turning up in places with no name attached -- a failure reason,
        a checkpoint message, the tree dumped when the next step goes wrong.
        So once the run has *seen* a value the artifact classified, it is
        scrubbed from everything written afterwards.

        The ordering is the honest limit: this can only protect records
        written after the value was read. A page dump taken earlier is raw
        perception, and a member detail screen holds plenty the artifact never
        declared -- an address, a phone number. That is why the failure bundle
        has its own retention switch rather than being assumed safe.
        """
        self.redactor = self.redactor.bind(values)

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one record to ``run.jsonl``. Everything else funnels here."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": _now(),
            "kind": kind,
            **fields,
        }
        safe = self.redactor.structure(jsonable(record))
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=False) + "\n")
        return safe

    # --- what the engines call ------------------------------------------

    @contextmanager
    def step(
        self,
        step_id: str,
        intent: str,
        action: str,
        target: str | None = None,
    ) -> Iterator[StepRecord]:
        """Record one step, timing it and surviving an exception.

        A step that raises is the one most worth having in the log, so the
        record is written on the way out either way and the exception is
        re-raised untouched. Swallowing it here would turn a crash into a
        silently missing step.
        """
        record = StepRecord(
            step_id=step_id, intent=intent, action=action, target=target
        )
        began = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.ok = False
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_ms = int((time.perf_counter() - began) * 1000)
            if record.extracted:
                self.learn(record.extracted)
            self.event("step", **record.as_record())

    def recovery(self, code: str, strategy: str, occurrence: int, budget: int) -> None:
        """A declared condition handled internally.

        The caller never hears about these -- dismissing a maintenance notice
        is not news. But the count against budget belongs in evidence, because
        a recovery that fires twice as often this month as last is the first
        sign the application changed under us.
        """
        self.event(
            "recovery",
            code=code,
            strategy=strategy,
            occurrence=occurrence,
            budget=budget,
        )

    def outcome(self, code: str, message: str | None = None) -> None:
        """A declared business result. Not a failure, and logged as such."""
        self.event("outcome", code=code, message=message)

    def policy(self, decision: Any, action: str, target: str | None = None) -> None:
        self.event(
            "policy",
            action=action,
            target=target,
            permitted=getattr(decision, "permitted", None),
            rule=getattr(decision, "rule", None),
            reason=getattr(decision, "reason", None),
        )

    def control(self, frm: Any, to: Any, owner: Any, reason: str = "") -> None:
        """Who held the session, and when it changed hands."""
        self.event(
            "control", frm=jsonable(frm), to=jsonable(to),
            owner=jsonable(owner), reason=reason,
        )

    def human_actions(self, actions: Any) -> None:
        """What the operator did while they held the session.

        Captured so a handoff is auditable rather than a gap in the record.
        Values go through the same redactor as everything else: a human typing
        into a field declared ``secret`` does not make it less secret.
        """
        self.event("human_actions", actions=jsonable(actions))

    def screenshot(self, surface: Any, label: str | None = None) -> str | None:
        """Capture a frame, if retention allows. Returns a relative path."""
        if not self.config.screenshots:
            return None
        self._shot_seq += 1
        name = label or f"{self._shot_seq:03d}"
        path = self.steps_dir / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            surface.screenshot(str(path))
        except Exception as exc:  # evidence must never break the run
            self.event("evidence_error", what="screenshot", error=str(exc))
            return None
        return str(path.relative_to(self.dir).as_posix())

    def failure(
        self,
        surface: Any,
        *,
        step_id: str | None = None,
        reason: str = "",
        category: Any = None,
    ) -> dict[str, str]:
        """Everything worth having about the moment it went wrong.

        Deliberately more than a step record: a failure is diagnosed later, by
        someone who was not watching, and the accessibility tree at the instant
        of the failure is the single most useful thing they can be handed --
        it shows what the system could actually see, which is usually the
        difference between "the app changed" and "the locator was wrong".
        """
        captured: dict[str, str] = {}
        if self.config.failure_bundle:
            self.failure_dir.mkdir(parents=True, exist_ok=True)
            try:
                snapshot = surface.observe()
                path = self._write_text(
                    self.failure_dir / "a11y.txt", snapshot.text_view()
                )
                captured["a11y"] = path.name
                captured["location"] = snapshot.location
            except Exception as exc:
                self.event("evidence_error", what="a11y", error=str(exc))

            source = getattr(surface, "page_source", None)
            if self.config.page_source and callable(source):
                try:
                    path = self._write_text(
                        self.failure_dir / "page.html", source()
                    )
                    captured["page_source"] = path.name
                except Exception as exc:
                    self.event("evidence_error", what="page_source", error=str(exc))

        if self.config.screenshots:
            path = self.failure_dir / "screenshot.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                surface.screenshot(str(path))
                captured["screenshot"] = path.name
            except Exception as exc:
                self.event("evidence_error", what="screenshot", error=str(exc))

        self.event(
            "failure",
            step_id=step_id,
            reason=reason,
            category=jsonable(category),
            captured=captured,
        )
        return captured

    def finish(self, result: Any) -> Path:
        """Write the typed result and close the bundle."""
        finished = _now()
        outputs = getattr(result, "outputs", None)
        if outputs is None and isinstance(result, Mapping):
            outputs = result.get("outputs")
        if isinstance(outputs, Mapping):
            self.learn(outputs)
        self.event("finished", result=type(result).__name__)
        self._meta["finished_at"] = finished
        self._meta["duration_ms"] = int(
            (finished - self.started_at).total_seconds() * 1000
        )
        self._meta["events"] = self._seq
        self._meta["result"] = type(result).__name__
        self._write_meta()
        return self._write_json(self.dir / "result.json", result)

    def __repr__(self) -> str:
        return f"Recorder(run_id={self.run_id!r}, dir={str(self.dir)!r})"
