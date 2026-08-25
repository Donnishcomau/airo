"""Alerts have to reach a person on every platform Airo installs on.

`notify()` shelled straight to `osascript`. On Linux and Windows that raises
FileNotFoundError, which is caught, logged, and returned as False -- so the
alerting feature did nothing at all on two of the three shipped platforms, and
said so only in a log nobody reads.

The risk register already carries "an alert that never fires at all", written
about a bug in the firing logic. This was the same outcome by a different
route: the logic fired correctly and the notification went nowhere.

ROADMAP #14 said "the data layer is portable; only scheduling is
macOS-specific", which was no longer true. It says so now.

The commands are built by a pure function so every platform's can be inspected
from any platform -- the pattern `folder_chooser_commands()` already uses, and
for the same reason: two thirds of this was unreachable in CI otherwise.

**Text travels as data, never interpolated into a script body.** That is the
same rule the folder chooser learned the hard way, and it matters more here:
the strings include a `site_name` that came from a provider's JSON, so it is
third-party text heading for a shell.
"""

import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poller  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



def setUpModule():
    redirect_airo_paths_for_module()
    block_outbound_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


PLATFORMS = (("posix", "darwin"), ("posix", "linux"), ("nt", "win32"))

# Deliberately hostile, and not hypothetical: a site_name arrives from a
# provider's JSON and lands in this text.
NASTY = 'Site "; do shell script "touch /tmp/pwned" -- \'$(id)\' `id` %n'


class TestEveryPlatformCanNotify(unittest.TestCase):

    def commands(self, os_name, platform):
        return poller.notification_commands(
            "Airo", "Air quality: Poor", "45.0 µg/m³", sound="Ping",
            os_name=os_name, platform=platform)

    def test_each_platform_offers_something(self):
        for os_name, platform in PLATFORMS:
            with self.subTest(platform=platform):
                cmds = self.commands(os_name, platform)
                self.assertTrue(cmds,
                                f"{platform} has no way to notify anybody")

    def test_each_uses_that_platforms_own_mechanism(self):
        expected = {"darwin": "osascript", "linux": "notify-send",
                    "win32": "powershell"}
        for os_name, platform in PLATFORMS:
            with self.subTest(platform=platform):
                argv, _, _ = self.commands(os_name, platform)[0]
                self.assertIn(expected[platform], argv[0],
                              f"{platform} got {argv[0]}")

    def test_the_message_reaches_the_command_somehow(self):
        for os_name, platform in PLATFORMS:
            with self.subTest(platform=platform):
                argv, env, stdin = self.commands(os_name, platform)[0]
                blob = " ".join(argv) + " " + " ".join(
                    f"{k}={v}" for k, v in (env or {}).items()) + " " + (stdin or "")
                self.assertIn("45.0", blob,
                              f"{platform} never receives the reading")


class TestTextIsDataNotScript(unittest.TestCase):
    """The lesson the folder chooser paid for, applied before it costs again.

    A `site_name` comes from a provider's JSON. Interpolating it into an
    AppleScript or a PowerShell command is arbitrary code execution as the
    user, reached by whoever controls that feed or anything between.
    """

    def commands(self, os_name, platform):
        return poller.notification_commands(
            NASTY, NASTY, NASTY, sound=NASTY,
            os_name=os_name, platform=platform)

    def test_no_platform_interpolates_it_into_a_script_body(self):
        for os_name, platform in PLATFORMS:
            with self.subTest(platform=platform):
                for argv, env, stdin in self.commands(os_name, platform):
                    script = " ".join(
                        a for a in argv
                        if "display notification" in a or "Command" in a
                        or "$env:" in a or "Toast" in a)
                    self.assertNotIn(
                        "do shell script", script,
                        f"{platform} put caller text inside a script body")

    def test_macos_passes_it_after_the_script_as_argv(self):
        """osascript hands everything after the script to `on run argv`,
        where it is a string value and cannot be anything else."""
        argv, _, _ = self.commands("posix", "darwin")[0]
        self.assertIn("on run argv", " ".join(argv))
        self.assertIn(NASTY, argv, "the text was not passed as an argument")

    def test_linux_passes_it_as_argv(self):
        """argv is a list and no shell is involved, so a token is one token
        whatever it contains."""
        argv, _, _ = self.commands("posix", "linux")[0]
        self.assertIn(NASTY, argv)

    def test_windows_passes_it_in_the_environment(self):
        """PowerShell's -Command takes one string, so there is no argv to
        use, and its quoting rules are their own hazard."""
        argv, env, _ = self.commands("nt", "win32")[0]
        self.assertTrue(env, "nothing was passed out of band")
        self.assertIn(NASTY, list(env.values()),
                      "the text did not travel as an environment variable")
        self.assertNotIn(NASTY, " ".join(argv),
                         "the text was interpolated into the command anyway")


class TestNotifyDispatches(unittest.TestCase):
    """The function that actually runs them."""

    def setUp(self):
        self.ran = []
        self.returncode = 0
        self.logged = []
        self._log = poller.log
        poller.log = self.logged.append
        self.addCleanup(lambda: setattr(poller, "log", self._log))

        def fake_run(argv, **kw):
            self.ran.append((list(argv), kw))
            return subprocess.CompletedProcess(argv, self.returncode, "", "")

        self._run = poller.subprocess.run
        poller.subprocess.run = fake_run
        self.addCleanup(lambda: setattr(poller.subprocess, "run", self._run))

    def test_it_notifies_on_each_platform(self):
        for os_name, platform in PLATFORMS:
            with self.subTest(platform=platform):
                self.ran.clear()
                ok = poller.notify("Airo", "Poor", "45 µg/m³",
                                   os_name=os_name, platform=platform)
                self.assertTrue(ok, f"{platform} could not notify")
                self.assertTrue(self.ran)

    def test_a_missing_mechanism_falls_through_to_the_next(self):
        """A minimal Linux install may have no notify-send. Trying the next
        candidate beats giving up, and giving up quietly is what this whole
        change is about."""
        calls = []

        def sometimes(argv, **kw):
            calls.append(argv[0])
            if len(calls) == 1:
                raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv, 0, "", "")

        poller.subprocess.run = sometimes
        cmds = poller.notification_commands("a", "b", "c",
                                            os_name="posix", platform="linux")
        if len(cmds) < 2:
            self.skipTest("linux offers a single mechanism")
        self.assertTrue(poller.notify("a", "b", "c",
                                      os_name="posix", platform="linux"))

    def test_nothing_working_is_reported_rather_than_raised(self):
        def explode(argv, **kw):
            raise FileNotFoundError(argv[0])
        poller.subprocess.run = explode
        ok = poller.notify("a", "b", "c", os_name="posix", platform="linux")
        self.assertFalse(ok)
        self.assertTrue(any("notification" in m.lower() for m in self.logged),
                        "it failed without saying so")

    def test_it_never_raises_whatever_happens(self):
        """A poll must not die because a desktop notifier did. The reading is
        the product; the notification is a courtesy."""
        def explode(argv, **kw):
            raise RuntimeError("something unexpected")
        poller.subprocess.run = explode
        self.assertFalse(poller.notify("a", "b", "c"))

    def test_the_default_is_still_this_machine(self):
        self.assertIsInstance(poller.notify("a", "b", "c"), bool)


class TestDoctorSaysWhetherAlertsCanArrive(unittest.TestCase):
    """An alert that cannot be delivered should be visible before the night it
    matters, not discovered afterwards."""

    def test_it_reports_the_mechanism_for_this_platform(self):
        lines = poller.notification_report()
        self.assertTrue(lines)
        joined = " ".join(lines).lower()
        self.assertIn("notification", joined)

    def test_it_names_what_is_missing_rather_than_only_failing(self):
        with unittest.mock.patch.object(poller.shutil, "which",
                                        lambda name: None):
            joined = " ".join(poller.notification_report(
                os_name="posix", platform="linux")).lower()
        self.assertIn("notify-send", joined,
                      "it did not say which program to install")




class TestDoctorActuallyAsks(unittest.TestCase):
    """The fourth time this shape has appeared, so it gets a test by default.

    `axisCeiling`, `resolve_timezone` and `timezone_is_a_problem` were each
    fully tested while their call site was deleted, leaving a green suite
    against a broken product. A helper being right is not the same as anything
    running it.
    """

    def test_run_doctor_reports_notifications(self):
        import inspect
        self.assertIn("notification_report",
                      inspect.getsource(poller.run_doctor),
                      "--doctor no longer says whether alerts can arrive")

    def test_maybe_alert_still_reaches_notify(self):
        import inspect
        self.assertIn("notify(", inspect.getsource(poller.maybe_alert),
                      "the alert path no longer notifies anybody")


if __name__ == "__main__":
    unittest.main()
