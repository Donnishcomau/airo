# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end regression: a whole install, from first poll to restore.

Every other file here tests a part. This one tests the *path between* the
parts, because that is where this project's real failures have lived. Each of
these was found in production code that had passing unit tests:

  * `weather.py` was absent from the installer's module list. Every unit test
    passed; the built app raised ModuleNotFoundError on launch.
  * a reading over 350 µg/m³ was flagged, filtered from `series()`, dropped by
    `fusion`, and so never reached `maybe_alert`. Four correct layers, and the
    tool went silent in exactly the conditions it exists for.
  * `--prune --dry-run` was an argparse error for its whole life while being
    documented twice as the way to preview a destructive delete.

So these drive the real entry points -- `poller.main()` with a real argv, real
SQLite, real files on disk, a real loopback HTTP server -- and fake only the
one boundary that touches somebody else's machine: `http_get`.

Two kinds of test live here, and the difference matters.

**Journeys** run a sequence a person actually performs and assert the outcome
they would see. **Invariants** are the promises that must hold at *every* step
of every journey: rule 5 (never lose a reading), rule 6 (raw µg/m³ is
canonical), rule 2 (never log or store a key), rule 4 (attribution survives).
The invariants are checked by walking the whole install after each stage, so a
regression is caught at the step that caused it rather than three commands
later.

This suite is the answer to "so we don't go backwards". A test that fails here
is a user-visible regression, not a refactor complaining.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poller   # noqa: E402
import store    # noqa: E402
# Importable both ways: `discover -s tests` puts this directory on the path,
# `-m unittest tests.test_x` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from netguard import block_outbound  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)

# A journey through `--uninstall` reaches the scheduler backends, which
# address the logged-in session by uid rather than by HOME. See
# tests/schedguard.py -- this is the effect that stopped the maintainer's own
# poller, with nothing in any log to say so.
from schedguard import (  # noqa: E402
    block_session_managers_for_module, restore_session_managers_for_module)


# The synthetic frame. Rule 2b: no real coordinate in the repository, ever.
HOME_LAT, HOME_LON = -33.5000, 151.0000

# A key that must never appear in anything written to disk. Distinctive on
# purpose, so a leak is unambiguous rather than a substring coincidence.
SENTINEL_KEY = "airo-test-key-3f9c1d7e-do-not-log"


class Fake(poller.Provider):
    """A keyless network with a controllable record.

    Keyless so a first run needs no account, and controllable so a journey can
    say "now the air gets bad" without waiting for weather.
    """

    slug = "e2e"
    label = "End-to-end network"
    tier = "reference"
    accuracy_note = "test double"
    resolution_minutes = 60
    needs_key = False
    attribution = "End-to-end test data, CC0"
    licence = "CC0"

    level = 8.0
    channels = None          # (a, b) to simulate a two-channel instrument
    fail_with = None         # an exception to raise instead of answering

    #: When set, `current()` keeps answering — 200, parses, yields a PM2.5
    #: figure — while reporting this fixed observation time. That is what a
    #: PurpleAir sensor which has dropped off the network looks like from the
    #: outside, and the shape that went undetected through a two-day outage
    #: on a real install. `fail_with` models the other kind of outage, where the
    #: provider itself is unreachable; the two need opposite advice.
    frozen_at = None

    def current(self, src, key):
        if self.fail_with:
            raise self.fail_with
        now = self.frozen_at or datetime.now(timezone.utc).replace(
            microsecond=0)
        measures = {"headline": self.level, "now": self.level}
        if self.channels:
            measures["pm25_a"], measures["pm25_b"] = self.channels
        return measures, {
            "site_id": src.get("site_id"), "site_name": "E2E site",
            "latitude": HOME_LAT, "longitude": HOME_LON,
            "last_seen_utc": now.isoformat(timespec="seconds"),
            "temperature_unit": "C",
        }

    #: When set, `history()` reports its hours as **strings** in this form
    #: rather than as datetimes. OpenAQ's `current()` passed the API's string
    #: straight through, which is how one instant ended up stored in two
    #: spellings; a provider is entitled to hand back either, and the store
    #: has to canonicalise whichever arrives.
    stamp_as = None

    def history(self, src, key, start, end):
        # A dark sensor has no history past the moment it went quiet either.
        # Without this the gap check backfills straight over the outage and
        # the record never shows one -- which would make this double a
        # provider that is merely slow, not a sensor that has stopped.
        if self.frozen_at:
            end = min(end, self.frozen_at)
        out, t = [], start
        while t < end:
            when = t
            if self.stamp_as:
                when = t.isoformat(timespec="seconds")
                if self.stamp_as == "Z":
                    when = when.replace("+00:00", "Z")
            # A temperature, because the backfill path handles it differently
            # from the live path and for a long time handled it wrongly --
            # copying the provider's number and recording no unit. With this
            # absent, every journey's temperature invariants were watching a
            # column no journey ever filled.
            out.append({"utc": when, "pm25": 6.0, "temperature": 12.0})
            t += timedelta(hours=1)
        return out

    def discover(self, latitude, longitude, radius_km, key):
        return [{"site_id": "e2e-1", "site_name": "E2E site",
                 "latitude": latitude, "longitude": longitude,
                 "distance_km": 0.4}]


class Journey(unittest.TestCase):
    """One isolated install per test, driven through its real entry points."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        (self.home / ".airo").mkdir(parents=True)
        self.data = base / "data"

        self._env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

        self._paths = (poller.DATA, poller.LATEST_PATH, poller.LOG_PATH,
                       poller.CONFIG_PATH, poller.CSV_PATH,
                       poller.ALERT_STATE_PATH)
        poller.DATA = self.data
        poller.LATEST_PATH = self.data / "latest.json"
        poller.LOG_PATH = self.data / "poller.log"
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.CSV_PATH = self.data / "readings.csv"
        poller.ALERT_STATE_PATH = self.data / "alert_state.json"
        poller.FORECAST_PENDING_PATH = self.data / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = self.data / "forecast_skill.json"

        self.provider = Fake()
        poller.PROVIDERS["e2e"] = self.provider
        self.addCleanup(lambda: poller.PROVIDERS.pop("e2e", None))

        # Nothing in this suite may reach the network. Not a convenience: a
        # test that quietly calls a real API is slow, flaky and occasionally
        # sends somebody's query to a third party.
        # Two layers, because there are two ways out. poller.http_get is the
        # provider boundary; urllib is everything else, and weather.py goes
        # straight there -- which is how a fresh install ended up showing 72
        # hours of real weather in a database that had just been created.
        self.outbound = block_outbound(self)
        self._http = poller.http_get
        poller.http_get = self._refuse_network
        self.notified = []
        self._notify = poller.notify
        poller.notify = lambda *a, **kw: (self.notified.append(a) or True)

        self.addCleanup(self._restore)

    def _refuse_network(self, *a, **kw):
        raise AssertionError(
            "an end-to-end test tried to reach the network: "
            f"{a[0] if a else '?'}")

    def _restore(self):
        (poller.DATA, poller.LATEST_PATH, poller.LOG_PATH, poller.CONFIG_PATH,
         poller.CSV_PATH, poller.ALERT_STATE_PATH) = self._paths
        poller.http_get = self._http
        poller.notify = self._notify
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---------------------------------------------------------- driving it

    def cli(self, *argv):
        """Run the real CLI. Returns (exit code, everything it printed)."""
        saved = sys.argv
        sys.argv = ["poller.py", *argv]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = poller.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if not isinstance(e.code, int) and e.code is not None:
                out.write(str(e.code))
        finally:
            sys.argv = saved
        return code, out.getvalue()

    def install(self, **over):
        cfg = {
            "location": {"name": "Testville", "latitude": HOME_LAT,
                         "longitude": HOME_LON,
                         "timezone": "Australia/Brisbane"},
            "sources": [{"provider": "e2e", "site_id": "e2e-1",
                         "site_name": "E2E site", "enabled": True}],
            "aqi_scale": "au",
            "serve": False,
            "backfill_days_on_first_run": 1,
        }
        cfg.update(over)
        poller.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    def db(self):
        return store.connect(self.data / "airo.db")

    def readings(self):
        conn = self.db()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT observed_utc, pm25, quality FROM readings "
                "ORDER BY observed_utc")]
        finally:
            conn.close()

    def latest(self):
        return json.loads(poller.LATEST_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------- the invariants

    def files_written(self):
        """Everything this install has put on disk."""
        out = []
        for root in (self.data, self.home):
            for p in root.rglob("*"):
                if p.is_file():
                    out.append(p)
        return out

    def assert_invariants(self, stage):
        """The promises that must hold after every step of every journey."""
        conn = self.db()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
            rows = conn.execute(
                "SELECT pm25, quality FROM readings").fetchall()
        finally:
            conn.close()

        # Rule 6: raw µg/m³ is canonical. A stored index would bake one
        # country's opinion into the record, and the scale is configurable.
        for banned in ("aqi", "index", "band"):
            self.assertNotIn(banned, cols,
                             f"{stage}: a derived {banned} was stored")

        # Rule 5a: nothing is silently discarded. Every verdict must be one
        # the code can explain, or a surface cannot say why a reading is
        # marked -- enumerated from the module rather than listed here.
        allowed = {"ok", "extreme", "suspect"}
        for r in rows:
            self.assertIn(r["quality"], allowed,
                          f"{stage}: unknown quality {r['quality']!r}")

        # Rule 2: never log, print or write an API key.
        #
        # Two files are allowed to contain one, and only two: `~/.airo/*.key`,
        # which is where keys are *supposed* to live, and `config.json`, which
        # carries a per-source `read_key` for a private PurpleAir sensor. Both
        # are outside the repository and mode 600. Everything else -- the log,
        # latest.json, the database, exports, backups -- must never see it,
        # and those are checked by walking what was actually written rather
        # than a list of the files somebody thought of.
        allowed_to_hold_a_key = {"config.json"}
        for p in self.files_written():
            if p.suffix == ".key" or p.name in allowed_to_hold_a_key:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            self.assertNotIn(SENTINEL_KEY, text,
                             f"{stage}: an API key reached {p.name}")

        # Rule 2a: nothing of the user's lives in the repository.
        for name in ("airo.db", "latest.json", "config.json"):
            self.assertFalse((ROOT / name).exists(),
                             f"{stage}: {name} was written into the repo")

        # One spelling per value. Audited by hand once against a real
        # database and found clean everywhere except timestamps; kept as an
        # invariant so it stays that way, because "checked once" is how the
        # timestamp split survived long enough to reach 73 rows.
        conn = self.db()
        try:
            for table, col in (("readings", "observed_utc"),
                               ("readings", "fetched_utc"),
                               ("weather", "observed_utc"),
                               ("sources", "added_utc")):
                for (value,) in conn.execute(
                        f"SELECT DISTINCT {col} FROM {table} "
                        f"WHERE {col} IS NOT NULL"):
                    self.assertEqual(
                        value, store.canonical_utc(value),
                        f"{stage}: {table}.{col} holds {value!r}, which is "
                        f"not its own canonical form — text comparisons sort "
                        f"it against the others wrongly")

            # Rule 6 again, from the other side: no Fahrenheit survives
            # ingest, so the column means one thing.
            #
            # This one cannot fail in a journey and is kept anyway, which is
            # worth stating rather than leaving to be discovered. `self.db()`
            # opens the database, opening runs the v3 migration, and that
            # migration converts every row marked 'F' -- so a planted
            # Fahrenheit row is repaired before this reads it. It stands as a
            # statement of the rule. The check below it is the one with teeth.
            left = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE temperature_unit = 'F'"
            ).fetchone()[0]
            self.assertEqual(0, left,
                             f"{stage}: a Fahrenheit reading was stored")

            # And the state that was actually dangerous: a temperature with
            # no unit recorded at all. `backfill_source()` wrote those for a
            # year, and the check above could not see them -- it looks for
            # the wrong label, and these carried no label. That is worse than
            # wrong, because the v3 migration repairs rows marked 'F' and has
            # nothing to key on when the mark is absent. Unknown is not the
            # same as Celsius; rule 5a's shape, one column over.
            unmarked = conn.execute(
                "SELECT COUNT(*) FROM readings "
                "WHERE temperature IS NOT NULL AND temperature_unit IS NULL"
            ).fetchone()[0]
            self.assertEqual(
                0, unmarked,
                f"{stage}: a temperature was stored with no unit recorded, "
                f"so nothing downstream and no later migration can tell "
                f"whether it is Celsius")

            # A negative mass concentration is a feed sentinel, not a reading.
            neg = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE pm25 < 0").fetchone()[0]
            self.assertEqual(0, neg,
                             f"{stage}: a sentinel was stored as a reading")

            for (place,) in conn.execute("SELECT DISTINCT place FROM weather"):
                self.assertRegex(
                    place, r"^-?\d+\.\d{3},-?\d+\.\d{3}$",
                    f"{stage}: {place!r} is not a canonical place key, so a "
                    f"re-geocode would start a second weather series")
        finally:
            conn.close()


class TestTheFirstHalfHour(Journey):
    """Install, poll, and look at it. The path every user takes exactly once,
    and the one nobody reports as broken because they simply leave."""

    def test_a_fresh_install_collects_and_displays(self):
        self.install()

        code, said = self.cli("--once")
        self.assertEqual(0, code, said)
        self.assert_invariants("after first poll")

        # Readings landed, and history came with them: a chart with one point
        # on it looks broken on the day somebody installs.
        rows = self.readings()
        self.assertGreater(len(rows), 1,
                           "the first run left nothing to draw")

        # latest.json is the contract with every surface.
        latest = self.latest()
        for field in ("pm25_10min", "aqi", "band", "fetched_utc",
                      "fetched_local", "sources", "attributions"):
            self.assertIn(field, latest, f"latest.json has no {field}")
        self.assertIsNotNone(latest["pm25_10min"])

        # Rule 4: attribution travels with the data, and is rendered from what
        # was actually used rather than written as a literal.
        self.assertTrue(any("End-to-end test data" in a
                            for a in latest["attributions"]),
                        latest["attributions"])

    def test_polling_again_adds_without_duplicating(self):
        self.install()
        self.cli("--once")
        before = len(self.readings())
        self.cli("--once")
        after = self.readings()
        self.assertGreaterEqual(len(after), before)
        stamps = [r["observed_utc"] for r in after]
        self.assertEqual(len(stamps), len(set(stamps)),
                         "a second poll duplicated a reading")
        self.assert_invariants("after second poll")

    def test_the_status_command_describes_a_working_install(self):
        self.install()
        self.cli("--once")
        code, said = self.cli("--status")
        self.assertEqual(0, code, said)
        self.assertIn(str(self.data), said,
                      "--status does not say where the data is")


class TestABadNightEndToEnd(Journey):
    """The journey the project exists for: the air turns dangerous, and every
    layer between the sensor and the user has to pass it along.

    This is the one that was broken. A reading over 350 µg/m³ was flagged at
    ingest, filtered out of `series()`, dropped by `fusion` before it could be
    chosen, and so never reached `maybe_alert`. Every layer behaved exactly as
    documented and the result was silence.
    """

    def test_extreme_air_reaches_the_chart_the_analysis_and_the_alert(self):
        self.install(alerts={"enabled": True, "threshold_pm25": 25.0,
                             "cooldown_minutes": 0, "quiet_hours": None})
        self.cli("--once")                      # a quiet baseline first

        self.provider.level = 900.0             # smoke
        self.provider.channels = (890.0, 910.0)  # and the instrument agrees
        code, said = self.cli("--once")
        self.assertEqual(0, code, said)
        self.assert_invariants("after extreme reading")

        # Stored, and marked as what it is.
        worst = max(self.readings(), key=lambda r: r["pm25"] or 0)
        self.assertEqual(900.0, worst["pm25"])
        self.assertEqual("extreme", worst["quality"],
                         "genuinely bad air was filed as a sensor fault")

        # It reached the headline rather than being dropped as implausible.
        self.assertEqual(900.0, self.latest()["pm25_10min"])

        # It is drawn: series() is what every chart reads.
        conn = self.db()
        try:
            drawn = [r["pm25"] for r in store.series(conn)]
        finally:
            conn.close()
        self.assertIn(900.0, drawn, "the worst air on record was not charted")

        # And somebody was told.
        self.assertTrue(self.notified, "nobody was warned about 900 µg/m³")

    def test_a_broken_instrument_takes_the_other_path(self):
        """The distinction the whole design rests on. A blocked inlet must not
        raise an alarm, and must not vanish either."""
        self.install(alerts={"enabled": True, "threshold_pm25": 25.0,
                             "cooldown_minutes": 0, "quiet_hours": None})
        self.cli("--once")
        self.notified.clear()

        self.provider.level = 900.0
        self.provider.channels = (1700.0, 100.0)   # the channels disagree
        self.cli("--once")
        self.assert_invariants("after faulty reading")

        worst = max(self.readings(), key=lambda r: r["pm25"] or 0)
        self.assertEqual("suspect", worst["quality"])
        self.assertEqual(900.0, worst["pm25"],
                         "a suspect reading was discarded rather than flagged")

        conn = self.db()
        try:
            drawn = [r["pm25"] for r in store.series(conn)]
        finally:
            conn.close()
        self.assertNotIn(900.0, drawn, "a sensor fault was drawn as air quality")
        self.assertFalse(self.notified,
                         "a blocked inlet raised a health alarm")


class TestNothingIsEverLost(Journey):
    """Rule 5, exercised across the commands that touch the record.

    Each of these has a unit test. What they did not have was proof that the
    count survives the *sequence* -- and the sequence is what a real install
    performs.
    """

    def counts(self):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        finally:
            conn.close()

    def test_the_record_survives_a_full_lifecycle(self):
        self.install()
        self.cli("--once")
        baseline = self.counts()
        self.assertGreater(baseline, 0)

        for stage, argv in (
                ("status", ("--status",)),
                ("doctor-free", ("--where",)),
                ("verify", ("--verify",)),
                ("export", ("--export", str(Path(self.tmp.name) / "out"))),
                ("prune dry run", ("--prune", "--days", "1", "--dry-run")),
        ):
            with self.subTest(stage=stage):
                self.cli(*argv)
                self.assertEqual(baseline, self.counts(),
                                 f"{stage} changed the number of readings")
                self.assert_invariants(stage)

    def test_uninstalling_deletes_nothing(self):
        """Removing the software is a statement about wanting it to stop, not
        about wanting years of measurements destroyed -- and an uninstaller is
        reached at exactly the moment nobody is reading carefully."""
        self.install()
        self.cli("--once")
        before = self.counts()

        import scheduler
        saved = scheduler.uninstall
        scheduler.uninstall = lambda: (True, "stubbed")
        try:
            code, said = self.cli("--uninstall")
        finally:
            scheduler.uninstall = saved

        self.assertEqual(before, self.counts(),
                         "uninstall destroyed the readings")
        self.assertTrue((self.data / "airo.db").exists())
        self.assertIn(str(self.data), said,
                      "it did not say where the data was left")

    def test_a_backup_round_trip_returns_the_same_readings(self):
        self.install()
        self.cli("--once")
        before = self.readings()

        import backup
        archive = Path(self.tmp.name) / "airo-backup.tar.gz"
        backup.create(archive)
        self.assertTrue(archive.exists())

        # Lose the database, as a disk failure would.
        (self.data / "airo.db").unlink()
        backup.restore(archive, force=True)

        self.assertEqual(before, self.readings(),
                         "a restored backup did not match what was taken")
        self.assert_invariants("after restore")


class TestAKeyNeverEscapes(Journey):
    """Rule 2, checked by putting a real one in and looking everywhere.

    The existing audit walks named commands. This walks *every file the
    install writes*, which is the difference between checking the places
    somebody thought of and checking the places there are.
    """

    def setUp(self):
        super().setUp()
        self.keyed = type("Keyed", (Fake,), {
            "slug": "keyed", "needs_key": True,
            "key_url": "https://example.invalid/signup"})()
        poller.PROVIDERS["keyed"] = self.keyed
        self.addCleanup(lambda: poller.PROVIDERS.pop("keyed", None))
        (self.home / ".airo" / "keyed.key").write_text(
            SENTINEL_KEY, encoding="utf-8")

    def test_no_command_writes_the_key_anywhere(self):
        self.install(sources=[{"provider": "keyed", "site_id": "k-1",
                               "enabled": True,
                               "read_key": SENTINEL_KEY}])
        for argv in (("--once",), ("--status",), ("--where",),
                     ("--list-sources",), ("--verify",)):
            with self.subTest(command=argv[0]):
                _, said = self.cli(*argv)
                self.assertNotIn(SENTINEL_KEY, said,
                                 f"{argv[0]} printed the key")
                self.assert_invariants(f"after {argv[0]}")

    def test_a_backup_leaves_the_key_behind_by_default(self):
        self.install(sources=[{"provider": "keyed", "site_id": "k-1",
                               "enabled": True}])
        self.cli("--once")
        import backup
        archive = Path(self.tmp.name) / "b.tar.gz"
        backup.create(archive)
        raw = archive.read_bytes()
        self.assertNotIn(SENTINEL_KEY.encode(), raw,
                         "the key travelled in a backup archive")

    def test_the_settings_payload_never_carries_it(self):
        cfg = self.install(sources=[{"provider": "keyed", "site_id": "k-1",
                                     "enabled": True,
                                     "read_key": SENTINEL_KEY}])
        payload = json.dumps(poller.settings_payload(cfg))
        self.assertNotIn(SENTINEL_KEY, payload,
                         "the settings API served a credential")
        self.assertIn("has_read_key", payload,
                      "the page cannot tell whether a key is set")


class TestAnUpgradeKeepsEverything(Journey):
    """The path nobody tests until it breaks somebody's four years of data.

    An install that has been running since before a schema change opens its
    database with new code. Every migration in this project has to be proven
    on a database written the old way, not on one the new code created.
    """

    def old_database(self, rows=40):
        """A v4 database: extreme air filed as a sensor fault, as it was."""
        self.data.mkdir(parents=True, exist_ok=True)
        conn = store.connect(self.data / "airo.db")
        sid = store.upsert_source(conn, "e2e", "e2e-1", "E2E site")
        base = datetime.now(timezone.utc) - timedelta(hours=rows)
        for i in range(rows):
            pm = 900.0 if i % 10 == 0 else 9.0
            conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25, pm25_a, "
                "pm25_b, quality) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, (base + timedelta(hours=i)).isoformat(timespec="seconds"),
                 pm, pm - 10, pm + 10,
                 "suspect" if pm > store.SUSPECT_PM25 else "ok"))
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        return rows

    def test_opening_an_old_database_loses_no_row(self):
        n = self.old_database()
        self.install()
        self.cli("--status")
        self.assertGreaterEqual(len(self.readings()), n,
                                "an upgrade lost readings")
        self.assert_invariants("after upgrade")

    def test_the_smoke_that_was_hidden_becomes_visible(self):
        """The point of the migration, stated as the user would see it: nights
        that showed nothing now show what was actually measured."""
        self.old_database()
        self.install()
        self.cli("--status")

        conn = self.db()
        try:
            drawn = [r["pm25"] for r in store.series(conn)]
        finally:
            conn.close()
        self.assertIn(900.0, drawn,
                      "readings hidden by the old verdict stayed hidden")

    def test_no_concentration_is_altered_by_the_upgrade(self):
        self.old_database()
        conn = store.connect(self.data / "airo.db")
        try:
            before = sorted(r["pm25"] for r in
                            conn.execute("SELECT pm25 FROM readings"))
        finally:
            conn.close()
        self.install()
        self.cli("--status")
        after = sorted(r["pm25"] for r in self.readings())
        self.assertEqual(before, after, "a stored measurement changed")


class TestTheApiServesWhatWasCollected(Journey):
    """The dashboard reads the API, not the database. A poll that stores
    perfectly and an endpoint that cannot serve it looks, to a user, exactly
    like a poller that does not work."""

    def test_the_series_endpoint_returns_the_readings_just_polled(self):
        self.install()
        self.cli("--once")

        from functools import partial
        from http.server import ThreadingHTTPServer
        from threading import Thread
        import urllib.request

        handler = partial(poller.QuietHandler, directory=str(ROOT))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(),
                                 thread.join(timeout=5)))

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/series?days=3650")
        # The real network is refused in this suite; loopback is the product.
        poller.http_get = self._http
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8"))
        finally:
            poller.http_get = self._refuse_network

        points = [p for s in body.get("series", []) for p in s["points"]]
        self.assertTrue(points, "the API served no points after a poll")
        stored = {r["pm25"] for r in self.readings() if r["quality"] != "suspect"}
        self.assertTrue({p["pm25"] for p in points} & stored,
                        "the API served points that are not in the database")


class TestTheSuiteItselfIsHonest(Journey):
    """Checks on this file, because an end-to-end suite that quietly stopped
    exercising anything would be worse than not having one."""

    def test_a_journey_that_touches_the_network_fails_loudly(self):
        """Called the way real code calls it -- url and key.

        The single-argument version passed only because the stub accepts
        anything; with the stub removed it would have raised TypeError rather
        than the guard, which is a test passing for a reason unrelated to what
        it claims to check.
        """
        with self.assertRaises(AssertionError):
            poller.http_get("https://example.invalid/", "")

    def test_the_invariants_notice_a_stored_index(self):
        """The guard is only worth having if it can fail. Verified by adding
        the column it forbids, rather than by trusting it."""
        self.install()
        self.cli("--once")
        conn = self.db()
        try:
            conn.execute("ALTER TABLE readings ADD COLUMN aqi REAL")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(AssertionError):
            self.assert_invariants("deliberately broken")

    def test_the_invariants_notice_a_leaked_key(self):
        self.install()
        self.cli("--once")
        (self.data / "stray.log").write_text(
            f"debug: key={SENTINEL_KEY}", encoding="utf-8")
        with self.assertRaises(AssertionError):
            self.assert_invariants("deliberately broken")

    def test_the_invariants_notice_an_unknown_quality_verdict(self):
        self.install()
        self.cli("--once")
        conn = self.db()
        try:
            conn.execute("UPDATE readings SET quality = 'mystery'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(AssertionError):
            self.assert_invariants("deliberately broken")


class TestASensorGoesDarkAndComesBack(Journey):
    """The journey that actually happened, and was silent.

    On the maintainer's install the nearest sensor — the headline source —
    went dark for about two days. Every poll in that window succeeded: the
    provider answered, the response parsed, a PM2.5 figure was logged. Only
    the timestamp never moved. Nothing was said, and the first visible sign
    was blank cells on a heatmap days later.

    No journey covered a source going quiet, so the unit tests' assumption —
    that a silent source is one whose fetch raised — was never contradicted by
    anything driving the real path.

    The outage is set up as a *state* rather than a transition: an install
    whose newest observation is already nine hours old, which is the state the
    maintainer was in. Modelling the transition would need nine hours of wall
    clock to pass between two polls, and a test that sleeps is a test nobody
    runs. The transition itself — counter, threshold, recovery, reset — is
    covered directly in `test_alerts.py`.
    """

    DARK_HOURS = 9

    def poll(self, times=1):
        for _ in range(times):
            code, out = self.cli("--once")
            self.assertEqual(0, code, out)

    def go_dark(self):
        """The provider keeps answering; the observation time stops moving."""
        self.provider.frozen_at = (
            datetime.now(timezone.utc)
            - timedelta(hours=self.DARK_HOURS)).replace(microsecond=0)

    def said(self):
        return " ".join(m for sent in self.notified for m in map(str, sent))

    def test_a_sensor_that_stops_reporting_is_announced(self):
        """The whole point. A provider that keeps answering must not keep the
        outage secret."""
        self.install(source_failure_alert_after=3)
        self.go_dark()

        self.poll(times=4)

        self.assertIn("stopped reporting", self.said(),
                      "a sensor dark behind a working provider was never "
                      "announced — the two-day outage that went unreported")

    def test_the_message_does_not_send_them_after_their_network(self):
        """"Check the key, check your internet" is the wrong advice here and
        costs the reader an evening. The provider is fine; the sensor is not.
        """
        self.install(source_failure_alert_after=3)
        self.go_dark()

        self.poll(times=4)

        self.assertIn("still answering", self.said())

    def test_it_is_said_once_rather_than_every_poll(self):
        """An alert per poll is an alert that gets muted, and then the next
        one is missed."""
        self.install(source_failure_alert_after=3)
        self.go_dark()

        self.poll(times=8)

        self.assertEqual(1, self.said().count("stopped reporting"),
                         "the dark sensor nagged once per poll")

    def test_a_healthy_sensor_is_never_announced_as_dark(self):
        """The control, and the one that matters most for trust."""
        self.install(source_failure_alert_after=3)

        self.poll(times=6)

        self.assertNotIn("stopped reporting", self.said(),
                         "a healthy sensor was reported as dark")

    def test_nothing_is_invented_while_the_sensor_is_dark(self):
        """Rule 5a's other half. The provider keeps handing back the same
        observation; storing it again each poll would fabricate a record of
        readings that were never taken.

        clock-independent: asserted against the frozen instant rather than
        against a row count. The first version compared totals before and
        after, which failed on the Windows runner because the gap check
        legitimately backfills, and how many hourly rows that yields depends
        on where the hour boundary falls relative to the moment the test
        started. The count was never the property — "the same observation
        stored twice" is.
        """
        self.install(source_failure_alert_after=3)
        self.go_dark()
        self.poll()
        frozen = self.provider.frozen_at.isoformat(timespec="seconds")

        self.poll(times=4)

        rows = self.readings()
        at_the_frozen_instant = [r for r in rows
                                 if r["observed_utc"].startswith(frozen[:19])]
        # Exactly one, not "at most one". At zero the assertion would hold
        # while proving nothing -- and zero is also wrong: the last thing the
        # sensor said before going quiet is a real observation and must be
        # kept.
        self.assertEqual(
            1, len(at_the_frozen_instant),
            f"the frozen observation was stored {len(at_the_frozen_instant)} "
            f"times; once is the record, more than once invents readings that "
            f"were never taken, and none loses the last thing it said")

        later = [r for r in rows if r["observed_utc"] > frozen]
        self.assertEqual(
            [], later,
            f"readings appeared after the sensor went quiet: {later[:3]}")

    def test_the_sensor_coming_back_is_announced_too(self):
        """Without it the user is left believing it is still broken, and the
        counter never resets so the next outage is never announced either."""
        self.install(source_failure_alert_after=3)
        self.go_dark()
        self.poll(times=4)
        self.notified.clear()

        self.provider.frozen_at = None
        self.poll()

        self.assertIn("working again", self.said(),
                      "the sensor came back and nobody was told")

    def test_the_readings_resume_when_it_returns(self):
        """The alert is not the deliverable — the record is."""
        self.install(source_failure_alert_after=3)
        self.go_dark()
        self.poll(times=3)
        during = len(self.readings())

        self.provider.frozen_at = None
        self.poll()

        self.assertGreater(len(self.readings()), during,
                           "readings did not resume after the sensor came "
                           "back")




class TestTheLegacyCsvMigration(Journey):
    """Somebody's readings from before the database existed.

    Rule 5 applies with more force here than anywhere: this is the only copy.
    The CSV predates multi-source, so its rows belong to whichever source was
    configured at the time, and it records nothing about which -- so a wrong
    guess silently files years of one instrument's history under another.
    """

    HEADER = ("utc,local,source,pm25_10min,au_aqi_10min,pm25_now,pm25_30min,"
              "pm25_60min,pm25_6hr,pm25_24hr,pm25_1week,humidity,temperature")

    def write_csv(self, rows=5):
        self.data.mkdir(parents=True, exist_ok=True)
        lines = [self.HEADER]
        base = datetime.now(timezone.utc) - timedelta(hours=rows)
        for i in range(rows):
            t = (base + timedelta(hours=i)).isoformat(timespec="seconds")
            lines.append(f"{t},{t},E2E site,{10 + i}.0,40.0,{10 + i}.0,"
                         f",,,,,55.0,21.0")
        poller.CSV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return rows

    def test_with_no_csv_it_says_so_rather_than_failing(self):
        self.install()
        code, said = self.cli("--migrate-csv")
        self.assertEqual(0, code, said)
        self.assertIn("no legacy", said.lower())

    def test_the_rows_arrive_under_the_configured_source(self):
        n = self.write_csv()
        self.install()
        code, said = self.cli("--migrate-csv")
        self.assertEqual(0, code, said)
        rows = self.readings()
        self.assertGreaterEqual(len(rows), n, f"rows were lost: {said}")
        self.assert_invariants("after csv migration")

    def test_the_users_only_copy_is_left_on_disk(self):
        """Deleting somebody's sole record of their own history is not ours
        to do, however successfully it was imported."""
        self.write_csv()
        self.install()
        self.cli("--migrate-csv")
        self.assertTrue(poller.CSV_PATH.exists(),
                        "the legacy CSV was removed after importing it")

    def test_running_it_twice_adds_nothing(self):
        """Idempotent through the store's (source, observed_utc) key. Someone
        who is unsure whether it worked will run it again."""
        self.write_csv()
        self.install()
        self.cli("--migrate-csv")
        first = len(self.readings())
        self.cli("--migrate-csv")
        self.assertEqual(first, len(self.readings()),
                         "a second migration duplicated the history")

    def test_with_no_source_configured_it_refuses_rather_than_guessing(self):
        """The CSV does not record which instrument produced it. Filing it
        under an arbitrary source would be a silent, unfixable mistake."""
        self.write_csv()
        self.install(sources=[])
        code, said = self.cli("--migrate-csv")
        self.assertNotEqual(0, code)
        self.assertIn("no sources configured", said.lower())


class TestTheCommandsSomebodyReachesForWhenWorried(Journey):
    """Every one of these is run at the moment something looks wrong.

    A command that fails there is worse than one that fails during setup: the
    user is already suspicious, and a traceback confirms the tool is broken
    rather than telling them what is. None of them had a test.
    """

    def test_test_notification_reports_what_happened(self):
        self.install()
        code, said = self.cli("--test-alert")
        self.assertEqual(0, code, said)
        self.assertTrue(self.notified, "nothing was sent")
        self.assertIn("sent", said.lower())

    def test_a_failing_notification_says_so_and_exits_nonzero(self):
        """Reporting success for an alert nobody saw is how somebody trusts a
        warning system that cannot warn them."""
        self.install()
        poller.notify = lambda *a, **kw: False
        code, said = self.cli("--test-alert")
        self.assertEqual(1, code)
        self.assertIn("failed", said.lower())

    def test_backfill_weather_reports_the_span_it_reached(self):
        self.install()
        self.cli("--once")

        import weather
        rows = [{"observed_utc": (datetime.now(timezone.utc)
                                  - timedelta(hours=h)).isoformat(
                                      timespec="seconds"),
                 "temperature_c": 15.0, "wind_speed_ms": 1.0}
                for h in range(48)]
        saved = (weather.history, weather.recent)
        weather.history = lambda *a, **kw: rows
        weather.recent = lambda *a, **kw: rows
        try:
            code, said = self.cli("--backfill-weather", "2")
        finally:
            weather.history, weather.recent = saved

        self.assertEqual(0, code, said)
        self.assertIn("hour", said.lower())
        self.assert_invariants("after weather backfill")

    def test_backfill_weather_with_nothing_stored_exits_nonzero(self):
        """A command that reports success having stored nothing teaches the
        user to stop reading its output."""
        self.install()
        self.cli("--once")
        import weather
        saved = (weather.history, weather.recent)
        weather.history = lambda *a, **kw: []
        weather.recent = lambda *a, **kw: []
        try:
            code, said = self.cli("--backfill-weather", "2")
        finally:
            weather.history, weather.recent = saved
        self.assertEqual(1, code)
        self.assertIn("no weather", said.lower())

    def test_stop_server_says_which_case_happened(self):
        """"nothing was running" and "I stopped it" are different answers, and
        a user chasing a stuck port needs to know which."""
        self.install()
        code, said = self.cli("--stop-server")
        self.assertEqual(0, code, said)
        self.assertTrue(said.strip(), "it said nothing at all")

    def test_where_reports_the_data_directory(self):
        self.install()
        self.cli("--once")
        code, said = self.cli("--where")
        self.assertEqual(0, code, said)
        self.assertIn(str(self.data), said)

    def test_doctor_runs_to_completion_and_says_what_it_checked(self):
        """It probes every source end to end. With the network refused the
        probes fail, which is the interesting case: it must still report
        rather than raise, or the command is useless exactly when the network
        is the problem."""
        self.install()
        self.cli("--once")
        code, said = self.cli("--doctor")
        self.assertIsInstance(code, int)
        for expected in ("timezone", "notification"):
            self.assertIn(expected, said.lower(),
                          f"--doctor no longer reports {expected}")

    def test_repair_leaves_a_clean_database_alone(self):
        self.install()
        self.cli("--once")
        before = self.readings()
        code, said = self.cli("--repair")
        self.assertEqual(0, code, said)
        self.assertEqual(before, self.readings(),
                         "--repair altered readings that were already fine")
        self.assert_invariants("after repair")


def setUpModule():
    redirect_airo_paths_for_module()
    block_session_managers_for_module()


def tearDownModule():
    restore_session_managers_for_module()
    restore_airo_paths_for_module()


class TestDoctorDiagnoses(Journey):
    """`--doctor` exists to tell somebody *what* is wrong, not that something
    is. Every branch below is a distinct diagnosis, and each was untested —
    on the command a user runs precisely when they already suspect trouble.

    A wrong or missing message here costs more than a wrong number elsewhere:
    it sends someone to check their API key when their site id was retired, or
    to re-register an account when they were simply rate limited.
    """

    def setUp(self):
        super().setUp()
        self.install()

    def doctor(self):
        return self.cli("--doctor")

    def fail_current(self, exc):
        self.provider.fail_with = exc

    def test_an_unauthorised_key_is_named_as_the_likely_cause(self):
        import urllib.error
        self.fail_current(urllib.error.HTTPError(
            "u", 401, "Unauthorized", {}, None))
        _, said = self.doctor()
        self.assertIn("401", said)
        self.assertIn("key", said.lower(),
                      "a 401 did not point at the key")

    def test_a_retired_site_is_not_blamed_on_the_key(self):
        """The distinction that matters: 404 means the site id is gone, and
        telling somebody to check their key sends them to reissue a working
        credential."""
        import urllib.error
        self.fail_current(urllib.error.HTTPError("u", 404, "Not Found", {}, None))
        _, said = self.doctor()
        self.assertIn("404", said)
        self.assertIn("site id", said.lower())
        self.assertNotIn("key looks wrong", said.lower())

    def test_rate_limiting_says_to_wait_rather_than_to_fix_anything(self):
        import urllib.error
        self.fail_current(urllib.error.HTTPError("u", 429, "Too Many", {}, None))
        _, said = self.doctor()
        self.assertIn("429", said)
        self.assertIn("rate limited", said.lower())

    def test_a_source_that_answers_with_no_reading_is_reported(self):
        """Responding is not the same as working. A feed that returns 200 and
        no PM2.5 leaves a hole in the record that looks like a network fault."""
        self.provider.level = None
        _, said = self.doctor()
        self.assertIn("no PM2.5", said)

    def test_history_returning_nothing_warns_that_gap_repair_is_dead(self):
        """Gap repair is rule 5's mechanism. A provider whose history call
        answers nothing cannot repair a gap, and the poller will keep trying
        forever without saying so."""
        self.provider.history = lambda src, key, start, end: []
        _, said = self.doctor()
        self.assertIn("gap repair", said.lower())

    def test_history_outside_the_requested_window_is_reported(self):
        """Every provider must honour the same window contract or gap
        detection reasons about a different range per source — which is how a
        gap gets 'repaired' with readings from the wrong week."""
        far = datetime.now(timezone.utc) - timedelta(days=400)
        self.provider.history = lambda src, key, start, end: [
            {"utc": far, "pm25": 5.0}]
        _, said = self.doctor()
        self.assertIn("outside the requested window", said)

    def test_a_world_readable_key_file_is_called_out(self):
        """Rule 2. A key file every account on the machine can read is not a
        protected credential, and --doctor is where somebody would find out."""
        keyed = type("Keyed", (Fake,), {
            "slug": "keyed", "needs_key": True,
            "key_url": "https://example.invalid/signup"})()
        poller.PROVIDERS["keyed"] = keyed
        self.addCleanup(lambda: poller.PROVIDERS.pop("keyed", None))
        kf = self.home / ".airo" / "keyed.key"
        kf.write_text(SENTINEL_KEY, encoding="utf-8")
        os.chmod(kf, 0o644)
        self.install(sources=[{"provider": "keyed", "site_id": "k-1",
                               "enabled": True}])
        _, said = self.doctor()
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows grants access")
        self.assertIn("readable by others", said)
        self.assert_invariants("after doctor with a loose key")

    def test_it_reports_a_problem_count_rather_than_only_prose(self):
        """Something has to be countable, or --doctor cannot be used by
        anything but a human reading carefully.

        The count is asserted as well as the exit code -- the first version
        checked only the code, so it passed with the summary line deleted,
        while claiming in its own name to be about the count.
        """
        import urllib.error
        self.fail_current(urllib.error.HTTPError("u", 401, "no", {}, None))
        code, said = self.doctor()
        self.assertNotEqual(0, code,
                            "--doctor found a broken source and exited 0")
        self.assertIn("problem(s) found", said,
                      "it exited non-zero without saying how much was wrong")

    def test_a_healthy_install_exits_zero(self):
        """The control. Without it, a --doctor that always reports a problem
        would pass every test above and be useless."""
        code, said = self.doctor()
        self.assertEqual(0, code, said)


class TestAnUpgradeFromMixedTimestamps(Journey):
    """The journey the timestamp fix exists for, driven end to end.

    A database written by the old code holds one instant in two spellings.
    Opening it with new code must leave one row per instant, lose nothing, and
    then keep polling normally — the last part matters because a migration
    that repairs history and breaks the next write has moved the problem
    rather than fixed it.
    """

    def legacy_mixed(self):
        self.data.mkdir(parents=True, exist_ok=True)
        conn = store.connect(self.data / "airo.db")
        sid = store.upsert_source(conn, "e2e", "e2e-1", "E2E site")
        base = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0) - timedelta(hours=6)
        pairs = 0
        for i in range(6):
            t = base + timedelta(hours=i)
            iso = t.isoformat(timespec="seconds")
            zed = iso.replace("+00:00", "Z")
            # Every other hour written twice, the way OpenAQ's current() and
            # the backfill path each wrote it.
            # The humidity goes on the Z row deliberately. The table is
            # WITHOUT ROWID, so rows arrive in primary-key order, and '+' is
            # 0x2B where 'Z' is 0x5A -- put the value on the +00:00 row and a
            # migration that simply keeps whichever it saw first still looks
            # correct. That trap had already caught this suite once, in
            # test_timestamp_format, and it was written again here.
            conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25,"
                " humidity, quality) VALUES (?, ?, ?, 55.0, 'ok')",
                (sid, zed, 6.0 + i))
            if i % 2 == 0:
                conn.execute(
                    "INSERT INTO readings (source_id, observed_utc, pm25,"
                    " quality) VALUES (?, ?, ?, 'ok')", (sid, iso, 6.0 + i))
                pairs += 1
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        return pairs

    def test_one_row_per_instant_afterwards(self):
        self.legacy_mixed()
        self.install()
        self.cli("--status")
        rows = self.readings()
        stamps = [r["observed_utc"] for r in rows]
        self.assertEqual(len(stamps), len(set(stamps)))
        self.assertEqual(6, len(rows), f"expected six instants: {stamps}")
        self.assert_invariants("after the timestamp migration")

    def test_the_humidity_on_the_duplicate_survives(self):
        """Rule 5. One row of each pair carried a humidity the other lacked."""
        self.legacy_mixed()
        self.install()
        self.cli("--status")
        conn = self.db()
        try:
            n = conn.execute("SELECT COUNT(*) FROM readings "
                             "WHERE humidity IS NOT NULL").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(6, n, "the merge discarded a humidity reading")

    def test_polling_still_works_afterwards(self):
        """A migration that repairs history and breaks the next write has
        moved the problem, not fixed it."""
        self.legacy_mixed()
        self.install()
        self.cli("--status")
        before = len(self.readings())
        code, said = self.cli("--once")
        self.assertEqual(0, code, said)
        self.assertGreaterEqual(len(self.readings()), before)
        self.assert_invariants("after polling a migrated database")

    def test_a_provider_sending_the_old_spelling_does_not_resplit_it(self):
        """The half of this the journey was missing.

        Everything above proves the *migration* repairs a database written by
        the old code. None of it proved that a write arriving afterwards stays
        canonical — the fake provider hands back datetimes, so a store that
        passed strings through untouched, which is the original defect
        exactly, would have kept every test here green while the table split
        again on the next poll from a provider that sends a string.

        Verified by reintroducing that fault: `insert_readings` taking
        `r["observed_utc"]` raw instead of through `canonical_utc` turns this
        red and nothing else in this class.
        """
        self.legacy_mixed()
        self.install()
        self.cli("--status")
        before = {r["observed_utc"] for r in self.readings()}

        # A provider that reports its hours the way OpenAQ did.
        self.provider.stamp_as = "Z"
        self.cli("--backfill", "1")

        rows = self.readings()
        stamps = [r["observed_utc"] for r in rows]
        self.assertEqual(len(stamps), len(set(stamps)),
                         "a Z-form reading was stored beside its own instant")
        for value in stamps:
            self.assertEqual(
                value, store.canonical_utc(value),
                f"{value!r} was stored in the spelling the provider chose")
        self.assertTrue(before <= set(stamps), "the backfill lost a reading")
        self.assert_invariants("after a provider sent the old spelling")

    def test_a_second_poll_still_dedups(self):
        """The property the split broke in the first place."""
        self.legacy_mixed()
        self.install()
        self.cli("--once")
        after_one = len(self.readings())
        self.cli("--once")
        stamps = [r["observed_utc"] for r in self.readings()]
        self.assertEqual(len(stamps), len(set(stamps)),
                         "a repeat poll duplicated an instant")
        self.assertGreaterEqual(len(stamps), after_one)


class TestAFeedSentinelNeverBecomesAReading(Journey):
    """Queensland reports -9999 when a station is offline.

    Stored as a concentration it became AQI -39,996, which falls below the
    first breakpoint and rendered as **"Very good"** — the most reassuring
    label there is, for air nobody measured. It is rejected at three
    independent layers; this drives the whole path and checks the record
    afterwards, because the layers each have their own test and none of them
    proves a sentinel cannot reach the database.
    """

    def test_a_negative_reading_is_never_stored(self):
        self.install()
        self.provider.level = -9999.0
        code, said = self.cli("--once")
        self.assertEqual(0, code, said)

        conn = self.db()
        try:
            neg = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE pm25 < 0").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, neg, "a feed sentinel was stored as a reading")
        self.assert_invariants("after a sentinel")

    def test_it_does_not_render_as_good_air(self):
        """The consequence that made this a health issue rather than a data
        one. Whatever the surface says, it must not be reassuring.

        The station has to be the *only* thing there. The first version left
        the fake's history in place, so the headline fell back to a genuine
        6.0 µg/m³ from an hour earlier and correctly said "Very good" —
        correct behaviour, and a test that read it as a bug. Isolating the
        sentinel is what makes the assertion mean anything.
        """
        self.install(backfill_days_on_first_run=0)
        self.provider.history = lambda src, key, start, end: []
        self.provider.level = -9999.0
        self.cli("--once")

        latest = self.latest()
        self.assertIsNone(latest.get("pm25_10min"),
                          "a sentinel became the headline reading")
        band = str(latest.get("band") or "").lower()
        self.assertNotIn("good", band,
                         f"an offline station rendered as {band!r}")

    def test_the_row_is_kept_as_a_gap_rather_than_dropped(self):
        """Rule 5a: nothing is silently discarded. The reading is NULL, which
        the gap detector then treats as the missing observation it is —
        deleting the row instead would hide that we asked and were answered."""
        self.install()
        self.provider.level = -9999.0
        self.cli("--once")
        conn = self.db()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE pm25 IS NULL").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(rows, 1,
                                "the sentinel row vanished entirely, so "
                                "nothing records that the station answered")


class TestTheMacOsInstallLifecycle(Journey):
    """The journey a Mac user actually takes, driven through the built app.

    Every other journey here runs the code from the checkout. This one runs
    the artefact: the interpreter inside the bundle, the modules inside it,
    against a home and a data directory that are not the developer's.

    `test_macos_bundle.py` already checks the bundle thoroughly, and this is
    not a second copy of that. What it adds is the thing that file does not
    do — re-checking **every invariant after every step**. A bundle can poll
    and still lose a reading, store a derived index, write a Fahrenheit
    temperature, or leave a key in its log, and none of those show up in a
    test that only asks whether the command succeeded.

    Skipped with a stated reason when there is no bundle, because the Python
    suite has to stay runnable without a Rust toolchain.
    """

    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise unittest.SkipTest("the .app bundle is macOS only")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_macos_bundle as bundle
        cls.bundle_mod = bundle
        cls.app = bundle._resolve_bundle()
        if not cls.app.exists():
            raise unittest.SkipTest(
                "no bundle built (cd tray && cargo tauri build --bundles dmg)")

    @classmethod
    def tearDownClass(cls):
        # Detach anything this class mounted. test_macos_bundle caches by
        # image, so whichever file runs second re-attaches rather than
        # reusing a mount the other one has already torn down.
        mod = getattr(cls, "bundle_mod", None)
        if mod is not None:
            for at in list(mod._MOUNTS.values()):
                mod.detach(at)

    def payload(self):
        return self.app / "Contents" / "Resources" / "payload"

    def bundle_env(self):
        """Isolated home, isolated data, and no way out to the internet.

        `netguard` patches `urllib.request.urlopen` in *this* process, which
        does nothing for a subprocess -- so every bundle test in this project
        has been one careless `--doctor` away from calling a real provider,
        with the same failure mode that made this rule: the call succeeds
        quietly, the test passes because a third party answered, and it keeps
        passing until the day they change a field.

        Pointing the proxy variables at a port with nothing behind it closes
        it. urllib reads them, the connection is refused immediately rather
        than hanging, and loopback is exempted so a dashboard still works.
        """
        return dict(os.environ,
                    HOME=str(self.home), USERPROFILE=str(self.home),
                    AIRO_DATA=str(self.data),
                    AIRO_CONFIG=str(self.home / ".airo" / "config.json"),
                    http_proxy="http://127.0.0.1:1",
                    https_proxy="http://127.0.0.1:1",
                    HTTP_PROXY="http://127.0.0.1:1",
                    HTTPS_PROXY="http://127.0.0.1:1",
                    no_proxy="127.0.0.1,localhost")

    def in_bundle(self, *argv, timeout=180):
        """Run one of the shipped commands with the shipped interpreter."""
        return subprocess.run(
            [str(self.payload() / "runtime" / "bin" / "python3"),
             str(self.payload() / "airo" / argv[0]), *argv[1:]],
            capture_output=True, text=True, timeout=timeout,
            env=self.bundle_env())

    def seed_through_the_shipped_store(self, observed, pm25):
        """A reading, written by the bundle's own store.py.

        Not the checkout's. The point of this journey is that the code which
        wrote the row is the code a user has, so a divergence between them --
        which is exactly what the missing-module defect was -- is visible.
        """
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import store\n"
            "c = store.connect(%r)\n"
            "sid = store.upsert_source(c, 'qld', 'wbk', 'Sandbox site')\n"
            "store.insert_readings(c, sid, [{'observed_utc': %r,"
            " 'pm25': %r}])\n"
            "c.close()" % (str(self.payload() / "airo"),
                           str(self.data / "airo.db"), observed, pm25))
        r = subprocess.run(
            [str(self.payload() / "runtime" / "bin" / "python3"), "-c", code],
            capture_output=True, text=True, timeout=180, env=self.bundle_env())
        self.assertEqual(0, r.returncode, r.stderr[-600:])

    def test_the_whole_lifecycle_holds_every_promise_at_every_step(self):
        # A real provider name, not the in-process `e2e` fake: the bundle has
        # never heard of it, and `--doctor` correctly refuses a config naming
        # a provider it does not ship. Enabled, because a disabled source
        # sends --doctor down its "nothing configured" branch and the journey
        # would then never reach the code it exists to exercise. Reaching the
        # provider fails at the proxy, which is the point -- see bundle_env.
        self.install(sources=[{"provider": "qld", "site_id": "wbk",
                               "site_name": "Sandbox site", "enabled": True}])
        self.assertTrue((self.home / ".airo" / "config.json").exists())

        # 1. It knows where its own data lives, and says so rather than
        #    guessing — the thing the tray was getting wrong.
        where = self.in_bundle("poller.py", "--where")
        self.assertEqual(0, where.returncode,
                         (where.stdout + where.stderr)[-800:])
        self.assertIn(str(self.data), where.stdout + where.stderr)

        # 2. A reading arrives, written by the shipped code.
        self.seed_through_the_shipped_store("2026-07-31T11:00:00Z", 7.4)
        self.assert_invariants("after the bundle stored a reading")
        self.assertEqual(1, len(self.readings()))

        # 3. A second one in the *other* timestamp spelling. The migration
        #    that fixed this shipped inside the bundle or it did not ship.
        self.seed_through_the_shipped_store("2026-07-31T11:00:00+00:00", 7.4)
        self.assert_invariants("after the same instant arrived twice")
        self.assertEqual(1, len(self.readings()),
                         "the shipped store wrote one instant as two rows")

        # 4. It reports what it collected.
        status = self.in_bundle("poller.py", "--status")
        self.assertIn("Sandbox site", status.stdout + status.stderr)
        self.assert_invariants("after --status")

        # 5. The command a worried user runs. It reaches the real check --
        #    the source is named and the database is examined -- and the
        #    provider probe fails at the proxy rather than at somebody's API.
        doctor = self.in_bundle("poller.py", "--doctor")
        said = doctor.stdout + doctor.stderr
        self.assertIn("qld/wbk", said, said[-800:])
        self.assertIn("database integrity ok", said, said[-800:])
        self.assert_invariants("after --doctor")

        # 6. Leaving. Rule 5 does not stop applying because somebody
        #    uninstalls — the readings are theirs.
        before = self.readings()
        self.in_bundle("scheduler.py", "uninstall")
        self.assert_invariants("after uninstall")
        self.assertEqual(before, self.readings(),
                         "uninstalling cost the user their readings")

    def test_the_bundle_cannot_reach_the_internet_from_a_test(self):
        """`netguard` patches urlopen in *this* process and does nothing for a
        subprocess, so every bundle test in this project has been one careless
        command away from calling a real provider. It would have passed --
        that is the failure mode: the call succeeds quietly, the test is green
        because a third party answered, and it stays green until they change a
        field.

        Asserted rather than assumed, because a guard nobody has watched fail
        is a claim nobody has checked.
        """
        self.install(sources=[{"provider": "qld", "site_id": "wbk",
                               "site_name": "Sandbox site", "enabled": True}])
        doctor = self.in_bundle("poller.py", "--doctor")
        said = doctor.stdout + doctor.stderr
        self.assertIn("current reading failed", said,
                      f"a test subprocess reached a real provider:\n"
                      f"{said[-800:]}")

    def test_nothing_of_the_users_was_written_inside_the_app(self):
        """The bundle is read-only in Applications for anyone who is not an
        administrator, and a build that writes into itself works for the
        developer and fails for the user."""
        self.install(sources=[{"provider": "qld", "site_id": "wbk",
                               "site_name": "Sandbox site", "enabled": False}])
        self.seed_through_the_shipped_store("2026-07-31T11:00:00Z", 7.4)
        self.in_bundle("poller.py", "--status")

        strays = [p.name for p in self.payload().rglob("*")
                  if p.suffix in (".db", ".json", ".log", ".csv")
                  and p.name not in ("tauri.conf.json", "config.example.json")
                  and "runtime" not in p.parts]
        self.assertEqual([], strays,
                         f"the app wrote into itself: {strays}")


class TestUpgradingTheInstalledApp(TestTheMacOsInstallLifecycle):
    """Installing a new version over an existing one.

    The journey that matters most and is hardest to get a second chance at:
    somebody has been running Airo for months, downloads a new build, drags it
    over the old one, and opens it. Their settings and their readings are the
    only things in the picture that cannot be rebuilt.

    Everything of the user's lives outside the bundle by design (rule 2a), so
    the *claim* is that replacing the app cannot touch it. This drives it
    rather than asserting it: an old install is created, the new bundle is
    pointed at it, and both are compared byte for byte and row for row.
    """

    def existing_install(self, schema=6, rows=40, extra=None):
        """An install as a previous version left it: settings and history."""
        cfg = {
            "location": {"name": "Testville", "latitude": HOME_LAT,
                         "longitude": HOME_LON,
                         "timezone": "Australia/Brisbane"},
            "sources": [{"provider": "qld", "site_id": "wbk",
                         "site_name": "Sandbox site", "enabled": True}],
            "aqi_scale": "au",
            "serve": False,
            "backfill_days_on_first_run": 1,
        }
        cfg.update(extra or {})
        config_path = self.home / ".airo" / "config.json"
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        self.data.mkdir(parents=True, exist_ok=True)
        conn = store.connect(self.data / "airo.db")
        sid = store.upsert_source(conn, "qld", "wbk", "Sandbox site")
        base = datetime.now(timezone.utc) - timedelta(hours=rows)
        for i in range(rows):
            conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25, quality)"
                " VALUES (?, ?, ?, 'ok')",
                (sid, (base + timedelta(hours=i)).isoformat(timespec="seconds"),
                 6.0 + i))
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                     (str(schema),))
        conn.commit()
        conn.close()
        return config_path, rows

    def test_the_new_app_does_not_rewrite_the_existing_settings(self):
        """Byte for byte. A settings file rewritten on upgrade is how a
        hand-edited threshold or a chosen scale disappears without anybody
        touching it, and the user finds out when an alert does not fire."""
        config_path, _ = self.existing_install()
        before = config_path.read_bytes()

        for argv in (("poller.py", "--where"), ("poller.py", "--status"),
                     ("poller.py", "--doctor")):
            # The exit code first. Without it this test passes when the
            # command does not run at all -- a bundle that cannot start
            # rewrites nothing, and "it did not touch your settings" would be
            # true for the least useful reason there is.
            run = self.in_bundle(*argv)
            self.assertIn(run.returncode, (0, 1),
                          f"{argv[1]} did not run: "
                          f"{(run.stdout + run.stderr)[-500:]}")
            self.assertEqual(
                before, config_path.read_bytes(),
                f"{argv[1]} rewrote the user's settings")

    def test_every_reading_survives_the_upgrade(self):
        _, rows = self.existing_install()
        run = self.in_bundle("poller.py", "--status")
        self.assertEqual(0, run.returncode,
                         (run.stdout + run.stderr)[-500:])
        self.assertGreaterEqual(
            len(self.readings()), rows,
            "the new app opened an old database and lost readings")
        self.assert_invariants("after upgrading the installed app")

    def test_no_stored_measurement_is_altered(self):
        """Rule 6. A migration may relabel, requalify or convert a *unit* it
        can prove — it may never quietly change a concentration."""
        self.existing_install()
        conn = self.db()
        try:
            before = sorted(r[0] for r in
                            conn.execute("SELECT pm25 FROM readings"))
        finally:
            conn.close()
        run = self.in_bundle("poller.py", "--status")
        self.assertEqual(0, run.returncode,
                         (run.stdout + run.stderr)[-500:])
        after = sorted(r["pm25"] for r in self.readings())
        self.assertEqual(before, after, "an upgrade changed a measurement")

    def test_the_schema_is_brought_forward(self):
        """The old database is at 6; the shipped code is at 7. If the bundle
        carries a store.py that does not migrate, every later feature reads a
        shape that is not there."""
        self.existing_install(schema=6)
        self.in_bundle("poller.py", "--status")
        conn = self.db()
        try:
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(store.SCHEMA_VERSION), str(version),
                         "the installed app did not bring the schema forward")

    def test_the_unlabelled_fahrenheit_history_is_repaired(self):
        """The defect this database is carrying in real life: 463 PurpleAir
        rows with a Fahrenheit number and no unit recorded. The repair has to
        travel *inside the bundle*, or updating the app fixes nothing for the
        person who actually has the problem."""
        self.existing_install(schema=6)
        conn = store.connect(self.data / "airo.db")
        sid = store.upsert_source(conn, "purpleair", "pa-1", "Backyard")
        conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25,"
            " temperature, temperature_unit, quality)"
            " VALUES (?, '2026-08-01T10:00:00+00:00', 5.0, 68.0, NULL, 'ok')",
            (sid,))
        conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()

        self.in_bundle("poller.py", "--status")

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT temperature, temperature_unit FROM readings "
                "WHERE temperature IS NOT NULL").fetchone()
        finally:
            conn.close()
        self.assertEqual("C", row["temperature_unit"])
        self.assertAlmostEqual(20.0, row["temperature"], places=1,
                               msg="the shipped code did not repair a "
                                   "Fahrenheit row written by an older one")

    def test_nothing_of_the_users_is_inside_the_bundle_to_lose(self):
        """Why replacing the app is safe at all. Dragging a new .app over an
        old one deletes everything in it — so the guarantee is not that the
        copy is careful, it is that there was never anything of theirs there.
        """
        self.existing_install()
        self.in_bundle("poller.py", "--status")
        strays = [p.name for p in self.payload().rglob("*")
                  if p.suffix in (".db", ".log", ".csv")
                  or p.name in ("config.json", "latest.json")]
        self.assertEqual([], strays,
                         f"the app keeps user state inside itself: {strays}")


if __name__ == "__main__":
    unittest.main()
