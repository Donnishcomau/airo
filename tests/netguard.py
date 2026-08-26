# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No test may reach the internet.

Found by an end-to-end test that showed weather data in a fresh install with an
empty database. One suite run made **25 real requests to Open-Meteo**, and
nothing reported it: `capture_weather()` is documented to swallow every failure
-- correctly, since a weather service being down must never cost a reading --
so the calls succeeded quietly, and would have failed just as quietly.

Three things wrong with that, in order of how much they matter:

  * the weather path was never actually tested. The tests were passing because
    a third party answered, not because the code was right, and the day
    Open-Meteo changes a field they would have kept passing while the feature
    broke.
  * CI depends on somebody else's uptime, on six jobs, on every push.
  * it is rude. That service is free and asks to be used considerately.

`discover -s tests` does not import this directory as a package, so there is no
one place to install this for everybody -- CI runs discovery, so a package
`__init__` would cover local runs and miss the ones that matter. It is applied
per file instead, and `test_contracts.py` enumerates the files that need it
from disk rather than from a list, so a new suite that drives a poll is in
scope without anyone remembering.

Loopback is allowed: the dashboard, the settings API and the port-collision
tests all bind 127.0.0.1, and that is the product rather than the internet.
"""

import urllib.request

LOOPBACK = ("http://127.0.0.1", "http://localhost", "https://127.0.0.1")


class OutboundBlocked(Exception):
    """A test tried to reach a host that is not this machine.

    Deliberately an `Exception` and not a `BaseException`, unlike the
    unexpected-request guard in `test_providers.py`. That one has to survive
    provider error handling, because its whole point is that a test made a
    request nobody queued. This one is the opposite: it *should* be caught by
    the same swallowing that hid the problem, so the code under test behaves
    exactly as it would with no network -- which is the condition being
    simulated. The attempt is recorded on the guard, so a test can assert on
    what was tried even though nothing raised out of it.
    """


class Guard:
    """Records every outbound attempt and refuses it."""

    def __init__(self):
        self.attempts = []

    def __call__(self, url, *args, **kwargs):
        target = str(getattr(url, "full_url", url))
        if target.startswith(LOOPBACK):
            return self._real(url, *args, **kwargs)
        self.attempts.append(target)
        raise OutboundBlocked(
            f"a test tried to reach {target}. Stub the boundary instead: "
            f"tests must not depend on somebody else's uptime, and a call "
            f"that is swallowed on failure will pass for the wrong reason.")


def block_outbound(testcase):
    """Refuse outbound HTTP for the duration of one test. Returns the guard.

    Restores itself through addCleanup, so a test that fails part-way cannot
    leave the block installed for whatever runs next.
    """
    guard = Guard()
    guard._real = urllib.request.urlopen
    urllib.request.urlopen = guard
    testcase.addCleanup(lambda: setattr(urllib.request, "urlopen", guard._real))
    return guard


_MODULE_GUARD = []


def block_outbound_for_module():
    """Install the block for a whole test module.

    For suites whose classes do not share a setUp. `unittest` calls
    `setUpModule()` and `tearDownModule()` automatically, so a file gets the
    guard with two lines and no per-class surgery -- which matters because the
    alternative is editing every class and missing one.
    """
    guard = Guard()
    guard._real = urllib.request.urlopen
    urllib.request.urlopen = guard
    _MODULE_GUARD.append(guard)
    return guard


def restore_outbound_for_module():
    if _MODULE_GUARD:
        guard = _MODULE_GUARD.pop()
        urllib.request.urlopen = guard._real
