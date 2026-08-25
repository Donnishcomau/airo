"""Retention, data location, and the promise that nothing deletes silently.

Rule 5 says the poller must never lose data. That is not only about crashes:
the ways a logger actually loses history are mundane — a retention window
someone set and forgot, a source toggled off, a backup rotation that ran on a
corrupt archive, a data directory pointed somewhere new. Each is tested here
for the same property: it either does not delete, or it says so.
"""

import json
import sqlite3
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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



def ts(minutes_ago):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "qld", "wbk", "Westbrook")

    def seed(self, days):
        rows = [{"observed_utc": ts(d * 1440), "pm25": 5.0} for d in days]
        store.insert_readings(self.conn, self.sid, rows)

    def count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM readings").fetchone()["n"]


class TestKeepEverythingIsTheDefault(StoreCase):
    """History is the point. A tool that quietly discards a record of what
    someone breathed, because a disk looked full, would be worse than useless."""

    def test_the_shipped_default_keeps_everything(self):
        self.assertEqual(poller.DEFAULT_CONFIG.get("retention_days"), 0)

    def test_the_example_config_keeps_everything(self):
        cfg = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertIn(cfg.get("retention_days", 0), (0, None))

    def test_zero_prunes_nothing_however_old(self):
        self.seed([0, 400, 4000])
        removed, kept, _ = store.prune(self.conn, 0)
        self.assertEqual(removed, 0)
        self.assertEqual(self.count(), 3)

    def test_none_prunes_nothing(self):
        self.seed([0, 4000])
        self.assertEqual(store.prune(self.conn, None)[0], 0)
        self.assertEqual(self.count(), 2)

    def test_a_negative_window_prunes_nothing(self):
        """A negative retention is a config error, not an instruction to
        delete everything."""
        self.seed([0, 4000])
        self.assertEqual(store.prune(self.conn, -30)[0], 0)
        self.assertEqual(self.count(), 2)


class TestPrune(StoreCase):
    def test_a_dry_run_deletes_nothing(self):
        self.seed([1, 100, 200])
        removed, _, _ = store.prune(self.conn, 30, dry_run=True)
        self.assertEqual(removed, 2, "the preview miscounted")
        self.assertEqual(self.count(), 3, "a dry run deleted rows")

    def test_the_preview_matches_what_is_actually_removed(self):
        """A preview a user cannot trust is worse than none."""
        self.seed([1, 5, 100, 200, 900])
        predicted, _, _ = store.prune(self.conn, 30, dry_run=True)
        actual, _, _ = store.prune(self.conn, 30)
        self.assertEqual(predicted, actual)

    def test_readings_inside_the_window_survive(self):
        self.seed([1, 5, 29, 31, 400])
        store.prune(self.conn, 30)
        self.assertEqual(self.count(), 3)

    def test_pruning_reports_what_it_removed(self):
        self.seed([1, 100])
        removed, kept, oldest = store.prune(self.conn, 30)
        self.assertEqual(removed, 1)
        self.assertEqual(kept, 1)
        self.assertIsNotNone(oldest)

    def test_the_poller_only_prunes_when_asked(self):
        """The automatic path must be gated on a finite window, and must log
        whenever it removes anything.

        Anchored on the code rather than on a numbered comment. It used to
        find its block with src.index("# 4. retention"), so inserting a step
        earlier in do_poll renumbered the comment and the test errored — a
        test that breaks when a *comment* is edited is asserting the wrong
        thing, and would equally have kept passing if the comment stayed while
        the guard went.
        """
        import inspect
        src = inspect.getsource(poller.do_poll)
        i = src.index("retention_days")
        block = src[i:i + 700]
        self.assertIn("if keep > 0:", block,
                      "automatic pruning is no longer gated on a finite window")
        self.assertIn("log(", block, "automatic pruning does not announce itself")


class TestDisablingASourceKeepsItsHistory(StoreCase):
    def test_remove_source_only_disables(self):
        self.seed([1, 2, 3])
        store.remove_source(self.conn, "qld", "wbk")
        self.assertEqual(self.count(), 3,
                         "toggling a source off destroyed its readings")

    def test_remove_source_cannot_be_asked_to_delete(self):
        """It used to take delete_readings=True, and readings.source_id is
        ON DELETE CASCADE — so that one argument silently erased every reading
        the source had produced. Nothing called it; it sat there anyway."""
        import inspect as _inspect
        params = _inspect.signature(store.remove_source).parameters
        self.assertNotIn("delete_readings", params)

    def test_forgetting_a_source_exports_before_it_deletes(self):
        self.seed([1, 2, 3])
        with tempfile.TemporaryDirectory() as out:
            rows, exported = store.forget_source(
                self.conn, "qld", "wbk", export_dir=out)
            self.assertEqual(rows, 3)
            self.assertIsNotNone(exported, "deleted without exporting first")
            self.assertTrue(Path(exported).exists())
        self.assertEqual(self.count(), 0)

    def test_forgetting_can_be_previewed(self):
        self.seed([1, 2])
        rows, exported = store.forget_source(
            self.conn, "qld", "wbk", dry_run=True)
        self.assertEqual(rows, 2)
        self.assertIsNone(exported)
        self.assertEqual(self.count(), 2, "a dry run deleted rows")

    def test_forgetting_an_unknown_source_is_a_no_op(self):
        self.seed([1])
        rows, _ = store.forget_source(self.conn, "qld", "nope")
        self.assertEqual(rows, 0)
        self.assertEqual(self.count(), 1)


class TestDataLocation(unittest.TestCase):
    def test_the_env_var_wins(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def _resolve_data_dir")
        block = src[i:i + 1600]
        self.assertLess(block.index("AIRO_DATA"), block.index("_configured_data_dir"),
                        "the config would override an explicit env var")

    def test_a_broken_config_does_not_hide_the_database(self):
        """load_config() needs DATA to resolve first, and a config that fails
        to parse must not stop the poller finding a database it has been
        writing to for years."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def _configured_data_dir")
        block = src[i:i + 700]
        self.assertIn("except (OSError, ValueError)", block)

    def test_an_existing_project_data_dir_is_preferred_over_an_empty_one(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def _resolve_data_dir")
        self.assertIn('(legacy / "airo.db").exists()', src[i:i + 1800])


class TestOrphanedDatabaseIsNoticed(unittest.TestCase):
    """Making the location configurable creates a way to abandon years of
    readings by editing one line. The rows are not deleted, which is why it is
    not a data-loss bug in the strict sense — it is worse in practice: the
    user believes they are logging, and they are, into a file they will never
    look at."""

    def test_the_active_directory_is_recorded(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertIn("def remember_data_dir", src)
        self.assertIn("remember_data_dir()", src.split("def remember_data_dir")[0]
                      + src.split("def remember_data_dir")[1],
                      "the marker is never written")

    def test_the_previous_directory_is_checked_first(self):
        """Checking only well-known locations would miss the case this exists
        for: an arbitrary path only the old config knew."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def other_databases")
        block = src[i:i + 1400]
        self.assertIn("_remembered_data_dir()", block)
        self.assertLess(block.index("_remembered_data_dir()"),
                        block.index('".airo" / "data"'))

    def test_the_warning_names_the_path_and_the_fix(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def warn_about_orphans")
        block = src[i:i + 1200]
        self.assertIn("--migrate-data", block)
        self.assertIn("data_dir", block)

    def test_status_where_and_doctor_all_surface_it(self):
        """A warning only shown by a command nobody runs is not a warning."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("warn_about_orphans()"), 3)


class TestBackupRotation(unittest.TestCase):
    def test_rotation_verifies_the_new_archive_first(self):
        """create() returning 0 only means it believed it succeeded. Rotating
        on a truncated write would trade every good backup for one bad one,
        unattended, on a schedule."""
        src = (ROOT / "backup.py").read_text(encoding="utf-8")
        # Inside auto() specifically, not merely somewhere in the file -- the
        # function definition appears earlier and would satisfy a naive search
        # even if the call were removed.
        i = src.index("def auto(")
        body = src[i:src.index("# Rotate.", i)]
        self.assertIn("verify_archive(", body,
                      "old backups are deleted before the new one is checked")
        self.assertIn("return out", body,
                      "a failed verification does not stop the rotation")

    def test_a_failed_rotation_is_reported(self):
        src = (ROOT / "backup.py").read_text(encoding="utf-8")
        i = src.index("# Rotate.")
        block = src[i:i + 600]
        self.assertNotIn("pass", block,
                         "a rotation failure is swallowed, so backups pile up "
                         "until a disk fills with nobody told")

    def test_verify_detects_a_corrupt_archive(self):
        import backup
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "airo-backup-broken.tar.gz"
            bad.write_bytes(b"not a tarball at all")
            ok, why, rows = backup.verify_archive(bad)
            self.assertFalse(ok)
            self.assertEqual(rows, 0)

    def test_verify_detects_a_missing_archive(self):
        import backup
        ok, why, _ = backup.verify_archive(Path("/nonexistent/x.tar.gz"))
        self.assertFalse(ok)




class TestSetupAsksWhereDataLives(unittest.TestCase):
    """A setting that can only be changed by hand-editing JSON is not a
    choice the user has been offered."""

    def block(self):
        src = (ROOT / "setup.py").read_text(encoding="utf-8")
        i = src.index("# --- where it lives")
        return src[i:src.index("return {", i)]

    def test_setup_offers_the_location(self):
        self.assertIn("Where should readings be stored", self.block())

    def test_the_default_is_the_standard_location(self):
        """Outside the project folder, so it survives a re-clone."""
        b = self.block()
        self.assertIn('".airo" / "data"', b)

    def test_an_unwritable_path_is_caught_at_setup_time(self):
        """Not on the first poll. A path that cannot be written is how someone
        logs into nowhere for weeks without noticing -- and an unmounted drive
        looks exactly like a typo, so the distinction is made out loud."""
        b = self.block()
        self.assertIn("except OSError", b)
        self.assertIn("mkdir", b)
        self.assertIn("write_text", b, "the path is never actually probed")

    def test_the_probe_file_is_cleaned_up(self):
        self.assertIn("unlink()", self.block())

    def test_an_existing_database_there_is_announced_not_clobbered(self):
        b = self.block()
        self.assertIn("airo.db", b)
        self.assertIn("existing", b.lower())

    def test_choosing_the_default_stores_no_override(self):
        """Writing an absolute path equal to the default would freeze the
        location, so a later move of ~/.airo would silently orphan it."""
        b = self.block()
        self.assertIn("answer == default_dir", b)

    def test_the_setting_is_written_to_the_config(self):
        src = (ROOT / "setup.py").read_text(encoding="utf-8")
        i = src.index("# --- where it lives")
        self.assertIn('"data_dir": data_dir', src[i:i + 2600])

    def test_the_example_config_documents_it(self):
        cfg = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertIn("data_dir", cfg)
        self.assertEqual(cfg["data_dir"], "",
                         "the shipped example must not pin a real path")


class TestUnwritableDataDirFailsClearly(unittest.TestCase):
    """The poller runs unattended on a schedule, so a raw traceback lands in a
    log nobody reads. A path that cannot be written is the ordinary result of a
    mistyped data_dir, an external drive that has not mounted, or a synced
    folder that has not appeared yet."""

    def test_it_explains_rather_than_tracebacks(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def ensure_data_dir")
        block = src[i:i + 1500]
        self.assertIn("except OSError", block)
        self.assertIn("cannot use the data directory", block)

    def test_it_says_which_setting_controls_the_path(self):
        """Otherwise the user has to guess between the env var and the config."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def ensure_data_dir")
        block = src[i:i + 1500]
        self.assertIn("AIRO_DATA", block)
        self.assertIn("data_dir", block)

    def test_it_never_substitutes_another_directory(self):
        """Quietly writing elsewhere is how a user ends up with two databases
        and no idea which is live — the exact failure the orphan detector
        exists to catch, and it must not be created here."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def ensure_data_dir")
        block = src[i:i + 1500]
        self.assertNotIn("Path.home()", block,
                         "a fallback directory would orphan the real database")
        self.assertIn("no other directory was substituted", block)

    def test_the_probe_is_cleaned_up(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def ensure_data_dir")
        block = src[i:i + 1500]
        self.assertIn("probe.unlink()", block)

    def test_failure_is_a_non_zero_exit(self):
        """A scheduler that sees exit 0 records a successful poll."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertIn("if not ensure_data_dir():\n        return 1", src)


class TestMigrateDataFollowsItsOwnAdvice(unittest.TestCase):
    """The orphan warning tells the user to run `--migrate-data`. That command
    used to look only at the pre-v0.6 project folder, so anyone who had moved
    data_dir — the only way to create an orphan at an arbitrary path — was told
    to run a command that reported "nothing to migrate" and left their readings
    exactly where they were. A remedy that does not apply to the case it is
    recommended for is worse than no remedy."""

    def source_block(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def migrate_data_dir")
        return src[i:i + 2200]

    def test_it_discovers_the_orphan_rather_than_assuming_the_project_folder(self):
        b = self.source_block()
        self.assertIn("other_databases()", b,
                      "migration cannot find an orphan at an arbitrary path")

    def test_it_prefers_the_orphan_holding_the_most_readings(self):
        b = self.source_block()
        self.assertIn("max(orphans", b)

    def test_it_still_falls_back_to_the_legacy_project_folder(self):
        b = self.source_block()
        self.assertIn("LEGACY_DATA", b)

    def test_it_refuses_to_merge_two_populated_databases(self):
        """Merging is a different and riskier operation than a move. Guessing
        which rows win would be a data-loss decision made silently."""
        b = self.source_block()
        self.assertIn("not merging automatically", b)
        self.assertIn("nothing has been changed", b)

    def test_it_does_nothing_when_source_and_target_are_the_same(self):
        b = self.source_block()
        self.assertIn("already the directory in use", b)

    def test_the_warning_and_the_command_agree(self):
        """The text the user is shown must name the command that works."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def warn_about_orphans")
        self.assertIn("--migrate-data", src[i:i + 1200])


class TestMovingTheDataDirectory(unittest.TestCase):
    """--migrate-data is the way out of an abandoned database, and every one
    of its refusals was unexercised.

    The failure it exists for: point data_dir at a path that does not exist
    yet -- a typo, an unmounted drive, a synced folder that has not appeared --
    and the poller starts a blank database beside the full one, forever.
    Nothing is deleted, which is why it is not a data-loss bug in the strict
    sense and worse in practice.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        (self.home / ".airo").mkdir(parents=True)
        self.old = base / "old"
        self.new = base / "new"

        self._env = (os.environ.get("HOME"), os.environ.get("USERPROFILE"))
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self._saved = (poller.DATA, poller.CONFIG_PATH, poller.LOG_PATH,
                       poller.LATEST_PATH, poller.CSV_PATH,
                       poller.ALERT_STATE_PATH)
        poller.DATA = self.new
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.LOG_PATH = self.new / "poller.log"
        # Everything under DATA moves with it, or a test writes into the
        # developer's own ~/.airo.
        poller.LATEST_PATH = self.new / "latest.json"
        poller.CSV_PATH = self.new / "readings.csv"
        poller.ALERT_STATE_PATH = self.new / "alert_state.json"
        poller.FORECAST_PENDING_PATH = self.new / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = self.new / "forecast_skill.json"
        self.logged = []
        self._log = poller.log
        poller.log = self.logged.append

    def tearDown(self):
        (poller.DATA, poller.CONFIG_PATH, poller.LOG_PATH,
         poller.LATEST_PATH, poller.CSV_PATH,
         poller.ALERT_STATE_PATH) = self._saved
        poller.log = self._log
        home, profile = self._env
        if home is not None:
            os.environ["HOME"] = home
        if profile is not None:
            os.environ["USERPROFILE"] = profile
        else:
            os.environ.pop("USERPROFILE", None)
        self.tmp.cleanup()

    def populate(self, where, rows=12):
        where.mkdir(parents=True, exist_ok=True)
        conn = store.connect(where / "airo.db")
        try:
            sid = store.upsert_source(conn, "qld", "1", "Site")
            store.insert_readings(conn, sid, [
                {"observed_utc": f"2026-07-{i + 1:02d}T00:00:00+00:00",
                 "pm25": 5.0 + i} for i in range(rows)])
        finally:
            conn.close()
        return rows

    def said(self):
        return "\n".join(self.logged)

    def rows_at(self, where):
        conn = store.connect(where / "airo.db")
        try:
            return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()

    def test_a_migration_moves_every_reading_and_verifies_the_count(self):
        n = self.populate(self.old)
        self.assertTrue(poller.migrate_data_dir(source=self.old))
        self.assertEqual(n, self.rows_at(self.new))

    def test_the_original_is_retired_rather_than_deleted(self):
        """The user removes it once they are satisfied. Deleting it here would
        make an unverified migration unrecoverable."""
        self.populate(self.old)
        poller.migrate_data_dir(source=self.old)
        self.assertFalse(self.old.exists())
        retired = list(Path(self.tmp.name).glob("data.migrated-*"))
        self.assertEqual(1, len(retired), f"original not retired: {retired}")
        self.assertTrue((retired[0] / "airo.db").exists())

    def test_a_dry_run_reports_and_changes_nothing(self):
        self.populate(self.old)
        self.assertFalse(poller.migrate_data_dir(source=self.old, dry_run=True))
        self.assertIn("dry run", self.said())
        self.assertTrue((self.old / "airo.db").exists())
        self.assertFalse((self.new / "airo.db").exists())

    def test_migrating_a_directory_onto_itself_does_nothing(self):
        self.populate(self.new)
        self.assertFalse(poller.migrate_data_dir(source=self.new))
        self.assertIn("already the directory in use", self.said())

    def test_a_source_with_no_database_is_not_a_migration(self):
        self.old.mkdir(parents=True)
        self.assertFalse(poller.migrate_data_dir(source=self.old))
        self.assertIn("no database", self.said())

    def test_two_databases_are_never_merged_automatically(self):
        """Merging is a different and riskier operation than a move. Refused
        rather than guessed at, with both row counts named so the user can
        choose."""
        self.populate(self.old, rows=5)
        self.populate(self.new, rows=9)

        self.assertFalse(poller.migrate_data_dir(source=self.old))

        self.assertIn("not merging", self.said())
        self.assertEqual(5, self.rows_at(self.old), "the orphan was touched")
        self.assertEqual(9, self.rows_at(self.new), "the live database was touched")

    def test_every_file_beside_the_database_travels_with_it(self):
        """Copying only airo.db leaves the sidecars behind, and a database in
        WAL mode keeps recent writes in one of them.

        Asserted with an ordinary sidecar rather than a fabricated -wal. The
        first version wrote b"pretend wal" and checked it arrived, which
        passed on macOS and failed on Linux: migrate_data_dir opens the
        database to count rows before copying, and SQLite discards a malformed
        WAL when it does. That was the test depending on SQLite's housekeeping
        rather than on the guarantee, which is simply that everything in the
        directory is copied.

        The row count either side is what proves no writes were lost — see
        the migration's own verify step.
        """
        self.populate(self.old)
        # Deliberately NOT airo.db-wal or airo.db-shm. Both are SQLite's own,
        # and it creates, checkpoints and removes them as it pleases — the
        # first two versions of this test used one of each and asserted a file
        # SQLite had already tidied away. macOS happened to leave them and
        # Linux did not, so both passed locally and failed in CI.
        #
        # These are files nothing but the copy touches, which is what makes
        # the assertion about migrate_data_dir rather than about SQLite.
        (self.old / "latest.json").write_text("{}", encoding="utf-8")
        (self.old / "poller.log").write_text("a line\n", encoding="utf-8")
        (self.old / "subdir").mkdir()

        poller.migrate_data_dir(source=self.old)

        for name in ("latest.json", "poller.log"):
            self.assertTrue((self.new / name).exists(),
                            f"{name} was left behind")

    def test_a_copy_that_loses_rows_keeps_the_original(self):
        """create-then-verify, not create-then-trust: the count is compared
        after the copy, and a mismatch leaves the original in place."""
        self.populate(self.old)
        real = poller._db_row_count
        calls = []

        def lying_count(path):
            calls.append(path)
            return 999 if len(calls) > 1 else real(path)

        poller._db_row_count = lying_count
        try:
            self.assertFalse(poller.migrate_data_dir(source=self.old))
        finally:
            poller._db_row_count = real

        self.assertIn("row count differs", self.said())
        self.assertTrue((self.old / "airo.db").exists(),
                        "the original was retired after a failed verification")


class TestFindingAnAbandonedDatabase(unittest.TestCase):
    """data_dir is configurable, which is a way to abandon years of readings
    by editing one line. The orphan can only be *named* if it is found."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        (self.home / ".airo").mkdir(parents=True)
        self._env = (os.environ.get("HOME"), os.environ.get("USERPROFILE"))
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self._saved = (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
                       poller.CSV_PATH, poller.ALERT_STATE_PATH, poller.LOG_PATH)
        poller.DATA = base / "current"
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.LATEST_PATH = poller.DATA / "latest.json"
        poller.CSV_PATH = poller.DATA / "readings.csv"
        poller.ALERT_STATE_PATH = poller.DATA / "alert_state.json"
        poller.LOG_PATH = poller.DATA / "poller.log"

    def tearDown(self):
        (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH, poller.CSV_PATH,
         poller.ALERT_STATE_PATH, poller.LOG_PATH) = self._saved
        home, profile = self._env
        if home is not None:
            os.environ["HOME"] = home
        if profile is not None:
            os.environ["USERPROFILE"] = profile
        else:
            os.environ.pop("USERPROFILE", None)
        self.tmp.cleanup()

    def make_db(self, where, rows=3):
        where.mkdir(parents=True, exist_ok=True)
        conn = store.connect(where / "airo.db")
        try:
            sid = store.upsert_source(conn, "qld", "1", "Site")
            store.insert_readings(conn, sid, [
                {"observed_utc": f"2026-07-{i + 1:02d}T00:00:00+00:00",
                 "pm25": 5.0} for i in range(rows)])
        finally:
            conn.close()

    def test_the_remembered_directory_is_where_the_orphan_is_looked_for(self):
        """A fixed list of well-known locations would miss exactly the case
        this exists for: an abandoned database at a path only the old config
        ever knew."""
        elsewhere = Path(self.tmp.name) / "an" / "unusual" / "place"
        self.make_db(elsewhere, rows=7)
        poller.data_marker_path().write_text(str(elsewhere), encoding="utf-8")

        found = poller.other_databases()

        self.assertIn(str(elsewhere), [str(p) for p, _ in found])
        self.assertEqual(7, dict((str(p), n) for p, n in found)[str(elsewhere)])

    def test_the_directory_in_use_is_never_reported_as_an_orphan(self):
        self.make_db(poller.DATA)
        poller.data_marker_path().write_text(str(poller.DATA), encoding="utf-8")
        self.assertEqual([], poller.other_databases())

    def test_a_remembered_directory_holding_no_database_is_not_an_orphan(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        poller.data_marker_path().write_text(str(empty), encoding="utf-8")
        self.assertEqual([], poller.other_databases())

    def test_the_same_directory_reached_two_ways_is_reported_once(self):
        """The known-locations list can name one directory more than once —
        via the marker and via the default path — and reporting it twice reads
        as two abandoned databases."""
        target = self.home / ".airo" / "data"
        self.make_db(target)
        poller.data_marker_path().write_text(str(target), encoding="utf-8")
        found = poller.other_databases()
        self.assertEqual(1, len(found), f"reported more than once: {found}")

    def test_an_orphan_is_announced_wherever_the_user_already_is(self):
        import contextlib, io
        elsewhere = Path(self.tmp.name) / "elsewhere"
        self.make_db(elsewhere, rows=4)
        poller.data_marker_path().write_text(str(elsewhere), encoding="utf-8")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            said = poller.warn_about_orphans()

        self.assertTrue(said)
        self.assertIn(str(elsewhere), out.getvalue())
        self.assertIn("--migrate-data", out.getvalue(),
                      "the user is told there is a problem and not how to fix it")

    def test_nothing_is_announced_when_there_is_no_orphan(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(poller.warn_about_orphans())
        self.assertEqual("", out.getvalue())


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
