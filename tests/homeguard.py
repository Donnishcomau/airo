# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No test may write into the developer's own ~/.airo.

CONVENTIONS is explicit: *never mutate the developer's own `~/.airo` from
tests*. It was being broken. A suite run appended these to a real install's
log, between two real polls:

    WARN weather unavailable: the service is down
    WARN weather failed: ValueError: something else entirely

Both are test fixture strings. They arrive because `poller.log()` writes to
`poller.LOG_PATH`, which is a module-level path resolved at import — so any
test that reaches code calling `log()` without redirecting it writes to the
real file. The maintainer read that tail and reasonably concluded their
monitor had died.

Nothing was lost: the readings were untouched, and the log is append-only
narration. But "it only corrupted the log this time" is luck, not design. The
same import-time paths point at the real database, the real config and the
real alert state, and a test that redirects none of them is one call away from
writing to any of them.

So the paths are redirected for every suite by default, and the ones that need
their own per-test isolation still do it -- this is a floor, not a replacement.
`discover -s tests` does not import this directory as a package, so there is
no single place to install it for everybody; it goes in a `setUpModule()`
hook, and `test_contracts.py` enumerates from disk which modules need one.
"""

import os
import tempfile
from pathlib import Path

import poller

#: Every module-level path in poller that points into a real installation.
#: Enumerated rather than listed by hand: a path added later is caught by the
#: contract test that compares this against poller's own module globals.
GUARDED = ("DATA", "LATEST_PATH", "LOG_PATH", "CONFIG_PATH", "CSV_PATH",
           "ALERT_STATE_PATH", "FORECAST_PENDING_PATH",
           "FORECAST_SKILL_PATH")

_STACK = []

#: What poller resolved at import, before anything redirected it. Captured
#: once, because a contract asking "which paths point into the real install"
#: cannot answer that from inside a process where they have already been
#: redirected -- which is every process that installs this guard.
ORIGINALS = {}


#: Captured at import, before anything redirects HOME. Resolving it later
#: returns the *fake* home this guard installs, so a contract asking "which
#: paths point into the real install" would compare against a temporary
#: directory, find nothing, and pass however broken the guard was. That is
#: exactly what happened once.
REAL_AIRO_HOME = Path.home() / ".airo"


def real_airo_home():
    """Where a real installation lives, regardless of any redirection."""
    return REAL_AIRO_HOME


def redirect_airo_paths_for_module():
    """Point poller's paths at a temporary directory for a whole module."""
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    data = base / "data"
    data.mkdir(parents=True, exist_ok=True)
    (base / "home" / ".airo").mkdir(parents=True, exist_ok=True)

    # HOME as well as poller's own globals. Anything calling Path.home() at
    # *runtime* escapes a redirect of the module paths -- `run_doctor()` scans
    # for orphaned databases that way and opened the developer's real one, and
    # `get_api_key()` would read their real key by the same route.
    #
    # $AIRO_CONFIG and $AIRO_DATA are cleared for the same reason one step
    # further out: they *outrank* HOME in poller's resolvers, so a developer
    # who has either set in their shell gets a suite that reads and writes
    # their real install however carefully HOME is redirected. Nothing had
    # driven that path while only import-time constants honoured them; with
    # `config_path()` resolving at call time, redirecting HOME alone is no
    # longer sufficient. A test that wants them set does it itself, and
    # restores it -- see TestSetupAndPollerAgreeOnWhereTheConfigIs.
    saved_env = {k: os.environ.get(k)
                 for k in ("HOME", "USERPROFILE", "AIRO_CONFIG", "AIRO_DATA")}
    fake_home = base / "home"
    os.environ["HOME"] = str(fake_home)
    os.environ["USERPROFILE"] = str(fake_home)
    os.environ.pop("AIRO_CONFIG", None)
    os.environ.pop("AIRO_DATA", None)

    saved = {name: getattr(poller, name) for name in GUARDED}
    if not ORIGINALS:
        ORIGINALS.update(
            {n: getattr(poller, n, None) for n in dir(poller)
             if isinstance(getattr(poller, n, None), Path) and n.isupper()})
    poller.DATA = data
    poller.LATEST_PATH = data / "latest.json"
    poller.LOG_PATH = data / "poller.log"
    poller.CSV_PATH = data / "readings.csv"
    poller.ALERT_STATE_PATH = data / "alert_state.json"
    poller.CONFIG_PATH = base / "home" / ".airo" / "config.json"
    poller.FORECAST_PENDING_PATH = data / "forecast_pending.json"
    poller.FORECAST_SKILL_PATH = data / "forecast_skill.json"

    _STACK.append((tmp, saved, saved_env))
    return base


def restore_airo_paths_for_module():
    if not _STACK:
        return
    tmp, saved, saved_env = _STACK.pop()
    for name, value in saved.items():
        setattr(poller, name, value)
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    tmp.cleanup()
