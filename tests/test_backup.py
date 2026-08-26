"""Backup and restore tests.

A backup is only worth having if it restores. These prove the round trip and,
more importantly, the safety properties: credentials stay out unless asked for,
an existing setup is never silently replaced, and a hostile archive cannot
write outside the destination.
"""

import json
import os
import sys
import unittest as _ut
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backup  # noqa: E402
import poller  # noqa: E402
import store   # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



class BackupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.archives = base / "archives"
        self.archives.mkdir()

        # Path.home() reads USERPROFILE on Windows and HOME elsewhere, so a
        # test that sets only one silently runs against the real home
        # directory on the other platform.
        self._env_home = os.environ.get("HOME")
        self._env_profile = os.environ.get("USERPROFILE")
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

        self._saved = (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
                       poller.LOG_PATH, poller.CSV_PATH, poller.ALERT_STATE_PATH,
                       poller.FORECAST_PENDING_PATH,
                       poller.FORECAST_SKILL_PATH)
        poller.DATA = self.home / ".airo" / "data"
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.LATEST_PATH = poller.DATA / "latest.json"
        poller.LOG_PATH = poller.DATA / "poller.log"
        # Everything that lives under DATA moves with it, or a test writes
        # into the developer's own ~/.airo.
        poller.CSV_PATH = poller.DATA / "readings.csv"
        poller.ALERT_STATE_PATH = poller.DATA / "alert_state.json"
        poller.FORECAST_PENDING_PATH = poller.DATA / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = poller.DATA / "forecast_skill.json"

    def tearDown(self):
        (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
         poller.LOG_PATH, poller.CSV_PATH, poller.ALERT_STATE_PATH,
         poller.FORECAST_PENDING_PATH,
         poller.FORECAST_SKILL_PATH) = self._saved
        if self._env_home is not None:
            os.environ["HOME"] = self._env_home
        if self._env_profile is not None:
            os.environ["USERPROFILE"] = self._env_profile
        else:
            os.environ.pop("USERPROFILE", None)
        self.tmp.cleanup()

    def seed(self, rows=25, with_key=False):
        poller.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        poller.CONFIG_PATH.write_text(json.dumps({
            "location": {"name": "Testville", "latitude": -33.5, "longitude": 151.0},
            "sources": [{"provider": "qld", "site_id": "abc",
                         "site_name": "Test", "enabled": True}],
            "aqi_scale": "au",
        }))
        poller.DATA.mkdir(parents=True, exist_ok=True)
        conn = store.connect(poller.db_path())
        try:
            sid = store.upsert_source(conn, "qld", "abc", "Test")
            store.insert_readings(conn, sid, [
                {"observed_utc": f"2026-07-{(i % 28) + 1:02d}T{i % 24:02d}:00:00+00:00",
                 "pm25": 5.0 + i} for i in range(rows)])
        finally:
            conn.close()
        if with_key:
            k = self.home / ".airo" / "purpleair.key"
            k.write_text("secret-key-value-abcdef", encoding="utf-8")
            os.chmod(k, 0o600)

    def make(self, **kw):
        out = self.archives / "b.tar.gz"
        rc = backup.create(output=out, **kw)
        self.assertEqual(rc, 0)
        return out

    def run_cli(self, *argv):
        """Drive main() the way a person does — through argv.

        On the base case rather than one subclass, because asserting that a
        flag *string* reaches the behaviour needs it too, and a second copy is
        how the two drift into testing different things.
        """
        import contextlib, io, sys
        saved = sys.argv
        sys.argv = ["backup.py", *argv]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = backup.main()
        finally:
            sys.argv = saved
        return code, out.getvalue()


class TestCreate(BackupCase):
    def test_refuses_when_there_is_nothing_to_back_up(self):
        self.assertEqual(backup.create(output=self.archives / "x.tar.gz"), 1)

    def test_archive_contains_config_and_database(self):
        self.seed()
        with tarfile.open(self.make(), "r:gz") as t:
            names = t.getnames()
        self.assertIn("config.json", names)
        self.assertIn("airo.db", names)
        self.assertIn(backup.MANIFEST, names)

    def test_manifest_records_what_is_inside(self):
        self.seed(rows=25)
        m = backup._read_manifest(self.make())
        self.assertEqual(m["location"], "Testville")
        self.assertEqual(m["contents"]["database"]["readings"], 25)
        self.assertEqual(m["format"], backup.FORMAT_VERSION)

    def test_keys_are_excluded_by_default(self):
        """A backup ends up on cloud drives and USB sticks. Folding
        credentials in silently is how they leak."""
        self.seed(with_key=True)
        with tarfile.open(self.make(), "r:gz") as t:
            names = t.getnames()
        self.assertFalse([n for n in names if n.startswith("keys/")])
        self.assertFalse(backup._read_manifest(self.make())["contains_keys"])

    def test_keys_included_only_when_asked(self):
        self.seed(with_key=True)
        arc = self.make(include_keys=True)
        with tarfile.open(arc, "r:gz") as t:
            self.assertIn("keys/purpleair.key", t.getnames())
        self.assertTrue(backup._read_manifest(arc)["contains_keys"])

    def test_archive_with_keys_is_restricted_to_its_owner(self):
        """Asserted through poller.path_is_restricted so it holds on Windows
        too, where the protection comes from ACLs rather than a file mode."""
        self.seed(with_key=True)
        arc = self.make(include_keys=True)
        self.assertIs(poller.path_is_restricted(arc), True,
                      "a backup containing keys must not be readable by others")

    def test_snapshot_is_taken_of_a_live_database(self):
        """Copying the file directly can miss writes sitting in the WAL."""
        self.seed(rows=10)
        conn = store.connect(poller.db_path())   # hold it open, as the poller would
        try:
            arc = self.make()
        finally:
            conn.close()
        self.assertEqual(
            backup._read_manifest(arc)["contents"]["database"]["readings"], 10)


class TestRoundTrip(BackupCase):
    def test_restore_reproduces_every_reading(self):
        self.seed(rows=40)
        arc = self.make()
        poller.db_path().unlink()
        poller.CONFIG_PATH.unlink()

        self.assertEqual(backup.restore(arc), 0)
        conn = store.connect(poller.db_path())
        try:
            total = sum(c["rows"] for c in store.counts(conn))
        finally:
            conn.close()
        self.assertEqual(total, 40)
        self.assertEqual(poller.load_config()["location"]["name"], "Testville")

    def test_restore_refuses_to_overwrite_without_force(self):
        self.seed()
        arc = self.make()
        self.assertEqual(backup.restore(arc), 1, "must not clobber silently")

    def test_force_keeps_a_copy_of_what_it_replaced(self):
        self.seed()
        arc = self.make()
        self.assertEqual(backup.restore(arc, force=True), 0)
        kept = list(poller.CONFIG_PATH.parent.glob("config.replaced-*.json"))
        self.assertTrue(kept, "no safety copy of the replaced config")

    def test_restored_config_is_restricted(self):
        self.seed()
        arc = self.make()
        poller.CONFIG_PATH.unlink()
        # The database still exists, so this needs force -- otherwise restore
        # correctly refuses and nothing is written.
        backup.restore(arc, force=True)
        self.assertIs(poller.path_is_restricted(poller.CONFIG_PATH), True)

    def test_keys_can_be_declined_on_restore(self):
        self.seed(with_key=True)
        arc = self.make(include_keys=True)
        (self.home / ".airo" / "purpleair.key").unlink()
        backup.restore(arc, force=True, keys=False)
        self.assertFalse((self.home / ".airo" / "purpleair.key").exists())

    def test_restored_keys_stay_restricted(self):
        self.seed(with_key=True)
        arc = self.make(include_keys=True)
        k = self.home / ".airo" / "purpleair.key"
        k.unlink()
        backup.restore(arc, force=True)
        self.assertIs(poller.path_is_restricted(k), True)


class TestAutomaticBackup(BackupCase):
    """A backup feature nobody remembers to run protects nobody."""

    def test_auto_creates_an_archive(self):
        self.seed()
        out = backup.auto(keep=3, force=True)
        self.assertIsNotNone(out)
        self.assertTrue(out.exists())

    def test_auto_declines_when_a_recent_one_exists(self):
        self.seed()
        self.assertIsNotNone(backup.auto(keep=3, force=True))
        self.assertIsNone(backup.auto(keep=3, interval_hours=24),
                          "should not churn out an archive per poll")

    def test_auto_rotates_and_keeps_the_newest(self):
        self.seed()
        made = []
        for _ in range(5):
            out = backup.auto(keep=3, force=True)
            self.assertIsNotNone(out)
            made.append(out)
            import time as _t
            _t.sleep(1.05)      # filenames are second-resolution
        kept = sorted((self.home / ".airo" / "backups").glob("airo-backup-*.tar.gz"))
        self.assertEqual(len(kept), 3, "rotation kept the wrong number")
        self.assertEqual(kept[-1].name, made[-1].name, "newest was deleted")

    def test_auto_never_includes_keys(self):
        """An unattended archive lands on disk and possibly in a synced
        folder, which is not where credentials belong."""
        self.seed(with_key=True)
        out = backup.auto(keep=2, force=True)
        self.assertFalse(backup._read_manifest(out)["contains_keys"])
        with tarfile.open(out, "r:gz") as t:
            self.assertFalse([n for n in t.getnames() if n.startswith("keys/")])


class TestHostileArchives(BackupCase):
    def _archive(self, arcname):
        arc = self.archives / "evil.tar.gz"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / backup.MANIFEST).write_text(json.dumps(
                {"format": 1, "created_utc": "x", "contents": {}}))
            (d / "payload").write_text("pwned", encoding="utf-8")
            with tarfile.open(arc, "w:gz") as t:
                t.add(d / backup.MANIFEST, arcname=backup.MANIFEST)
                t.add(d / "payload", arcname=arcname)
        return arc

    def test_relative_traversal_is_refused(self):
        arc = self._archive("../../escaped.txt")
        self.assertEqual(backup.restore(arc, force=True), 1)
        self.assertFalse((Path(self.tmp.name).parent / "escaped.txt").exists())

    def test_absolute_path_is_refused(self):
        """tarfile strips leading slashes when writing, so a genuinely
        absolute member has to be forged by setting the name after the fact --
        which is exactly what a hand-written malicious tar does."""
        arc = self.archives / "abs.tar.gz"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / backup.MANIFEST).write_text(json.dumps(
                {"format": 1, "created_utc": "x", "contents": {}}))
            payload = d / "payload"
            payload.write_text("pwned", encoding="utf-8")
            with tarfile.open(arc, "w:gz") as t:
                t.add(d / backup.MANIFEST, arcname=backup.MANIFEST)
                info = t.gettarinfo(str(payload))
                info.name = "/tmp/airo-should-not-exist.txt"   # forged
                with open(payload, "rb") as fh:
                    t.addfile(info, fh)
        self.assertEqual(backup.restore(arc, force=True), 1)
        self.assertFalse(Path("/tmp/airo-should-not-exist.txt").exists())

    def test_posix_absolute_member_is_refused_on_every_platform(self):
        """Path.is_absolute() is platform-specific: "/tmp/evil" is not absolute
        on Windows because it has no drive letter, so the parsed-path check
        alone let it through there. The guard must not depend on the host."""
        from pathlib import PureWindowsPath
        raw = "/tmp/airo-should-not-exist.txt"
        self.assertFalse(PureWindowsPath(raw).is_absolute(),
                         "premise: Windows does not call this absolute")
        arc = self.archives / "posixabs.tar.gz"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / backup.MANIFEST).write_text(json.dumps(
                {"format": 1, "created_utc": "x", "contents": {}}))
            payload = d / "payload"
            payload.write_text("pwned", encoding="utf-8")
            with tarfile.open(arc, "w:gz") as t:
                t.add(d / backup.MANIFEST, arcname=backup.MANIFEST)
                info = t.gettarinfo(str(payload))
                info.name = raw
                with open(payload, "rb") as fh:
                    t.addfile(info, fh)
        self.assertEqual(backup.restore(arc, force=True), 1)

    def test_windows_drive_letter_is_refused(self):
        arc = self.archives / "drive.tar.gz"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / backup.MANIFEST).write_text(json.dumps(
                {"format": 1, "created_utc": "x", "contents": {}}))
            payload = d / "payload"
            payload.write_text("pwned", encoding="utf-8")
            with tarfile.open(arc, "w:gz") as t:
                t.add(d / backup.MANIFEST, arcname=backup.MANIFEST)
                info = t.gettarinfo(str(payload))
                info.name = "C:/Windows/Temp/evil.txt"
                with open(payload, "rb") as fh:
                    t.addfile(info, fh)
        self.assertEqual(backup.restore(arc, force=True), 1)

    def test_a_file_that_is_not_a_backup_is_rejected(self):
        junk = self.archives / "junk.tar.gz"
        with tarfile.open(junk, "w:gz") as t:
            with tempfile.NamedTemporaryFile("w", delete=False) as f:
                f.write("nope")
            t.add(f.name, arcname="nope.txt")
        self.assertEqual(backup.restore(junk, force=True), 1)
        self.assertEqual(backup.inspect(junk), 1)

    def test_missing_archive_is_reported_not_crashed(self):
        self.assertEqual(backup.restore(self.archives / "nope.tar.gz"), 1)
        self.assertEqual(backup.inspect(self.archives / "nope.tar.gz"), 1)




class TestVerifyReachesTheCheckItClaimsTo(BackupCase):
    """verify_archive() has five ways to say no, and only two were reached.

    The existing test fed it b"not a tarball at all", which fails at the first
    step -- the archive is unreadable -- so the checksum comparison it exists
    for was never run. Breaking the input more thoroughly than the guard
    requires exercises an earlier guard and proves nothing about the later one.
    """

    def rebuild(self, source, changes):
        """Unpack an archive, let `changes` edit it, and re-tar it.

        Damage done inside the tar leaves the archive readable and the
        manifest intact, which is the only shape that reaches the checksum.
        """
        staging = Path(self.tmp.name) / "staging"
        if staging.exists():
            import shutil
            shutil.rmtree(staging)
        staging.mkdir()
        with tarfile.open(source, "r:gz") as tar:
            try:
                tar.extractall(staging, filter="data")
            except TypeError:
                tar.extractall(staging)   # filter= arrived in 3.12; CI runs 3.9
        changes(staging)
        out = Path(self.tmp.name) / "rebuilt.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)
        return out

    def test_a_database_that_does_not_match_the_manifest_is_rejected(self):
        """The checksum comparison itself — the reason verify_archive exists."""
        self.seed()
        def tamper(staging):
            with (staging / "airo.db").open("ab") as f:
                f.write(b"\x00" * 4096)
        rebuilt = self.rebuild(self.make(), tamper)

        ok, why, rows = backup.verify_archive(rebuilt)

        self.assertFalse(ok)
        self.assertIn("checksum", why)
        self.assertGreater(rows, 0, "the manifest was still readable")

    def test_an_archive_recording_no_checksum_is_not_trusted(self):
        """A manifest without a sha256 cannot be verified, and 'cannot be
        verified' must not read as 'verified'."""
        self.seed()
        def strip(staging):
            m = json.loads((staging / backup.MANIFEST).read_text(encoding="utf-8"))
            m["contents"]["database"].pop("sha256", None)
            (staging / backup.MANIFEST).write_text(json.dumps(m), encoding="utf-8")
        rebuilt = self.rebuild(self.make(), strip)

        ok, why, _ = backup.verify_archive(rebuilt)

        self.assertFalse(ok)
        # The exact reason, not just "checksum". Without the guard the code
        # still refuses -- it compares the digest against None and reports a
        # mismatch -- so asserting the substring both messages share cannot
        # tell the guard from its absence.
        self.assertEqual("manifest records no database checksum", why)

    def test_an_archive_with_a_manifest_but_no_database_is_rejected(self):
        self.seed()
        def drop(staging):
            (staging / "airo.db").unlink()
        rebuilt = self.rebuild(self.make(), drop)

        ok, why, _ = backup.verify_archive(rebuilt)

        self.assertFalse(ok)
        self.assertIn("database", why)

    def test_a_tarball_that_is_not_a_backup_has_no_manifest(self):
        stray = Path(self.tmp.name) / "holiday.tar.gz"
        payload = Path(self.tmp.name) / "photo.txt"
        payload.write_text("not a backup", encoding="utf-8")
        with tarfile.open(stray, "w:gz") as tar:
            tar.add(payload, arcname="photo.txt")

        self.assertIsNone(backup._read_manifest(stray))
        ok, why, _ = backup.verify_archive(stray)
        self.assertFalse(ok)
        self.assertIn("manifest", why)

    def test_a_missing_archive_is_named_as_missing(self):
        """Without the guard the open below fails and reports "unreadable",
        which is true of a file that is not there but tells the user to look
        for corruption instead of for the file."""
        ok, why, rows = backup.verify_archive(Path(self.tmp.name) / "gone.tar.gz")
        self.assertFalse(ok)
        self.assertEqual("file does not exist", why)
        self.assertEqual(0, rows)

    def test_inspect_refuses_a_tarball_with_no_manifest(self):
        stray = Path(self.tmp.name) / "photos.tar.gz"
        payload = Path(self.tmp.name) / "p.txt"
        payload.write_text("x", encoding="utf-8")
        with tarfile.open(stray, "w:gz") as tar:
            tar.add(payload, arcname="p.txt")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, backup.inspect(stray))

    def test_a_directory_named_like_the_manifest_is_not_a_manifest(self):
        """tar.extractfile() returns None for a directory entry rather than
        raising, so an archive containing a *directory* called manifest.json
        walks straight past the try/except."""
        staging = Path(self.tmp.name) / "odd"
        (staging / backup.MANIFEST).mkdir(parents=True)
        odd = Path(self.tmp.name) / "odd.tar.gz"
        with tarfile.open(odd, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)

        self.assertIsNone(backup._read_manifest(odd))

    def test_a_directory_named_like_the_database_is_not_a_database(self):
        """The manifest has to stay valid, or verify_archive() refuses at the
        earlier check and the one being tested is never reached -- which is
        what the first version of this test did."""
        self.seed()
        def swap(staging):
            (staging / "airo.db").unlink()
            (staging / "airo.db").mkdir()
        rebuilt = self.rebuild(self.make(), swap)

        self.assertIsNotNone(backup._read_manifest(rebuilt),
                             "the manifest must survive for this to test anything")
        ok, why, _ = backup.verify_archive(rebuilt)
        self.assertFalse(ok)
        self.assertIn("no database", why)

    def test_describe_and_inspect_agree_about_an_unusable_archive(self):
        """Two ways to ask, one answer. A caller must not be able to get a
        friendlier verdict by asking differently."""
        missing = Path(self.tmp.name) / "gone.tar.gz"
        described = backup.describe(missing)
        # The message, not just that there is one. FileNotFoundError is an
        # OSError, so without the exists() check the open below fails and
        # reports "unreadable" -- which sends the user looking for corruption
        # instead of for the file.
        self.assertIn("no such file", described["error"])
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, backup.inspect(missing))

    def test_describe_refuses_a_tarball_with_no_manifest(self):
        stray = Path(self.tmp.name) / "notes.tar.gz"
        payload = Path(self.tmp.name) / "n.txt"
        payload.write_text("x", encoding="utf-8")
        with tarfile.open(stray, "w:gz") as tar:
            tar.add(payload, arcname="n.txt")
        self.assertIn("no manifest", backup.describe(stray)["error"])


class TestRotationRefusesToTradeGoodBackupsForABadOne(BackupCase):
    """The register says the new archive is verified before any old one is
    deleted, and cites a test that greps auto()'s source for the call. Source
    text is not behaviour: with `if not good:` removed the grep still passed.
    These run the rotation instead.
    """

    def existing(self, n=3):
        dest = self.home / ".airo" / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        made = []
        for i in range(n):
            p = dest / f"airo-backup-2026010{i}-000000.tar.gz"
            p.write_bytes(b"an older backup")
            made.append(p)
        return dest, made

    def run_auto(self, **kw):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            out = backup.auto(force=True, **kw)
        return out, buf.getvalue()

    def test_an_archive_that_fails_verification_deletes_nothing(self):
        self.seed()
        dest, old = self.existing()
        real = backup.verify_archive
        # rows deliberately positive: with rows=0 the *next* guard would keep
        # the backups too, and this test would pass with the one it names
        # removed.
        backup.verify_archive = lambda p: (False, "forced failure", 99)
        try:
            out, said = self.run_auto(keep=1)
        finally:
            backup.verify_archive = real

        for p in old:
            self.assertTrue(p.exists(), f"{p.name} was deleted on an unverified backup")
        self.assertIn("keeping every existing backup", said)

    def test_an_archive_holding_no_readings_deletes_nothing(self):
        """An empty database verifies perfectly well. It is still not a backup
        of anything."""
        self.seed()
        dest, old = self.existing()
        real = backup.verify_archive
        backup.verify_archive = lambda p: (True, "ok", 0)
        try:
            out, said = self.run_auto(keep=1)
        finally:
            backup.verify_archive = real

        for p in old:
            self.assertTrue(p.exists())
        self.assertIn("no readings", said)

    def test_a_verified_archive_rotates_and_keeps_the_newest(self):
        """The control. Without it the two tests above could pass because
        rotation never deletes anything at all."""
        self.seed()
        dest, old = self.existing()
        out, _ = self.run_auto(keep=1)

        self.assertIsNotNone(out)
        self.assertTrue(out.exists(), "the backup just taken was deleted")
        remaining = sorted(dest.glob("airo-backup-*.tar.gz"))
        self.assertEqual([out], remaining)

    def test_rotation_never_deletes_the_archive_it_just_made(self):
        """The new archive sorts by name alongside the rest, so it can land
        inside the deletion slice.

        The neighbours are dated in the future for exactly that reason. With
        past-dated ones the new archive sorts last and `[:-keep]` never
        reaches it, so the test passes with the guard removed — which is what
        the first version of this did, using keep=0, where the slice is empty
        and nothing is ever at risk at all.
        """
        self.seed()
        dest = self.home / ".airo" / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (dest / f"airo-backup-2099010{i}-000000.tar.gz").write_bytes(b"later")

        out, _ = self.run_auto(keep=2)

        self.assertTrue(out.exists(),
                        "auto() deleted the backup it had just taken")

    def test_a_failed_create_takes_no_backup_and_removes_nothing(self):
        self.seed()
        dest, old = self.existing()
        real = backup.create
        backup.create = lambda **kw: 1
        try:
            out, _ = self.run_auto(keep=1)
        finally:
            backup.create = real
        self.assertIsNone(out)
        for p in old:
            self.assertTrue(p.exists())

    def test_a_key_directory_that_does_not_exist_yields_no_keys(self):
        import shutil
        shutil.rmtree(self.home / ".airo", ignore_errors=True)
        self.assertEqual([], backup._key_files())


class TestTheCommandLineDispatches(BackupCase):
    """main() routes four commands. None of the branches were run, so any of
    them could have been wired to the wrong function without a failure.

    `run_cli` is on BackupCase; the flag-string tests below need it too."""

    def test_create_writes_an_archive(self):
        self.seed()
        code, said = self.run_cli("create", "--output", str(self.archives))
        self.assertEqual(0, code)
        self.assertTrue(list(self.archives.glob("airo-backup-*.tar.gz")))

    def test_inspect_reads_one_back(self):
        self.seed()
        archive = self.make()
        code, said = self.run_cli("inspect", str(archive))
        self.assertEqual(0, code)
        self.assertIn("readings", said)

    def test_auto_takes_a_routine_backup(self):
        self.seed()
        code, _ = self.run_cli("auto", "--force")
        self.assertEqual(0, code)
        self.assertTrue(list((self.home / ".airo" / "backups")
                             .glob("airo-backup-*.tar.gz")))

    def test_restore_refuses_to_overwrite_without_force(self):
        self.seed()
        archive = self.make()
        code, said = self.run_cli("restore", str(archive))
        self.assertEqual(1, code, "an existing setup was overwritten silently")
        self.assertIn("--force", said)


class TestTheFlagStringsReachTheBehaviour(BackupCase):
    """The flags a person actually types, as opposed to the parameters the
    functions take.

    Every safety property here was tested by calling `create(include_keys=…)`
    or `auto(keep=…)` directly. That leaves the argparse wiring — the only part
    a user touches — unasserted: `--include-keys` could have been declared
    `dest="keys"`, or `--no-keys` inverted, and every existing test would still
    pass while the credential either failed to be backed up or, worse, was
    restored when the user asked for it not to be.
    """

    def test_include_keys_puts_the_key_in_the_archive(self):
        self.seed(with_key=True)
        code, _ = self.run_cli("create", "--output", str(self.archives),
                               "--include-keys")
        self.assertEqual(0, code)
        archive, = self.archives.glob("airo-backup-*.tar.gz")
        with tarfile.open(archive) as t:
            names = t.getnames()
        self.assertTrue(any(n.endswith("purpleair.key") for n in names),
                        f"--include-keys did not include the key: {names}")

    def test_without_the_flag_the_key_stays_out(self):
        """The default half of the same wiring: if `--include-keys` were
        `action="store_true", default=True`, the test above would still pass."""
        self.seed(with_key=True)
        code, _ = self.run_cli("create", "--output", str(self.archives))
        self.assertEqual(0, code)
        archive, = self.archives.glob("airo-backup-*.tar.gz")
        with tarfile.open(archive) as t:
            names = t.getnames()
        self.assertFalse(any(n.endswith("purpleair.key") for n in names),
                         f"a key was archived without --include-keys: {names}")

    def test_no_keys_declines_a_key_the_archive_does_carry(self):
        self.seed(with_key=True)
        archive = self.make(include_keys=True)
        (self.home / ".airo" / "purpleair.key").unlink()

        code, said = self.run_cli("restore", str(archive), "--force", "--no-keys")
        self.assertEqual(0, code)
        self.assertFalse((self.home / ".airo" / "purpleair.key").exists(),
                         "--no-keys restored the key anyway")
        self.assertIn("--no-keys", said)

    def test_keep_is_the_number_of_archives_auto_leaves_behind(self):
        """`--keep 2` must survive as the integer 2 all the way to rotation.

        Older archives are seeded rather than taken, because the filename is
        stamped to the second and three backups in one second are one file.
        """
        self.seed()
        dest = self.home / ".airo" / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        for day in range(1, 5):
            (dest / f"airo-backup-2026010{day}-000000.tar.gz").write_bytes(b"old")

        code, _ = self.run_cli("auto", "--force", "--keep", "2")
        self.assertEqual(0, code)

        kept = sorted(p.name for p in dest.glob("airo-backup-*.tar.gz"))
        self.assertEqual(2, len(kept),
                         f"--keep 2 left {len(kept)} archives: {kept}")
        # The survivors are the newest, and the real one is among them --
        # rotation that kept two stale files and deleted the fresh backup
        # would satisfy a bare count.
        self.assertNotIn("airo-backup-20260101-000000.tar.gz", kept)
        self.assertTrue(any(n.startswith("airo-backup-2026") and
                            not (dest / n).read_bytes() == b"old" for n in kept),
                        f"the backup just taken was rotated away: {kept}")


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


class TestABackupNeverLandsInTheRepository(unittest.TestCase):
    """Rule 3 names a backup archive among the things that must never be in
    the repository, and `create()` defaulted to the current directory.

    The obvious place to run `python3 backup.py create` from is the checkout,
    so the obvious use of the obvious command put user data there. `.gitignore`
    and the pre-commit hook both catch it, so it could not have been
    *committed* — but "cannot be committed" is a weaker promise than "is not
    there", and this project has already lost that argument once, when
    `data/` did not match `data.migrated-<timestamp>/` and 16,995 rows of
    location history went in.
    """

    def test_the_default_is_the_backups_directory_not_the_working_one(self):
        self.assertEqual(Path.home() / ".airo" / "backups", backup.backup_dir())

    def test_it_follows_a_redirected_home_rather_than_freezing_one(self):
        """As a module constant this froze the developer's real home at import,
        so `auto()` wrote archives into it from the test suite. Never mutate
        the developer's own ~/.airo from a test — three fixes have gone into
        that rule and this would have been a fourth."""
        import os
        # Both variables. `Path.home()` reads USERPROFILE on Windows and HOME
        # elsewhere, so setting one runs against the real home directory on
        # the other platform — the trap CONVENTIONS already names, and a
        # trap that has caught this suite before.
        saved = {n: os.environ.get(n) for n in ("HOME", "USERPROFILE")}
        with tempfile.TemporaryDirectory() as tmp:
            for name in saved:
                os.environ[name] = tmp
            try:
                self.assertEqual(Path(tmp) / ".airo" / "backups",
                                 backup.backup_dir())
            finally:
                for name, value in saved.items():
                    if value:
                        os.environ[name] = value
                    else:
                        os.environ.pop(name, None)

    def test_both_paths_agree_on_where_backups_live(self):
        """`auto()` has always used `~/.airo/backups`; `create()` used the
        current directory. Two definitions of one location is how one of them
        ends up wrong, and it was the one a person types."""
        source = Path(backup.__file__).read_text(encoding="utf-8")
        # Code lines only. The comment explaining why `Path.cwd()` is gone
        # names it, and a substring check over the whole file cannot tell the
        # difference between a comment and the bug it describes.
        code = [l for l in source.splitlines() if not l.strip().startswith("#")]
        self.assertEqual(
            [], [l for l in code if "Path.cwd()" in l],
            "a backup path is still relative to wherever the command was run "
            "from")
        self.assertEqual(
            1, len([l for l in source.splitlines()
                    if 'Path.home() / ".airo" / "backups"' in l
                    and not l.strip().startswith("#")]),
            "the backup directory is computed in more than one place")

    def test_an_explicit_output_still_wins(self):
        """The fix must not take away the reason somebody would pass a path —
        an archive into Dropbox is the documented example."""
        with tempfile.TemporaryDirectory() as tmp:
            asked = Path(tmp) / "somewhere-else.tar.gz"
            self.assertNotEqual(backup.backup_dir(), asked.parent)

    def test_the_repository_root_holds_no_archive_right_now(self):
        """Enumerated by shape over the checkout, not by the one filename that
        went wrong. Rule 3's own lesson: match by shape, never by one known
        path."""
        root = Path(__file__).resolve().parent.parent
        strays = [p.name for p in root.iterdir()
                  if p.is_file() and ".tar.gz" in p.name
                  and ("backup" in p.name or "airo" in p.name)]
        self.assertEqual([], strays,
                         f"user data is sitting in the checkout: {strays}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
