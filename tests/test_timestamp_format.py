"""One timestamp format, everywhere, forever.

`store.py`'s own docstring says it plainly:

    Times are ISO-8601 UTC strings, not integers. They sort correctly as text
    ... The cost is that comparisons are string comparisons, so anything
    writing a differently formatted timestamp silently sorts wrong -- _iso()
    is the only sanctioned writer.

It was not the only writer. `insert_readings()` normalised a `datetime` and
passed a *string* through untouched, and so did `_iso()` itself, which ends
`return str(v)`. OpenAQ's `current()` hands back the API's own
`2026-07-31T11:00:00Z`, so 73 rows of a real database are in a form nothing
else uses.

Two consequences, both live rather than theoretical:

  * **Dedup breaks.** The primary key is `(source_id, observed_utc)`, so one
    instant in two forms is two rows. 64 such pairs existed. "Overlapping
    backfill costs nothing" is rule 5's mechanism and it does not survive the
    boundary.
  * **Sorting interleaves wrongly.** `'+'` is 0x2B and `'Z'` is 0x5A, so
    `...+00:00` sorts before `...Z` for the same instant, and every range query
    behaves differently either side of the change.

The count was still growing while this was being written -- the poller keeps
running -- which is the difference between a historical mess and an open wound.
"""

import sys
import tempfile
import unittest
import json

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)

#: The one form. Twenty-five characters, offset spelled out.
CANONICAL = "2026-07-31T11:00:00+00:00"


def setUpModule():
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "airo.db"
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "openaq", "oaq-1", "Site")

    def stored(self):
        return [r[0] for r in self.conn.execute(
            "SELECT observed_utc FROM readings ORDER BY observed_utc")]


class TestEveryFormOfTheSameInstantIsOneRow(Case):
    """The property that matters. Everything else here supports it."""

    EQUIVALENT = [
        "2026-07-31T11:00:00Z",
        "2026-07-31T11:00:00+00:00",
        "2026-07-31T11:00:00.000Z",
        "2026-07-31T21:00:00+10:00",      # same instant, Brisbane
        "2026-07-31T04:00:00-07:00",      # same instant, Los Angeles
    ]

    def test_they_all_collapse_to_one_reading(self):
        for i, form in enumerate(self.EQUIVALENT):
            store.insert_readings(self.conn, self.sid,
                                  [{"observed_utc": form, "pm25": 5.0 + i}])
        self.assertEqual(1, len(self.stored()),
                         f"one instant became {len(self.stored())} rows: "
                         f"{self.stored()}")

    def test_and_that_row_is_in_the_canonical_form(self):
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": "2026-07-31T11:00:00Z",
                                "pm25": 5.0}])
        self.assertEqual([CANONICAL], self.stored())

    def test_a_datetime_and_its_string_are_the_same_row(self):
        store.insert_readings(self.conn, self.sid, [
            {"observed_utc": datetime(2026, 7, 31, 11, tzinfo=timezone.utc),
             "pm25": 5.0}])
        store.insert_readings(self.conn, self.sid, [
            {"observed_utc": "2026-07-31T11:00:00Z", "pm25": 6.0}])
        self.assertEqual([CANONICAL], self.stored())

    def test_re_inserting_the_other_form_adds_nothing(self):
        """Rule 5's mechanism: an overlapping backfill window costs nothing.
        It stopped being true across the format boundary."""
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": CANONICAL, "pm25": 5.0}])
        added = store.insert_readings(
            self.conn, self.sid,
            [{"observed_utc": "2026-07-31T11:00:00Z", "pm25": 5.0}])
        self.assertEqual(0, added, "the same instant was counted as new")


class TestSortingIsNotDisturbed(Case):
    """`+` is 0x2B and `Z` is 0x5A. Mixed forms sort by their punctuation
    rather than by time, and every range query in the project is a string
    comparison."""

    def test_text_order_matches_time_order(self):
        forms = ["2026-07-31T09:00:00Z", "2026-07-31T10:00:00+00:00",
                 "2026-07-31T11:00:00Z", "2026-07-31T12:00:00+00:00"]
        for i, f in enumerate(forms):
            store.insert_readings(self.conn, self.sid,
                                  [{"observed_utc": f, "pm25": float(i)}])
        got = self.stored()
        self.assertEqual(sorted(got), got, "stored order is not time order")
        self.assertEqual(4, len(got))

    def test_a_range_query_catches_every_form(self):
        for f in ("2026-07-31T09:00:00Z", "2026-07-31T10:00:00+00:00"):
            store.insert_readings(self.conn, self.sid,
                                  [{"observed_utc": f, "pm25": 5.0}])
        rows = store.series(
            self.conn, since=datetime(2026, 7, 31, 8, tzinfo=timezone.utc),
            until=datetime(2026, 7, 31, 23, tzinfo=timezone.utc))
        self.assertEqual(2, len(rows),
                         "a range query missed one of the forms")


class TestWeatherAndSourcesToo(Case):
    """The same writer problem, in the tables nobody looked at. Fixing only
    `readings` would leave the shape in place."""

    def test_weather_normalises_its_hour(self):
        store.insert_weather(self.conn, "-33.500,151.000", [
            {"observed_utc": "2026-07-31T11:00:00Z", "temperature_c": 8.0}])
        got = [r[0] for r in self.conn.execute(
            "SELECT observed_utc FROM weather")]
        self.assertEqual([CANONICAL], got)

    def test_weather_dedups_across_forms(self):
        for f in ("2026-07-31T11:00:00Z", CANONICAL):
            store.insert_weather(self.conn, "-33.500,151.000",
                                 [{"observed_utc": f, "temperature_c": 8.0}])
        n = self.conn.execute("SELECT COUNT(*) FROM weather").fetchone()[0]
        self.assertEqual(1, n, "one hour of weather became two rows")


class TestTheCanonicaliserItself(unittest.TestCase):

    def test_it_accepts_every_shape_a_provider_sends(self):
        for raw in ("2026-07-31T11:00:00Z", "2026-07-31T11:00:00+00:00",
                    "2026-07-31T11:00:00.000Z", "2026-07-31T21:00:00+10:00",
                    datetime(2026, 7, 31, 11, tzinfo=timezone.utc)):
            with self.subTest(raw=raw):
                self.assertEqual(CANONICAL, store.canonical_utc(raw))

    def test_a_naive_datetime_is_read_as_utc_not_local(self):
        """Every naive timestamp reaching the store has already been given a
        zone by its provider -- Queensland's is +10:00 and NSW's is derived.
        Treating a naive one as local here would move it by the *reader's*
        offset, which is the bug QLD_TIMEZONE exists to prevent."""
        self.assertEqual(CANONICAL,
                         store.canonical_utc(datetime(2026, 7, 31, 11)))

    def test_something_that_is_not_a_time_is_refused(self):
        for junk in ("", "   ", "not a date", "2026-13-45T99:00", None, 17):
            with self.subTest(junk=junk):
                self.assertIsNone(store.canonical_utc(junk))

    def test_a_row_with_an_unreadable_time_is_skipped_not_stored(self):
        """Rule 5a: nothing is silently discarded -- but a timestamp nothing
        can parse is not a reading, it is a corrupt row, and storing it would
        put a value in the key that every range query then sorts wrongly."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "a.db")
        self.addCleanup(conn.close)
        sid = store.upsert_source(conn, "qld", "a", "A")
        n = store.insert_readings(conn, sid, [
            {"observed_utc": CANONICAL, "pm25": 5.0},
            {"observed_utc": "not a date", "pm25": 9.0},
        ])
        self.assertEqual(1, n)
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM readings").fetchone()[0])


class TestTheMigration(Case):
    """A database written before the fix. Rule 5 governs: merging two rows for
    one instant must never lose a reading."""

    def legacy(self):
        """Rows in both forms, including 64-pair-style collisions."""
        rows = [
            # The same instant, two ways. The Z one carries a value the other
            # lacks, so a naive "keep the first" would lose it.
            ("2026-07-31T11:00:00Z", 6.6, None),
            ("2026-07-31T11:00:00+00:00", 6.6, 55.0),
            # Z-only, no collision.
            ("2026-07-31T12:00:00Z", 6.5, None),
            # Already canonical.
            ("2026-07-31T13:00:00+00:00", 7.0, 60.0),
        ]
        for when, pm, hum in rows:
            self.conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25, humidity,"
                " quality) VALUES (?, ?, ?, ?, 'ok')",
                (self.sid, when, pm, hum))
        self.conn.execute(
            "UPDATE meta SET value = '5' WHERE key = 'schema_version'")
        self.conn.commit()

    def test_every_row_ends_in_the_canonical_form(self):
        self.legacy()
        self.conn.commit()
        again = store.connect(self.db)
        self.addCleanup(again.close)
        got = [r[0] for r in again.execute("SELECT observed_utc FROM readings")]
        self.assertTrue(all(len(g) == 25 and g.endswith("+00:00") for g in got),
                        f"a non-canonical timestamp survived: {got}")

    def test_the_collision_becomes_one_row(self):
        self.legacy()
        again = store.connect(self.db)
        self.addCleanup(again.close)
        n = again.execute(
            "SELECT COUNT(*) FROM readings WHERE observed_utc = ?",
            (CANONICAL,)).fetchone()[0]
        self.assertEqual(1, n)

    def test_merging_keeps_the_better_populated_value(self):
        """Rule 5. One row had humidity and the other did not; the survivor
        must carry it, or the migration loses a measurement while claiming to
        tidy up.

        Both orderings, because one of them passes for the wrong reason. The
        table is WITHOUT ROWID, so rows come back in primary-key order, and
        `'+'` sorts before `'Z'` — put the humidity on the `+00:00` row and a
        migration that simply keeps whichever it saw first still looks
        correct. Only the case where the *later* row holds the value can tell
        a merge from a coincidence.
        """
        for holder in ("Z", "+00:00"):
            with self.subTest(value_on=holder):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                db = Path(tmp.name) / "airo.db"
                conn = store.connect(db)
                sid = store.upsert_source(conn, "openaq", "oaq-1", "Site")
                pairs = [("2026-07-31T11:00:00Z", 55.0 if holder == "Z" else None),
                         ("2026-07-31T11:00:00+00:00",
                          55.0 if holder == "+00:00" else None)]
                for when, hum in pairs:
                    conn.execute(
                        "INSERT INTO readings (source_id, observed_utc, pm25,"
                        " humidity, quality) VALUES (?, ?, 6.6, ?, 'ok')",
                        (sid, when, hum))
                conn.execute(
                    "UPDATE meta SET value = '5' WHERE key = 'schema_version'")
                conn.commit(); conn.close()

                again = store.connect(db)
                try:
                    row = again.execute(
                        "SELECT pm25, humidity FROM readings "
                        "WHERE observed_utc = ?", (CANONICAL,)).fetchone()
                finally:
                    again.close()
                self.assertEqual(6.6, row["pm25"])
                self.assertEqual(
                    55.0, row["humidity"],
                    f"the merge discarded the humidity when it was on the "
                    f"{holder} row")

    def test_no_reading_is_lost_overall(self):
        self.legacy()
        before = self.conn.execute(
            "SELECT COUNT(DISTINCT substr(observed_utc, 1, 19)) FROM readings"
        ).fetchone()[0]
        again = store.connect(self.db)
        self.addCleanup(again.close)
        after = again.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        self.assertEqual(before, after,
                         "the migration changed how many instants are held")

    def test_it_does_not_run_a_second_time(self):
        self.legacy()
        store.connect(self.db).close()
        again = store.connect(self.db)
        try:
            again.execute("INSERT INTO readings (source_id, observed_utc, pm25,"
                          " quality) VALUES (?, '2026-08-01T00:00:00Z', 1.0,"
                          " 'ok')", (self.sid,))
            again.commit()
        finally:
            again.close()
        third = store.connect(self.db)
        self.addCleanup(third.close)
        left = third.execute(
            "SELECT COUNT(*) FROM readings WHERE observed_utc LIKE '%Z'"
        ).fetchone()[0]
        self.assertEqual(1, left,
                         "the migration ran again and rewrote a row it should "
                         "not have seen")


class TestNoTableEscapesTheRule(Case):
    """Enumerated from the schema, not from the table that broke.

    `readings` was the one caught, but `weather`, `sources` and anything added
    later store times the same way and compare them the same way. A check
    naming one table stops covering the moment somebody adds another — which
    is the shape this project has been bitten by three times.
    """

    def time_columns(self):
        """Every column in every table whose name says it holds a time."""
        found = []
        for (table,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            for row in self.conn.execute(f"PRAGMA table_info({table})"):
                name = row[1]
                if name.endswith("_utc") or name in ("observed_utc",):
                    found.append((table, name))
        return found

    def test_the_enumeration_finds_something(self):
        """A check that silently matched nothing would pass forever."""
        cols = self.time_columns()
        self.assertGreaterEqual(len(cols), 4, f"only found {cols}")
        self.assertIn(("readings", "observed_utc"), cols)
        self.assertIn(("weather", "observed_utc"), cols)

    def test_every_stored_time_is_canonical(self):
        store.insert_readings(self.conn, self.sid, [
            {"observed_utc": "2026-07-31T11:00:00Z", "pm25": 5.0},
            {"observed_utc": datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
             "pm25": 6.0}])
        store.insert_weather(self.conn, "-33.500,151.000", [
            {"observed_utc": "2026-07-31T11:00:00Z", "temperature_c": 8.0}])

        for table, col in self.time_columns():
            for (value,) in self.conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} "
                    f"WHERE {col} IS NOT NULL"):
                with self.subTest(column=f"{table}.{col}"):
                    self.assertEqual(
                        store.CANONICAL_UTC_LEN, len(value),
                        f"{table}.{col} holds {value!r}, which is not the "
                        f"canonical form — text comparisons will sort it "
                        f"against the others wrongly")
                    self.assertEqual(value, store.canonical_utc(value),
                                     f"{table}.{col} holds a time that is not "
                                     f"its own canonical form: {value!r}")

    def test_the_check_can_actually_fail(self):
        """Written past the writers on purpose. A guard that has only ever
        seen good data is a guard nobody has tested."""
        self.conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25, quality) "
            "VALUES (?, '2026-07-31T11:00:00Z', 5.0, 'ok')", (self.sid,))
        self.conn.commit()
        bad = [v for (v,) in self.conn.execute(
            "SELECT observed_utc FROM readings")
            if len(v) != store.CANONICAL_UTC_LEN]
        self.assertTrue(bad, "the fixture did not create a bad row")


class TestNoLedgerOnDiskEscapesEither(unittest.TestCase):
    """The rule is about one canonical form, not about SQLite.

    `TestNoTableEscapesTheRule` walks the database. Two ledgers live outside
    it as JSON -- the pending predictions and the verified-skill record -- and
    between them they decide whether the forecast is ever allowed to speak. A
    contract that stops at the database boundary would have said the project
    was normalised while the file that gates the feature was not.

    Enumerated by walking the directory for any `when` key at any depth, so a
    third ledger is covered by existing rather than by somebody remembering.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def whens(self):
        """Every value under a `when` key, from every JSON file written."""
        found = []

        def walk(node, where):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "when" and v is not None:
                        found.append((where, v))
                    else:
                        walk(v, where)
            elif isinstance(node, list):
                for item in node:
                    walk(item, where)

        for path in sorted(self.dir.rglob("*.json")):
            walk(json.loads(path.read_text(encoding="utf-8")), path.name)
        return found

    def exercise(self):
        """Drive both ledgers with the messiest forms a caller can supply."""
        import forecast
        pending = self.dir / "pending.json"
        forecast.remember(pending, when="2026-08-09T10:00:00Z",
                          predicted=10.0, persistence=6.0)
        # A different hour, deliberately: 21:00+11:00 *is* 10:00Z, and the
        # dedup above correctly folds it into one promise.
        forecast.remember(pending, when="2026-08-09T22:00:00+11:00",
                          predicted=12.0, persistence=7.0)
        skill = forecast.Skill(self.dir / "skill.json")
        skill.record(10.0, 6.0, 9.0, when="2026-08-09T10:00:00Z")
        skill.record(11.0, 7.0, 8.0, when=datetime(2026, 8, 9, 11))

    def test_the_enumeration_finds_something(self):
        """Guards the walk itself: a contract over an empty set passes and
        means nothing, which is how three checks in this project stayed green
        after the thing they enumerated moved."""
        self.exercise()
        self.assertGreaterEqual(len(self.whens()), 4,
                                "the walk found no timestamps to check")

    def test_every_timestamp_written_to_disk_is_canonical(self):
        self.exercise()
        for where, value in self.whens():
            self.assertEqual(store.canonical_utc(value), value,
                             f"{where} holds a non-canonical timestamp: "
                             f"{value!r}")

    def test_the_check_can_actually_fail(self):
        """A non-canonical value planted by hand must be caught, or the two
        tests above prove only that nothing wrote anything odd today."""
        (self.dir / "planted.json").write_text(
            json.dumps([{"when": "2026-08-09T10:00:00Z"}]), encoding="utf-8")
        bad = [v for _, v in self.whens() if store.canonical_utc(v) != v]
        self.assertEqual(["2026-08-09T10:00:00Z"], bad)
