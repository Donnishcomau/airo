# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No test may put a notification on somebody's screen.

The third guard of this shape, after `netguard` (no test reaches the internet)
and `browserguard` (no test opens a window). It exists for the same reason and
was written for the same reason: it had already happened.

A new suite exercised the alerting path with a 400 µg/m³ reading — a deliberate
"this is a genuine emergency" fixture — and `maybe_alert()` did what it is for.
The maintainer received, on a perfectly ordinary Tuesday:

    Air quality: Hazardous
    AQI 1600 — into the hazardous band
    400.0 µg/m³. Close up and start the purifiers.

Four other suites stub `poller.notify` by hand. That is the pattern this
replaces: stubbing works until somebody writes a fifth suite, and the fifth
suite is the one that reaches the screen. `test_contracts.py` enumerates which
files drive alerting and fails if any of them has not installed this, so the
fifth is caught by existing rather than by remembering.

Attempts are recorded rather than raised. Alerting must never fail on a
notification the desktop refuses — a suppressed alert is a health warning
nobody got — so the product treats a refusal as normal, and a guard that
raised would exercise a path the product does not have.
"""


class Sent:
    """One notification that would have reached a screen."""

    def __init__(self, title, subtitle, message, sound=None):
        self.title = title
        self.subtitle = subtitle
        self.message = message
        self.sound = sound

    @property
    def text(self):
        return " ".join(str(p) for p in (self.title, self.subtitle,
                                         self.message) if p)

    def __repr__(self):
        return f"Sent({self.text!r})"


class Guard:
    def __init__(self):
        self.sent = []

    @property
    def messages(self):
        return [s.text for s in self.sent]

    def __call__(self, title, subtitle, message, sound=None, **kwargs):
        self.sent.append(Sent(title, subtitle, message, sound))
        # True, because that is what a delivered notification returns and the
        # alerting path records whether it got through. Returning False here
        # would have every test exercise the "notification failed" branch,
        # which is not the branch any of them mean to be testing.
        return True


_STACK = []


def block_notifications_for_module():
    """Install for a whole test module. Returns the guard."""
    import poller
    guard = Guard()
    guard._real = poller.notify
    poller.notify = guard
    _STACK.append(guard)
    return guard


def restore_notifications_for_module():
    if _STACK:
        import poller
        guard = _STACK.pop()
        poller.notify = guard._real


def current():
    return _STACK[-1] if _STACK else None
