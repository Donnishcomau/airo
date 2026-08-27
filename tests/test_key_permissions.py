# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a key file is actually protected, on every platform.

Rule 2: a key never enters the repository, and the file it lives in is
readable only by its owner. `secure_path()` applies that and
`path_is_restricted()` reads it back rather than assuming the attempt worked --
which matters because on Windows the attempt is a different mechanism
entirely, and a credential file that is *not* protected must never look like
one that is.

Two thirds of both functions could not run here. They branched on the global
`os.name`, so on a Mac only the POSIX path executed and the Windows path was
carried untested from the day it was written -- on the platform where getting
it wrong means every account on the machine can read the key, because chmod
there only toggles a read-only attribute and does nothing about access.

The platform is an argument now, the same shape `folder_chooser_commands()`
already uses and for the same reason: a function whose answer depends on the
platform needs the platform as an input if it is ever to be checked from
another one.
"""

import os
import stat
import subprocess
import sys
import tempfile
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


class KeyFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "provider.key"
        self.path.write_text("not-a-real-key", encoding="utf-8")

    def as_windows(self, returncode=0, stdout=""):
        """Run the Windows branch, wherever this test is running.

        `subprocess.run` is stubbed because icacls does not exist here, and
        because a test that shelled out to a real ACL tool would be asserting
        something about the machine rather than about this code.
        """
        self.ran = []

        def fake_run(cmd, **kw):
            self.ran.append(list(cmd))
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")

        return unittest.mock.patch.object(poller.subprocess, "run", fake_run)


class TestPosixPermissions(KeyFileCase):

    def test_a_key_file_ends_up_readable_only_by_its_owner(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows grants access")
        self.assertTrue(poller.secure_path(self.path, os_name="posix"))
        mode = oct(stat.S_IMODE(self.path.stat().st_mode))[-3:]
        self.assertEqual("600", mode)

    def test_a_directory_gets_the_directory_mode(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows grants access")
        d = Path(self.tmp.name) / "keys"
        d.mkdir()
        self.assertTrue(poller.secure_path(d, is_dir=True, os_name="posix"))
        self.assertEqual("700", oct(stat.S_IMODE(d.stat().st_mode))[-3:])

    def test_a_file_that_is_not_there_reports_failure_rather_than_raising(self):
        missing = Path(self.tmp.name) / "gone.key"
        self.assertFalse(poller.secure_path(missing, os_name="posix"))

    def test_reading_it_back_agrees(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows grants access")
        poller.secure_path(self.path, os_name="posix")
        self.assertTrue(poller.path_is_restricted(self.path, os_name="posix"))

    def test_a_world_readable_key_is_reported_as_unprotected(self):
        """The case that matters. Saying "protected" about a file every
        account on the machine can read is worse than saying nothing."""
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows grants access")
        os.chmod(self.path, 0o644)
        self.assertFalse(poller.path_is_restricted(self.path, os_name="posix"))

    def test_a_path_that_does_not_exist_answers_none_not_false(self):
        """None means "no answer"; False means "not protected". Collapsing
        them would report a missing key file as an insecure one."""
        self.assertIsNone(poller.path_is_restricted(
            Path(self.tmp.name) / "nothing", os_name="posix"))


class TestWindowsPermissions(KeyFileCase):
    """The branch that never ran on this project's CI-visible failures.

    chmod on Windows toggles a read-only attribute and does nothing about who
    may read the file. Access is governed by ACLs, so the real work is icacls:
    drop inherited permissions, grant the current user full control.
    """

    def test_it_drops_inheritance_and_grants_only_this_user(self):
        with self.as_windows():
            ok = poller.secure_path(self.path, os_name="nt",
                                    username="testuser")
        self.assertTrue(ok)
        cmd = self.ran[-1]
        self.assertEqual("icacls", cmd[0])
        self.assertIn("/inheritance:r", cmd,
                      "inherited permissions were left in place")
        self.assertIn("/grant:r", cmd)
        self.assertIn("testuser:F", cmd)

    def test_a_directory_grants_the_inheritance_flags(self):
        """(OI)(CI) is what makes the restriction apply to files created in
        the directory later -- without it, the next key written there is
        unprotected."""
        d = Path(self.tmp.name) / "keys"
        d.mkdir()
        with self.as_windows():
            poller.secure_path(d, is_dir=True, os_name="nt",
                               username="testuser")
        self.assertIn("testuser:(OI)(CI)F", self.ran[-1])

    def test_icacls_failing_is_reported_not_swallowed(self):
        """False here makes --status tell the user their key is not actually
        protected. Returning True would be a lie with consequences."""
        with self.as_windows(returncode=1):
            self.assertFalse(poller.secure_path(self.path, os_name="nt",
                                                username="testuser"))

    def test_icacls_missing_entirely_is_reported(self):
        def explode(cmd, **kw):
            raise OSError("icacls not found")
        with unittest.mock.patch.object(poller.subprocess, "run", explode):
            self.assertFalse(poller.secure_path(self.path, os_name="nt",
                                                username="testuser"))

    def test_with_no_username_it_gives_up_rather_than_guessing(self):
        """A grant to the wrong principal is worse than no grant: it looks
        applied and protects somebody else's account."""
        with self.as_windows():
            ok = poller.secure_path(self.path, os_name="nt", username="")
        self.assertFalse(ok)

    def test_reading_it_back_rejects_a_broad_principal(self):
        for principal in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
            with self.subTest(principal=principal):
                with self.as_windows(stdout=f"{self.path}\n {principal}:(F)\n"):
                    self.assertFalse(
                        poller.path_is_restricted(self.path, os_name="nt"),
                        f"{principal} could read the key and it said protected")

    def test_reading_it_back_accepts_an_owner_only_acl(self):
        with self.as_windows(stdout=f"{self.path}\n TESTUSER:(F)\n"):
            self.assertTrue(poller.path_is_restricted(self.path, os_name="nt"))

    def test_icacls_failing_on_read_answers_none(self):
        with self.as_windows(returncode=1):
            self.assertIsNone(poller.path_is_restricted(self.path,
                                                        os_name="nt"))

    def test_icacls_missing_on_read_answers_none(self):
        def explode(cmd, **kw):
            raise OSError("icacls not found")
        with unittest.mock.patch.object(poller.subprocess, "run", explode):
            self.assertIsNone(poller.path_is_restricted(self.path,
                                                        os_name="nt"))


class TestTheDefaultIsStillThisMachine(KeyFileCase):
    """Making the platform an argument must not change what happens when
    nobody passes one -- every existing caller relies on that."""

    def test_secure_path_defaults_to_the_real_platform(self):
        self.assertTrue(poller.secure_path(self.path))
        if os.name != "nt":
            self.assertEqual(
                "600", oct(stat.S_IMODE(self.path.stat().st_mode))[-3:])

    def test_path_is_restricted_defaults_to_the_real_platform(self):
        poller.secure_path(self.path)
        self.assertIsNotNone(poller.path_is_restricted(self.path))


if __name__ == "__main__":
    unittest.main()
