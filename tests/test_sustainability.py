# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The project outliving its maintainer, or not -- and what that costs a user.

Airo is one person's side project reading four public APIs. The realistic
failure is not a bug: it is the repository going quiet while someone's four
years of readings sit in it. Every test here protects the same property --
that leaving costs nothing, so staying is never a trap.

A promise in a README does not survive the README. These are the mechanisms.
"""

import json
import sqlite3
import os
import subprocess
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



class TestTheDataOutlivesTheTool(unittest.TestCase):

    def db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "airo.db"
        conn = store.connect(path)
        # Windows refuses to delete a file another handle still holds, so an
        # unclosed connection turns tmp.cleanup() into a PermissionError.
        # Registered after tmp.cleanup so LIFO closes the database first.
        self.addCleanup(conn.close)
        sid = store.upsert_source(conn, "qld", "fer", "Fernway",
                                  latitude=-33.51, longitude=151.01)
        store.insert_readings(conn, sid, [
            {"observed_utc": "2026-01-01T00:00:00+00:00", "pm25": 5.0},
            {"observed_utc": "2026-01-01T00:10:00+00:00", "pm25": 6.5},
        ])
        conn.commit()
        return conn, path

    def test_the_store_is_plain_sqlite_readable_without_airo(self):
        """Anything that can open a .db file can read the whole record --
        no custom format, no proprietary container, no code required."""
        _, path = self.db()
        raw = sqlite3.connect(str(path))
        self.addCleanup(raw.close)
        rows = raw.execute("SELECT pm25 FROM readings ORDER BY observed_utc").fetchall()
        self.assertEqual([r[0] for r in rows], [5.0, 6.5])

    def test_the_file_really_is_sqlite_not_something_wearing_the_extension(self):
        _, path = self.db()
        with open(path, "rb") as f:
            self.assertEqual(f.read(16), b"SQLite format 3\x00")

    def test_export_produces_plain_csv_a_spreadsheet_can_open(self):
        import csv, io
        conn, _ = self.db()
        with tempfile.TemporaryDirectory() as td:
            written = store.export_csv(conn, Path(td))
            self.assertTrue(written, "export produced no file")
            # export_csv returns (path, row_count) per source
            text = Path(written[0][0]).read_text(encoding="utf-8")
        body = [l for l in text.splitlines() if l and not l.startswith("#")]
        rows = list(csv.reader(io.StringIO("\n".join(body))))
        self.assertGreaterEqual(len(rows), 3, "header plus both readings")
        self.assertIn("pm25", rows[0])

    def test_an_export_carries_its_own_terms(self):
        """A CSV outlives the tool that made it and travels without context.
        Whoever finds it in five years needs to know whose data it is and
        what they may do with it."""
        header = "\n".join(store._export_header(
            {"provider": "qld", "site_id": "fer", "site_name": "Fernway"}))
        self.assertTrue(header.strip(), "export has no provenance header")
        low = header.lower()
        self.assertTrue(any(w in low for w in ("cc by", "licen", "attribut")),
                        "export states no licence")

    def test_config_is_json_a_human_can_edit(self):
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertIsInstance(cfg, dict)

    def test_nothing_is_stored_anywhere_but_the_user_s_own_machine(self):
        """There is no server to shut down, so no shutdown can strand anyone."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", src)
        for bind in ('"0.0.0.0"', "'0.0.0.0'"):
            self.assertNotIn(f"bind({bind}", src)


class TestNoLockIn(unittest.TestCase):

    def isolated_env(self):
        """An environment pointing at nothing of the developer's.

        `--help` writes nothing, so these two were harmless in fact. They are
        isolated anyway because the rule is about what a spawned interpreter
        *can* reach, not about what today's arguments happen to do -- and the
        contract that enforces it is worth more when it has no exceptions to
        remember.
        """
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        return dict(os.environ,
                    AIRO_DATA=str(base / "data"),
                    AIRO_CONFIG=str(base / "config.json"),
                    HOME=str(base / "home"),
                    USERPROFILE=str(base / "home"))

    def test_a_full_backup_can_be_taken_without_the_app_running(self):
        """`backup.py` is a standalone script, not a subcommand of a daemon
        that has to be alive to let you leave."""
        r = subprocess.run([sys.executable, str(ROOT / "backup.py"), "--help"],
                           capture_output=True, text=True, timeout=60,
                           env=self.isolated_env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_export_is_reachable_from_the_command_line_alone(self):
        r = subprocess.run([sys.executable, str(ROOT / "poller.py"), "--help"],
                           capture_output=True, text=True, timeout=60,
                           env=self.isolated_env())
        self.assertIn("--export", r.stdout + r.stderr)

    def test_the_licence_permits_a_fork(self):
        """If the project stalls, continuing it must not need permission."""
        text = (ROOT / "LICENSE").read_text(encoding="utf-8").lower()
        self.assertIn("gnu affero general public license", text)


class TestNoSingleProviderCanEndTheProject(unittest.TestCase):

    def test_more_than_one_network_is_supported(self):
        self.assertGreaterEqual(len(poller.PROVIDERS), 3)

    def test_at_least_one_network_needs_no_account(self):
        """If every provider required a key, one terms change could lock out
        every new user at once."""
        keyless = [s for s, p in poller.PROVIDERS.items() if not p.needs_key]
        self.assertTrue(keyless, "every network requires an account")

    def test_no_provider_is_load_bearing_for_the_core_loop(self):
        """Polling, storing and displaying must not import a specific
        provider -- they go through the Provider interface."""
        src = (ROOT / "store.py").read_text(encoding="utf-8")
        for slug in ("PurpleAirProvider", "QldProvider", "NswProvider"):
            self.assertNotIn(slug, src, f"store.py depends on {slug}")

    def test_adding_a_network_needs_only_a_subclass(self):
        for name in ("slug", "current", "discover", "covers", "attribution"):
            self.assertTrue(hasattr(poller.Provider, name),
                            f"Provider has no {name}; a new network cannot "
                            f"be added without editing the core")




class TestTestsCleanUpAfterThemselves(unittest.TestCase):
    """Windows refuses to delete a file another handle still holds, so a test
    that opens a database inside a TemporaryDirectory and never closes it
    fails on Windows and passes everywhere else. That has now cost three
    separate CI rounds; this catches it before the push."""

    def test_every_test_that_opens_a_database_also_closes_it(self):
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            src = f.read_text(encoding="utf-8")
            opens = src.count("store.connect(") + src.count("sqlite3.connect(")
            if not opens:
                continue
            closes = src.count(".close)") + src.count(".close()")
            self.assertGreaterEqual(
                closes, opens,
                f"{f.name} opens {opens} database connection(s) but closes "
                f"{closes} — the temp directory cannot be removed on Windows")


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
