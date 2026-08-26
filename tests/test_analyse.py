"""analyse.py — the two reports, and the arithmetic under them.

This module had no tests at all, which for a *reporting* tool is worse than it
sounds: its whole job is to make a claim checkable, and nothing was checking
the claim-maker. Both commands print and return None, so every assertion here
is on what they print. For a CLI that is the product.

Three properties matter more than coverage:

  * A reading at 00:30 belongs to the *previous* evening. Bucketing on the
    calendar date splits every drainage episode in half and halves the effect
    the project exists to measure.
  * "A complete night" is counted in distinct *hours*, not samples. Six
    samples is one hour of PurpleAir and six hours of an hourly government
    feed; counting samples silently applies a different standard per provider.
  * The corroboration thresholds printed by `agreement` are read from
    fusion.py, not restated here. A second copy of a health-relevant number is
    free to drift, which is rule 7 in a different file.

Timestamps are built from local wall-clock hours and converted, because the
bucketing is local: seeding UTC hours directly would assert something
different in every timezone CI runs in. The one gap is the spring-forward
hour, which does not exist locally -- it lands at 02:00-03:00 in almost every
zone, and nothing here seeds those hours.
"""

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analyse  # noqa: E402
import fusion   # noqa: E402
import poller   # noqa: E402
import store    # noqa: E402

# No test may reach the internet: a call that a swallowing error handler hides
# passes for the wrong reason, and this suite mentions the poll path. See
# tests/netguard.py -- one suite run was making 25 real requests before this.
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



def local_utc(days_ago, hour, minute=0):
    """UTC ISO stamp for a local wall-clock time, `days_ago` days back."""
    day = (datetime.now().astimezone() - timedelta(days=days_ago)).date()
    naive = datetime(day.year, day.month, day.day, hour, minute)
    return naive.astimezone(timezone.utc).isoformat(timespec="seconds")


def night_of(days_ago):
    """The date label `evening` will print for that local day."""
    return str((datetime.now().astimezone() - timedelta(days=days_ago)).date())


class AnalyseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.cfg = {"aqi_scale": "au"}

    def fresh_db(self):
        """A second, independent database, for a test that needs more than
        one arrangement of the same sources."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "airo.db")
        self.addCleanup(conn.close)
        return conn

    def add_source(self, provider="qld", site_id="wbk", name="Westbrook",
                   conn=None):
        return store.upsert_source(conn or self.conn, provider, site_id, name)

    def seed(self, sid, stamps_and_values, conn=None):
        store.insert_readings(conn or self.conn, sid, [
            {"observed_utc": s, "pm25": v} for s, v in stamps_and_values])

    def complete_night(self, sid, days_ago, day_pm=10.0, eve_pm=10.0):
        """Three distinct day hours and three distinct evening hours."""
        self.seed(sid, [(local_utc(days_ago, h), day_pm) for h in (9, 10, 11)]
                  + [(local_utc(days_ago, h), eve_pm) for h in (18, 19, 20)])

    def evening_out(self, nights=30):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            analyse.evening(self.conn, self.cfg, nights)
        return buf.getvalue()

    def agreement_out(self, by_hour=False, conn=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            analyse.agreement(conn or self.conn, self.cfg, by_hour)
        return buf.getvalue()


class TestTheHelpersFailSoftOnBadData(AnalyseCase):
    """One unparseable row in a database of thousands must not end the report.

    These are three lines of defensive code that look like noise until the
    row that trips them is the only copy of last Tuesday.
    """

    def test_an_unparseable_timestamp_is_skipped_not_raised(self):
        for bad in ("", "not-a-date", "2026-13-45T99:00", None, 17):
            self.assertIsNone(analyse._local(bad), f"{bad!r} should give None")

    def test_a_good_timestamp_comes_back_in_local_time(self):
        stamp = local_utc(1, 18)
        self.assertEqual(analyse._local(stamp).hour, 18)

    def test_the_mean_of_nothing_is_none_not_a_division_error(self):
        self.assertIsNone(analyse._mean([]))
        self.assertEqual(analyse._mean([1.0, 2.0, 6.0]), 3.0)

    def test_a_percentile_of_nothing_is_none(self):
        self.assertIsNone(analyse._pct([], 0.5))

    def test_percentiles_stay_inside_the_list_at_both_ends(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        # q=0 and q=1 are the ends, not an IndexError; and out-of-range q
        # clamps rather than wrapping round to the wrong end of the list.
        self.assertEqual(analyse._pct(vals, 0.0), 1.0)
        self.assertEqual(analyse._pct(vals, 1.0), 4.0)
        self.assertEqual(analyse._pct(vals, -5), 1.0)
        self.assertEqual(analyse._pct(vals, 5), 4.0)

    def test_the_median_of_one_value_is_that_value(self):
        self.assertEqual(analyse._pct([7.5], 0.5), 7.5)


class TestNightsRunPastMidnight(AnalyseCase):
    """A reading at 00:30 belongs to the evening that just was.

    Cold-air drainage traps particulates after sunset and releases them after
    midnight, so the tail of an episode routinely lands on the next calendar
    date. Bucketing on that date reports two half-nights, each too thin to
    qualify, and the effect disappears into the gaps.
    """

    def test_a_reading_after_midnight_lands_on_the_night_before(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=2)
        # The spike arrives at 00:30 on the *following* calendar day. Kept
        # under SUSPECT_PM25: above that store.assess_quality marks a reading
        # as an instrument fault and series() leaves it out, so a bigger
        # number here would test the quality filter and not the bucketing.
        self.seed(sid, [(local_utc(1, 0, 30), 90.0)])

        out = self.evening_out()
        line = [ln for ln in out.splitlines() if night_of(2) in ln]
        self.assertEqual(len(line), 1, f"expected one row for that night:\n{out}")
        # The peak column is the highest evening reading of that night. If the
        # 00:30 spike had been filed under the next date it would read 10.0.
        self.assertIn("90.0", line[0])
        self.assertNotIn(night_of(1), out)

    def test_the_hours_between_one_and_three_pm_are_neither_bucket(self):
        # The evening window is 15:00-01:00. Early afternoon is daytime; the
        # boundary hours are the ones a fencepost error moves.
        sid = self.add_source()
        self.seed(sid, [(local_utc(2, h), 10.0) for h in (9, 10, 11)]
                  + [(local_utc(2, h), 40.0) for h in (15, 16, 17)])
        out = self.evening_out()
        self.assertIn("4.00x", out, f"15:00 should count as evening:\n{out}")


class TestACompleteNightIsCountedInHours(AnalyseCase):
    """Distinct hours, not samples -- the same rule the dashboard got wrong.

    A sample count means something different per provider. Requiring twelve
    samples in a ten-hour window is a requirement no hourly government feed can
    ever meet, so it reported nothing and looked like it had no data.
    """

    def test_many_samples_in_two_hours_is_not_a_complete_night(self):
        sid = self.add_source()
        # Ten evening samples, but only across hours 18 and 19.
        self.seed(sid, [(local_utc(2, 18, m), 40.0) for m in (0, 10, 20, 30, 40)]
                  + [(local_utc(2, 19, m), 40.0) for m in (0, 10, 20, 30, 40)]
                  + [(local_utc(2, h), 10.0) for h in (9, 10, 11)])
        out = self.evening_out()
        self.assertNotIn(night_of(2), out, f"two hours is not a night:\n{out}")
        self.assertIn("Not enough complete nights", out)

    def test_three_hourly_samples_each_side_is_a_complete_night(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=2, day_pm=10.0, eve_pm=20.0)
        out = self.evening_out()
        self.assertIn(night_of(2), out)
        self.assertIn("2.00x", out)

    def test_a_thin_daytime_disqualifies_the_night_too(self):
        sid = self.add_source()
        self.seed(sid, [(local_utc(2, h), 40.0) for h in (18, 19, 20)]
                  + [(local_utc(2, 9), 10.0)])
        out = self.evening_out()
        self.assertNotIn(night_of(2), out)

    def test_a_night_with_no_daytime_reading_cannot_produce_a_ratio(self):
        # Guards the division: a day mean of zero or None must skip the night
        # rather than raise or print an infinity.
        sid = self.add_source()
        self.seed(sid, [(local_utc(2, h), 40.0) for h in (18, 19, 20)]
                  + [(local_utc(2, h), 0.0) for h in (9, 10, 11)])
        out = self.evening_out()
        self.assertNotIn("inf", out.lower())
        self.assertIn("Not enough complete nights", out)


class TestSourcesAreNeverPooled(AnalyseCase):
    """Two instruments a few kilometres apart can have genuinely different
    evenings. Averaging them hides exactly the effect being looked for."""

    def test_each_source_gets_its_own_block_and_its_own_ratio(self):
        a = self.add_source("purpleair", "pa-1", "Backyard")
        b = self.add_source("qld", "wbk", "Westbrook")
        self.complete_night(a, days_ago=2, day_pm=10.0, eve_pm=40.0)   # 4.00x
        self.complete_night(b, days_ago=2, day_pm=10.0, eve_pm=10.0)   # 1.00x

        out = self.evening_out()
        self.assertIn("purpleair/pa-1", out)
        self.assertIn("qld/wbk", out)
        self.assertIn("4.00x", out)
        self.assertIn("1.00x", out)
        # A pooled report would show one block and the mean of the two.
        self.assertNotIn("2.50x", out)

    def test_a_trapping_night_is_called_out_and_a_quiet_one_is_not(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=3, day_pm=10.0, eve_pm=20.0)  # 2.00x
        self.complete_night(sid, days_ago=2, day_pm=10.0, eve_pm=11.0)  # 1.10x
        out = self.evening_out()
        flagged = [ln for ln in out.splitlines() if "trapping night" in ln]
        self.assertEqual(len(flagged), 1, out)
        self.assertIn(night_of(3), flagged[0])
        self.assertIn("1 of 2", out)


class TestEveningSaysWhyItHasNothingToSay(AnalyseCase):
    """Silence reads as breakage. Both empty cases name themselves."""

    def test_an_empty_database_says_so(self):
        self.add_source()
        self.assertIn("No readings in that window.", self.evening_out())

    def test_readings_outside_the_window_are_the_same_case(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=40)
        self.assertIn("No readings in that window.", self.evening_out(nights=7))

    def test_the_window_covers_the_night_it_was_asked_for(self):
        # nights=N must include the Nth night back, not N-1 of them.
        sid = self.add_source()
        self.complete_night(sid, days_ago=6)
        self.assertIn(night_of(6), self.evening_out(nights=7))

    def test_the_report_names_the_scale_it_used(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=2)
        _, scale = poller.get_scale(self.cfg)
        self.assertIn(scale["label"], self.evening_out())
        self.assertIn("raw ug/m3", self.evening_out())


class TestAgreementReportsTheThresholdsInForce(AnalyseCase):
    """The numbers printed are read from fusion.py, not restated.

    Restating them is how a report ends up describing thresholds the fuser
    stopped using -- someone tunes fusion.py and the tuning tool keeps
    recommending against the old value.
    """

    def two_sources_that_overlap(self, spike=False, conn=None):
        """Two sources reading the same hours, one at twice the other.

        With `spike`, one extra hour where it reads ten times its peer -- so
        max exceeds a threshold that p90 does not, which is the only way to
        reach the "occasionally exceeds" branch.
        """
        a = self.add_source("purpleair", "pa-1", "Backyard", conn=conn)
        b = self.add_source("qld", "wbk", "Westbrook", conn=conn)
        stamps = [local_utc(2, h) for h in (9, 10, 11, 18, 19, 20)]
        self.seed(a, [(s, 20.0) for s in stamps], conn=conn)
        self.seed(b, [(s, 10.0) for s in stamps], conn=conn)
        if spike:
            self.seed(a, [(local_utc(2, 21), 100.0)], conn=conn)
            self.seed(b, [(local_utc(2, 21), 10.0)], conn=conn)
        return a, b

    # Each threshold, the line that must carry it, and the shipped default
    # that must not survive a patch. Asserting the patched value is somewhere
    # in the output is not enough: the first version of this test passed with
    # "flag above" hardcoded to 3.0x, because the same constant is
    # interpolated into three other lines further down.
    THRESHOLD_LINES = [
        ("flag above", 7.25, 3.0),
        ("ignore below", 4.5, 12.0),
        ("historical tolerance", 9.75, 1.3),
        ("minimum samples", 42, 20),
    ]

    def test_every_printed_threshold_comes_from_fusion(self):
        self.two_sources_that_overlap()
        with unittest.mock.patch.object(fusion, "UNCORROBORATED_RATIO", 7.25), \
                unittest.mock.patch.object(fusion, "UNCORROBORATED_FLOOR_UGM3", 4.5), \
                unittest.mock.patch.object(fusion, "HISTORY_TOLERANCE", 9.75), \
                unittest.mock.patch.object(fusion, "MIN_HISTORY_SAMPLES", 42):
            out = self.agreement_out()

        for label, patched, shipped in self.THRESHOLD_LINES:
            line = [ln for ln in out.splitlines() if label in ln]
            self.assertEqual(len(line), 1, f"expected one {label!r} line:\n{out}")
            self.assertIn(str(patched), line[0])
            self.assertNotIn(str(shipped), line[0],
                             f"{label!r} restates the shipped default")

    def test_no_narrative_line_keeps_the_shipped_default_either(self):
        # Three branches sit below the table, each interpolating the same
        # constant, and only one prints per source. Checking the output as a
        # whole missed a literal in the branch the fixture never reached, so
        # every branch is driven here and each is checked on its own.
        branches = [
            (1.5, False, "cry wolf"),
            (7.25, True, "occasionally exceeds"),
            (50.0, False, "looser than it needs to be"),
        ]
        for ratio, spike, expected in branches:
            with self.subTest(branch=expected):
                conn = self.fresh_db()
                self.two_sources_that_overlap(spike=spike, conn=conn)
                with unittest.mock.patch.object(
                        fusion, "UNCORROBORATED_RATIO", ratio):
                    out = self.agreement_out(conn=conn)
                self.assertIn(expected, out)
                self.assertNotIn("3.0x", out)
                self.assertIn(f"{ratio}x", out)

    def test_it_reports_the_ratio_between_the_two_sources(self):
        self.two_sources_that_overlap()
        out = self.agreement_out()
        # The PurpleAir site reads twice its peer, consistently.
        pa = [ln for ln in out.splitlines() if ln.startswith("purpleair/pa-1")]
        self.assertEqual(len(pa), 1, out)
        self.assertIn("median=2.00x", pa[0])
        # And the government site reads half, symmetrically.
        qld = [ln for ln in out.splitlines() if ln.startswith("qld/wbk")]
        self.assertIn("median=0.50x", qld[0])

    def test_a_site_that_routinely_exceeds_the_threshold_is_not_called_faulty(self):
        self.two_sources_that_overlap()
        with unittest.mock.patch.object(fusion, "UNCORROBORATED_RATIO", 1.5):
            out = self.agreement_out()
        self.assertIn("cry wolf", out)
        self.assertIn("measuring", out)

    def test_a_site_that_never_exceeds_it_says_the_threshold_is_loose(self):
        self.two_sources_that_overlap()
        with unittest.mock.patch.object(fusion, "UNCORROBORATED_RATIO", 50.0):
            out = self.agreement_out()
        self.assertIn("looser than it needs to be", out)

    def test_sources_with_no_overlapping_hours_say_so_rather_than_nothing(self):
        a = self.add_source("purpleair", "pa-1", "Backyard")
        b = self.add_source("qld", "wbk", "Westbrook")
        self.seed(a, [(local_utc(3, h), 20.0) for h in (9, 10, 11)])
        self.seed(b, [(local_utc(2, h), 10.0) for h in (9, 10, 11)])
        out = self.agreement_out()
        self.assertEqual(out.count("no overlapping readings"), 2, out)


class TestAgreementWithOneSource(AnalyseCase):
    """Nothing to compare is a configuration answer, not an empty report."""

    def test_it_explains_what_to_add_and_prints_no_thresholds(self):
        self.add_source()
        out = self.agreement_out()
        self.assertIn("Only one source configured", out)
        self.assertIn("government monitor", out)
        self.assertNotIn("currently in force", out)

    def test_a_disabled_second_source_still_counts_as_two(self):
        # list_sources is called with enabled_only=False deliberately: history
        # from a source someone switched off is still the best cross-check
        # available, and is exactly what tuning wants to look at.
        self.add_source("purpleair", "pa-1", "Backyard")
        store.upsert_source(self.conn, "qld", "wbk", "Westbrook",
                            enabled=False)
        self.assertIn("currently in force", self.agreement_out())


class TestAgreementByHour(AnalyseCase):
    """The hour breakdown is labelled in local time, because that is what a
    user reasons about -- 'worse after dinner', not 'worse at 09:00 UTC'."""

    def test_the_hour_labels_are_local_not_utc(self):
        a = self.add_source("purpleair", "pa-1", "Backyard")
        b = self.add_source("qld", "wbk", "Westbrook")
        stamp = local_utc(2, 19)
        self.seed(a, [(stamp, 20.0)])
        self.seed(b, [(stamp, 10.0)])

        out = self.agreement_out(by_hour=True)
        self.assertIn("by hour of day (local)", out)
        self.assertIn("19:00", out)

        utc_hour = datetime.fromisoformat(stamp).astimezone(timezone.utc).hour
        if utc_hour == 19:
            # Where local time is UTC the two labels are the same string, so
            # the assertion above cannot tell a correct conversion from no
            # conversion at all. Said out loud rather than passing quietly:
            # CI runners are usually UTC, and this is the one machine where
            # this test proves nothing.
            self.skipTest("local time is UTC here; the labels are "
                          "indistinguishable, so this cannot fail")
        self.assertNotIn(f"{utc_hour:02d}:00  n=", out)

    def test_without_the_flag_there_is_no_hour_breakdown(self):
        a = self.add_source("purpleair", "pa-1", "Backyard")
        b = self.add_source("qld", "wbk", "Westbrook")
        stamp = local_utc(2, 19)
        self.seed(a, [(stamp, 20.0)])
        self.seed(b, [(stamp, 10.0)])
        self.assertNotIn("by hour of day", self.agreement_out(by_hour=False))


class TestTheCommandLine(AnalyseCase):
    """main() picks the database and the config the poller would, and says
    what to run when there is no database rather than raising a traceback."""

    def run_main(self, argv, db=None):
        db = db if db is not None else Path(self.tmp.name) / "airo.db"
        buf = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(poller, "load_config",
                                           lambda: self.cfg), \
                unittest.mock.patch.object(poller, "db_path", lambda: db), \
                contextlib.redirect_stdout(buf):
            code = analyse.main()
        return code, buf.getvalue()

    def test_no_database_names_the_command_that_makes_one(self):
        missing = Path(self.tmp.name) / "nowhere" / "airo.db"
        with self.assertRaises(SystemExit) as caught:
            self.run_main(["analyse.py", "evening"], db=missing)
        message = str(caught.exception)
        self.assertIn(str(missing), message)
        self.assertIn("poller.py --once", message)

    def test_evening_runs_end_to_end(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=2, day_pm=10.0, eve_pm=20.0)
        code, out = self.run_main(["analyse.py", "evening"])
        self.assertEqual(code, 0)
        self.assertIn("2.00x", out)

    def test_the_nights_argument_reaches_the_report(self):
        sid = self.add_source()
        self.complete_night(sid, days_ago=6)
        _, out = self.run_main(["analyse.py", "evening", "--nights", "2"])
        self.assertIn("No readings in that window.", out)
        _, out = self.run_main(["analyse.py", "evening", "--nights", "10"])
        self.assertIn(night_of(6), out)

    def test_agreement_runs_end_to_end(self):
        self.add_source()
        code, out = self.run_main(["analyse.py", "agreement"])
        self.assertEqual(code, 0)
        self.assertIn("Only one source configured", out)

    def test_the_by_hour_flag_reaches_the_report(self):
        a = self.add_source("purpleair", "pa-1", "Backyard")
        b = self.add_source("qld", "wbk", "Westbrook")
        stamp = local_utc(2, 19)
        self.seed(a, [(stamp, 20.0)])
        self.seed(b, [(stamp, 10.0)])
        _, out = self.run_main(["analyse.py", "agreement", "--by-hour"])
        self.assertIn("by hour of day (local)", out)

    def test_a_subcommand_is_required(self):
        # argparse exits 2 rather than defaulting to one of the two reports;
        # guessing which one someone meant is worse than asking.
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.run_main(["analyse.py"])
        self.assertEqual(caught.exception.code, 2)




class TestExtremeAirIsCountedAndSaidOutLoud(AnalyseCase):
    """The nights this tool exists to find were the nights it said least about.

    Anything over 350 µg/m³ used to be filed as a sensor fault and filtered out
    before `evening` ever saw it, so a night of genuine smoke arrived as a
    handful of ordinary readings with a hole where the peak had been -- or as
    no complete night at all, because the hours were gone. Counted now, and
    marked, because a 12x ratio should not read like an ordinary Tuesday.
    """

    def smoky_night(self, sid, days_ago):
        """Three ordinary daytime hours, three evening hours of thick smoke."""
        self.seed(sid, [(local_utc(days_ago, h), 10.0) for h in (9, 10, 11)])
        store.insert_readings(self.conn, sid, [
            {"observed_utc": local_utc(days_ago, h), "pm25": 900.0,
             "pm25_a": 890.0, "pm25_b": 910.0} for h in (18, 19, 20)])

    def test_the_night_of_the_fire_is_reported_at_all(self):
        sid = self.add_source("purpleair", "pa-1", "Backyard")
        self.smoky_night(sid, days_ago=2)
        out = self.evening_out()
        self.assertIn(night_of(2), out,
                      "the worst night on record produced no row")
        self.assertIn("90.00x", out)

    def test_the_row_says_the_readings_were_extreme(self):
        sid = self.add_source("purpleair", "pa-1", "Backyard")
        self.smoky_night(sid, days_ago=2)
        line = [ln for ln in self.evening_out().splitlines()
                if night_of(2) in ln][0]
        self.assertIn("3 extreme", line)

    def test_an_ordinary_night_is_not_marked(self):
        sid = self.add_source("purpleair", "pa-1", "Backyard")
        self.complete_night(sid, days_ago=2)
        line = [ln for ln in self.evening_out().splitlines()
                if night_of(2) in ln][0]
        self.assertNotIn("extreme", line)

    def test_a_broken_instrument_is_still_kept_out(self):
        """The other half. A blocked inlet reading 900 must not manufacture a
        trapping night -- that is the distinction the whole change rests on."""
        sid = self.add_source("purpleair", "pa-1", "Backyard")
        self.seed(sid, [(local_utc(2, h), 10.0) for h in (9, 10, 11)])
        store.insert_readings(self.conn, sid, [
            {"observed_utc": local_utc(2, h), "pm25": 900.0,
             "pm25_a": 1700.0, "pm25_b": 100.0} for h in (18, 19, 20)])
        out = self.evening_out()
        self.assertNotIn("90.00x", out)
        self.assertIn("Not enough complete nights", out)


if __name__ == "__main__":
    unittest.main()
