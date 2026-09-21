"""One capability, two deployments, measured rather than asserted.

This is the file that decides whether the tiered locator chain earns its
cost. Everything else about it is argument; this is evidence.

The experiment is three runs of the *same* artifact:

1. against **base** -- the deployment it was written for
2. against **riverbend** with **no override** -- a real deployment nobody
   prepared it for, which calls the field "Member Number" and puts its
   Balance column in a different place
3. against **riverbend with its override** -- the repair

What has to be true for the design to be worth anything: run 2 **succeeds**,
and says it degraded. A chain that failed there would mean the fallbacks are
decoration; a chain that succeeded silently would mean the tier log is not a
drift signal.
"""

from __future__ import annotations

import socket
import threading

import pytest

from cua.artifact.overrides import for_tenant
from cua.artifact.store import CapabilityStore
from cua.policy import Allowlist, Policy, PolicyEngine, RiskPolicy
from cua.replay import ReplayEngine, Success
from cua.session import Credentials, SessionManager, authenticate
from cua.types import ActionType, LocatorStrategy, RiskLevel
from targetapp import exceptional, seed
from targetapp.app import APP_PASS, APP_USER, create_app

pytestmark = pytest.mark.browser

CREDENTIALS = Credentials(user=APP_USER, password=APP_PASS)
TENANT = "cu-riverbend"

#: 10001, whose savings balance is the number all three runs must return --
#: the same answer through three different routes to it.
MEMBER = "10001"
BALANCE = "4210.33"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(tenant: str):
    """One app per tenant, because TENANT is read at startup.

    Two processes would be more faithful still; two apps in one process is
    the same configuration difference with none of the ceremony, and what is
    under test is the automation, not the deployment mechanism.
    """
    import os

    from werkzeug.serving import make_server

    previous = os.environ.get("TENANT")
    os.environ["TENANT"] = tenant
    try:
        port = _free_port()
        server = make_server("127.0.0.1", port, create_app(), threaded=True)
    finally:
        if previous is None:
            os.environ.pop("TENANT", None)
        else:
            os.environ["TENANT"] = previous

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def apps():
    seed.seed()
    exceptional.reset_all()

    servers = {}
    urls = {}
    for tenant in ("base", "riverbend"):
        servers[tenant], urls[tenant] = _serve(tenant)
    try:
        yield urls
    finally:
        for server in servers.values():
            server.shutdown()


@pytest.fixture(scope="module")
def manager():
    manager = SessionManager()
    try:
        yield manager
    finally:
        manager.close_all()


@pytest.fixture(scope="module")
def library():
    return CapabilityStore()


@pytest.fixture(scope="module")
def base(library):
    return library.load("member.read_savings_balance")


def policy_for(url: str) -> PolicyEngine:
    return PolicyEngine(
        Policy(
            allowlist=Allowlist(
                origins=(url,),
                paths=("/", "/home", "/members/**"),
                actions=frozenset(
                    {
                        ActionType.NAVIGATE,
                        ActionType.CLICK,
                        ActionType.TYPE,
                        ActionType.EXTRACT,
                        ActionType.WAIT_FOR,
                    }
                ),
            ),
            risk=RiskPolicy(unattended_max=RiskLevel.CAUTION),
        )
    )


def run_against(manager, url: str, artifact):
    session = manager.create(url, headless=True)
    try:
        authenticate(session.surface, CREDENTIALS)
        return ReplayEngine(
            session.surface, artifact, policy=policy_for(url)
        ).run({"member_id": MEMBER})
    finally:
        manager.close(session.id)


def tier_for(result, step_id: str):
    return next(entry for entry in result.tier_log if entry.step_id == step_id)


# --- the measurement -----------------------------------------------------


@pytest.fixture(scope="module")
def runs(apps, manager, library, base):
    """All three runs, once, because each one launches a browser."""
    return {
        "base": run_against(manager, apps["base"], base),
        "unprepared": run_against(manager, apps["riverbend"], base),
        "overridden": run_against(
            manager, apps["riverbend"], for_tenant(library, base, TENANT)
        ),
    }


def test_the_same_capability_works_on_a_deployment_it_never_saw(runs):
    """The claim the whole fallback chain exists to support.

    Riverbend renamed the one field this capability cannot find by its
    accessible name, and the run still returns the right number. Had this
    failed, the chain would be decoration and the honest design would be one
    artifact per tenant.
    """
    result = runs["unprepared"]
    assert isinstance(result, Success), getattr(result, "observed", result)
    assert result.outputs["savings_balance"] == BALANCE


def test_and_it_says_that_it_degraded(runs):
    """Succeeding quietly would be worse than failing.

    The tier a locator resolved on is the drift signal, and it is only a
    signal if it is different here. This is the difference between "it still
    works" and "it still works, and something changed underneath it".
    """
    prepared = tier_for(runs["base"], "s2")
    unprepared = tier_for(runs["unprepared"], "s2")

    assert unprepared.tier > prepared.tier
    assert unprepared.degraded is True
    assert prepared.degraded is False
    assert unprepared.strategy != prepared.strategy


def test_the_override_returns_the_step_to_its_own_rule(runs):
    """The repair, and the reason an override is one field rather than a
    forked document."""
    unprepared = tier_for(runs["unprepared"], "s2")
    overridden = tier_for(runs["overridden"], "s2")

    assert overridden.tier < unprepared.tier
    assert overridden.degraded is False
    assert str(overridden.strategy) == LocatorStrategy.LABEL_PROXIMITY.value


def test_all_three_runs_return_the_same_answer(runs):
    """Three routes to one number. A cross-tenant capability that returned
    different answers per deployment would be worse than one that failed."""
    balances = {name: result.outputs.get("savings_balance") for name, result in runs.items()}
    assert set(balances.values()) == {BALANCE}, balances


def test_the_reordered_column_needed_no_override_at_all(runs):
    """Riverbend moved Balance from the third column to the second, and the
    extract step is untouched in the override file.

    It is keyed on the column *name*, so it follows the move. The structural
    fallback beneath it would also have resolved -- and read the account
    number, confidently. This is the whole argument for ranking `row_cell`
    above `region_path`, and it is why the run does not merely succeed, it
    succeeds with the right number.
    """
    for name in ("unprepared", "overridden"):
        entry = tier_for(runs[name], "s5")
        assert str(entry.strategy) == LocatorStrategy.ROW_CELL.value, name
        assert entry.degraded is False, name
        assert runs[name].outputs["savings_balance"] == BALANCE


def test_the_tier_log_is_the_evidence(runs):
    """What the report quotes. Printed so a failure shows the whole picture
    rather than one assertion's worth of it."""
    lines = []
    for name, result in runs.items():
        for entry in result.tier_log:
            lines.append(
                f"{name:<12} {entry.step_id} {str(entry.strategy):<16}"
                f" tier={entry.tier}{'  degraded' if entry.degraded else ''}"
            )
    report = "\n".join(lines)
    assert "degraded" in report, report
    # Exactly one step degrades, in exactly one of the three runs.
    assert report.count("degraded") == 1, report
