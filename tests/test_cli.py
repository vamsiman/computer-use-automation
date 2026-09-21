"""The command line, and the catalogue an agent would read.

No browser and no model: every command that touches either is tested where it
lives. What is tested here is the surface a person and an agent actually
touch -- what `cua list` promises, what `cua show` discloses, and the places
the CLI must refuse.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cua.artifact.store import CapabilityStore
from cua.cli.main import app, parse_inputs

runner = CliRunner()

CAPABILITY = "member.read_savings_balance"


def run(*args):
    return runner.invoke(app, list(args))


# --- the catalogue -------------------------------------------------------


def test_the_catalogue_prints_a_typed_signature():
    """The §8 stretch goal, and the cheapest proof the artifact really is an
    invocable capability: if the catalogue reads like an API, it is one."""
    result = run("list")

    assert result.exit_code == 0
    assert f"{CAPABILITY}(member_id: string)" in result.output
    assert "savings_balance: money" in result.output


def test_the_catalogue_is_machine_readable():
    """The same information for the caller that is not a person."""
    result = run("list", "--json")
    entries = json.loads(result.output)

    entry = next(e for e in entries if e["id"] == CAPABILITY)
    assert entry["signature"].startswith(CAPABILITY)
    assert entry["inputs"]["member_id"]["pattern"] == "^[0-9]{5}$"
    assert entry["outputs"]["savings_balance"]["type"] == "money"
    # The outcomes are part of the contract, not incidental detail: a caller
    # needs to know it can be told "no such member" without that being a
    # failure.
    assert {o["code"] for o in entry["outcomes"]} >= {"MEMBER_NOT_FOUND"}


def test_show_discloses_what_a_caller_has_to_know():
    result = run("show", CAPABILITY)

    assert result.exit_code == 0
    for expected in (
        "an authenticated session",  # the precondition
        "MEMBER_NOT_FOUND",  # an answer, not an error
        "SYSTEM_NOTICE",  # handled internally, never surfaced
        "^[0-9]{5}$",  # the contract on the input
    ):
        assert expected in result.output, expected


def test_show_separates_outcomes_from_recoveries():
    """The distinction the whole error taxonomy rests on, made visible in the
    one place a caller reads before writing any code against it."""
    result = run("show", CAPABILITY)

    outcomes = result.output.index("outcomes a caller should expect")
    recoveries = result.output.index("handled internally, never surfaced")
    assert outcomes < recoveries
    assert "PERMISSION_DENIED" in result.output[outcomes:recoveries]


def test_an_unknown_capability_says_what_there_is():
    result = run("show", "member.do_something_else")

    assert result.exit_code == 2
    assert CAPABILITY in result.output


def test_raw_prints_the_artifact_as_stored():
    """Reviewable is a requirement, so there is a command that shows the
    document a reviewer would be approving."""
    result = run("show", CAPABILITY, "--raw")
    assert "schema_version" in result.output
    assert "steps:" in result.output


# --- inputs --------------------------------------------------------------


def test_inputs_are_parsed_but_never_interpreted():
    """`10002` stays a string.

    Deciding it is an integer here would be the CLI doing the one thing this
    system promises never to do: guessing what a caller meant. The
    capability's own contract says what the value has to look like.
    """
    assert parse_inputs(["member_id=10002"]) == {"member_id": "10002"}
    assert parse_inputs(["note=a=b"]) == {"note": "a=b"}


def test_a_malformed_input_is_refused():
    result = run("replay", CAPABILITY, "--input", "member_id")
    assert result.exit_code == 2
    assert "name=value" in result.output


# --- approval ------------------------------------------------------------


def test_approving_is_something_a_person_does(tmp_path):
    """The only thing in the system that promotes a draft, and it is a human
    typing a command. A capability that approved itself would make the draft
    status decorative."""
    library = CapabilityStore(tmp_path)
    artifact = CapabilityStore().load(CAPABILITY)
    artifact.capability.status = "draft"
    library.save(artifact)

    assert "[draft]" in run("list", "--root", str(tmp_path)).output

    result = run("approve", CAPABILITY, "--root", str(tmp_path))
    assert result.exit_code == 0
    assert library.load(CAPABILITY).is_approved
    assert "[draft]" not in run("list", "--root", str(tmp_path)).output


def test_approving_twice_is_not_an_error(tmp_path):
    library = CapabilityStore(tmp_path)
    library.save(CapabilityStore().load(CAPABILITY))
    assert run("approve", CAPABILITY, "--root", str(tmp_path)).exit_code == 0


def test_an_empty_catalogue_says_what_to_do(tmp_path):
    assert "cua discover" in run("list", "--root", str(tmp_path)).output


# --- the tenant flag -----------------------------------------------------


def test_a_tenant_is_resolved_through_its_override():
    """`--tenant` is not a label on the run; it changes which document runs."""
    from cua.artifact.overrides import for_tenant

    library = CapabilityStore()
    base = library.load(CAPABILITY)
    merged = for_tenant(library, base, "cu-riverbend")
    assert merged.steps[1].target.primary.label == "Member Number:"
    assert merged.signature() == base.signature()


# --- discovery's one hard requirement ------------------------------------


def test_discovery_refuses_without_a_key(monkeypatch):
    """The one command that cannot work offline says so immediately, rather
    than opening a browser and failing later."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = run("discover", "--goal", "anything")

    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.output


def test_every_command_is_reachable():
    """A CLI whose help does not list a command is a CLI that command does
    not exist in, as far as anybody using it is concerned."""
    output = run("--help").output
    for command in (
        "seed", "serve-app", "list", "show", "approve",
        "discover", "replay", "operator",
    ):
        assert command in output, command
