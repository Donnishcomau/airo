# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The command line, actually run.

Nothing exercised `poller.main()`. The contract tests assert that every flag
documented in the README exists in argparse, which catches a flag that was
renamed and never catches a flag that does not work — and `--prune --dry-run`
was an argparse error for its entire life while being documented twice as the
way to preview a destructive delete.

Found by mutation: removing any one of nineteen branches in main() left the
whole suite green.

Each test asserts on what the command *prints*, because for a CLI that is the
product. No network: providers are stubbed at the boundary.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poller  # noqa: E402
import store   # noqa: E402

# No test may reach the internet: a call that a swallowing error handler hides
# passes for the wrong reason, and this suite mentions the poll path. See
# tests/netguard.py -- one suite run was making 25 real requests before this.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import browserguard  # noqa: E402
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

# `--doctor`, `--install` and `--uninstall` all reach the scheduler backends,
# which address the logged-in session by uid rather than by HOME. Redirecting
# the home directory does not protect it. See tests/schedguard.py.
from schedguard import (  # noqa: E402
    block_session_managers_for_module, restore_session_managers_for_module)


def setUpModule():
    redirect_airo_paths_for_module()
    block_outbound_for_module()
    block_session_managers_for_module()


def tearDownModule():
    restore_session_managers_for_module()
    restore_airo_paths_for_module()
    restore_outbound_for_module()



class StubProvider(poller.Provider):
    slug = "stub"
    label = "Stub network"
    tier = "reference"
    accuracy_note = "test double"
    resolution_minutes = 60
    needs_key = False
    attribution = "Stub data"
    licence = "CC0"

    def current(self, src, key):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return ({"headline": 7.0, "now": 7.0},
                {"site_id": src.get("site_id"), "site_name": "Stub site",
                 "observed_utc": now.isoformat(), "latitude": -27.0,
                 "longitude": 153.0})

    def history(self, src, key, start, end):
        # `utc` is a tz-aware datetime, not a string. Everything downstream
        # compares it against a window.
        return [{"utc": start, "pm25": 7.0}]

    def discover(self, lat, lon, radius_km, key):
        return [{"site_id": "1", "site_name": "Stub site", "distance_km": 1.0,
                 "latitude": -27.0, "longitude": 153.0}]


class CliCase(unittest.TestCase):
    """A whole install in a temp directory, as a fresh clone would have."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        (self.home / ".airo").mkdir(parents=True)
        self.data = base / "data"
        self.data.mkdir()

        self._env = (os.environ.get("HOME"), os.environ.get("USERPROFILE"))
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

        self._saved = (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
                       poller.LOG_PATH, poller.CSV_PATH, poller.ALERT_STATE_PATH)
        poller.DATA = self.data
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.LATEST_PATH = self.data / "latest.json"
        poller.LOG_PATH = self.data / "poller.log"
        poller.CSV_PATH = self.data / "readings.csv"
        poller.ALERT_STATE_PATH = self.data / "alert_state.json"
        poller.FORECAST_PENDING_PATH = self.data / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = self.data / "forecast_skill.json"
        poller.PROVIDERS["stub"] = StubProvider()

        # log() prints as well as writing to LOG_PATH, and several commands
        # narrate only through it. Stubbing it out made "the command said
        # nothing" look like "the command printed nothing", which is a
        # different claim.
        self._log = poller.log

    def tearDown(self):
        (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
         poller.LOG_PATH, poller.CSV_PATH, poller.ALERT_STATE_PATH) = self._saved
        poller.PROVIDERS.pop("stub", None)
        home, profile = self._env
        if home is not None:
            os.environ["HOME"] = home
        if profile is not None:
            os.environ["USERPROFILE"] = profile
        else:
            os.environ.pop("USERPROFILE", None)
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------

    def configure(self, **over):
        cfg = {
            "location": {"name": "Testville", "latitude": -27.0,
                         "longitude": 153.0, "timezone": "Australia/Brisbane"},
            "sources": [{"provider": "stub", "site_id": "1",
                         "site_name": "Stub site", "latitude": -27.0,
                         "longitude": 153.0, "enabled": True}],
            "aqi_scale": "au",
        }
        cfg.update(over)
        poller.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    def readings(self, n=30, pm25=7.0):
        conn = store.connect(poller.db_path())
        try:
            sid = store.upsert_source(conn, "stub", "1", "Stub site")
            start = datetime.now(timezone.utc) - timedelta(hours=n)
            store.insert_readings(conn, sid, [
                {"observed_utc": (start + timedelta(hours=i))
                    .replace(microsecond=0).isoformat(),
                 "pm25": pm25 + i} for i in range(n)])
            return sid
        finally:
            conn.close()

    def cli(self, *argv):
        """Run the CLI and return (exit code, everything it printed).

        Not called `run`: that is TestCase.run(result), and overriding it
        hands the test runner's result object to argparse.
        """
        saved = sys.argv
        sys.argv = ["poller.py", *argv]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = poller.main()
        except SystemExit as e:
            # SystemExit("message") carries the text in .code, and nothing
            # prints it when the exception is caught rather than reaching the
            # interpreter. Fold it into the captured output, or a command that
            # explains itself on the way out looks silent.
            if isinstance(e.code, int) or e.code is None:
                code = e.code or 0
            else:
                out.write(str(e.code))
                code = 1
        finally:
            sys.argv = saved
        return code, out.getvalue()


class TestReportingCommands(CliCase):
    """Commands that answer a question. Every one of these branches could have
    been deleted without a test failing."""

    def test_status_reports_the_configuration_and_the_database(self):
        self.configure()
        self.readings()
        code, said = self.cli("--status")
        self.assertEqual(0, code)
        self.assertIn("Testville", said)
        self.assertIn("stub/1", said)
        self.assertIn(str(poller.db_path()), said)

    def test_status_on_a_fresh_install_says_what_to_do_next(self):
        """The first thing a new user runs, before anything is configured."""
        code, said = self.cli("--status")
        self.assertEqual(0, code)
        self.assertIn("setup.py", said)

    def test_where_reports_the_resolved_paths(self):
        self.configure()
        code, said = self.cli("--where")
        self.assertEqual(0, code)
        self.assertIn(str(self.data), said)
        self.assertIn(str(poller.CONFIG_PATH), said)

    def test_list_sources_names_the_providers_and_their_licences(self):
        self.configure()
        code, said = self.cli("--list-sources")
        self.assertEqual(0, code)
        self.assertIn("stub", said)
        self.assertIn("CC0", said, "a provider's licence is not surfaced")

    def test_list_sources_works_before_any_configuration(self):
        """Documented as the command that works on a bare clone."""
        code, said = self.cli("--list-sources")
        self.assertEqual(0, code)

    def test_no_command_and_no_sources_refuses_to_pretend_it_polled(self):
        code, said = self.cli("--once")
        self.assertNotEqual(0, code)
        self.assertIn("setup.py", said)

    def test_no_key_for_every_source_is_reported_as_such(self):
        class Keyed(StubProvider):
            slug, needs_key = "keyed", True
            key_url = "https://example.invalid/signup"
        poller.PROVIDERS["keyed"] = Keyed()
        try:
            self.configure(sources=[{"provider": "keyed", "site_id": "9",
                                     "enabled": True}])
            code, said = self.cli("--once")
        finally:
            poller.PROVIDERS.pop("keyed", None)
        self.assertNotEqual(0, code)
        self.assertIn("key", said.lower())


class TestIntegrityCommands(CliCase):

    def test_verify_reports_damage_rather_than_saying_nothing(self):
        """A reading dated in the future is a clock or parsing fault and would
        poison every average it lands in. --verify exists to say so; without
        the branch that prints them, it reports "integrity ok" over a database
        it has just found problems in."""
        self.configure()
        sid = self.readings()
        conn = store.connect(poller.db_path())
        try:
            conn.execute("INSERT INTO readings (source_id, observed_utc, pm25) "
                         "VALUES (?, '2099-01-01T00:00:00+00:00', 5.0)", (sid,))
            conn.commit()
        finally:
            conn.close()

        code, said = self.cli("--verify")

        self.assertNotEqual(0, code, "a damaged database verified clean")
        self.assertIn("future", said)
        self.assertNotIn("integrity ok", said)

    def test_verify_on_a_healthy_database_says_so(self):
        self.configure()
        self.readings()
        code, said = self.cli("--verify")
        self.assertEqual(0, code)

    def test_verify_without_a_database_does_not_pretend_to_check_one(self):
        self.configure()
        code, said = self.cli("--verify")
        self.assertIn("no database", said.lower())

    def test_repair_without_a_database_says_so(self):
        self.configure()
        code, said = self.cli("--repair")
        self.assertEqual(0, code)
        self.assertIn("no database", said.lower())

    def test_repair_with_nothing_to_repair_says_nothing_to_repair(self):
        self.configure()
        self.readings()
        code, said = self.cli("--repair")
        self.assertEqual(0, code)
        self.assertIn("nothing to repair", said.lower())

    def test_repair_dry_run_reports_without_changing_anything(self):
        """A feed sentinel stored before the guards existed. --repair clears
        it; --dry-run must only say that it would."""
        self.configure()
        sid = self.readings()
        conn = store.connect(poller.db_path())
        try:
            conn.execute("UPDATE readings SET pm25 = -9999 WHERE source_id = ?",
                         (sid,))
            conn.commit()
        finally:
            conn.close()

        code, said = self.cli("--repair", "--dry-run")

        self.assertEqual(0, code)
        self.assertIn("dry run", said.lower())
        conn = store.connect(poller.db_path())
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE pm25 = -9999").fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(left, 0, "--dry-run changed the database")

    def test_repair_says_so_when_a_sentinel_belongs_to_a_source_that_is_gone(self):
        """--repair clears the value and re-asks the provider for the window.
        A source no longer in the config has nobody to ask, so the row is
        cleared and the user told it was not re-fetched -- rather than left
        looking like a successful repair."""
        self.configure()
        sid = self.readings()
        conn = store.connect(poller.db_path())
        try:
            conn.execute("UPDATE readings SET pm25 = -9999 WHERE source_id = ?",
                         (sid,))
            conn.commit()
        finally:
            conn.close()
        # Same database, but the config no longer lists that source.
        self.configure(sources=[])

        code, said = self.cli("--repair")

        self.assertIn("no longer configured", said)

    def test_repair_actually_clears_a_stored_sentinel(self):
        """The control. Without it every dry-run test above could pass because
        --repair does nothing at all."""
        self.configure()
        sid = self.readings()
        conn = store.connect(poller.db_path())
        try:
            conn.execute("UPDATE readings SET pm25 = -9999 WHERE source_id = ?",
                         (sid,))
            conn.commit()
        finally:
            conn.close()

        code, said = self.cli("--repair")

        self.assertEqual(0, code)
        conn = store.connect(poller.db_path())
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE pm25 = -9999").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, left, "a stored sentinel survived --repair")


class TestDestructiveCommandsPreviewFirst(CliCase):
    """--prune --dry-run was documented twice in the README and was an
    argparse error for its whole life. Someone checking what pruning would
    remove got an error, and could reasonably have run --prune without the
    preview."""

    def test_prune_dry_run_is_accepted_and_deletes_nothing(self):
        self.configure(retention_days=1)
        self.readings(n=60)
        before = self.count()

        code, said = self.cli("--prune", "--dry-run")

        self.assertEqual(0, code, f"--prune --dry-run was refused: {said}")
        self.assertEqual(before, self.count(), "--dry-run deleted readings")

    def test_prune_actually_prunes(self):
        self.configure(retention_days=1)
        self.readings(n=60)
        before = self.count()
        code, said = self.cli("--prune")
        self.assertEqual(0, code)
        self.assertLess(self.count(), before, "--prune removed nothing")

    def test_prune_without_a_retention_policy_removes_nothing(self):
        """Keeping everything is the default and the point: this record cannot
        be regenerated."""
        self.configure(retention_days=0)
        self.readings(n=60)
        before = self.count()
        self.cli("--prune")
        self.assertEqual(before, self.count(),
                         "readings were deleted with no retention configured")

    def test_two_modes_at_once_are_still_refused(self):
        """--dry-run became a modifier; the modes must stay exclusive."""
        code, said = self.cli("--prune", "--verify")
        self.assertNotEqual(0, code)

    def count(self):
        conn = store.connect(poller.db_path())
        try:
            return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()


class TestExport(CliCase):

    def test_export_writes_one_csv_per_source(self):
        self.configure()
        self.readings()
        target = Path(self.tmp.name) / "out"
        code, said = self.cli("--export", str(target))
        self.assertEqual(0, code)
        written = list(target.glob("*.csv"))
        self.assertEqual(1, len(written), f"exported {written}")
        self.assertIn("stub", written[0].name)

    def test_an_export_carries_its_own_provenance(self):
        """An export outlives the tool that made it, so it has to say where it
        came from and under what licence."""
        self.configure()
        self.readings()
        target = Path(self.tmp.name) / "out"
        self.cli("--export", str(target))
        text = next(target.glob("*.csv")).read_text(encoding="utf-8")
        self.assertIn("Stub data", text)
        self.assertIn("medical", text.lower())


class TestBackfill(CliCase):

    def test_backfill_pulls_history_and_reports_what_it_stored(self):
        self.configure()
        code, said = self.cli("--backfill", "2")
        self.assertEqual(0, code)
        self.assertIn("stub/1", said)

    def test_backfill_skips_a_source_whose_key_is_missing(self):
        """Reported, not silently skipped — a source that never backfills
        leaves a hole nobody is told about."""
        class Keyed(StubProvider):
            slug, needs_key = "keyed", True
            key_url = "https://example.invalid/signup"
        poller.PROVIDERS["keyed"] = Keyed()
        try:
            self.configure(sources=[
                {"provider": "stub", "site_id": "1", "enabled": True},
                {"provider": "keyed", "site_id": "9", "enabled": True}])
            code, said = self.cli("--backfill", "1")
        finally:
            poller.PROVIDERS.pop("keyed", None)
        self.assertIn("keyed", said)
        self.assertNotIn("backfill keyed", said,
                         "a source with no key was backfilled anyway")


class TestMigrationCommands(CliCase):

    def test_migrate_data_with_nothing_to_migrate_says_so(self):
        self.configure()
        code, said = self.cli("--migrate-data")
        self.assertEqual(0, code)

    def test_migrate_csv_with_no_csv_says_so(self):
        self.configure()
        code, said = self.cli("--migrate-csv")
        self.assertEqual(0, code)


class TestDoctor(CliCase):
    """--doctor exercises the whole path -- auth, current reading, history --
    rather than waiting for a hole to appear in the record.

    Providers change their APIs, revoke keys and retire stations without
    telling anyone, and each of those looks identical from the outside:
    readings simply stop.
    """

    def test_a_healthy_install_reports_no_problems(self):
        self.configure()
        code, said = self.cli("--doctor")
        self.assertEqual(0, code, said)
        self.assertIn("checks out", said)

    def test_nothing_configured_is_a_problem_with_an_instruction(self):
        code, said = self.cli("--doctor")
        self.assertEqual(1, code)
        self.assertIn("setup.py", said)

    def test_a_missing_key_is_a_problem_that_names_where_to_get_one(self):
        """A provider that needs an account and has none is the single most
        common reason a source goes quiet, and the least obvious from the
        outside."""
        class Keyed(StubProvider):
            slug, needs_key = "keyed", True
            key_env = "KEYED_API_KEY"
            key_url = "https://example.invalid/signup"
        poller.PROVIDERS["keyed"] = Keyed()
        try:
            self.configure(sources=[{"provider": "keyed", "site_id": "9",
                                     "enabled": True}])
            code, said = self.cli("--doctor")
        finally:
            poller.PROVIDERS.pop("keyed", None)

        self.assertEqual(1, code)
        self.assertIn("no API key", said)
        self.assertIn("example.invalid/signup", said,
                      "told there is a problem and not how to fix it")

    def test_a_provider_that_fails_is_reported_rather_than_swallowed(self):
        class Broken(StubProvider):
            slug = "broken"
            def current(self, src, key):
                raise RuntimeError("the station has been retired")
        poller.PROVIDERS["broken"] = Broken()
        try:
            self.configure(sources=[{"provider": "broken", "site_id": "1",
                                     "enabled": True}])
            code, said = self.cli("--doctor")
        finally:
            poller.PROVIDERS.pop("broken", None)

        self.assertEqual(1, code)
        self.assertIn("retired", said)

    def test_the_doctor_reports_database_damage_too(self):
        """--verify and --doctor both run store.verify(), and both have their
        own branch for what to do with the answer. Testing one leaves the
        other free to report "integrity ok" over a database it has just found
        problems in."""
        self.configure()
        sid = self.readings()
        conn = store.connect(poller.db_path())
        try:
            conn.execute("INSERT INTO readings (source_id, observed_utc, pm25) "
                         "VALUES (?, '2099-01-01T00:00:00+00:00', 5.0)", (sid,))
            conn.commit()
        finally:
            conn.close()

        code, said = self.cli("--doctor")

        self.assertEqual(1, code, "the doctor passed a damaged database")
        self.assertIn("future", said)
        self.assertNotIn("integrity ok", said)

    def test_a_stray_key_file_for_a_keyless_network_is_not_a_problem(self):
        """The doctor checks permissions on the key files it would use. A
        keyless network's stray file -- left by an experiment, or by a provider
        that used to need one -- is not a credential this install relies on,
        and reporting it as a problem sends the user to fix something that
        does not matter."""
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows expresses this")
        self.configure()
        stray = self.home / ".airo" / "stub.key"
        stray.write_text("not used by anything", encoding="utf-8")
        os.chmod(stray, 0o644)

        code, said = self.cli("--doctor")

        self.assertEqual(0, code, said)
        self.assertNotIn("stub.key", said)

    def test_an_orphaned_database_is_a_problem_the_doctor_reports(self):
        """The doctor is where someone looks when readings stopped, and an
        abandoned data directory is exactly that symptom."""
        elsewhere = Path(self.tmp.name) / "abandoned"
        elsewhere.mkdir()
        conn = store.connect(elsewhere / "airo.db")
        try:
            sid = store.upsert_source(conn, "stub", "1", "Old site")
            store.insert_readings(conn, sid, [
                {"observed_utc": "2026-07-01T00:00:00+00:00", "pm25": 5.0}])
        finally:
            conn.close()
        # No override needed: data_marker_path() follows CONFIG_PATH, which
        # this harness already isolates.
        poller.data_marker_path().write_text(str(elsewhere), encoding="utf-8")
        self.configure()
        code, said = self.cli("--doctor")

        self.assertEqual(1, code)
        self.assertIn(str(elsewhere), said)


class TestServing(CliCase):

    def test_serve_refuses_a_port_another_airo_already_has(self):
        """Two servers make "which one am I looking at?" unanswerable."""
        real = poller._serving_this_project
        poller._serving_this_project = lambda port, timeout=1.5: "/another/airo"
        import socket
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            self.configure(serve_port=port)
            code, said = self.cli("--serve")
        finally:
            poller._serving_this_project = real
            held.close()
        self.assertNotEqual(0, code)
        self.assertIn("already serving", said)


class TestOpeningAPage(CliCase):
    """--open resolves the URL rather than assuming it.

    The tray held `http://127.0.0.1:8787/dashboard.html` as a literal for the
    whole of v0.5. serve_port is configurable, and the server deliberately
    moves to the next free port when an unrelated program holds 8787 -- so the
    tray opened a dead page, or a stranger's page on 8787.
    """

    def setUp(self):
        super().setUp()
        # The guard, not a stub on `webbrowser.open`. Stubbing the library
        # covers the one route somebody thought of: a change that shelled out
        # to `/usr/bin/open` first went straight past it and opened real tabs
        # on the maintainer's machine, once per suite run, while this list
        # stayed empty and the failure read as an IndexError.
        self.browser = browserguard.block_browser_for_module()
        self.addCleanup(browserguard.restore_browser_for_module)

    @property
    def opened(self):
        return self.browser.urls

    def serving_on(self, port):
        """Pretend an Airo of this project is answering on `port`."""
        real = poller._serving_this_project
        poller._serving_this_project = (
            lambda p, timeout=1.5: "Testville" if p == port else None)
        self.addCleanup(lambda: setattr(poller, "_serving_this_project", real))

    def test_it_opens_the_port_the_server_actually_has(self):
        """serve_forever moves to the next free port when an unrelated program
        holds the configured one, so the opener scans forward from it."""
        self.configure(serve_port=8787)
        self.serving_on(8790)                 # moved, as a collision would
        code, said = self.cli("--open")
        self.assertEqual(0, code, said)
        self.assertEqual(["http://127.0.0.1:8790/dashboard.html"], self.opened)

    def test_a_server_beyond_the_scan_range_is_found_by_the_marker(self):
        """The scan only reaches 20 ports past the configured one, which is as
        far as serve_forever would ever move. A server told to listen further
        away is found because it recorded where it bound -- without the marker
        this is unreachable, which is the reason the marker exists."""
        self.configure(serve_port=8787)
        (self.home / ".airo" / "serve-port").write_text("9500", encoding="utf-8")
        self.serving_on(9500)
        code, said = self.cli("--open")
        self.assertEqual(0, code, said)
        self.assertEqual(["http://127.0.0.1:9500/dashboard.html"], self.opened)

    def test_the_configured_port_is_used_when_the_server_is_there(self):
        self.configure(serve_port=8790)
        self.serving_on(8790)
        self.cli("--open")
        self.assertIn("8790", self.opened[0])

    def test_the_remembered_port_is_tried_first(self):
        """Recorded by serve_forever when it binds, so the common case is one
        request rather than a scan of twenty ports."""
        self.configure(serve_port=8787)
        (self.home / ".airo" / "serve-port").write_text("9100", encoding="utf-8")
        self.serving_on(9100)
        self.assertEqual(9100, poller.running_server_port(poller.load_config()))

    def test_a_stale_remembered_port_does_not_stop_it_finding_the_server(self):
        """The marker is best-effort and survives a crash. It must be a hint,
        not an answer."""
        self.configure(serve_port=8787)
        (self.home / ".airo" / "serve-port").write_text("9999", encoding="utf-8")
        self.serving_on(8787)
        self.assertEqual(8787, poller.running_server_port(poller.load_config()))

    def test_the_settings_page_is_a_known_destination(self):
        self.configure()
        self.serving_on(8787)
        self.cli("--open", "settings")
        self.assertEqual(["http://127.0.0.1:8787/settings"], self.opened)

    def test_an_unknown_page_is_refused_by_name(self):
        self.configure()
        code, said = self.cli("--open", "nonsense")
        self.assertNotEqual(0, code)

    def test_a_server_with_no_readings_yet_is_still_our_server(self):
        """The probe must not depend on data existing.

        It asked /api/latest, which 404s until the first poll — so on a fresh
        install the server was invisible to the very code that had just
        started it. first_run() opened nothing, which is the first thing a new
        user sees. It also made serve_forever's duplicate guard blind to a
        dataless Airo, so a second server started beside the first.
        """
        self.configure()
        served = {}

        class Fake:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url, timeout=None):
            served.setdefault("asked", []).append(url)
            if url.endswith("/api/ping"):
                return Fake({"airo": True, "location_name": "Testville"})
            raise OSError("404: no reading yet")     # what /api/latest does

        real = poller.urllib.request.urlopen
        poller.urllib.request.urlopen = fake_urlopen
        self.addCleanup(
            lambda: setattr(poller.urllib.request, "urlopen", real))

        self.assertEqual("Testville", poller._serving_this_project(8787))
        self.assertTrue(any(u.endswith("/api/ping") for u in served["asked"]),
                        "the probe never asked the endpoint that always answers")

    def test_something_else_on_the_port_is_not_mistaken_for_airo(self):
        """A JSON server that happens to answer /api/ping is not us."""
        self.configure()

        class Fake:
            def read(self):
                return b'{"hello": "world"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        real = poller.urllib.request.urlopen
        poller.urllib.request.urlopen = lambda url, timeout=None: Fake()
        self.addCleanup(
            lambda: setattr(poller.urllib.request, "urlopen", real))
        self.assertIsNone(poller._serving_this_project(8787))

    # ---- --url: the same resolution, for a caller that is not a browser

    def test_url_prints_the_address_and_opens_nothing(self):
        """The app's own window shows settings, so the tray needs the address
        without a browser tab appearing beside it."""
        self.configure()
        self.serving_on(8787)
        code, said = self.cli("--url", "settings")
        self.assertEqual(0, code, said)
        self.assertEqual("http://127.0.0.1:8787/settings", said.strip())
        self.assertEqual([], self.opened, "a browser was opened anyway")

    def test_url_prints_nothing_but_the_url(self):
        """Read by another program, which uses the whole of stdout as the
        address. A tick, a status line or a "started the server" note would
        become part of the URL and the window would show nothing."""
        self.configure()
        self.serving_on(8787)
        _, said = self.cli("--url", "settings")
        self.assertEqual(1, len(said.strip().splitlines()),
                         f"stdout carried more than the URL: {said!r}")
        self.assertTrue(said.strip().startswith("http://127.0.0.1:"))

    def test_url_defaults_to_settings(self):
        """The dashboard has a browser link; the window is what needed this."""
        self.configure()
        self.serving_on(8787)
        _, said = self.cli("--url")
        self.assertEqual("http://127.0.0.1:8787/settings", said.strip())

    def test_url_resolves_the_moved_port_too(self):
        self.configure(serve_port=8787)
        self.serving_on(8790)
        _, said = self.cli("--url", "settings")
        self.assertEqual("http://127.0.0.1:8790/settings", said.strip())

    def test_url_refuses_an_unknown_page(self):
        self.configure()
        code, _ = self.cli("--url", "nonsense")
        self.assertNotEqual(0, code)

    def test_url_fails_loudly_enough_when_no_server_can_be_reached(self):
        """Exit code, not a plausible-looking address. A caller that pointed a
        window at a guessed URL would show a connection error the user cannot
        act on."""
        self.configure()
        real = poller.page_url
        poller.page_url = lambda *a, **kw: (None, False)
        self.addCleanup(lambda: setattr(poller, "page_url", real))
        code, said = self.cli("--url", "settings")
        self.assertNotEqual(0, code)
        self.assertEqual("", said.strip())

    def test_starting_the_server_never_waits_on_dns(self):
        """HTTPServer.server_bind calls socket.getfqdn() — a reverse lookup,
        inside the constructor, before the server exists and before anything
        can be logged.

        Where reverse resolution is slow or filtered, that blocks the dashboard
        from appearing at all. It blocked past thirty seconds on a CI runner,
        and presented as "the server bound a port and told nobody": thread
        alive, no server object, empty log, no exception, because none of that
        code had been reached.

        Asserted by making the lookup fatal rather than by timing it — a
        duration threshold would be flaky on exactly the loaded machines this
        protects.
        """
        import socket
        asked = []
        real = socket.getfqdn

        def refuse(*a):
            asked.append(a)
            raise AssertionError("starting the server consulted DNS")

        socket.getfqdn = refuse
        self.addCleanup(lambda: setattr(socket, "getfqdn", real))

        server = poller.LoopbackHTTPServer(("127.0.0.1", 0), poller.QuietHandler)
        try:
            self.assertEqual([], asked, "the bind path still resolves a name")
            self.assertEqual("127.0.0.1", server.server_name)
        finally:
            server.server_close()

    def test_a_running_server_is_reused_rather_than_duplicated(self):
        """Two servers make "which one am I looking at?" unanswerable, and the
        second would serve the same data from a different port. Nothing may be
        spawned when one is already answering."""
        self.configure(serve_port=8787)
        self.serving_on(8787)
        spawned = []
        real = poller.subprocess.Popen
        poller.subprocess.Popen = lambda *a, **kw: spawned.append(a)
        try:
            url, started = poller.open_page("dashboard")
        finally:
            poller.subprocess.Popen = real
        self.assertEqual([], spawned, "started a second server over a live one")
        self.assertFalse(started)
        self.assertIn("8787", url)

    def test_the_server_records_the_port_it_bound(self):
        """The marker is what lets anything find a server that moved. Written
        by serve_forever itself, so it cannot drift from reality.

        The port is not pre-bound to "find a free one". Doing that leaves it in
        TIME_WAIT, which serve_forever deliberately treats as busy — so the
        test was exercising the collision path rather than the ordinary one,
        and failed on macOS CI for reasons that had nothing to do with the
        marker. A high port is used instead; if something does hold it,
        serve_forever moves and the assertion below still holds, because it
        asks the server what it actually bound.

        serve_forever's exception is captured rather than lost in a daemon
        thread. Without that, anything going wrong in there shows up here as
        "the marker never appeared" with no way to tell why — which cost three
        CI rounds.
        """
        import threading, time
        port = 8899

        self.configure(serve_port=port)
        marker = self.home / ".airo" / "serve-port"
        self.assertFalse(marker.exists())

        failed = []

        def run():
            try:
                poller.serve_forever(port)
            except BaseException as e:          # SystemExit included
                failed.append(f"{type(e).__name__}: {e}")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            # Wait for BOTH signals, not just the marker. serve_forever
            # writes the marker and *then* assigns _active_server, so there is
            # a window where the file exists and the handle is still None —
            # a few instructions wide here, wide enough to lose on a loaded CI
            # runner, which is where it was found. The old wait then fell
            # through to `_active_server.server_address` and raised
            # AttributeError on None.
            def ready():
                return marker.exists() and poller._active_server is not None

            deadline = time.time() + 30
            while time.time() < deadline and not ready() and not failed:
                time.sleep(0.05)
            self.assertEqual([], failed,
                             f"serve_forever did not start: {failed}")
            if not marker.exists():
                # Everything needed to tell apart the ways this can go wrong,
                # gathered at the moment it does. Guessing from "the marker is
                # missing" cost several CI rounds and two confident wrong
                # diagnoses; a macOS-only failure is not reproducible here, so
                # the evidence has to travel back in the failure message.
                where = poller.CONFIG_PATH.parent
                self.fail(
                    "the server bound a port and told nobody.\n"
                    f"  thread alive : {thread.is_alive()}\n"
                    f"  server object: {poller._active_server!r}\n"
                    f"  CONFIG_PATH  : {poller.CONFIG_PATH}\n"
                    f"  marker wanted: {marker}\n"
                    f"  that dir has : "
                    f"{sorted(x.name for x in where.iterdir()) if where.exists() else 'MISSING'}\n"
                    f"  log tail     : {poller.recent_log(8)}")
            bound = poller._active_server.server_address[1]
            self.assertEqual(str(bound),
                             marker.read_text(encoding="utf-8").strip(),
                             "the marker does not name the port in use")
        finally:
            if poller._active_server is not None:
                poller._active_server.shutdown()
                poller._active_server.server_close()
                poller._active_server = None
            thread.join(timeout=5)


    def test_no_server_and_no_launch_reports_rather_than_pretending(self):
        self.configure()
        real = poller._serving_this_project
        poller._serving_this_project = lambda p, timeout=1.5: None
        try:
            url, started = poller.open_page("dashboard", launch=False)
        finally:
            poller._serving_this_project = real
        self.assertIsNone(url)
        self.assertFalse(started)
        self.assertEqual([], self.opened, "a browser was opened at nothing")


class TestFirstRun(CliCase):
    """What a freshly installed app does on its first launch.

    Copying files is not the end of installing: nothing is collected until a
    background poll is scheduled, and nothing useful is collected until the
    user has said where they are. The app calls this on every start, so the
    property that matters is that the second call is as safe as the first.
    """

    def setUp(self):
        super().setUp()
        import scheduler
        self.scheduler = scheduler
        self.scheduled = []
        self._install = scheduler.install
        # Never register a real launchd agent from a test. One did move this
        # machine's data directory aside fifteen times; a test that installs
        # background jobs is the same mistake with a longer fuse.
        scheduler.install = lambda minutes=15: (
            self.scheduled.append(minutes) or (True, "scheduled"))
        self.addCleanup(lambda: setattr(scheduler, "install", self._install))

        self.browser = browserguard.block_browser_for_module()
        self.addCleanup(browserguard.restore_browser_for_module)

        real = poller._serving_this_project
        poller._serving_this_project = lambda p, timeout=1.5: "Testville"
        self.addCleanup(
            lambda: setattr(poller, "_serving_this_project", real))

    def test_a_fresh_install_schedules_the_background_poll(self):
        """Without this the app looks installed and collects nothing, which
        the user discovers days later as an empty chart."""
        did = poller.first_run(open_browser=False)
        self.assertTrue(did["scheduled"])
        self.assertEqual([15], self.scheduled)

    @property
    def opened(self):
        return self.browser.urls

    def test_a_fresh_install_opens_the_settings_page(self):
        did = poller.first_run()
        self.assertFalse(did["configured"])
        self.assertTrue(self.opened, "no browser was opened at all")
        self.assertIn("/settings", self.opened[0])

    def test_a_configured_install_is_not_dragged_back_to_settings(self):
        """It runs on every launch. Opening the page each time would be a
        browser tab nobody asked for, every login, forever."""
        self.configure()
        did = poller.first_run()
        self.assertTrue(did["configured"])
        self.assertEqual([], self.opened)
        self.assertIsNone(did["opened"])

    def test_it_still_schedules_when_nothing_is_configured_yet(self):
        """An unconfigured poll logs that it has nothing to do, which is
        harmless. The alternative is an app that silently collects nothing
        until somebody remembers to come back and switch it on."""
        did = poller.first_run(open_browser=False)
        self.assertFalse(did["configured"])
        self.assertTrue(did["scheduled"])

    def test_running_it_twice_leaves_one_of_everything(self):
        """Idempotent by construction rather than by a flag -- a flag is a
        thing that can be wrong."""
        poller.first_run(open_browser=False)
        poller.first_run(open_browser=False)
        self.assertEqual([15, 15], self.scheduled,
                         "the second run took a different path")

    def test_the_configured_interval_is_the_one_registered(self):
        self.configure(poll_minutes=30)
        poller.first_run(open_browser=False)
        self.assertEqual([30], self.scheduled)

    def test_a_scheduler_that_refuses_is_reported_not_swallowed(self):
        """An app that cannot schedule anything is an app that collects
        nothing. Saying so is the difference between a fixable problem and a
        mystery."""
        self.scheduler.install = lambda minutes=15: (False, "launchctl said no")
        did = poller.first_run(open_browser=False)
        self.assertFalse(did["scheduled"])
        self.assertIn("launchctl said no", did["problems"])

    def test_an_unwritable_data_directory_stops_before_scheduling(self):
        """Scheduling a poll that cannot write is worse than not scheduling:
        it fails every fifteen minutes into a log nobody reads."""
        if os.name == "nt":
            self.skipTest("directory permissions are not how Windows refuses this")
        blocked = Path(self.tmp.name) / "locked"
        blocked.mkdir()
        blocked.chmod(0o500)
        saved = poller.DATA
        poller.DATA = blocked / "inside"
        try:
            did = poller.first_run(open_browser=False)
        finally:
            poller.DATA = saved
            blocked.chmod(0o700)
        self.assertFalse(did["scheduled"])
        self.assertTrue(did["problems"])
        self.assertEqual([], self.scheduled,
                         "an agent was registered for a directory it cannot write")

    def test_the_command_reports_what_it_did(self):
        code, said = self.cli("--first-run")
        self.assertEqual(0, code, said)
        self.assertIn("background", said)
        self.assertIn("configured", said)


class TestUninstall(CliCase):
    """Uninstalling says the user wants the software to stop. It does not say
    they want years of measurements destroyed, and those cannot be
    regenerated.

    The asymmetry is the whole design: removing the software is reversible in
    ten minutes, and removing the record is not reversible at all.
    """

    def setUp(self):
        super().setUp()
        import scheduler
        self.scheduler = scheduler
        self.calls = []
        saved = (scheduler.uninstall, scheduler.uninstall_tray)
        scheduler.uninstall = lambda: (
            self.calls.append("poller") or (True, "removed"))
        scheduler.uninstall_tray = lambda: (
            self.calls.append("tray") or (True, "removed"))
        self.addCleanup(lambda: (
            setattr(scheduler, "uninstall", saved[0]),
            setattr(scheduler, "uninstall_tray", saved[1])))

    def test_it_stops_both_background_jobs(self):
        """One without the other leaves an app that is half uninstalled and
        still writing."""
        did = poller.uninstall_everything()
        self.assertEqual({"poller", "tray"}, set(self.calls))
        self.assertEqual(2, len(did["removed"]))

    def test_it_never_deletes_a_reading(self):
        self.configure()
        self.readings(n=12)
        before = self.count_rows()

        poller.uninstall_everything()

        self.assertEqual(before, self.count_rows(),
                         "uninstalling destroyed the record")
        self.assertTrue(poller.db_path().exists())

    def test_it_never_deletes_the_settings(self):
        self.configure()
        poller.uninstall_everything()
        self.assertTrue(poller.CONFIG_PATH.exists())

    def test_it_says_what_it_kept_and_where(self):
        """Naming the directory is what lets somebody who *does* want it gone
        make that decision with their eyes open -- which is the only way it
        should ever happen."""
        self.configure()
        self.readings(n=7)
        did = poller.uninstall_everything()
        self.assertEqual(7, did["readings"])
        self.assertTrue(any(str(self.data) in k for k in did["kept"]))
        self.assertTrue(any("settings" in k for k in did["kept"]))

    def test_a_job_that_will_not_stop_is_reported_not_swallowed(self):
        """Reporting success over a job still running means the user thinks
        it is gone while it keeps polling."""
        self.scheduler.uninstall_tray = lambda: (False, "launchctl refused")
        did = poller.uninstall_everything()
        self.assertTrue(any("launchctl refused" in p for p in did["problems"]))

    def test_an_install_with_no_data_says_nothing_about_data(self):
        """A fresh install that never polled has nothing to keep, and telling
        someone where their zero readings are is noise."""
        did = poller.uninstall_everything()
        self.assertEqual(0, did["readings"])
        self.assertFalse(any("readings" in k for k in did["kept"]))

    def test_the_command_prints_the_directory_to_delete(self):
        self.configure()
        self.readings(n=3)
        code, said = self.cli("--uninstall")
        self.assertEqual(0, code, said)
        self.assertIn(str(self.data), said)
        self.assertIn("Kept", said)

    def test_the_command_reports_a_failure_in_its_exit_code(self):
        self.scheduler.uninstall = lambda: (False, "no")
        code, said = self.cli("--uninstall")
        self.assertEqual(1, code, "a failed uninstall reported success")

    def count_rows(self):
        conn = store.connect(poller.db_path())
        try:
            return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()


class TestWhatTheShellScriptsUsedToDo(CliCase):
    """install.sh, check.sh, ctl.sh and dashboard.sh were 759 lines of
    macOS-only shell wrapping commands Python already exposed. They were the
    largest obstacle to cross-platform parity and a second copy of maintained
    behaviour.

    Deleting them is only safe once the things they did that Python could not
    exist here. These are those things.
    """

    def test_alerts_can_be_turned_on_and_off(self):
        """ctl.sh edited config.json from a shell script -- a second writer
        with its own idea of the file's shape, and macOS-only besides."""
        self.configure()
        self.assertFalse(poller.set_alerts(False))
        self.assertFalse(poller.load_config()["alerts"]["enabled"])
        self.assertTrue(poller.set_alerts(True))
        self.assertTrue(poller.load_config()["alerts"]["enabled"])

    def test_changing_alerts_goes_through_the_one_validator(self):
        """So the shell path and the settings page cannot disagree about what
        a valid config looks like."""
        import inspect
        self.assertIn("apply_settings", inspect.getsource(poller.set_alerts))

    def test_the_alerts_command_says_which_way_it_went(self):
        self.configure()
        code, said = self.cli("--alerts", "off")
        self.assertEqual(0, code)
        self.assertIn("off", said)

    def test_the_log_can_be_read_without_a_terminal(self):
        self.configure()
        poller.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        poller.LOG_PATH.write_text("\n".join(f"line {i}" for i in range(100)),
                                   encoding="utf-8")
        self.assertEqual(["line 97", "line 98", "line 99"],
                         poller.recent_log(3))

    def test_no_log_yet_is_not_an_error(self):
        """A fresh install has never polled. Saying so beats a traceback."""
        self.assertEqual([], poller.recent_log())
        code, said = self.cli("--logs")
        self.assertEqual(0, code)

    def test_stopping_the_server_only_stops_ours(self):
        """Killing whatever holds the port would be a surprising thing for a
        menu item to do."""
        real = poller._serving_this_project
        poller._serving_this_project = lambda p, timeout=1.5: None
        try:
            self.assertFalse(poller.stop_server())
        finally:
            poller._serving_this_project = real

    def test_the_doctor_notices_an_agent_registered_for_another_folder(self):
        """check.sh's one genuinely unique diagnostic. The launchd label is
        fixed, so with two checkouts -- or a folder that has been moved --
        launchd reports the other install as healthy while this one never
        runs. Everything looks fine and nothing is collected."""
        import scheduler
        real = scheduler.agent_belongs_to_this_project
        scheduler.agent_belongs_to_this_project = lambda: (
            False, "registered for /somewhere/else")
        try:
            self.configure()
            code, said = self.cli("--doctor")
        finally:
            scheduler.agent_belongs_to_this_project = real
        self.assertEqual(1, code)
        self.assertIn("/somewhere/else", said)

    def test_an_agent_for_this_folder_is_not_reported_as_a_problem(self):
        """The control: without it the check could fail everything."""
        import scheduler
        real = scheduler.agent_belongs_to_this_project
        scheduler.agent_belongs_to_this_project = lambda: (True, "")
        try:
            self.configure()
            code, said = self.cli("--doctor")
        finally:
            scheduler.agent_belongs_to_this_project = real
        self.assertEqual(0, code, said)

    def test_the_shell_scripts_are_gone(self):
        """Left in place they would drift, and on Windows and Linux they never
        ran at all."""
        for name in ("install.sh", "check.sh", "ctl.sh", "dashboard.sh"):
            with self.subTest(name=name):
                self.assertFalse((ROOT / name).exists(), f"{name} still exists")

    def test_nothing_still_points_at_them(self):
        """A reference to a deleted script is an instruction that fails."""
        for path in list(ROOT.glob("*.md")) + list((ROOT / "tray" / "src").glob("*.rs")):
            if path.name == "SESSION-LOG.md":
                continue          # a record of history, which may name them
            text = path.read_text(encoding="utf-8")
            for script in ("install.sh", "check.sh", "ctl.sh", "dashboard.sh"):
                with self.subTest(path=path.name, script=script):
                    self.assertNotIn(script, text,
                                     f"{path.name} still refers to {script}")


class TestGuidanceFitsTheReader(CliCase):
    """Two audiences, two sentences.

    Someone who downloaded a disk image has no terminal open and no reason to
    want one. "run: python3 setup.py" is, to them, an error message -- and it
    is what a brand-new install said until an end-to-end test from the dmg
    showed it.
    """

    def as_installed_app(self):
        """Pretend this process is running from inside the bundle."""
        saved = poller.HERE
        fake = Path(self.tmp.name) / "Airo.app" / "Contents" / "Resources"
        (fake / "airo").mkdir(parents=True)
        (fake / "runtime").mkdir()
        poller.HERE = fake / "airo"
        self.addCleanup(lambda: setattr(poller, "HERE", saved))

    def test_a_checkout_is_told_the_command(self):
        self.assertIn("python3", poller.how_to("configure"))

    def test_an_installed_app_is_told_where_to_click(self):
        self.as_installed_app()
        said = poller.how_to("configure")
        self.assertIn("Settings", said)
        self.assertNotIn("python3", said,
                         "an app with no terminal was told to type a command")

    def test_every_phrasing_has_both_halves(self):
        """A message that only knows how to speak to a developer is one that
        reaches a user eventually."""
        for action in ("configure", "restart", "restore", "check"):
            with self.subTest(action=action):
                self.assertTrue(poller.how_to(action))
                self.as_installed_app_once(action)

    def as_installed_app_once(self, action):
        saved = poller.HERE
        fake = Path(self.tmp.name) / f"once-{action}" / "Resources"
        (fake / "airo").mkdir(parents=True)
        (fake / "runtime").mkdir()
        poller.HERE = fake / "airo"
        try:
            said = poller.how_to(action)
            self.assertNotIn("python3", said, f"{action} tells an app user to type")
        finally:
            poller.HERE = saved

    def test_a_fresh_install_does_not_send_a_user_to_a_terminal(self):
        self.as_installed_app()
        code, said = self.cli("--status")
        self.assertNotIn("python3 setup.py", said)
        self.assertIn("Settings", said)

    def test_a_checkout_still_gets_the_command(self):
        """The control: developers keep the terminal instruction."""
        code, said = self.cli("--status")
        self.assertIn("python3 setup.py", said)

    def test_no_command_the_app_shows_assumes_a_terminal(self):
        """Swept rather than spot-checked. Three of these were missed on the
        first pass and only turned up because a test read the whole output
        instead of the single line under test."""
        self.as_installed_app()
        self.configure()
        for argv in (["--status"], ["--list-sources"], ["--where"]):
            with self.subTest(argv=argv):
                code, said = self.cli(*argv)
                self.assertNotIn("python3 setup.py", said)
                self.assertNotIn("python3 scheduler.py", said)




class TestOpeningABrowserReportsWhetherItWorked(unittest.TestCase):
    """`webbrowser.open()` returns a bool and the caller ignored it, so a
    browser that never appeared looked exactly like one that did.

    The tray discards the result too (`let _ = airo::open_dashboard()`), which
    together made "Open dashboard in browser" a menu item that could fail
    completely silently — and it is what the maintainer reported: click,
    nothing, no message anywhere.
    """

    def test_a_refused_browser_is_reported_not_swallowed(self):
        saved = poller.launch_browser
        try:
            import webbrowser
            real = webbrowser.open
            webbrowser.open = lambda *a, **kw: False
            real_run = poller.subprocess.run
            poller.subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(
                OSError("no such file"))
            self.addCleanup(lambda: setattr(webbrowser, "open", real))
            self.addCleanup(lambda: setattr(poller.subprocess, "run", real_run))

            self.assertFalse(poller.launch_browser("http://127.0.0.1:1/x"),
                             "a browser that refused reported success")
        finally:
            poller.launch_browser = saved

    def test_a_successful_open_is_reported_as_such(self):
        import webbrowser
        real = webbrowser.open
        webbrowser.open = lambda *a, **kw: True
        real_run = poller.subprocess.run
        poller.subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(
            OSError("pretend there is no /usr/bin/open"))
        self.addCleanup(lambda: setattr(webbrowser, "open", real))
        self.addCleanup(lambda: setattr(poller.subprocess, "run", real_run))
        self.assertTrue(poller.launch_browser("http://127.0.0.1:1/x"))

    @unittest.skipUnless(sys.platform == "darwin", "macOS launcher")
    def test_macos_prefers_open_by_absolute_path(self):
        """`/usr/bin/open` activates the application; `osascript -e 'open
        location'`, which is what webbrowser reaches, hands the URL over
        without necessarily bringing it forward — so a click can load the page
        behind everything else, which to the person who clicked is
        indistinguishable from nothing happening.

        The absolute path matters because this runs from a launchd agent,
        whose PATH is not a login shell's.
        """
        calls = []
        real_run = poller.subprocess.run

        class Done:
            returncode = 0
            stderr = ""

        poller.subprocess.run = lambda cmd, *a, **kw: (calls.append(cmd)
                                                       or Done())
        self.addCleanup(lambda: setattr(poller.subprocess, "run", real_run))

        self.assertTrue(poller.launch_browser("http://127.0.0.1:8787/x"))
        self.assertEqual(1, len(calls), "the macOS launcher was not used")
        self.assertEqual("/usr/bin/open", calls[0][0],
                         "not an absolute path, so a stripped PATH breaks it")

    def test_open_page_tells_the_caller_when_no_browser_opened(self):
        """The call site, not just the helper."""
        import inspect
        src = inspect.getsource(poller.open_page)
        self.assertIn("launch_browser(url)", src)
        self.assertIn("if not launch_browser", src,
                      "open_page ignores whether a browser opened")


class TestNoTabIsOpenedAtADeadUrl(CliCase):
    """What the maintainer actually saw: about fifteen browser tabs, each
    reading "Problem loading page", all pointing at the settings URL.

    The URL was handed to the browser on the strength of having *started* a
    server, not on the server answering. A tab is the most expensive way there
    is to report that a local server is down, and clicking again makes another
    one.
    """

    def setUp(self):
        super().setUp()
        self.browser = browserguard.block_browser_for_module()
        self.addCleanup(browserguard.restore_browser_for_module)

    def test_a_dead_url_opens_nothing_and_says_so(self):
        real = poller.page_url
        poller.page_url = lambda *a, **kw: ("http://127.0.0.1:1/dead", False)
        self.addCleanup(lambda: setattr(poller, "page_url", real))

        url, _ = poller.open_page("dashboard")

        self.assertIsNone(url, "a URL nothing answers was reported as opened")
        self.assertEqual([], self.browser.urls,
                         f"a browser was opened at a dead URL: "
                         f"{self.browser.urls}")

    def test_a_live_url_is_opened(self):
        """The other half. A guard that refuses everything is a broken
        feature, not a safe one."""
        real_url = poller.page_url
        real_answers = poller.page_answers
        poller.page_url = lambda *a, **kw: ("http://127.0.0.1:9/live", False)
        poller.page_answers = lambda url, timeout=3.0: True
        self.addCleanup(lambda: setattr(poller, "page_url", real_url))
        self.addCleanup(lambda: setattr(poller, "page_answers", real_answers))

        url, _ = poller.open_page("dashboard")

        self.assertEqual("http://127.0.0.1:9/live", url)
        self.assertEqual(["http://127.0.0.1:9/live"], self.browser.urls)

    def test_it_asks_the_same_question_the_rest_of_the_module_asks(self):
        """`_serving_this_project`, not an independent HTTP request.

        The first version made its own request, which bypassed the stub three
        existing tests use to simulate a running server — so they started
        failing against ports nothing was listening on. Two notions of "the
        server is up" are two things to keep in step, and the second one is
        always the one that is wrong.
        """
        asked = []
        real = poller._serving_this_project
        poller._serving_this_project = (
            lambda port, timeout=1.5: asked.append(port) or "Testville")
        self.addCleanup(
            lambda: setattr(poller, "_serving_this_project", real))

        self.assertTrue(poller.page_answers("http://127.0.0.1:8787/x"))
        self.assertEqual([8787], asked,
                         "page_answers did not ask _serving_this_project")

    def test_a_port_with_nothing_on_it_is_not_answering(self):
        real = poller._serving_this_project
        poller._serving_this_project = lambda port, timeout=1.5: None
        self.addCleanup(
            lambda: setattr(poller, "_serving_this_project", real))
        self.assertFalse(poller.page_answers("http://127.0.0.1:9/x"))

    def test_a_url_with_no_usable_port_is_not_answering(self):
        self.assertFalse(poller.page_answers("not-a-url"))


if __name__ == "__main__":
    unittest.main()
