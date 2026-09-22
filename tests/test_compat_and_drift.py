"""Two things the design promised and the code did not do.

`AppRef.version_range` was declared in every artifact, printed by `cua show`,
and checked nowhere — its own docstring said "a capability recorded on 4.2
quietly failing on 5.0 is worse than one that refuses to run", and nothing
made that true.

The tier log had the same shape of gap. Every run recorded which rule
resolved each step, including the runs that worked, and nothing read it back
across runs — so the drift signal was real in principle and invisible in
practice.
"""

from __future__ import annotations

import json

import pytest

from cua.artifact.compat import is_compatible, parse_version
from cua.artifact.store import CapabilityStore
from cua.evidence.drift import collect, render
from cua.replay import Failure, ReplayEngine
from cua.types import FailureCategory
from tests.test_replay import FAST_MS, FakeSurface, engine_policy, screen

policy = engine_policy


@pytest.fixture
def artifact():
    loaded = CapabilityStore().load("member.read_savings_balance")
    for step in loaded.steps:
        if step.checkpoint is not None:
            step.checkpoint.timeout_ms = FAST_MS
    return loaded


# --- is this capability allowed to run here? ------------------------------


@pytest.mark.parametrize(
    "version_range, app_version, allowed",
    [
        (">=4.2 <5.0", "MemberConsole 4.3.1", True),
        (">=4.2 <5.0", "MemberConsole 5.1.0", False),
        (">=4.2 <5.0", "MemberConsole 4.1.9", False),
        ("*", "anything at all", True),
        # Unknown is permitted, and this is the important row: most
        # applications never say what they are, and a system that refused to
        # work whenever it could not identify the software would be useless in
        # exactly the legacy estate it is built for.
        (">=4.2 <5.0", None, True),
        (">=4.2 <5.0", "no numbers here", True),
        # A range nobody can parse is a documentation string, not a gate.
        # Turning a typo into an outage is the wrong failure mode.
        ("whatever", "4.3", True),
    ],
)
def test_compatibility_is_conservative_in_one_direction(
    version_range, app_version, allowed
):
    assert is_compatible(version_range, app_version) is allowed


def test_a_version_is_found_in_whatever_the_application_said():
    """Software announces itself in prose. Pulling the first dotted number
    out is cruder than a real parser and far likelier to work on the sort of
    application this is aimed at."""
    assert str(parse_version("MemberConsole 4.3.1")) == "4.3.1"
    assert str(parse_version("Release 4.3.1 (build 9912)")) == "4.3.1"
    assert str(parse_version("v2.0")) == "2.0"
    assert parse_version("no version here") is None
    assert parse_version("") is None


def test_a_capability_refuses_an_application_it_was_not_recorded_for(
    artifact, policy
):
    """The docstring's promise, kept. Nothing is broken here -- the capability
    is being pointed at software it never saw, and saying so beats finding out
    four steps in."""
    surface = FakeSurface([screen("Member Search")])
    result = ReplayEngine(
        surface, artifact, policy=policy, app_version="MemberConsole 5.1.0"
    ).run({"member_id": "10001"})

    assert isinstance(result, Failure)
    assert result.category is FailureCategory.INCOMPATIBLE_APP
    assert surface.acted == [], "nothing should have been done to the application"
    assert "5.1.0" in result.observed
    assert "re-record" in (result.detail or "").lower()


def test_the_version_check_runs_before_the_contract_check(artifact, policy):
    """Both are wrong; only one of them is the caller's fault.

    Telling somebody their arguments are malformed, when the real problem is
    that this capability cannot run against this software at all, sends them
    to fix the wrong thing.
    """
    result = ReplayEngine(
        FakeSurface([screen("Member Search")]),
        artifact,
        policy=policy,
        app_version="MemberConsole 9.0",
    ).run({"member_id": "not-a-member-number"})

    assert result.category is FailureCategory.INCOMPATIBLE_APP


def test_an_unknown_version_does_not_stop_anything(artifact, policy):
    surface = FakeSurface(
        [screen("Member Search"), screen("Member Search"), screen("Member Details")],
        values={"member_name": "Dana", "savings_balance": "4,210.33"},
    )
    result = ReplayEngine(surface, artifact, policy=policy).run(
        {"member_id": "10001"}
    )
    assert result.ok, getattr(result, "observed", result)


# --- reading the signal every run has been writing ------------------------


def bundle(tmp_path, run_id, *, tier_log=(), recoveries=(), kind="Success"):
    directory = tmp_path / run_id
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(
        json.dumps(
            {
                "capability": "member.read_savings_balance@1.0.0",
                "kind": kind,
                "tier_log": list(tier_log),
                "recoveries": list(recoveries),
            }
        ),
        encoding="utf-8",
    )
    return directory


def entry(step_id, strategy, tier, degraded=False, match_count=1):
    return {
        "step_id": step_id,
        "intent": f"do {step_id}",
        "strategy": strategy,
        "tier": tier,
        "degraded": degraded,
        "match_count": match_count,
    }


def test_a_step_that_slid_to_a_lower_rule_is_called_out(tmp_path):
    """The whole point. It worked every time, and it is not working the way
    it used to -- which is the difference between "it still works" and "it
    still works, and something changed underneath it"."""
    bundle(tmp_path, "20260101T000000Z-a", tier_log=[entry("s2", "label_proximity", 0)])
    bundle(tmp_path, "20260201T000000Z-b", tier_log=[entry("s2", "label_proximity", 0)])
    bundle(
        tmp_path,
        "20260301T000000Z-c",
        tier_log=[entry("s2", "region_path", 1, degraded=True)],
    )

    report = collect(tmp_path)
    history = report.steps[("member.read_savings_balance@1.0.0", "s2")]

    assert history.verdict == "WORSENING"
    assert history.strategies == ["label_proximity", "region_path"]
    assert history.runs == 3
    assert history.degraded_runs == 1
    assert history in report.concerning


def test_a_healthy_step_is_not_reported_as_a_problem(tmp_path):
    for n in range(3):
        bundle(tmp_path, f"2026010{n}T000000Z-x", tier_log=[entry("s3", "role_name", 0)])

    report = collect(tmp_path)
    assert report.steps[("member.read_savings_balance@1.0.0", "s3")].verdict == "healthy"
    assert report.concerning == []


def test_a_locator_that_stopped_meaning_one_thing_is_the_loudest(tmp_path):
    """The dangerous one, and the reason `match_count` is collected at all.

    A locator that used to identify one control and now matches several still
    resolves, still returns a value, and is no longer pointing at anything in
    particular. It does not look like a failure anywhere else.
    """
    bundle(tmp_path, "20260101T000000Z-a", tier_log=[entry("s5", "row_cell", 0)])
    bundle(
        tmp_path,
        "20260201T000000Z-b",
        tier_log=[entry("s5", "row_cell", 0, match_count=3)],
    )

    report = collect(tmp_path)
    history = report.steps[("member.read_savings_balance@1.0.0", "s5")]
    assert history.ambiguous == 1
    assert history.verdict == "AMBIGUOUS"
    assert "more than one" in render(report)


def test_a_recovery_firing_twice_counts_as_twice(tmp_path):
    """`SYSTEM_NOTICEx2` is one log line and two events, and the number that
    matters for drift is the second one."""
    bundle(tmp_path, "20260101T000000Z-a", recoveries=["SYSTEM_NOTICEx2"])
    bundle(tmp_path, "20260102T000000Z-b", recoveries=["SYSTEM_NOTICEx1"])

    assert collect(tmp_path).recoveries["SYSTEM_NOTICE"] == 3


def test_a_step_that_never_resolved_says_so(tmp_path):
    """"None" in a drift report reads like a bug in the report rather than a
    fact about the run."""
    bundle(
        tmp_path,
        "20260101T000000Z-a",
        kind="Failure",
        tier_log=[{"step_id": "s5", "strategy": None, "tier": None}],
    )
    assert "unresolved" in render(collect(tmp_path))
    assert "None" not in render(collect(tmp_path))


def test_an_empty_evidence_directory_says_what_to_do(tmp_path):
    assert "run something first" in render(collect(tmp_path))


def test_the_report_counts_every_kind_of_result(tmp_path):
    bundle(tmp_path, "20260101T000000Z-a", kind="Success")
    bundle(tmp_path, "20260102T000000Z-b", kind="BusinessOutcome")
    bundle(tmp_path, "20260103T000000Z-c", kind="Failure")

    report = collect(tmp_path)
    assert report.runs == 3
    assert report.results == {"Success": 1, "BusinessOutcome": 1, "Failure": 1}


def test_an_unreadable_bundle_does_not_stop_the_report(tmp_path):
    """A half-written bundle is what you get when a run died mid-step, and
    that is precisely the run somebody is trying to understand."""
    bundle(tmp_path, "20260101T000000Z-a", tier_log=[entry("s2", "role_name", 0)])
    broken = tmp_path / "20260102T000000Z-b"
    broken.mkdir()
    (broken / "result.json").write_text("{not json", encoding="utf-8")

    assert collect(tmp_path).runs == 1
