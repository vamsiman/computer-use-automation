"""Suite-wide safety rails.

Nothing here changes what the tests assert. It changes how they fail: a
browser test that stalls should fail loudly and name itself, not sit there
looking like a suite that is still working.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _bounded_handoffs():
    """Never let a test wait out the production handoff timeout.

    A paused run waits fifteen minutes for a person, which is right in
    production and catastrophic in a test: a handoff nobody releases turns
    into a suite that appears frozen with no indication which test is
    responsible. Five of those is an hour and a quarter of silence.

    Set low here so that the same situation fails in seconds instead, and
    reports itself as the escalation it is.
    """
    os.environ.setdefault("CUA_HANDOFF_TIMEOUT", "20")
    yield
