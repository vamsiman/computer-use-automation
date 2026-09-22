"""The command line, and the capability catalogue.

Two audiences, and the second is the interesting one.

A person uses this to run the demo: seed the data, serve the app, discover a
capability, approve it, replay it. That is ordinary.

The other audience is an *agent*. ``cua list`` and ``cua show`` print exactly
the contract an agent would consume to decide whether a capability is the one
it wants and how to call it -- a typed signature, a description written for a
caller rather than a maintainer, and the errors it can expect back. That is
the cheapest possible proof that a saved artifact really is an invocable
capability and not merely a list of clicks somebody recorded. If the catalogue
reads like an API, the artifact is one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from cua.artifact.models import Artifact
from cua.artifact.store import CapabilityStore, to_yaml

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Record a UI task once, replay it deterministically.",
)

DEFAULT_TARGET = "http://localhost:5000"


def echo(text: str = "") -> None:
    typer.echo(text)


def fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    """``--input member_id=10002`` into a dict.

    Values stay strings. The capability's contract says what each one must
    look like, and guessing that ``10002`` is an integer here would be this
    system doing the one thing it promises never to do: deciding what a
    caller meant.
    """
    values: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            fail(f"--input expects name=value, got {pair!r}")
        values[name.strip()] = value
    return values


def store_for(root: Path | None) -> CapabilityStore:
    return CapabilityStore(root) if root else CapabilityStore()


# --- the target application ----------------------------------------------


@app.command()
def seed() -> None:
    """Rebuild the demo credit union's database."""
    from targetapp import seed as seeder

    seeder.seed()
    echo("seeded targetapp/cu.db")


@app.command("serve-app")
def serve_app(
    tenant: str = typer.Option("base", help="base | riverbend"),
    port: int = typer.Option(5000),
    host: str = typer.Option("127.0.0.1"),
) -> None:
    """Run the demo application. Two tenants, one codebase."""
    os.environ["TENANT"] = tenant
    from targetapp.app import create_app

    echo(f"{tenant} on http://{host}:{port}  (ctrl-c to stop)")
    create_app().run(host=host, port=port, threaded=True)


# --- the catalogue -------------------------------------------------------


@app.command("list")
def list_capabilities(
    root: Path = typer.Option(None, help="Where the artifacts live."),
    as_json: bool = typer.Option(False, "--json", help="For an agent to read."),
) -> None:
    """Every capability, with its typed signature.

    The stretch goal of the brief's final section: saved artifacts exposed as
    a catalogue an agent could browse by name and call by contract.
    """
    artifacts = sorted(store_for(root).catalogue(), key=lambda a: a.capability.id)
    if as_json:
        echo(json.dumps([_summary(a) for a in artifacts], indent=2))
        return

    if not artifacts:
        echo("no capabilities yet -- run `cua discover`")
        return

    for artifact in artifacts:
        meta = artifact.capability
        flag = "" if artifact.is_approved else "  [draft]"
        typer.secho(f"{artifact.signature()}{flag}", bold=True)
        echo(f"    {meta.title}")
        echo(f"    v{meta.version} | {meta.app.vendor}/{meta.app.product}"
             f"{' | tenant ' + meta.tenant if meta.tenant else ''}")
        if artifact.outcomes:
            echo(f"    outcomes: {', '.join(o.code for o in artifact.outcomes)}")
        echo()


@app.command()
def show(
    capability_id: str,
    version: str = typer.Option(None, help="Defaults to the latest."),
    root: Path = typer.Option(None),
    raw: bool = typer.Option(False, "--raw", help="The artifact as stored."),
) -> None:
    """The full contract, as a caller would read it."""
    artifact = _load(store_for(root), capability_id, version)
    if raw:
        echo(to_yaml(artifact))
        return

    meta = artifact.capability
    typer.secho(artifact.signature(), bold=True)
    echo()
    echo(f"  {meta.title}")
    echo(f"  {meta.description}")
    echo()
    echo(f"  version    {meta.version} ({meta.status})")
    echo(f"  app        {meta.app.vendor}/{meta.app.product} {meta.app.version_range}")
    if meta.tenant:
        echo(f"  tenant     {meta.tenant}")
    echo(f"  requires   {'an authenticated session' if artifact.preconditions.authenticated else 'nothing'}")
    entry = artifact.preconditions.entry_checkpoint
    if entry is not None:
        echo(f"  starts on  {entry.target.describe()}")

    echo("\n  inputs")
    for name, spec in artifact.inputs.items():
        bits = [str(spec.type)]
        if spec.pattern:
            bits.append(spec.pattern)
        if not spec.required:
            bits.append("optional")
        if spec.sensitivity != "internal":
            bits.append(str(spec.sensitivity))
        echo(f"    {name:<18} {' | '.join(bits)}")
        if spec.description:
            echo(f"    {'':<18} {spec.description}")

    echo("\n  outputs")
    for name, spec in artifact.outputs.items():
        bits = [str(spec.type)]
        if spec.currency:
            bits.append(spec.currency)
        bits.append(str(spec.sensitivity))
        echo(f"    {name:<18} {' | '.join(bits)}")
        if spec.description:
            echo(f"    {'':<18} {spec.description}")

    if artifact.outcomes:
        # Not errors. Answers about the world that a caller branches on, and
        # the reason they are in the catalogue at all.
        echo("\n  outcomes a caller should expect")
        for outcome in artifact.outcomes:
            echo(f"    {outcome.code:<18} {outcome.message or ''}")

    if artifact.recoveries:
        echo("\n  handled internally, never surfaced")
        for recovery in artifact.recoveries:
            echo(f"    {recovery.code:<18} {recovery.strategy}"
                 f" (up to {recovery.max_occurrences}x)")

    echo(f"\n  steps ({len(artifact.steps)})")
    for step in artifact.steps:
        echo(f"    {step.id}  {step.action:<9} {step.intent}")

    prov = meta.provenance
    echo()
    if prov.model and prov.discovered_at:
        when = getattr(prov.discovered_at, "date", lambda: prov.discovered_at)()
        echo(f"  discovered by {prov.model} on {when} | "
             f"{prov.step_count_raw} actions distilled to "
             f"{prov.step_count_final}")
    else:
        echo("  hand written; no discovery run behind this one")


@app.command()
def approve(
    capability_id: str,
    version: str = typer.Option(None),
    root: Path = typer.Option(None),
) -> None:
    """Mark a draft as read and fit to run unattended.

    The only thing in the system that does this, and it is a person typing a
    command on purpose. A capability that promoted itself would make the
    draft status decorative.
    """
    library = store_for(root)
    artifact = _load(library, capability_id, version)
    if artifact.is_approved:
        echo(f"{artifact.ref} is already approved")
        return
    artifact.capability.status = "approved"
    path = library.save(artifact)
    echo(f"approved {artifact.ref} -> {path}")


# --- running things ------------------------------------------------------


@app.command()
def discover(
    goal: str = typer.Option(..., help="What to work out how to do."),
    target: str = typer.Option(DEFAULT_TARGET),
    input: list[str] = typer.Option([], help="name=value, repeatable."),
    capability_id: str = typer.Option(None, help="Defaults to a slug of the goal."),
    root: Path = typer.Option(None),
    headed: bool = typer.Option(True, help="Watch it work."),
) -> None:
    """One run with a model in the loop, distilled into a draft capability."""
    from cua.artifact.models import AppRef
    from cua.discovery import AnthropicClient, DiscoveryAgent, annotate
    from cua.discovery import apply_annotation, distil, slug_from_goal
    from cua.evidence import Recorder
    from cua.session import Credentials, SessionManager, authenticate

    if not os.environ.get("ANTHROPIC_API_KEY"):
        fail("ANTHROPIC_API_KEY is not set; discovery is the one part that needs it")

    values = parse_inputs(input)
    manager = SessionManager()
    session = manager.create(target, headless=not headed)
    try:
        authenticate(session.surface, Credentials.from_env())
        recorder = Recorder.start(inputs=values, mode="discovery", goal=goal)
        client = AnthropicClient()
        agent = DiscoveryAgent(session.surface, client, recorder=recorder)

        echo(f"model {agent.model}\ngoal  {goal}\n")
        trace = agent.run(goal, values)

        for record in trace.records:
            mark = "ok  " if record.ok else "FAIL"
            action = record.action.type if record.action else record.tool
            echo(f"  {record.seq:2d} {mark} {action}  {record.intent or ''}")

        echo(f"\nended: {trace.terminal}")
        if not trace.succeeded:
            fail(f"the run did not finish: {trace.summary}")

        artifact = distil(
            trace,
            capability_id=capability_id or slug_from_goal(goal),
            app=AppRef(vendor="meridian", product="MemberConsole"),
        )
        artifact = apply_annotation(artifact, annotate(trace, artifact, client))
        path = store_for(root).save(artifact)

        echo(f"\n{artifact.signature()}")
        echo(f"{artifact.capability.provenance.step_count_raw} actions distilled "
             f"to {len(artifact.steps)} steps")
        echo(f"saved {path} (draft -- read it, then `cua approve`)")
        echo(f"evidence {recorder.dir}")
    finally:
        manager.close_all()


@app.command()
def replay(
    capability_id: str,
    input: list[str] = typer.Option([], help="name=value, repeatable."),
    target: str = typer.Option(DEFAULT_TARGET),
    version: str = typer.Option(None),
    tenant: str = typer.Option(None, help="Apply this tenant's overrides."),
    root: Path = typer.Option(None),
    allow_draft: bool = typer.Option(False, help="Run a capability nobody has read."),
    headed: bool = typer.Option(True),
    evidence: bool = typer.Option(True, help="Write an evidence bundle."),
) -> None:
    """Run a saved capability. No model, no improvisation."""
    from cua.evidence import Recorder
    from cua.replay import ReplayEngine
    from cua.session import Credentials, Handoff, SessionManager, authenticate

    library = store_for(root)
    artifact = _load(library, capability_id, version, tenant=tenant)
    values = parse_inputs(input)

    manager = SessionManager()
    session = manager.create(target, headless=not headed)
    try:
        credentials = Credentials.from_env()
        # The sign-in screen is the one place legacy software says what
        # version it is, and the bootstrap is the only part of a run that
        # always sees it.
        signed_in = authenticate(session.surface, credentials)
        session.start()

        recorder = (
            Recorder.start(artifact=artifact, inputs=values, tenant=tenant)
            if evidence
            else None
        )
        result = ReplayEngine(
            session.controlled,
            artifact,
            recorder=recorder,
            allow_draft=allow_draft,
            session_id=session.id,
            app_version=signed_in.app_version,
            reauthenticate=lambda: authenticate(session.surface, credentials),
            escalate=Handoff(session, recorder=recorder, inputs=values,
                             capability=artifact.ref),
        ).run(values)

        _report(result)
        if recorder is not None:
            recorder.finish(result)
            echo(f"evidence {recorder.dir}")
        raise typer.Exit(code=0 if result.ok else 1)
    finally:
        manager.close_all()


@app.command()
def drift(
    root: Path = typer.Option(None, help="Where the evidence bundles live."),
) -> None:
    """What the tier logs have been saying across every run.

    Every replay records which rule found each control, on successful runs as
    much as failed ones, because a capability sliding onto its third fallback
    is degrading weeks before it breaks. This reads that back.

    It is a report rather than a monitor. No daemon, no thresholds, no
    alerting: the point is that the answer is already in the evidence on disk,
    and what was missing was somebody reading it.
    """
    from cua.evidence.drift import DEFAULT_ROOT, collect, render

    echo(render(collect(root or DEFAULT_ROOT)))


@app.command()
def operator(
    port: int = typer.Option(5100),
    host: str = typer.Option("127.0.0.1"),
) -> None:
    """The console, on its own.

    Of limited use standing alone -- it reads sessions out of a manager that
    lives in the process driving the browsers, so run it from the same
    process as a replay to see anything. Here so the command exists and the
    page can be looked at.
    """
    from cua.operator import ConsoleServer
    from cua.session import InterventionRegistry, SessionManager

    server = ConsoleServer(SessionManager(), InterventionRegistry(), host=host, port=port)
    echo(f"console on {server.start()}  (ctrl-c to stop)")
    try:
        while True:
            import time

            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


# --- reporting -----------------------------------------------------------


def _report(result: Any) -> None:
    """Say which of the four kinds of answer this is, in as many words.

    A caller that has to infer "not found" from an empty outputs dict is a
    caller that will eventually treat it as a balance.
    """
    echo()
    if result.kind == "Success":
        typer.secho("Success", fg=typer.colors.GREEN, bold=True)
        for name, value in result.outputs.items():
            echo(f"  {name} = {value}")
    elif result.kind == "BusinessOutcome":
        typer.secho(f"{result.code}", fg=typer.colors.YELLOW, bold=True)
        echo(f"  {result.message or 'the application gave a definite answer'}")
        echo("  (this is an answer about the world, not a failure)")
    elif result.kind == "Escalated":
        typer.secho("Escalated", fg=typer.colors.MAGENTA, bold=True)
        echo(f"  {result.reason}")
        if result.observed:
            echo(f"  on screen: {result.observed}")
        echo("  the session is still open; `cua operator` to deal with it")
    else:
        typer.secho(f"Failed ({result.category})", fg=typer.colors.RED, bold=True)
        echo(f"  step      {result.step_id or '-'}  {result.intent}")
        echo(f"  expected  {result.expected}")
        echo(f"  observed  {result.observed}")
        if result.detail:
            echo(f"  {result.detail}")

    if result.tier_log:
        echo(f"\n  {len(result.tier_log)} step(s), locators resolved on:")
        for entry in result.tier_log:
            mark = "  <- degraded" if entry.degraded else ""
            echo(f"    {entry.step_id}  {entry.strategy}{mark}")
    if result.recoveries:
        echo(f"  recovered from: {', '.join(result.recoveries)}")


def _summary(artifact: Artifact) -> dict[str, Any]:
    meta = artifact.capability
    return {
        "id": meta.id,
        "version": meta.version,
        "status": meta.status,
        "title": meta.title,
        "description": meta.description,
        "signature": artifact.signature(),
        "inputs": {
            name: {
                "type": str(spec.type),
                "required": spec.required,
                "pattern": spec.pattern,
                "description": spec.description,
            }
            for name, spec in artifact.inputs.items()
        },
        "outputs": {
            name: {"type": str(spec.type), "description": spec.description}
            for name, spec in artifact.outputs.items()
        },
        "outcomes": [
            {"code": o.code, "message": o.message} for o in artifact.outcomes
        ],
    }


def _load(
    library: CapabilityStore,
    capability_id: str,
    version: str | None,
    tenant: str | None = None,
) -> Artifact:
    from cua.artifact.overrides import for_tenant
    from cua.artifact.store import CapabilityNotFound

    try:
        artifact = library.load(capability_id, version)
    except CapabilityNotFound as exc:
        known = library.capability_ids() or ["(none)"]
        fail(f"{exc}; known: " + ", ".join(known))
        raise SystemExit(2)  # unreachable: fail() exits

    return for_tenant(library, artifact, tenant) if tenant else artifact


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()
