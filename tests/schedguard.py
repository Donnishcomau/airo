# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No test may reach the session manager the developer is logged in to.

The fourth guard of this shape, after `netguard` (no test reaches the
internet), `browserguard` (no test opens a window) and `notifyguard` (no test
puts a notification on somebody's screen). It exists for the reason all three
do: a side effect that leaves the process, easy to cause without noticing, and
invisible in a passing run.

This one has already happened, and it is the least visible of the four.
`launchctl` addresses agents as `gui/<uid>/<label>` — by the *session*, keyed
on a uid and a fixed label, not by HOME. So a test running under a redirected
home directory deletes a plist that was never there and unloads the **real**
agent belonging to the login session. `systemctl --user` and Task Scheduler are
addressed the same way, so a Linux or Windows test doing what the macOS one did
has exactly the same effect there.

That is how the maintainer's poller stopped collecting. The evidence was
unhelpful in every direction: the plists sat untouched on disk, `launchctl
list` showed nothing, and the last line in the log was a clean successful poll.
Nothing failed, so nothing was reported — the data simply stopped.

`scheduler.run()` carries the product's own guard for this, and it is the right
place for it: an invocation under a home directory this process does not own is
refused there, in the shipped code, where a user benefits too. This guard is
the test-side floor underneath it, and it covers the two routes that one
cannot:

  * a call that does not go through `scheduler.run` at all — `subprocess.run`
    reached directly, which is one autocomplete away in any new suite;
  * a test that legitimately restores the real HOME (some do, to check that the
    ordinary case is *not* refused) and then drives an install path.

Three suites stub `scheduler.run`, `scheduler.install` or `scheduler.uninstall`
by hand today. That is the arrangement this replaces: stubbing works until
somebody writes a fourth suite, and the fourth suite is the one that reaches
the session. `test_contracts.py` enumerates from disk which files can reach a
session manager and fails if any of them has not installed this, so the fourth
is caught by something that already exists rather than by remembering.

**Attempts are refused rather than faked successful** — the opposite choice
from `browserguard` and `notifyguard`, deliberately. A browser tab that did not
open costs nothing, and a suppressed notification is a branch the product
already has. Reporting a session-manager command as having succeeded is
different in kind: `install()` returns "your schedule is registered" on the
strength of that return code, so a guard answering 0 would let a test assert
that a schedule exists when nothing was ever registered — a green that means
the opposite of what it says. The refusal is the same shape `scheduler.run`
already produces, and it says why. The attempt is recorded either way, so a
test can assert on what was tried.

`discover -s tests` does not import this directory as a package, so there is no
one place to install this for everybody. It goes in a `setUpModule()` hook, the
same as the other three.
"""

import subprocess

#: The tools that address the logged-in session. Named here as a claim about
#: the world, and reconciled against `scheduler.SESSION_MANAGERS` at install
#: time so the two cannot drift apart — the shipped guard and the test guard
#: disagreeing about which commands are dangerous is the kind of gap that
#: reads as covered from either side alone.
MANAGERS = ("launchctl", "systemctl", "schtasks")


def session_managers():
    """Everything either side considers a session manager."""
    try:
        import scheduler
        shipped = tuple(scheduler.SESSION_MANAGERS)
    except Exception:                       # a guard must not need the module
        shipped = ()
    return tuple(dict.fromkeys(MANAGERS + shipped))


def _is_a_session_manager(argv):
    """Matched by shape rather than by exact argv[0].

    `schtasks` is `schtasks.exe` on Windows, and a launchd agent gets an
    absolute path because its PATH is not a login shell's. A rule naming one
    spelling is the failure this project keeps paying for, so the basename is
    compared with any executable suffix removed.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    first = str(argv[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first.endswith(".exe"):
        first = first[:-4]
    return first if first in session_managers() else None


class Attempt:
    """One recorded attempt to address the logged-in session."""

    def __init__(self, argv, manager):
        self.argv = list(argv)
        self.manager = manager

    @property
    def text(self):
        return " ".join(str(a) for a in self.argv)

    def __repr__(self):
        return f"Attempt({self.text!r})"


class Guard:
    def __init__(self):
        self.attempts = []

    @property
    def commands(self):
        return [a.text for a in self.attempts]

    def refuse(self, argv, manager):
        self.attempts.append(Attempt(argv, manager))
        return subprocess.CompletedProcess(
            list(argv), 1, "",
            f"a test tried to run {manager} against the logged-in session. "
            f"Agents are addressed by uid and by a fixed label rather than by "
            f"HOME, so this would act on the developer's own install however "
            f"carefully the home directory was redirected. Stub the backend "
            f"and assert on the argv instead.")


_STACK = []


def _install(guard):
    real_run = subprocess.run

    def guarded_run(cmd, *args, **kwargs):
        manager = _is_a_session_manager(cmd)
        if manager:
            return guard.refuse(cmd, manager)
        return real_run(cmd, *args, **kwargs)

    subprocess.run = guarded_run
    guard._real = real_run
    return guard


def block_session_managers_for_module():
    """Install for a whole test module. Returns the guard."""
    guard = _install(Guard())
    _STACK.append(guard)
    return guard


def restore_session_managers_for_module():
    if _STACK:
        subprocess.run = _STACK.pop()._real


def current():
    """The guard installed for this module, if any."""
    return _STACK[-1] if _STACK else None
