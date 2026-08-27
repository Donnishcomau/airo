# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No test may open a real browser window.

The counterpart to `netguard`, and it exists for the same reason that one
does: a side effect that reaches out of the process, is easy to add without
noticing, and is invisible in a passing test run.

It was written after opening roughly fifteen browser tabs on the maintainer's
machine. `open_page()` used `webbrowser.open()`, which the tests stubbed. A
change added `/usr/bin/open` ahead of it -- correctly, because `webbrowser`
reaches `osascript -e 'open location ...'` and does not necessarily bring the
window forward -- and that call went straight past the stub. Every run of the
suite from then on opened a tab at a URL with no server behind it, and the
tests that recorded "which URL was opened" saw nothing and failed with an
IndexError that said nothing about browsers.

Two lessons are built into the shape here:

  * **Stubbing the library is not the same as blocking the effect.** A stub
    covers the one route somebody thought of. This blocks the effect and
    records the attempt, so a new route shows up as a recorded attempt rather
    than as a window.
  * **The seam has to be enforced, not agreed.** `test_contracts.py` fails if
    any shipped module reaches a browser outside `poller.launch_browser`, so
    there is exactly one place this needs to cover and it stays that way.

Attempts are recorded rather than raised, because opening a browser is not
something the product should ever fail on -- a headless box has no browser and
that is fine. A test asserts on what was attempted.
"""

import subprocess


class Attempt:
    """One recorded attempt to put a URL in front of somebody."""

    def __init__(self, url, how):
        self.url = url
        self.how = how

    def __repr__(self):
        return f"Attempt({self.url!r}, via={self.how!r})"


class Guard:
    def __init__(self):
        self.attempts = []

    @property
    def urls(self):
        return [a.url for a in self.attempts]

    def record(self, url, how):
        self.attempts.append(Attempt(url, how))
        return True


#: The command names that hand a URL to the desktop. Enumerated rather than
#: matched on "open", because `subprocess.run(["open", ...])` is not the only
#: shape -- macOS wants an absolute path from a launchd agent, and Linux and
#: Windows have their own.
LAUNCHERS = ("/usr/bin/open", "open", "xdg-open", "start")

_STACK = []


def _install(guard):
    import webbrowser

    real_run = subprocess.run
    real_open = webbrowser.open

    def guarded_run(cmd, *args, **kwargs):
        first = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else None
        if first in LAUNCHERS:
            url = cmd[1] if len(cmd) > 1 else ""
            guard.record(str(url), first)

            class Refused:
                returncode = 0        # the product must not treat this as a
                stdout = ""           # failure; it did open, as far as it knows
                stderr = ""
            return Refused()
        return real_run(cmd, *args, **kwargs)

    def guarded_open(url, *args, **kwargs):
        return guard.record(str(url), "webbrowser")

    subprocess.run = guarded_run
    webbrowser.open = guarded_open
    guard._restore = (real_run, real_open)
    return guard


def block_browser_for_module():
    """Install for a whole test module. Returns the guard."""
    guard = _install(Guard())
    _STACK.append(guard)
    return guard


def restore_browser_for_module():
    if _STACK:
        import webbrowser
        guard = _STACK.pop()
        subprocess.run, webbrowser.open = guard._restore


def current():
    """The guard installed for this module, if any."""
    return _STACK[-1] if _STACK else None
