"""Storage layer tests.

CONVENTIONS.md hard rule #5: any change to the ingest path needs a test proving
gaps are still detected and repaired. These cover ingest, dedup, gap repair,
the CSV migration and the export round-trip.
"""

import csv
import functools
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import poller  # noqa: E402
import store  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



def ts(minutes_ago, now=None):
    now = now or datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    return (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "airo.db"
        self.conn = store.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestSources(StoreTestCase):
    def test_upsert_is_idempotent(self):
        a = store.upsert_source(self.conn, "purpleair", 1234, "Example sensor")
        b = store.upsert_source(self.conn, "purpleair", 1234, "Example sensor")
        self.assertEqual(a, b)
        self.assertEqual(len(store.list_sources(self.conn)), 1)

    def test_upsert_updates_metadata_without_duplicating(self):
        store.upsert_source(self.conn, "qld", "station-a", "Example station")
        store.upsert_source(self.conn, "qld", "station-a", "Example station",
                            latitude=-33.56, longitude=151.07)
        srcs = store.list_sources(self.conn)
        self.assertEqual(len(srcs), 1)
        self.assertAlmostEqual(srcs[0]["latitude"], -33.56)

    def test_site_id_type_does_not_split_the_source(self):
        """1234 and '1234' must be the same source, not two.

        The equivalence *is* the property, so a number is required here.
        1234 is sequential and cannot be mistaken for a real sensor index.
        The marker below sits on the line it excuses rather than up here: the
        contract test reads it per line, because one sentence in a docstring
        used to exempt every id in the file, including ones written later by
        someone who never read it.
        """
        a = store.upsert_source(self.conn, "purpleair", 1234)
        # numeric-id-is-the-point
        b = store.upsert_source(self.conn, "purpleair", "1234")
        self.assertEqual(a, b)
        self.assertEqual(len(store.list_sources(self.conn)), 1)

    def test_disabling_a_source_keeps_its_readings(self):
        sid = store.upsert_source(self.conn, "qld", "station-a")
        store.insert_readings(self.conn, sid, [{"observed_utc": ts(10), "pm25": 5.0}])
        store.remove_source(self.conn, "qld", "station-a")
        self.assertEqual(store.list_sources(self.conn), [])
        rows = self.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        self.assertEqual(rows, 1, "disabling a source must not delete history")


class TestIngest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.sid = store.upsert_source(self.conn, "purpleair", 1234, "Example sensor")

    def test_insert_counts_only_new_rows(self):
        rows = [{"observed_utc": ts(m), "pm25": 1.0 * m} for m in (30, 20, 10)]
        self.assertEqual(store.insert_readings(self.conn, self.sid, rows), 3)

    def test_reinserting_the_same_window_adds_nothing(self):
        rows = [{"observed_utc": ts(m), "pm25": 1.0} for m in (30, 20, 10)]
        store.insert_readings(self.conn, self.sid, rows)
        again = store.insert_readings(self.conn, self.sid, rows)
        self.assertEqual(again, 0, "overlapping backfill must not double-count")

    def test_same_instant_from_two_sources_is_kept_separately(self):
        other = store.upsert_source(self.conn, "qld", "station-a")
        when = ts(10)
        store.insert_readings(self.conn, self.sid, [{"observed_utc": when, "pm25": 4.0}])
        store.insert_readings(self.conn, other, [{"observed_utc": when, "pm25": 9.0}])
        n = self.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        self.assertEqual(n, 2, "sources must not collide on a shared timestamp")

    def test_update_never_blanks_an_existing_value(self):
        when = ts(10)
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 7.0, "humidity": 55.0}])
        # A later write missing humidity must not erase it.
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 7.0}])
        row = self.conn.execute("SELECT humidity FROM readings").fetchone()
        self.assertEqual(row["humidity"], 55.0)

    def test_implausible_reading_is_flagged_not_dropped(self):
        """Flagged and stored, which was always the point of this test.

        The verdict is now 'extreme' rather than 'suspect'. With no channels
        and no confidence figure there is nothing here that says the
        *instrument* is wrong -- only that the number is enormous, which is a
        claim about the air. Whether to believe it is corroboration's job, and
        fusion already refuses a reading more than UNCORROBORATED_RATIO times
        its peers. Two mechanisms, each answering the question it can.
        """
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": ts(5), "pm25": 4176.0}])
        row = self.conn.execute("SELECT pm25, quality FROM readings").fetchone()
        self.assertEqual(row["quality"], "extreme")
        self.assertEqual(row["pm25"], 4176.0, "flagged data must still be stored")

    def test_rows_without_a_timestamp_are_skipped(self):
        n = store.insert_readings(self.conn, self.sid, [
            {"observed_utc": ts(5), "pm25": 3.0},
            {"pm25": 9.0},  # no timestamp
        ])
        self.assertEqual(n, 1)

    def test_datetime_objects_are_accepted(self):
        when = datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)
        store.insert_readings(self.conn, self.sid, [{"observed_utc": when, "pm25": 2.0}])
        row = self.conn.execute("SELECT observed_utc FROM readings").fetchone()
        self.assertTrue(row["observed_utc"].startswith("2026-07-31T01:00:00"))


class TestGapRepair(StoreTestCase):
    """The never-lose-data guarantee: a gap must be detectable and fillable."""

    def setUp(self):
        super().setUp()
        self.sid = store.upsert_source(self.conn, "purpleair", 1234)

    def test_gap_is_visible_as_a_stale_last_observation(self):
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": ts(180), "pm25": 5.0}])
        last = store.last_observed(self.conn, self.sid)
        gap = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc) - last
        self.assertGreater(gap, timedelta(minutes=25))

    def test_backfill_fills_the_hole_exactly(self):
        # A night of readings with three hours missing in the middle.
        before = [{"observed_utc": ts(m), "pm25": 5.0} for m in range(600, 540, -10)]
        after = [{"observed_utc": ts(m), "pm25": 6.0} for m in range(360, 300, -10)]
        store.insert_readings(self.conn, self.sid, before + after)
        pre = self.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]

        # Backfill the gap, deliberately overlapping both edges.
        repair = [{"observed_utc": ts(m), "pm25": 7.0, "kind": "history"}
                  for m in range(560, 300, -10)]
        added = store.insert_readings(self.conn, self.sid, repair)

        total = self.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        self.assertEqual(total, pre + added)

        # No duplicate instants survived the overlap.
        dupes = self.conn.execute("""
            SELECT observed_utc, COUNT(*) c FROM readings
            GROUP BY observed_utc HAVING c > 1
        """).fetchall()
        self.assertEqual(dupes, [], "overlapping repair created duplicates")

    def test_repair_does_not_overwrite_live_values_with_history(self):
        when = ts(100)
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 12.0, "kind": "live"}])
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": None, "kind": "history"}])
        row = self.conn.execute("SELECT pm25 FROM readings").fetchone()
        self.assertEqual(row["pm25"], 12.0)


class TestSeries(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.sid = store.upsert_source(self.conn, "purpleair", 1234)
        rows = [{"observed_utc": ts(m), "pm25": float(m)} for m in range(120, 0, -10)]
        store.insert_readings(self.conn, self.sid, rows)

    def test_series_is_ordered(self):
        rows = store.series(self.conn, self.sid)
        stamps = [r["observed_utc"] for r in rows]
        self.assertEqual(stamps, sorted(stamps))

    def test_bucketing_preserves_extremes_not_averages(self):
        """Spikes are the signal; a bucket must keep its min and max."""
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": ts(65), "pm25": 300.0}])
        buckets = store.series(self.conn, self.sid, bucket_minutes=60)
        self.assertTrue(any(b["pm25_max"] == 300.0 for b in buckets),
                        "peak was lost to downsampling")

    def test_suspect_rows_excluded_by_default_and_available_on_request(self):
        # A *fault*, evidenced by the two channels disagreeing. The level
        # alone no longer means this: 5000 with both channels agreeing is
        # extreme air, and the chart is the first place that has to show it.
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": ts(5), "pm25": 5000.0,
                                "pm25_a": 9000.0, "pm25_b": 40.0}])
        self.assertNotIn(5000.0, [r["pm25"] for r in store.series(self.conn, self.sid)])
        self.assertIn(5000.0, [r["pm25"] for r in
                               store.series(self.conn, self.sid, include_suspect=True)])


class TestMigration(StoreTestCase):
    def _write_legacy_csv(self, path, rows, header_aqi_col="au_aqi_10min"):
        cols = ["utc", "local", "source", "pm25_10min", header_aqi_col,
                "pm25_now", "pm25_30min", "pm25_60min", "pm25_6hr",
                "pm25_24hr", "pm25_1week", "humidity", "temperature"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])

    def test_legacy_csv_imports_every_row(self):
        path = Path(self.tmp.name) / "readings.csv"
        rows = [{"utc": ts(m), "local": ts(m), "source": "live",
                 "pm25_10min": m / 10.0, "au_aqi_10min": m / 2.5,
                 "humidity": 50, "temperature": 70}
                for m in range(100, 0, -10)]
        self._write_legacy_csv(path, rows)

        added, skipped = store.migrate_from_csv(
            self.conn, path, "purpleair", 1234, "Example sensor")
        self.assertEqual(added, len(rows))
        self.assertEqual(skipped, 0)

    def test_migration_preserves_raw_values(self):
        path = Path(self.tmp.name) / "readings.csv"
        self._write_legacy_csv(path, [
            {"utc": ts(10), "source": "live", "pm25_10min": 2.6, "humidity": 45,
             "temperature": 71},
        ])
        store.migrate_from_csv(self.conn, path, "purpleair", 1234)
        row = self.conn.execute("SELECT pm25, humidity, temperature, "
                                "temperature_unit FROM readings").fetchone()
        self.assertEqual(row["pm25"], 2.6)
        self.assertEqual(row["humidity"], 45.0)
        self.assertEqual(row["temperature_unit"], "F")

    def test_migration_is_idempotent(self):
        path = Path(self.tmp.name) / "readings.csv"
        self._write_legacy_csv(path, [{"utc": ts(m), "pm25_10min": 1.0}
                                      for m in (30, 20, 10)])
        first, _ = store.migrate_from_csv(self.conn, path, "purpleair", 1234)
        second, _ = store.migrate_from_csv(self.conn, path, "purpleair", 1234)
        self.assertEqual(first, 3)
        self.assertEqual(second, 0, "re-running the migration must be safe")

    def test_blank_pm25_is_kept_as_null_not_zero(self):
        """A missing reading must not become a real measurement of zero."""
        path = Path(self.tmp.name) / "readings.csv"
        self._write_legacy_csv(path, [{"utc": ts(10), "pm25_10min": ""}])
        store.migrate_from_csv(self.conn, path, "purpleair", 1234)
        row = self.conn.execute("SELECT pm25 FROM readings").fetchone()
        self.assertIsNone(row["pm25"])


class TestExport(StoreTestCase):
    def test_export_round_trips(self):
        """The preservation guarantee: SQLite out, CSV back, values intact."""
        sid = store.upsert_source(self.conn, "purpleair", 1234, "Example sensor")
        rows = [{"observed_utc": ts(m), "pm25": m / 10.0, "humidity": 50.0}
                for m in range(100, 0, -10)]
        store.insert_readings(self.conn, sid, rows)

        out = Path(self.tmp.name) / "export"
        written = store.export_csv(self.conn, out)
        self.assertEqual(len(written), 1)
        path, count = written[0]
        self.assertEqual(count, len(rows))

        back = store.read_export(path)
        self.assertEqual(len(back), len(rows))

        # Export is ordered oldest first, so compare against the input sorted
        # by observation time -- not by value.
        expected = [r["pm25"] for r in sorted(rows, key=lambda r: r["observed_utc"])]
        self.assertEqual([float(r["pm25"]) for r in back], expected)
        stamps = [r["observed_utc"] for r in back]
        self.assertEqual(stamps, sorted(stamps), "export must be chronological")

    def test_export_carries_its_licence_terms(self):
        """An export is the copy that leaves the machine. Whoever opens it will
        not have read the README, and PurpleAir's terms forbid redistribution."""
        sid = store.upsert_source(self.conn, "purpleair", 1234, "Example sensor")
        store.insert_readings(self.conn, sid, [{"observed_utc": ts(10), "pm25": 5.0}])
        written = store.export_csv(self.conn, Path(self.tmp.name) / "export")
        text = written[0][0].read_text(encoding="utf-8")
        self.assertIn("DO NOT REDISTRIBUTE", text)
        self.assertIn("Powered by PurpleAir", text)
        self.assertIn("Not medical advice", text)
        self.assertIn("reveal a location", text)

    def test_open_licence_export_is_not_marked_do_not_redistribute(self):
        """Over-warning is its own failure: if CC BY data carries a scary
        notice, the notice stops meaning anything."""
        sid = store.upsert_source(self.conn, "qld", "abc", "Gov station")
        store.insert_readings(self.conn, sid, [{"observed_utc": ts(10), "pm25": 5.0}])
        written = store.export_csv(self.conn, Path(self.tmp.name) / "export2")
        text = written[0][0].read_text(encoding="utf-8")
        self.assertIn("CC BY 4.0", text)
        self.assertNotIn("DO NOT REDISTRIBUTE", text)

    def test_every_provider_has_export_terms(self):
        """A provider without terms exports data with 'licence unknown', which
        is the one outcome that helps nobody."""
        for slug in poller.PROVIDERS:
            self.assertIn(slug, store._SOURCE_TERMS,
                          f"{slug} has no export licence text")

    def test_export_terms_match_the_provider_declaration(self):
        """Two places state the licence; they must not drift apart."""
        for slug, prov in poller.PROVIDERS.items():
            attribution, _ = store._SOURCE_TERMS[slug]
            if prov.attribution:
                self.assertEqual(attribution, prov.attribution,
                                 f"{slug} attribution differs between "
                                 f"poller and store")

    def test_export_writes_one_file_per_source(self):
        a = store.upsert_source(self.conn, "purpleair", 1234)
        b = store.upsert_source(self.conn, "qld", "station-a")
        store.insert_readings(self.conn, a, [{"observed_utc": ts(10), "pm25": 1.0}])
        store.insert_readings(self.conn, b, [{"observed_utc": ts(10), "pm25": 2.0}])
        written = store.export_csv(self.conn, Path(self.tmp.name) / "export")
        names = sorted(p.name for p, _ in written)
        self.assertEqual(names, ["purpleair-1234.csv", "qld-station-a.csv"])


class TestTemperatureUnits(StoreTestCase):
    """ROADMAP known issue C.

    PurpleAir reports Fahrenheit, regulatory feeds report Celsius. Mixing both
    in one column with the unit merely implied silently corrupts any
    cross-source comparison.
    """

    def test_conversion(self):
        self.assertAlmostEqual(poller.to_celsius(72.0, "F"), 22.2, places=1)
        self.assertAlmostEqual(poller.to_celsius(32.0, "F"), 0.0, places=1)
        self.assertEqual(poller.to_celsius(20.0, "C"), 20.0)
        self.assertIsNone(poller.to_celsius(None, "F"))

    def test_unknown_unit_is_left_alone(self):
        """Better to pass a value through than to guess and corrupt it."""
        self.assertEqual(poller.to_celsius(20.0, None), 20.0)

    def test_legacy_fahrenheit_rows_are_migrated_on_open(self):
        sid = store.upsert_source(self.conn, "purpleair", 1)
        self.conn.execute("""
            INSERT INTO readings (source_id, observed_utc, pm25, temperature,
                                  temperature_unit, quality)
            VALUES (?, ?, ?, ?, 'F', 'ok')
        """, (sid, ts(10), 5.0, 212.0))
        self.conn.commit()
        self.conn.close()

        conn = store.connect(self.db)          # reopening runs the upgrade
        try:
            row = conn.execute(
                "SELECT temperature, temperature_unit FROM readings").fetchone()
            self.assertAlmostEqual(row["temperature"], 100.0, places=1)
            self.assertEqual(row["temperature_unit"], "C")
        finally:
            conn.close()

    def test_migration_cannot_double_convert(self):
        sid = store.upsert_source(self.conn, "purpleair", 1)
        self.conn.execute("""
            INSERT INTO readings (source_id, observed_utc, pm25, temperature,
                                  temperature_unit, quality)
            VALUES (?, ?, ?, ?, 'F', 'ok')
        """, (sid, ts(10), 5.0, 212.0))
        self.conn.commit()
        self.conn.close()

        for _ in range(3):
            conn = store.connect(self.db)
            conn.close()
        conn = store.connect(self.db)
        try:
            t = conn.execute("SELECT temperature FROM readings").fetchone()["temperature"]
            self.assertAlmostEqual(t, 100.0, places=1,
                                   msg="temperature was converted more than once")
        finally:
            conn.close()

    def test_celsius_rows_are_untouched(self):
        sid = store.upsert_source(self.conn, "qld", "station-a")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": ts(10), "pm25": 4.0, "temperature": 18.5,
             "temperature_unit": "C"}])
        self.conn.close()
        conn = store.connect(self.db)
        try:
            t = conn.execute("SELECT temperature FROM readings").fetchone()["temperature"]
            self.assertEqual(t, 18.5)
        finally:
            conn.close()


class TestRetention(StoreTestCase):
    """Deleting readings is irreversible and they cannot be regenerated, so
    every guard here matters more than the feature does."""

    def setUp(self):
        super().setUp()
        self.sid = store.upsert_source(self.conn, "qld", "x")
        now = datetime.now(timezone.utc)
        rows = []
        for days in (500, 400, 100, 10, 1):
            rows.append({
                "observed_utc": (now - timedelta(days=days)).isoformat(timespec="seconds"),
                "pm25": float(days)})
        store.insert_readings(self.conn, self.sid, rows)

    def test_no_policy_deletes_nothing(self):
        for keep in (0, None, -1, -365):
            removed, _, _ = store.prune(self.conn, keep)
            self.assertEqual(removed, 0, f"retention={keep!r} deleted rows")
        n = self.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        self.assertEqual(n, 5)

    def test_dry_run_reports_without_removing(self):
        removed, kept, _ = store.prune(self.conn, 200, dry_run=True)
        self.assertEqual(removed, 2)
        self.assertEqual(kept, 5, "dry run must not delete")

    def test_prune_removes_only_what_is_older(self):
        removed, kept, _ = store.prune(self.conn, 200)
        self.assertEqual(removed, 2)
        self.assertEqual(kept, 3)
        oldest = self.conn.execute(
            "SELECT MIN(observed_utc) m FROM readings").fetchone()["m"]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=200))
        self.assertGreater(datetime.fromisoformat(oldest), cutoff)

    def test_prune_is_idempotent(self):
        store.prune(self.conn, 200)
        removed, _, _ = store.prune(self.conn, 200)
        self.assertEqual(removed, 0, "second run should find nothing left")

    def test_size_reporting_includes_the_write_ahead_log(self):
        size = store.db_size_bytes(self.db)
        self.assertGreater(size, 0)


class TestIntegrityVerification(StoreTestCase):
    """SQLite corruption is silent: a truncated write surfaces as missing rows,
    not an error. Since this data cannot be regenerated, it is worth asking."""

    def test_a_healthy_database_reports_no_problems(self):
        sid = store.upsert_source(self.conn, "qld", "x")
        store.insert_readings(self.conn, sid, [{"observed_utc": ts(10), "pm25": 5.0}])
        self.assertEqual(store.verify(self.conn), [])

    def test_readings_dated_in_the_future_are_reported(self):
        """A clock or parsing fault would poison every average it lands in."""
        sid = store.upsert_source(self.conn, "qld", "x")
        ahead = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
        store.insert_readings(self.conn, sid, [{"observed_utc": ahead, "pm25": 5.0}])
        self.assertTrue(any("future" in p for p in store.verify(self.conn)))

    def test_negative_readings_are_reported(self):
        """Written by an older version, before insert_readings rejected them.
        Inserted directly so the guard under test is verify(), not ingest."""
        sid = store.upsert_source(self.conn, "qld", "x")
        self.conn.execute(
            "INSERT INTO readings (source_id, observed_utc, kind, pm25) "
            "VALUES (?, ?, 'live', ?)", (sid, ts(5), -3.0))
        self.assertTrue(any("negative" in p for p in store.verify(self.conn)))

    def test_ingest_refuses_a_sentinel_before_it_is_ever_stored(self):
        """Queensland reports -9999 when a station is offline. Stored as a
        reading it corrupts every average computed from that row -- and on the
        Australian scale it renders as "Very good", which is the most
        reassuring label the tool has, for air nobody measured."""
        sid = store.upsert_source(self.conn, "qld", "sou")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": ts(20), "pm25": 3.2},
            {"observed_utc": ts(10), "pm25": -9999.0},
        ])
        rows = self.conn.execute(
            "SELECT observed_utc, pm25 FROM readings ORDER BY observed_utc"
        ).fetchall()
        self.assertEqual([r["pm25"] for r in rows], [3.2, None],
                         "a sentinel was stored as a real concentration")
        self.assertEqual(store.verify(self.conn), [],
                         "ingest let through what verify would have flagged")

    def test_a_sentinel_channel_reading_is_also_rejected(self):
        """PurpleAir A/B channels feed the disagreement check; a negative
        there would make a healthy sensor look faulty."""
        sid = store.upsert_source(self.conn, "purpleair", 1)
        store.insert_readings(self.conn, sid, [
            {"observed_utc": ts(10), "pm25": 5.0,
             "pm25_a": -9999.0, "pm25_b": 5.1},
        ])
        row = self.conn.execute(
            "SELECT pm25, pm25_a, pm25_b FROM readings").fetchone()
        self.assertIsNone(row["pm25_a"])
        self.assertEqual(row["pm25_b"], 5.1)

    def test_orphaned_readings_are_reported(self):
        sid = store.upsert_source(self.conn, "qld", "x")
        store.insert_readings(self.conn, sid, [{"observed_utc": ts(5), "pm25": 5.0}])
        # Delete the source row directly, bypassing the cascade.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
        self.conn.commit()
        self.assertTrue(any("no known source" in p for p in store.verify(self.conn)))


class TestSchemaUpgrade(StoreTestCase):
    def test_columns_added_to_an_existing_database(self):
        """CREATE TABLE IF NOT EXISTS does nothing to an existing table, so new
        columns must be added explicitly or every write fails."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(readings)")}
        for expected in ("pm25_a", "pm25_b", "confidence"):
            self.assertIn(expected, cols)

    def test_schema_version_is_recorded(self):
        v = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(int(v["value"]), store.SCHEMA_VERSION)




class TestBackfillHonoursZero(unittest.TestCase):
    """`days or 7` turned an explicit 0 into 7. A user who answered "0" to
    "Days of history to pull now" got a week of history anyway -- the setting
    silently did nothing, and on a slow feed it also made first run take
    minutes they had asked to skip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "qld", "wbk", "Westbrook")
        self.asked = []

        outer = self

        class Fake(poller.Provider):
            slug = "qld"
            needs_key = False
            resolution_minutes = 60

            def history(self, src, key, start, end):
                outer.asked.append((start, end))
                return [{"observed_utc": start.isoformat(timespec="seconds"),
                         "pm25": 4.0}]
        self.provider = Fake()

    def test_zero_days_fetches_nothing(self):
        n = poller.backfill_source(self.conn, self.sid,
                                   {"provider": "qld", "site_id": "wbk"},
                                   self.provider, days=0)
        self.assertEqual(n, 0, "0 days of history still pulled rows")
        self.assertEqual(self.asked, [], "the provider was called anyway")

    def test_none_still_means_the_default_week(self):
        poller.backfill_source(self.conn, self.sid,
                               {"provider": "qld", "site_id": "wbk"},
                               self.provider, days=None)
        self.assertEqual(len(self.asked), 1)
        start, end = self.asked[0]
        self.assertAlmostEqual((end - start).days, 7, delta=1)

    def test_an_explicit_span_is_respected(self):
        poller.backfill_source(self.conn, self.sid,
                               {"provider": "qld", "site_id": "wbk"},
                               self.provider, days=3)
        start, end = self.asked[0]
        self.assertAlmostEqual((end - start).days, 3, delta=1)

    def test_a_negative_span_cannot_reach_into_the_future(self):
        n = poller.backfill_source(self.conn, self.sid,
                                   {"provider": "qld", "site_id": "wbk"},
                                   self.provider, days=-5)
        self.assertEqual(n, 0)
        self.assertEqual(self.asked, [])


class TestSentinelRepair(unittest.TestCase):
    """Ingest rejects sentinels now, but a database written by an earlier
    version still holds whatever the feed sent -- and those rows poison every
    average computed from them. --repair corrects the record."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "qld", "sou", "Southmoor")
        # Inserted directly: insert_readings would refuse them, which is the
        # whole point -- these predate that guard.
        rows = [(self.sid, ts(60 - i * 10), "backfill",
                 -9999.0 if i in (1, 2) else 4.0 + i,
                 -9999.0 if i in (1, 2) else 4.0 + i)
                for i in range(5)]
        self.conn.executemany(
            "INSERT INTO readings (source_id, observed_utc, kind, pm25, pm25_now)"
            " VALUES (?,?,?,?,?)", rows)
        self.conn.commit()

    def test_find_reports_the_damage_per_source_and_column(self):
        found = store.find_sentinels(self.conn)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["n"], 2)
        self.assertEqual(found[0]["provider"], "qld")
        self.assertEqual(found[0]["columns"]["pm25"], 2)
        self.assertEqual(found[0]["columns"]["pm25_now"], 2)

    def test_a_dry_run_changes_nothing(self):
        """The preview before a correcting write must be exactly that."""
        store.repair_sentinels(self.conn, dry_run=True)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE pm25 < 0").fetchone()["n"]
        self.assertEqual(n, 2, "a dry run modified the database")

    def test_repair_clears_every_pm_column(self):
        store.repair_sentinels(self.conn)
        for col in store.PM_COLUMNS:
            n = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM readings WHERE {col} < 0").fetchone()["n"]
            self.assertEqual(n, 0, f"{col} still holds a sentinel")

    def test_repair_nulls_rather_than_guessing(self):
        """The correct value is unknown; inventing one would be worse than
        the sentinel."""
        store.repair_sentinels(self.conn)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE pm25 IS NULL").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_repair_never_deletes_a_row(self):
        """Deleting would erase that we asked and the station answered, and
        would make the gap detector re-fetch a window already known unfillable."""
        before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        store.repair_sentinels(self.conn)
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        self.assertEqual(before, after)

    def test_repair_leaves_good_readings_alone(self):
        store.repair_sentinels(self.conn)
        vals = sorted(r["pm25"] for r in self.conn.execute(
            "SELECT pm25 FROM readings WHERE pm25 IS NOT NULL"))
        self.assertEqual(vals, [4.0, 7.0, 8.0])

    def test_verify_is_clean_afterwards(self):
        self.assertTrue(store.verify(self.conn), "verify missed the damage")
        store.repair_sentinels(self.conn)
        self.assertEqual(store.verify(self.conn), [])

    def test_quality_is_not_left_judging_a_value_that_is_gone(self):
        """quality is NOT NULL and was derived from the sentinel."""
        self.conn.execute("UPDATE readings SET quality = 'suspect' WHERE pm25 < 0")
        self.conn.commit()
        store.repair_sentinels(self.conn)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM readings "
            "WHERE pm25 IS NULL AND quality <> 'ok'").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_a_clean_database_reports_nothing_to_do(self):
        store.repair_sentinels(self.conn)
        self.assertEqual(store.repair_sentinels(self.conn), [])


class TestDryRunIsAModifierNotAMode(unittest.TestCase):
    """`--prune --dry-run` is documented twice in the README as the way to
    preview a destructive delete, and it was an argparse error: --dry-run sat
    inside the mutually exclusive group with the modes it modifies. Someone
    checking what pruning would remove got an error, and could reasonably have
    run --prune without the preview."""

    ROOT = Path(__file__).resolve().parent.parent

    def _accepts(self, argv):
        """Does the parser accept this combination? Runs the real parser."""
        import subprocess
        import sys as _sys
        code = (
            "import sys; sys.path.insert(0, %r); import poller, argparse\n"
            "sys.argv = ['poller.py'] + %r\n"
            "try:\n"
            "    poller.main()\n"
            "except SystemExit as e:\n"
            "    sys.exit(2 if e.code == 2 else 0)\n"
            "except Exception:\n"
            "    sys.exit(0)\n" % (str(self.ROOT), argv))
        # A subprocess is outside every in-process guard, so it needs the
        # environment overrides poller already honours. Without them this ran
        # `--prune` against the DEVELOPER'S OWN install: it read their config
        # and appended to their log, between two real polls, which is exactly
        # what CONVENTIONS forbids. Nothing was deleted -- retention_days is 0
        # there -- but only because of how that machine happened to be set up.
        import os as _os
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            env = dict(_os.environ,
                       AIRO_DATA=str(Path(tmp) / "data"),
                       AIRO_CONFIG=str(Path(tmp) / "config.json"),
                       HOME=str(Path(tmp) / "home"),
                       USERPROFILE=str(Path(tmp) / "home"))
            r = subprocess.run([_sys.executable, "-c", code], env=env,
                               capture_output=True, text=True, timeout=90)
        return r.returncode != 2      # 2 is argparse's usage error

    def test_prune_accepts_dry_run(self):
        self.assertTrue(self._accepts(["--prune", "--dry-run"]),
                        "the documented preview for a destructive delete is "
                        "rejected by the parser")

    def test_repair_accepts_dry_run(self):
        self.assertTrue(self._accepts(["--repair", "--dry-run"]))

    def test_two_modes_are_still_mutually_exclusive(self):
        """Loosening --dry-run must not loosen the modes themselves."""
        self.assertFalse(self._accepts(["--prune", "--verify"]),
                         "two modes were accepted together")

    def test_dry_run_is_not_in_the_exclusive_group(self):
        src = (self.ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index('"--dry-run"')
        line_start = src.rfind("\n", 0, i)
        self.assertIn("ap.add_argument", src[line_start:i],
                      "--dry-run is back inside the mutually exclusive group")


class TestGuardsNothingWasExercising(StoreTestCase):
    """Seven guards in store.py that no test reached.

    Found by removing each one and running the suite: nothing went red. Two of
    them decide what gets ingested from a CSV, and one decides what an export
    contains -- the kind of thing that is wrong quietly, in a file somebody
    reads a year later.
    """

    def seed(self, provider="qld", site="a", rows=3):
        sid = store.upsert_source(self.conn, provider, site, f"Site {site}")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": f"2026-07-0{i + 1}T00:00:00+00:00", "pm25": 5.0 + i}
            for i in range(rows)])
        return sid

    def test_inserting_nothing_stores_nothing_and_says_so(self):
        sid = self.seed()
        before = self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        self.assertEqual(0, store.insert_readings(self.conn, sid, []))
        self.assertEqual(before,
                         self.conn.execute("SELECT COUNT(*) FROM readings")
                             .fetchone()[0])

    def test_exporting_one_source_does_not_export_the_others(self):
        """The filter is one `continue`. Without it, asking for one site's
        readings hands you everybody's -- including, on a shared machine, a
        location the person asking did not mean to share."""
        wanted = self.seed(site="wanted")
        self.seed(site="other")
        out = Path(self.tmp.name) / "export"
        out.mkdir()

        store.export_csv(self.conn, out, source_id=wanted)

        written = sorted(p.name for p in out.glob("*.csv"))
        self.assertEqual(1, len(written), f"exported {written}")
        self.assertIn("wanted", written[0])

    def test_exporting_everything_still_writes_every_source(self):
        """The control: without it the test above would pass if export_csv
        wrote nothing at all."""
        self.seed(site="one")
        self.seed(site="two")
        out = Path(self.tmp.name) / "all"
        out.mkdir()
        store.export_csv(self.conn, out)
        self.assertEqual(2, len(list(out.glob("*.csv"))))

    def test_a_csv_row_with_no_timestamp_is_skipped_not_stored(self):
        """A reading with no time cannot be placed in the record, and storing
        it under a blank key would corrupt the ordering everything else
        depends on."""
        csv_path = Path(self.tmp.name) / "old.csv"
        csv_path.write_text(
            "utc,pm25_10min\n"
            "2026-07-01T00:00:00+00:00,5.0\n"
            ",9.9\n"
            "   ,8.8\n", encoding="utf-8")

        added, skipped = store.migrate_from_csv(self.conn, csv_path, "qld", "m",
                                           "Migrated")

        self.assertEqual(1, added)
        self.assertEqual(2, skipped)
        stored = self.conn.execute(
            "SELECT observed_utc FROM readings").fetchall()
        self.assertEqual(["2026-07-01T00:00:00+00:00"], [r[0] for r in stored])

    def test_a_missing_csv_migrates_nothing_rather_than_raising(self):
        added, skipped = store.migrate_from_csv(
            self.conn, Path(self.tmp.name) / "nope.csv", "qld", "x", "X")
        self.assertEqual((0, 0), (added, skipped))

    def test_an_empty_csv_field_becomes_null_not_zero(self):
        """`float("")` raises, but a bare except would turn a blank humidity
        into 0.0 — a measurement nobody took, indistinguishable from one they
        did."""
        self.assertIsNone(store._f(""))
        self.assertIsNone(store._f(None))
        self.assertIsNone(store._f("not a number"))
        self.assertEqual(0.0, store._f("0"),
                         "a real zero must survive as a real zero")

    def test_a_datetime_is_serialised_as_utc_iso(self):
        moment = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.assertEqual("2026-07-31T12:00:00+00:00", store._iso(moment))
        offset = datetime(2026, 7, 31, 22, 0,
                          tzinfo=timezone(timedelta(hours=10)))
        self.assertEqual("2026-07-31T12:00:00+00:00", store._iso(offset),
                         "a non-UTC datetime was not converted")
        self.assertEqual("already a string", store._iso("already a string"))

    def test_a_history_with_no_overlap_reports_no_samples(self):
        """`pct()` indexes into the ratio list. Empty, that raises — inside
        the corroboration path, which then takes down the poll rather than
        declining to have an opinion."""
        sid = self.seed(site="alone")
        h = store.peer_ratio_history(self.conn, sid)
        self.assertEqual(0, h["n"])
        self.assertIsNone(h["p90"])

    def test_history_can_be_narrowed_to_one_hour_of_the_day(self):
        """The evening premium is the whole point of the tool, so "how does
        this site compare at 7pm" must not silently answer "at any hour"."""
        a = store.upsert_source(self.conn, "qld", "target", "Target")
        b = store.upsert_source(self.conn, "qld", "peer", "Peer")
        rows_a, rows_b = [], []
        for day in range(1, 11):
            # 19:00 — the target runs 4x its peer. 07:00 — they agree.
            rows_a.append({"observed_utc": f"2026-07-{day:02d}T19:00:00+00:00",
                           "pm25": 40.0})
            rows_b.append({"observed_utc": f"2026-07-{day:02d}T19:00:00+00:00",
                           "pm25": 10.0})
            rows_a.append({"observed_utc": f"2026-07-{day:02d}T07:00:00+00:00",
                           "pm25": 10.0})
            rows_b.append({"observed_utc": f"2026-07-{day:02d}T07:00:00+00:00",
                           "pm25": 10.0})
        store.insert_readings(self.conn, a, rows_a)
        store.insert_readings(self.conn, b, rows_b)

        evening = store.peer_ratio_history(self.conn, a, hour_of_day=19,
                                           days=36500)
        morning = store.peer_ratio_history(self.conn, a, hour_of_day=7,
                                           days=36500)

        self.assertGreater(evening["median"], 3.0)
        self.assertLess(morning["median"], 1.5)
        self.assertNotEqual(evening["n"] + morning["n"], evening["n"],
                            "the hour filter matched everything")


class DaylightSaving(tzinfo):
    """A zone that shifts by an hour mid-year, standing in for a real one.

    Written out rather than taken from zoneinfo because zoneinfo needs a
    system tz database, which Windows does not have without a package -- and
    a runtime dependency is out of the question here, so a test that quietly
    skipped on one platform is the alternative. Eight lines of arithmetic
    tests the property everywhere instead.

    Los Angeles, roughly: UTC-8 in winter, UTC-7 from March to November.
    """

    def __init__(self, standard=-8, summer=-7):
        self.standard, self.summer = standard, summer

    def _is_summer(self, dt):
        return 3 <= dt.month <= 10

    def utcoffset(self, dt):
        return timedelta(hours=self.summer if self._is_summer(dt)
                         else self.standard)

    def dst(self, dt):
        return timedelta(hours=1) if self._is_summer(dt) else timedelta(0)

    def tzname(self, dt):
        return "SUMMER" if self._is_summer(dt) else "STANDARD"


class TestAnHourOfTheDayIsNotAnOffset(StoreTestCase):
    """CONVENTIONS: "Date logic assumes no DST. Correct in Queensland, wrong
    elsewhere."

    The hour a person means by "7pm" is a different UTC hour in July than in
    January, anywhere that puts its clocks forward. Anything that converts by
    applying one offset to a bare hour number is right for half the year and
    an hour out for the rest — and an evening-premium tool being an hour out
    about the evening is the whole finding, misplaced.
    """

    def test_the_utc_hour_is_read_from_the_key_as_stored(self):
        self.assertEqual(store._utc_hour("2026-07-04T19"), 19)
        self.assertEqual(store._utc_hour("2026-01-04T00"), 0)

    def test_a_malformed_key_gives_none_rather_than_raising(self):
        for bad in ("", "nonsense", "2026-07-04T", None, 19):
            self.assertIsNone(store._local_hour(bad), f"{bad!r}")
        for bad in ("", "xx", None):
            self.assertIsNone(store._utc_hour(bad), f"{bad!r}")

    def test_a_key_of_the_wrong_length_is_refused_not_half_read(self):
        """The dangerous malformed key is the one that still parses.

        Slicing a fixed-width string does not notice a truncation: one
        character short reads hour 1 where the truncated value was 19, and one
        long silently ignores the tail. Neither raises, so without a length
        check they return a plausible number instead of nothing — which is the
        failure mode this project keeps finding, not a crash but a confident
        wrong answer.
        """
        for bad in ("2026-07-04T1", "2026-07-04T199", "2026-07-04T19:00"):
            self.assertIsNone(store._local_hour(bad),
                              f"{bad!r} was read as a valid hour")

    def test_a_key_of_the_right_length_but_impossible_values_is_refused(self):
        """The length check catches truncation; it cannot catch nonsense that
        happens to be thirteen characters long. Month 99 and hour 44 have to
        be refused by trying to build the date, not by measuring the string."""
        for bad in ("2026-99-04T19", "2026-07-04T44", "20x6-07-04T19",
                    "2026-02-30T19"):
            self.assertIsNone(store._local_hour(bad),
                              f"{bad!r} was read as a valid hour")

    def test_a_nonsense_hour_asks_for_no_rows_rather_than_raising(self):
        """poller.py's corroboration falls back to all-hours history when a
        narrower query comes back empty. Raising instead would lose that
        source's history altogether, which is the worse of the two."""
        a = store.upsert_source(self.conn, "qld", "target", "Target")
        b = store.upsert_source(self.conn, "qld", "peer", "Peer")
        store.insert_readings(self.conn, a, [
            {"observed_utc": "2026-07-01T02:00:00+00:00", "pm25": 40.0}])
        store.insert_readings(self.conn, b, [
            {"observed_utc": "2026-07-01T02:00:00+00:00", "pm25": 10.0}])
        for junk in ("banana", object(), [2]):
            with self.subTest(hour=junk):
                got = store.peer_ratio_history(self.conn, a, hour_of_day=junk,
                                               days=36500)
                self.assertEqual(got["n"], 0)
                self.assertIsNone(got["p90"])

    def test_the_same_local_hour_in_summer_and_winter_is_two_utc_hours(self):
        tz = DaylightSaving()
        # 7pm local is 03:00 UTC in January and 02:00 UTC in July.
        self.assertEqual(store._local_hour("2026-01-05T03", tz), 19)
        self.assertEqual(store._local_hour("2026-07-05T02", tz), 19)
        # And the fixed-offset answer, which is what was being printed: using
        # January's offset all year labels the July bucket 18:00.
        self.assertEqual(store._local_hour("2026-07-05T03", tz), 20)

    def test_an_offset_that_is_not_whole_hours_still_lands_on_the_right_hour(self):
        """India, Adelaide, Nepal, Newfoundland, Chatham.

        A UTC-hour bucket straddles two local hours in these zones, so the
        bucket's start labels 7pm as 6pm. Attributing by the midpoint puts it
        back where a reading taken at 7pm actually is. Fixed offsets here, so
        this asserts the arithmetic and not a tz database.
        """
        zones = {
            "Adelaide  +9:30": (timedelta(hours=9, minutes=30), "2026-07-05T09"),
            "India     +5:30": (timedelta(hours=5, minutes=30), "2026-07-05T13"),
            "Nepal     +5:45": (timedelta(hours=5, minutes=45), "2026-07-05T13"),
            "Chatham  +12:45": (timedelta(hours=12, minutes=45), "2026-07-05T06"),
            "St John's -3:30": (timedelta(hours=-3, minutes=-30), "2026-07-05T22"),
            "Brisbane    +10": (timedelta(hours=10), "2026-07-05T09"),
        }
        for name, (offset, bucket) in zones.items():
            with self.subTest(zone=name):
                tz = timezone(offset)
                # The bucket named is the one holding that zone's 7pm.
                self.assertEqual(
                    store._local_hour(bucket, tz), 19,
                    f"{name}: bucket {bucket} should read as the 7pm hour")

    def test_a_local_hour_filter_gathers_both_sides_of_the_changeover(self):
        a = store.upsert_source(self.conn, "qld", "target", "Target")
        b = store.upsert_source(self.conn, "qld", "peer", "Peer")
        # Ten evenings at 7pm local: five in January, five in July. Stored in
        # UTC they are 03:00 and 02:00 respectively.
        rows_a, rows_b = [], []
        for day in range(1, 6):
            for month, hour in (("01", "03"), ("07", "02")):
                stamp = f"2026-{month}-{day:02d}T{hour}:00:00+00:00"
                rows_a.append({"observed_utc": stamp, "pm25": 40.0})
                rows_b.append({"observed_utc": stamp, "pm25": 10.0})
        store.insert_readings(self.conn, a, rows_a)
        store.insert_readings(self.conn, b, rows_b)

        # The zone is an argument now, so the test says what it means rather
        # than patching the helper underneath the query. It used to bind the
        # synthetic zone with functools.partial, which broke the moment the
        # caller started passing tz itself -- a test coupled to how the code
        # reached its answer instead of to the answer.
        evening = store.peer_ratio_history(
            self.conn, a, hour_of_day=19, days=36500, hour_is_local=True,
            tz=DaylightSaving())

        self.assertEqual(evening["n"], 10,
                         "a local-hour filter must collect both offsets")

    def test_utc_selection_is_unchanged_and_still_the_default(self):
        # poller.py's corroboration check asks about the UTC hour happening
        # now. Changing what that means would change a safety-critical input,
        # so the default must stay exactly what it was.
        a = store.upsert_source(self.conn, "qld", "target", "Target")
        b = store.upsert_source(self.conn, "qld", "peer", "Peer")
        rows = [{"observed_utc": f"2026-07-{d:02d}T02:00:00+00:00",
                 "pm25": 40.0} for d in range(1, 6)]
        peers = [{"observed_utc": f"2026-07-{d:02d}T02:00:00+00:00",
                  "pm25": 10.0} for d in range(1, 6)]
        store.insert_readings(self.conn, a, rows)
        store.insert_readings(self.conn, b, peers)

        self.assertEqual(
            store.peer_ratio_history(self.conn, a, hour_of_day=2,
                                     days=36500)["n"], 5)
        self.assertEqual(
            store.peer_ratio_history(self.conn, a, hour_of_day=19,
                                     days=36500)["n"], 0)


class TestQualityIsDecidedOnceAndSurfaced(StoreTestCase):
    """ARCHITECTURE §3.5. "Is this reading plausible?" is a health-relevant
    judgement, so it is made once, at ingest, on the concentration -- and every
    surface renders the answer rather than forming its own.

    The dashboard had its own: a threshold on the *index*, converting back to
    µg/m³ by dividing by four, which is the Australian scale written out as
    arithmetic and wrong on any other scale.
    """

    def test_an_implausible_level_is_flagged_not_dropped(self):
        sid = store.upsert_source(self.conn, "qld", "a", "Site")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": "2026-07-01T00:00:00+00:00", "pm25": 5.0},
            {"observed_utc": "2026-07-01T01:00:00+00:00", "pm25": 900.0}])

        rows = self.conn.execute(
            "SELECT pm25, quality FROM readings ORDER BY observed_utc").fetchall()

        self.assertEqual(2, len(rows), "the implausible reading was discarded")
        self.assertEqual("ok", rows[0]["quality"])
        self.assertEqual("extreme", rows[1]["quality"])

    def test_channel_disagreement_is_a_fault_even_at_a_believable_level(self):
        """The signal the dashboard's level-only threshold could never see: a
        blocked inlet on one of a PurpleAir's two lasers reads as ordinary
        air."""
        self.assertEqual("suspect", store.assess_quality(30.0, pm25_a=10.0,
                                                         pm25_b=50.0))
        self.assertEqual("ok", store.assess_quality(30.0, pm25_a=29.0,
                                                    pm25_b=31.0))

    def test_low_provider_confidence_is_a_fault(self):
        self.assertEqual("suspect", store.assess_quality(20.0, confidence=10.0))
        self.assertEqual("ok", store.assess_quality(20.0, confidence=95.0))

    def test_an_extreme_level_alone_is_not_evidence_of_a_fault(self):
        """The distinction this whole class turns on.

        `pm25 > SUSPECT_PM25` used to answer "the instrument is broken", and it
        cannot: it is a statement about the *air*. Black Summer put Australian
        suburbs past 350 µg/m³ for days, and on those days the analysis showed
        nothing, because the readings that mattered most were the ones being
        filed as sensor faults and dropped from every aggregate.

        Two verdicts now. `suspect` means positive evidence the instrument is
        wrong -- channels disagreeing, or the network's own confidence being
        low. `extreme` means the value is implausibly high and nothing suggests
        a fault, which during a fire is simply true. Only `suspect` is
        withheld from the numbers.
        """
        # Both channels agree at a terrible level: that is the air.
        self.assertEqual("extreme",
                         store.assess_quality(900.0, pm25_a=890.0, pm25_b=910.0))
        # A feed with one value and no way to self-check. Still not a fault --
        # every government monitor in the country reports exactly this shape.
        self.assertEqual("extreme", store.assess_quality(900.0))

    def test_fault_evidence_still_wins_over_the_level(self):
        """Order matters. Checking the level first made every faulty reading
        above the threshold indistinguishable from genuinely awful air."""
        self.assertEqual("suspect",
                         store.assess_quality(900.0, pm25_a=1700.0, pm25_b=100.0))
        self.assertEqual("suspect",
                         store.assess_quality(900.0, confidence=10.0))

    def test_the_boundary_itself(self):
        self.assertEqual("ok", store.assess_quality(store.SUSPECT_PM25))
        self.assertEqual("extreme",
                         store.assess_quality(store.SUSPECT_PM25 + 0.1))


    def test_a_high_but_corroborated_reading_is_not_called_a_fault(self):
        """Deciding whether genuinely bad air reflects the region is
        corroboration's job, not quality's. Flagging it here would hide a fire
        next door."""
        self.assertEqual("ok", store.assess_quality(200.0, pm25_a=198.0,
                                                    pm25_b=202.0))

    def test_the_series_leaves_a_broken_instrument_out_of_the_chart(self):
        """Right for a chart: a blocked inlet swamps the axis and every
        average drawn from it. It is the *blocked inlet* that earns the
        exclusion, though, not the size of the number -- 900 µg/m³ from an
        instrument agreeing with itself is the reading someone most needs to
        see, and it used to be the one thing the chart would not draw."""
        sid = store.upsert_source(self.conn, "purpleair", "a", "Site")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": "2026-07-01T00:00:00+00:00", "pm25": 5.0,
             "pm25_a": 5.0, "pm25_b": 5.0},
            {"observed_utc": "2026-07-01T01:00:00+00:00", "pm25": 900.0,
             "pm25_a": 1700.0, "pm25_b": 100.0}])
        rows = store.series(self.conn,
                            since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([5.0], [r["pm25"] for r in rows])

    def test_but_they_are_retrievable_so_nothing_is_dropped_in_silence(self):
        """Excluded and *unmentioned* is a silent drop, which is the one thing
        the policy forbids. The count has to be able to reach a surface."""
        sid = store.upsert_source(self.conn, "qld", "a", "Site")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": "2026-07-01T00:00:00+00:00", "pm25": 5.0},
            {"observed_utc": "2026-07-01T01:00:00+00:00", "pm25": 900.0}])

        flagged = store.suspect_readings(
            self.conn, since=datetime(2020, 1, 1, tzinfo=timezone.utc))

        self.assertEqual([900.0], [r["pm25"] for r in flagged])
        self.assertEqual("extreme", flagged[0]["quality"],
                         "a level with no fault evidence is not a fault")

    def test_asking_for_them_explicitly_puts_them_back_in_the_series(self):
        sid = store.upsert_source(self.conn, "qld", "a", "Site")
        store.insert_readings(self.conn, sid, [
            {"observed_utc": "2026-07-01T01:00:00+00:00", "pm25": 900.0}])
        rows = store.series(self.conn, include_suspect=True,
                            since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([900.0], [r["pm25"] for r in rows])

class TestExtremeAirReachesTheNumbers(StoreTestCase):
    """`extreme` is shown *and counted*; `suspect` is shown and not counted.

    The split exists so that the one case worth waking someone up for -- air
    that is genuinely dangerous -- is not filtered out on the way to the chart,
    the evening analysis and the alert, while a blocked inlet still is.
    """

    def seed_one_of_each(self):
        sid = store.upsert_source(self.conn, "purpleair", "1", "Sensor")
        store.insert_readings(self.conn, sid, [
            # ordinary
            {"observed_utc": "2026-07-01T00:00:00+00:00", "pm25": 10.0,
             "pm25_a": 10.0, "pm25_b": 10.0},
            # terrible air, instrument agreeing with itself
            {"observed_utc": "2026-07-01T01:00:00+00:00", "pm25": 900.0,
             "pm25_a": 890.0, "pm25_b": 910.0},
            # broken instrument
            {"observed_utc": "2026-07-01T02:00:00+00:00", "pm25": 900.0,
             "pm25_a": 1700.0, "pm25_b": 100.0},
        ])
        return sid

    def test_the_series_a_chart_draws_includes_extreme_air(self):
        self.seed_one_of_each()
        got = {r["pm25"]: r["quality"] for r in store.series(self.conn)}
        self.assertIn(900.0, got, "the worst air of the day was not charted")
        self.assertEqual("extreme", got[900.0])

    def test_the_series_still_leaves_out_a_broken_instrument(self):
        self.seed_one_of_each()
        qualities = {r["quality"] for r in store.series(self.conn)}
        self.assertNotIn("suspect", qualities)

    def test_asking_for_everything_brings_the_fault_back_too(self):
        self.seed_one_of_each()
        rows = store.series(self.conn, include_suspect=True)
        self.assertEqual(3, len(rows))

    def test_both_kinds_are_still_reported_as_flagged(self):
        """suspect_readings() is what tells a surface something was flagged.
        It matches by `!= 'ok'`, so a verdict added later is already in scope
        -- the enumeration habit, working as intended."""
        self.seed_one_of_each()
        flagged = {r["quality"] for r in store.suspect_readings(self.conn)}
        self.assertEqual({"extreme", "suspect"}, flagged)

    def test_peer_ratio_history_counts_extreme_air(self):
        """Corroboration compares a site against its peers. Dropping the
        highest readings from that comparison is how a site that is right
        about a fire gets told it is exaggerating."""
        a = self.seed_one_of_each()
        b = store.upsert_source(self.conn, "qld", "peer", "Peer")
        store.insert_readings(self.conn, b, [
            {"observed_utc": f"2026-07-01T{h:02d}:00:00+00:00", "pm25": 450.0}
            for h in (0, 1, 2)])
        h = store.peer_ratio_history(self.conn, a, days=36500)
        self.assertEqual(2, h["n"],
                         "the extreme hour was dropped from corroboration")


class TestExistingRowsAreReassessed(StoreTestCase):
    """A database written before the split holds the old verdict.

    Every reading over 350 µg/m³ is sitting there marked 'suspect', including
    the ones that were the whole point of running the tool -- somebody's
    record of the night the smoke came through. Leaving them would mean the
    fix only applied to air that had not happened yet, and the years already
    logged would stay hidden from the chart they were collected for.

    Re-deriving is safe because the evidence is still there: pm25_a, pm25_b and
    confidence are columns, so assess_quality() can be asked again with exactly
    what it had the first time. Nothing is deleted and no concentration is
    touched -- only the verdict about it, which is what changed.
    """

    def legacy_rows(self):
        """Write rows the way the old code would have: everything high is
        'suspect', whatever the channels said."""
        sid = store.upsert_source(self.conn, "purpleair", "1", "Sensor")
        rows = [
            # high, channels agreeing -- was 'suspect', should become 'extreme'
            ("2026-07-01T00:00:00+00:00", 900.0, 890.0, 910.0, None),
            # high, channels disagreeing -- a genuine fault, stays 'suspect'
            ("2026-07-01T01:00:00+00:00", 900.0, 1700.0, 100.0, None),
            # high, no channel data at all -- becomes 'extreme'
            ("2026-07-01T02:00:00+00:00", 900.0, None, None, None),
            # ordinary, low confidence -- a fault, stays 'suspect'
            ("2026-07-01T03:00:00+00:00", 20.0, None, None, 10.0),
            # ordinary -- untouched
            ("2026-07-01T04:00:00+00:00", 12.0, 12.0, 12.0, 99.0),
        ]
        for when, pm, a, b, conf in rows:
            self.conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25, pm25_a, "
                "pm25_b, confidence, quality) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, when, pm, a, b, conf,
                 "suspect" if (pm > store.SUSPECT_PM25 or (conf or 100) < 50)
                 else "ok"))
        # And the version those rows were written under. setUp() opened the
        # database with the *current* code, which stamps the current version,
        # so without this the fixture is not a legacy database at all and the
        # migration is correctly skipped -- a test that would then pass
        # whether or not the migration worked.
        self.conn.execute(
            "UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        self.conn.commit()
        return sid

    def verdicts(self, conn=None):
        return {r["observed_utc"][11:13]: r["quality"] for r in
                (conn or self.conn).execute(
                    "SELECT observed_utc, quality FROM readings")}

    def test_the_old_verdicts_are_what_we_start_from(self):
        """A migration test that does not check its own premise is testing
        nothing -- if the fixture already looked migrated, it would pass
        against a migration that did not run."""
        self.legacy_rows()
        self.assertEqual({"00": "suspect", "01": "suspect", "02": "suspect",
                          "03": "suspect", "04": "ok"}, self.verdicts())

    def test_reopening_the_database_re_derives_them(self):
        self.legacy_rows()
        self.conn.commit()
        again = store.connect(self.db)
        try:
            self.assertEqual({"00": "extreme",   # agreeing channels: the air
                              "01": "suspect",   # disagreeing: the instrument
                              "02": "extreme",   # nothing says fault
                              "03": "suspect",   # low confidence
                              "04": "ok"}, self.verdicts(again))
        finally:
            again.close()

    def test_not_one_concentration_is_altered(self):
        """Rule 5. The verdict changed; the measurement is the record."""
        self.legacy_rows()
        before = [r["pm25"] for r in self.conn.execute(
            "SELECT pm25 FROM readings ORDER BY observed_utc")]
        again = store.connect(self.db)
        try:
            after = [r["pm25"] for r in again.execute(
                "SELECT pm25 FROM readings ORDER BY observed_utc")]
        finally:
            again.close()
        self.assertEqual(before, after)

    def test_no_row_is_lost(self):
        self.legacy_rows()
        again = store.connect(self.db)
        try:
            self.assertEqual(5, again.execute(
                "SELECT COUNT(*) AS n FROM readings").fetchone()["n"])
        finally:
            again.close()

    def test_it_does_not_run_a_second_time(self):
        """Guarded by the schema version, so opening the database on every
        poll does not rewrite every row someone has ever recorded."""
        self.legacy_rows()
        store.connect(self.db).close()
        again = store.connect(self.db)
        try:
            v = again.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            self.assertEqual(str(store.SCHEMA_VERSION), v)
            # Put a migrated row back to its old verdict. If the migration is
            # not guarded it will "correct" this again on the next open, so
            # the row surviving is the observable proof that it did not run.
            #
            # The first version of this test set the row to 'ok', which the
            # migration skips anyway -- it only looks at rows that are not
            # 'ok'. It passed with the guard removed. Setting it to 'suspect'
            # puts it squarely in the migration's path.
            again.execute("UPDATE readings SET quality = 'suspect' "
                          "WHERE observed_utc LIKE '%T00:%'")
            again.commit()
        finally:
            again.close()
        third = store.connect(self.db)
        try:
            self.assertEqual("suspect", self.verdicts(third)["00"],
                             "the migration ran a second time and overwrote "
                             "a verdict someone had set by hand")
        finally:
            third.close()

    def test_a_database_too_old_to_record_a_version_is_still_migrated(self):
        """A database predating the meta row reports no version at all.

        Reading that as "current" would skip the migration on the very oldest
        installs -- the ones with the most history to reclaim, and the least
        likely to be watched closely afterwards.
        """
        self.legacy_rows()
        self.conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        self.conn.commit()
        again = store.connect(self.db)
        try:
            self.assertEqual("extreme", self.verdicts(again)["00"])
        finally:
            again.close()


class TestTheReassessmentSaysWhatItDid(StoreTestCase):
    """Somebody with years of history is about to see readings appear on
    charts that never had them. That should be announced, not discovered."""

    def legacy_smoke(self, n):
        sid = store.upsert_source(self.conn, "purpleair", "1", "Sensor")
        for i in range(n):
            self.conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25, pm25_a, "
                "pm25_b, quality) VALUES (?, ?, ?, ?, ?, 'suspect')",
                (sid, f"2026-07-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
                 900.0, 890.0, 910.0))
        self.conn.execute(
            "UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        self.conn.commit()

    def reopen(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            store.connect(self.db).close()
        return buf.getvalue()

    def test_a_real_history_is_told_what_changed(self):
        self.legacy_smoke(150)
        said = self.reopen()
        self.assertIn("150", said)
        self.assertIn("extreme air", said)
        self.assertIn("chart", said, "it does not say where they will appear")

    def test_a_handful_of_rows_is_not_worth_announcing(self):
        """A test fixture creating three rows should not print at somebody."""
        self.legacy_smoke(3)
        self.assertEqual("", self.reopen())

    def test_the_rows_are_migrated_either_way(self):
        """The announcement is cosmetic; the migration is not. A threshold on
        the message must never become a threshold on the work."""
        self.legacy_smoke(3)
        self.reopen()
        again = store.connect(self.db)
        try:
            verdicts = {r["quality"] for r in
                        again.execute("SELECT quality FROM readings")}
        finally:
            again.close()
        self.assertEqual({"extreme"}, verdicts)


class TestAnUnreadableSchemaVersionIsTreatedAsOld(StoreTestCase):
    """Migrating twice is cheap and idempotent; skipping one is not.

    A version that cannot be read as a number means something went wrong, and
    the safe direction is to assume the database predates every migration --
    not to assume it is current and leave rows half-converted for good.
    """

    def test_a_nonsense_version_still_migrates(self):
        sid = store.upsert_source(self.conn, "purpleair", "1", "Sensor")
        self.conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25, pm25_a, "
            "pm25_b, quality) VALUES (?, '2026-07-01T00:00:00+00:00', 900.0, "
            "890.0, 910.0, 'suspect')", (sid,))
        for junk in ("", "four", None):
            with self.subTest(version=junk):
                self.conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (junk,))
                self.conn.execute(
                    "UPDATE readings SET quality = 'suspect'")
                self.conn.commit()
                again = store.connect(self.db)
                try:
                    got = again.execute(
                        "SELECT quality FROM readings").fetchone()["quality"]
                finally:
                    again.close()
                self.assertEqual("extreme", got)


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


class TestBackfilledTemperaturesAreCelsius(unittest.TestCase):
    """ROADMAP known issue C, reopened — the half that was never closed.

    `capture_reading()` normalises to Celsius on the way in and labels the row
    `C`. `backfill_source()` does neither: it copies `o["temperature"]`
    straight from the provider and sets **no unit at all**.

    That is worse than storing `F`, because the v3 migration repairs rows
    marked `F` on open and cannot see a row marked nothing. A Fahrenheit value
    from a PurpleAir backfill is therefore stored permanently in a column
    documented to mean Celsius, and no later migration can find it — the
    evidence that it is wrong was discarded at the moment it was written.

    It matters beyond tidiness: Phase B correlates minimum temperature against
    peak PM2.5, and Phase C's forecast is fitted to Phase B's bands. A run of
    68-for-20 among genuine Celsius rows does not look like an error, it looks
    like a warm night.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "airo.db"

    def test_every_provider_declares_the_unit_it_reports(self):
        """Enumerated from PROVIDERS, not from a list here. `store.py` decided
        this with `"F" if provider == "purpleair" else "C"`, which is the
        check-written-as-a-list shape that has bitten this project three
        times: a second Fahrenheit provider is silently wrong."""
        for slug, provider in poller.PROVIDERS.items():
            unit = getattr(provider, "temperature_unit", None)
            self.assertIn(unit, ("C", "F"),
                          f"{slug} does not declare the temperature unit it "
                          f"reports, so a backfill cannot normalise it")

    def test_the_declared_unit_matches_what_current_actually_sends(self):
        """The declaration and the live path must not be able to disagree.

        `current()` already labels its meta with a literal; this reads that
        literal back out of the source and asserts the class attribute says
        the same thing, so fixing one and forgetting the other is caught
        rather than becoming two facts about one provider.
        """
        import ast
        import inspect
        import textwrap
        checked = 0
        for slug, provider in poller.PROVIDERS.items():
            try:
                src = textwrap.dedent(inspect.getsource(type(provider).current))
            except (OSError, TypeError):
                continue
            sent = set()
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant)
                            and key.value == "temperature_unit"
                            and isinstance(value, ast.Constant)):
                        sent.add(value.value)
            if not sent:
                continue
            checked += 1
            self.assertEqual(
                {provider.temperature_unit}, sent,
                f"{slug}.current() labels its reading {sent} but the class "
                f"declares {provider.temperature_unit!r}")

        self.assertGreater(checked, 0,
                           "no provider's current() was read, so this "
                           "asserted nothing")

    def test_a_backfill_from_a_fahrenheit_provider_stores_celsius(self):

        class Hot(poller.Provider):
            slug = "hot"
            label = "Fahrenheit network"
            needs_key = False
            temperature_unit = "F"
            resolution_minutes = 60

            def history(self, src, key, start, end):
                return [{"utc": start, "pm25": 5.0, "temperature": 68.0}]

        conn = store.connect(self.path)
        sid = store.upsert_source(conn, "hot", "h1", "Hot site")
        poller.backfill_source(conn, sid, {"site_id": "h1"}, Hot(), days=1)
        row = conn.execute(
            "SELECT temperature, temperature_unit FROM readings").fetchone()
        conn.close()

        self.assertEqual("C", row["temperature_unit"],
                         "a backfilled row carries no unit, so nothing can "
                         "ever repair it")
        self.assertAlmostEqual(20.0, row["temperature"], places=1)

    def test_a_celsius_provider_is_not_converted_twice(self):

        class Cool(poller.Provider):
            slug = "cool"
            label = "Celsius network"
            needs_key = False
            resolution_minutes = 60

            def history(self, src, key, start, end):
                return [{"utc": start, "pm25": 5.0, "temperature": 20.0}]

        conn = store.connect(self.path)
        sid = store.upsert_source(conn, "cool", "c1", "Cool site")
        poller.backfill_source(conn, sid, {"site_id": "c1"}, Cool(), days=1)
        row = conn.execute(
            "SELECT temperature, temperature_unit FROM readings").fetchone()
        conn.close()
        self.assertEqual("C", row["temperature_unit"])
        self.assertAlmostEqual(20.0, row["temperature"], places=1)


class TestTheUnlabelledTemperatureMigration(unittest.TestCase):
    """v7. Repairs what the backfill writer already wrote.

    The writer is fixed, which stops it happening again and does nothing for
    anyone who has already run `--backfill` against a PurpleAir source. Their
    temperatures are Fahrenheit numbers in a Celsius column with no unit
    recorded — and because the unit is absent rather than wrong, the v3
    migration that repairs `F` rows cannot see them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "airo.db"

    def legacy(self, provider, temperature):
        """A row as the old backfill wrote it: a value, and no unit."""
        conn = store.connect(self.path)
        sid = store.upsert_source(conn, provider, "s1", "Site")
        conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25,"
            " temperature, temperature_unit, quality)"
            " VALUES (?, '2026-08-01T10:00:00+00:00', 5.0, ?, NULL, 'ok')",
            (sid, temperature))
        conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()

    def reopened(self):
        conn = store.connect(self.path)
        try:
            return dict(conn.execute(
                "SELECT temperature, temperature_unit FROM readings"
            ).fetchone())
        finally:
            conn.close()

    def test_a_fahrenheit_provider_s_rows_are_converted(self):
        self.legacy("purpleair", 68.0)
        row = self.reopened()
        self.assertEqual("C", row["temperature_unit"])
        self.assertAlmostEqual(20.0, row["temperature"], places=1)

    def test_a_celsius_provider_s_rows_are_only_labelled(self):
        """Converting these would invent an error. They were always Celsius;
        what was missing is the label saying so."""
        self.legacy("qld", 20.0)
        row = self.reopened()
        self.assertEqual("C", row["temperature_unit"])
        self.assertAlmostEqual(20.0, row["temperature"], places=1)

    def test_it_cannot_convert_the_same_row_twice(self):
        """The guard is that the fixed writer never leaves the unit NULL, so
        a repaired row is invisible to this on the next open. Asserted rather
        than reasoned about: a double conversion turns 20°C into -6.7°C, and
        nothing downstream would flag it as anything but a cold night."""
        self.legacy("purpleair", 68.0)
        first = self.reopened()
        for _ in range(3):
            self.reopened()
        self.assertEqual(first, self.reopened())
        self.assertAlmostEqual(20.0, self.reopened()["temperature"], places=1)

    def test_a_row_with_no_temperature_is_left_alone(self):
        """NULL is not zero. Labelling an absent reading would make it look
        like a measurement nobody took."""
        self.legacy("purpleair", None)
        conn = store.connect(self.path)
        try:
            row = conn.execute("SELECT temperature, temperature_unit "
                               "FROM readings").fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["temperature"])

    def test_a_retired_provider_is_labelled_but_never_converted(self):
        """Guessing a conversion for an adapter the registry no longer carries
        would turn a recoverable unknown into a confident wrong number."""
        self.legacy("some-retired-network", 20.0)
        row = self.reopened()
        self.assertEqual("C", row["temperature_unit"])
        self.assertAlmostEqual(20.0, row["temperature"], places=1)

    def test_no_reading_is_lost(self):
        self.legacy("purpleair", 68.0)
        conn = store.connect(self.path)
        try:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM readings").fetchone()[0])
        finally:
            conn.close()

    def test_a_row_already_labelled_is_not_converted_again(self):
        """A real database holds both: `capture_reading()` wrote 'C' rows all
        along while `backfill_source()` wrote unlabelled ones, so the repair
        has to touch one and not the other in the same table.

        The schema version stops this running twice, and that is the primary
        guard. This is the second one, and it is the one that matters when the
        two kinds sit side by side — a row converted twice turns 20°C into
        -6.7°C, which reads as a cold night rather than as an error.
        """
        conn = store.connect(self.path)
        sid = store.upsert_source(conn, "purpleair", "s1", "Site")
        conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25,"
            " temperature, temperature_unit, quality)"
            " VALUES (?, '2026-08-01T10:00:00+00:00', 5.0, 68.0, NULL, 'ok')",
            (sid,))
        conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25,"
            " temperature, temperature_unit, quality)"
            " VALUES (?, '2026-08-01T11:00:00+00:00', 5.0, 20.0, 'C', 'ok')",
            (sid,))
        conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()

        conn = store.connect(self.path)
        try:
            rows = {r["observed_utc"][11:13]: r["temperature"]
                    for r in conn.execute(
                        "SELECT observed_utc, temperature FROM readings")}
        finally:
            conn.close()
        self.assertAlmostEqual(20.0, rows["10"], places=1,
                               msg="the unlabelled row was not converted")
        self.assertAlmostEqual(20.0, rows["11"], places=1,
                               msg="a row that was already Celsius was "
                                   "converted a second time")


class TestTheCsvImporterAsksTheProvider(unittest.TestCase):
    """It decided the unit with `"F" if provider == "purpleair" else "C"`.

    That is right today and wrong the moment a second Fahrenheit network is
    added — the check-written-as-a-list shape this project has been bitten by
    three times. Behaviour is identical for the providers that exist, so
    reintroducing the hard-coded version turns nothing red unless a test
    supplies the network that does not exist yet.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_second_fahrenheit_network_is_imported_as_celsius(self):

        class AlsoHot(poller.Provider):
            slug = "alsohot"
            label = "Another Fahrenheit network"
            needs_key = False
            temperature_unit = "F"

        poller.PROVIDERS["alsohot"] = AlsoHot()
        self.addCleanup(lambda: poller.PROVIDERS.pop("alsohot", None))

        csv_path = self.dir / "old.csv"
        csv_path.write_text(
            "utc,pm25,temperature\n2026-08-01T10:00:00+00:00,5.0,68.0\n",
            encoding="utf-8")

        conn = store.connect(self.dir / "airo.db")
        try:
            store.migrate_from_csv(conn, csv_path, "alsohot", "s1", "Site")
            row = conn.execute("SELECT temperature, temperature_unit "
                               "FROM readings").fetchone()
        finally:
            conn.close()
        self.assertEqual("F", row["temperature_unit"],
                         "the importer decided the unit from a hard-coded "
                         "provider name, so a second Fahrenheit network was "
                         "imported as if it reported Celsius")


if __name__ == "__main__":
    unittest.main(verbosity=2)
