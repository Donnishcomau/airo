# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ROADMAP #9 Phase B — does the weather explain the readings?

The premise the whole project rests on: calm, cold nights trap particulates in
a valley. Phase A recorded the weather beside the readings. This is the part
that checks the premise rather than assuming it, and it has to be able to
answer *no*.

That is the design constraint throughout. A correlation is trivially easy to
compute and trivially easy to over-read: with eight samples you can get r =
-0.9 from noise, and a tool that prints it without saying so is inviting
somebody to believe something the data does not support. Every number here
carries its n, thin data is refused rather than reported, and nothing in the
output says one thing *causes* another.

It is also, deliberately, a statement about the past. Under Australian
Consumer Law s4 a representation about a future matter puts the burden of
reasonable grounds on whoever makes it. Describing what happened is not that;
`forecast.py` holds the guardrails for the part that would be.
"""

import io
import contextlib
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analyse  # noqa: E402
import poller   # noqa: E402
import store    # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)

HOME_LAT, HOME_LON = -33.5000, 151.0000


def setUpModule():
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


class CorrelateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "qld", "wbk", "Reference")
        self.place = store.place_key(HOME_LAT, HOME_LON)
        self.cfg = {"aqi_scale": "au",
                    "location": {"latitude": HOME_LAT, "longitude": HOME_LON,
                                 "timezone": "Australia/Brisbane"}}

    def hour(self, days_ago, h):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
            hour=h, minute=0, second=0, microsecond=0)
        return d.isoformat(timespec="seconds")

    def record(self, days_ago, h, pm25, wind=None, temp=None,
               humidity=None, direction=None):
        """One hour of air with the weather that went with it."""
        when = self.hour(days_ago, h)
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": pm25}])
        if wind is not None or temp is not None:
            store.insert_weather(self.conn, self.place, [{
                "observed_utc": when,
                "wind_speed_ms": wind,
                "temperature_c": temp,
                "humidity_pct": humidity,
                "wind_dir_deg": direction,
            }])

    def report(self, nights=90, units=None):
        """The report, in a unit this test chose rather than the one the
        machine running it happens to prefer.

        The band edges are shown in the reader's own wind unit, so an
        assertion on `0.5` means "metric" and nothing else. Left to the
        environment it passes on a CI runner with no locale set and fails on
        an Australian laptop — the exact shape of platform-dependent test
        this project has been caught by four times.
        """
        cfg = dict(self.cfg)
        cfg["units"] = units or "metric"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            analyse.correlate(self.conn, cfg, nights)
        return buf.getvalue()


class TestItRefusesToSpeakOnThinData(CorrelateCase):
    """The most important behaviour, and the easiest to skip.

    A correlation over eight hours is noise with a decimal point. Printing one
    invites somebody to act on it, and the person most likely to act on it is
    the one who just installed the tool and has eight hours of data.
    """

    def test_no_weather_at_all_says_what_to_run(self):
        for d in range(3):
            self.record(d, 20, 10.0)
        out = self.report()
        self.assertIn("no weather", out.lower())
        self.assertIn("--backfill-weather", out)

    def test_too_few_paired_hours_is_refused_not_reported(self):
        for h in range(5):
            self.record(1, h, 10.0, wind=0.2, temp=8.0)
        out = self.report()
        self.assertIn("not enough", out.lower())
        self.assertNotIn("r =", out,
                         "a correlation was printed from five hours of data")

    def test_the_threshold_is_stated_rather_than_silent(self):
        """Somebody with thin data needs to know how much more to collect.

        Asserted on the refusal line itself. The first version matched any
        digits-then-"hours" in the whole report, which the header line
        satisfies -- it passed with the refusal reduced to "Not enough paired
        hours." and no number at all.
        """
        self.record(1, 3, 10.0, wind=0.2, temp=8.0)
        line = [l for l in self.report().splitlines()
                if "not enough" in l.lower()]
        self.assertTrue(line, "it did not refuse at all")
        self.assertRegex(line[0], r"\d+\s+of\s+\d+",
                         f"the refusal does not say how many are needed: "
                         f"{line[0]!r}")


class TestTheWindBands(CorrelateCase):
    """The table that motivated the project: mean PM2.5 by wind speed."""

    def seed_the_pattern(self):
        """Calm hours dirty, breezy hours clean — the premise, made up."""
        n = 0
        for day in range(1, 15):
            for h in range(24):
                if h % 3 == 0:
                    wind, pm = 0.2, 12.0        # calm
                elif h % 3 == 1:
                    wind, pm = 0.8, 8.0         # light
                else:
                    wind, pm = 2.5, 4.0         # breezy
                self.record(day, h, pm, wind=wind, temp=10.0, humidity=70.0,
                            direction=270)
                n += 1
        return n

    def test_each_band_reports_its_mean_and_its_count(self):
        self.seed_the_pattern()
        out = self.report()
        self.assertIn("12.0", out)
        self.assertIn("8.0", out)
        self.assertIn("4.0", out)
        self.assertRegex(out, r"n\s*=?\s*\d+|\b\d+\s+hours",
                         "a band mean was printed without its sample count")

    def test_the_bands_are_the_ones_the_finding_used(self):
        """0.5 and 1.0 m/s. Changing them silently would make the tool's
        output incomparable with the analysis it exists to reproduce.

        Read from `analyse.WIND_BANDS` rather than typed here, so this cannot
        pass against edges that have quietly moved — the literals below are
        the claim, and the loop is what checks the code still makes it.
        """
        self.assertEqual([0.0, 0.5, 1.0], [low for low, _, _ in analyse.WIND_BANDS],
                         "the band edges the published finding used have moved")
        self.seed_the_pattern()
        out = self.report()
        for low, high, _ in analyse.WIND_BANDS:
            for edge in (low, high):
                if edge:
                    self.assertIn(f"{edge:.1f}", out,
                                  f"the {edge} m/s edge is not in the report")

    def test_the_edges_are_converted_for_a_reader_who_uses_mph(self):
        """The banding itself must not move — converting the *threshold*
        rather than the label would silently redefine "calm" for anyone
        outside a metric country, and every conclusion after it."""
        self.seed_the_pattern()
        out = self.report(units="us")
        self.assertIn("mph", out, "the heading still claims m/s")
        self.assertIn("1.1", out, "0.5 m/s was not converted to mph")
        self.assertNotIn("wind (m/s)", out)
        self.assertIn("calm", out)
        self.assertIn("breezy", out)

    def test_an_empty_band_is_shown_as_empty_rather_than_omitted(self):
        """A missing row reads as "no data for this band"; an omitted one
        reads as though the band does not exist."""
        for day in range(1, 15):
            for h in range(24):
                self.record(day, h, 5.0, wind=3.0, temp=10.0)
        out = self.report()
        self.assertIn("0.5", out, "the calm band vanished when it was empty")
        self.assertIn("calm", out)


class TestTheCorrelations(CorrelateCase):
    """Pearson r, with its n, and never a claim of cause."""

    def seed(self, pairs):
        """`pairs` is (wind, pm25) per night; one night per day back.

        Six hours a night rather than four, so fifteen nights clears
        MIN_PAIRED_HOURS. The first version seeded sixty and the report
        correctly refused to speak — which is the behaviour being protected
        two classes up, so the fixture moved rather than the threshold.
        """
        for day, (wind, pm) in enumerate(pairs, start=1):
            for h in (18, 19, 20, 21, 22, 23):
                self.record(day, h, pm, wind=wind, temp=20.0 - pm,
                            humidity=60.0 + pm, direction=270)

    def test_a_strong_negative_relationship_is_reported_as_one(self):
        """Wind up, particulates down — the shape the project expects."""
        self.seed([(0.1, 20.0), (0.3, 17.0), (0.6, 14.0), (0.9, 11.0),
                   (1.2, 9.0), (1.6, 7.0), (2.0, 5.0), (2.4, 4.0),
                   (2.8, 3.0), (3.2, 2.0), (0.2, 19.0), (1.0, 10.0),
                   (1.8, 6.0), (2.6, 3.5), (0.4, 16.0)])
        out = self.report()
        self.assertIn("r =", out)
        self.assertRegex(out, r"r\s*=\s*-0\.[89]",
                         f"expected a strong negative r:\n{out}")

    def test_every_correlation_carries_its_sample_count(self):
        self.seed([(0.1 * i, 20.0 - i) for i in range(1, 16)])
        out = self.report()
        for line in out.splitlines():
            if "r =" in line:
                self.assertRegex(line, r"n\s*=\s*\d+",
                                 f"a correlation with no n: {line!r}")

    def test_it_never_claims_one_thing_causes_another(self):
        """A correlation is not a cause, and this tool is read by people
        making decisions about going outside."""
        self.seed([(0.1 * i, 20.0 - i) for i in range(1, 16)])
        out = self.report().lower()
        for word in ("causes", "caused by", "because of the wind",
                     "proves", "will be"):
            self.assertNotIn(word, out, f"the report claims {word!r}")


class TestTheDirectionOfTheWorstHours(CorrelateCase):
    """A wind is named for the direction it comes from, and a direction claim
    is worse than saying nothing if the compass is wrong. A 0° wind is a
    northerly; this pins that naming down. The elevated hours here are spread
    across the day and given an arbitrary bearing, so the fixture asserts the
    compass naming without standing in for any real diurnal or wind pattern."""

    def test_a_northerly_is_named_north(self):
        for day in range(1, 15):
            for h in range(24):
                elevated = h % 6 == 0
                self.record(day, h, 30.0 if elevated else 4.0,
                            wind=0.2 if elevated else 2.0, temp=8.0,
                            direction=0 if elevated else 180)
        out = self.report()
        self.assertRegex(out, r"\bN\b|north",
                         f"a 0° wind was not described as northerly:\n{out}")

    def test_the_compass_covers_the_whole_circle(self):
        """0 and 360 are both north. A bucketing that drops either loses a
        whole direction, and that may be the one a sea breeze arrives on.

        The off-boundary bearings are the ones that discriminate: every
        multiple of 45 lands on the same name whether the sectors are centred
        on the compass points or start at them, so a version testing only
        those passed with the whole rose rotated by half a sector.
        """
        for deg, name in ((0, "N"), (360, "N"), (90, "E"), (180, "S"),
                          (270, "W"),
                          (10, "N"), (250, "W"), (300, "NW"), (350, "N"),
                          (200, "S"), (100, "E")):
            with self.subTest(deg=deg):
                self.assertEqual(name, analyse.compass(deg))

    def test_a_missing_direction_is_not_counted_as_north(self):
        """None must not become 0°. Inventing a direction for every hour a
        provider did not report one would manufacture the finding."""
        self.assertIsNone(analyse.compass(None))


class TestTheSummaryIsComputedNotAsserted(CorrelateCase):
    """The project's premise is "calm and cold, not calm and dry". That is a
    conclusion from data, so the tool must reach it from data — and must be
    able to reach the opposite."""

    def seed_calm_and_cold(self):
        for day in range(1, 21):
            for h in range(24):
                calm = h % 2 == 0
                self.record(day, h,
                            15.0 if calm else 4.0,
                            wind=0.2 if calm else 2.5,
                            temp=6.0 if calm else 18.0,
                            humidity=80.0 if calm else 50.0,
                            direction=270)

    def test_it_says_cold_when_the_data_says_cold(self):
        self.seed_calm_and_cold()
        out = self.report().lower()
        self.assertIn("cold", out)

    def test_humidity_is_reported_with_its_sign(self):
        """Humidity correlating *positively* is what distinguishes calm+cold
        from calm+dry, and it is the part most likely to be assumed.

        Asserted on the summary line, not on the correlations table above it.
        The first version matched "humidity ... r =" anywhere, which the table
        satisfies -- it passed with the summary's humidity sentence deleted.
        """
        self.seed_calm_and_cold()
        line = [l for l in self.report().splitlines()
                if l.strip().lower().startswith("humidity:")]
        self.assertTrue(line, "the summary does not mention humidity")
        self.assertRegex(line[0], r"r\s*=\s*[+-]\d")

    def seed_warm_and_windy(self):
        """The opposite record: worse air on warm, breezy hours.

        Invented, and deliberately so. A tool that can only confirm its own
        premise is not checking anything, and this is the case that proves the
        summary is computed rather than printed.
        """
        for day in range(1, 21):
            for h in range(24):
                windy = h % 2 == 0
                self.record(day, h,
                            15.0 if windy else 4.0,
                            wind=3.0 if windy else 0.2,
                            temp=28.0 if windy else 8.0,
                            humidity=30.0 if windy else 80.0,
                            direction=90)

    def test_it_does_not_say_cold_when_the_data_says_warm(self):
        self.seed_warm_and_windy()
        summary = [l for l in self.report().splitlines()
                   if "worse air came with" in l]
        self.assertTrue(summary, "no summary was printed")
        self.assertNotIn("cold", summary[0].lower(),
                         f"the premise was asserted rather than measured: "
                         f"{summary[0]!r}")
        self.assertIn("warm", summary[0].lower())

    def test_a_record_with_no_signature_says_so(self):
        """The third answer, and the honest one for most short records."""
        import random
        rnd = random.Random(11)
        for day in range(1, 21):
            for h in range(24):
                self.record(day, h, round(rnd.uniform(4, 12), 1),
                            wind=round(rnd.uniform(0.1, 3.0), 2),
                            temp=round(rnd.uniform(5, 25), 1),
                            humidity=round(rnd.uniform(35, 90), 1),
                            direction=rnd.choice((0, 90, 180, 270)))
        out = self.report().lower()
        self.assertIn("does not show a clear weather signature", out)


class TestABrokenInstrumentIsNotEvidence(CorrelateCase):
    """A blocked inlet reading 900 µg/m³ on a breezy afternoon would drag
    every correlation here toward nonsense. Faults are excluded; extreme air
    is not, because a night of genuine smoke is exactly the night this
    analysis is about."""

    def seed(self):
        for day in range(1, 21):
            for h in range(24):
                calm = h % 2 == 0
                self.record(day, h, 14.0 if calm else 4.0,
                            wind=0.2 if calm else 2.5, temp=8.0 if calm else 20.0,
                            humidity=80.0, direction=270)

    def stats(self, report):
        """The coefficients and the band means, and nothing else.

        Comparing whole reports was too strict: a fault written over a good
        reading legitimately removes that hour, so the paired-hour count
        changes and should. Filtering by line was too loose -- the header
        carries both a count and an em dash. What must not change is what the
        record *says*, so that is what is extracted.
        """
        import re
        rs = re.findall(r"r = ([+-]\d+\.\d+)", report)
        means = re.findall(r"^\s+(?:calm|light|breezy)\s+\S+\s+\d+\s+(\S+)",
                           report, re.M)
        return {"r": rs, "means": means}

    def test_a_suspect_reading_is_left_out(self):
        self.seed()
        clean = self.stats(self.report())
        # A fault: the two channels disagree, on a breezy afternoon. Left in,
        # 900 µg/m³ against 2.5 m/s would drag the wind correlation toward
        # zero and invent a dirty breezy band.
        store.insert_readings(self.conn, self.sid, [{
            "observed_utc": self.hour(2, 13), "pm25": 900.0,
            "pm25_a": 1700.0, "pm25_b": 100.0}])
        store.insert_weather(self.conn, self.place, [{
            "observed_utc": self.hour(2, 13), "wind_speed_ms": 2.5,
            "temperature_c": 20.0, "humidity_pct": 80.0}])
        self.assertEqual(clean, self.stats(self.report()),
                         "a blocked inlet moved the correlation")

    def test_extreme_air_is_kept(self):
        """The other half. Dropping the worst nights would remove the
        evidence for the premise being tested."""
        self.seed()
        before = self.report()
        store.insert_readings(self.conn, self.sid, [{
            "observed_utc": self.hour(2, 22), "pm25": 900.0,
            "pm25_a": 890.0, "pm25_b": 910.0}])
        store.insert_weather(self.conn, self.place, [{
            "observed_utc": self.hour(2, 22), "wind_speed_ms": 0.1,
            "temperature_c": 6.0, "humidity_pct": 85.0}])
        self.assertNotEqual(self.stats(before), self.stats(self.report()),
                            "a night of genuine smoke was discarded")


class TestTheCommandLine(CorrelateCase):
    def test_correlate_is_a_subcommand(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, str(ROOT / "analyse.py"), "--help"],
            capture_output=True, text=True, timeout=60,
            env=self.isolated_env())
        self.assertIn("correlate", r.stdout + r.stderr)

    def isolated_env(self):
        import os
        return dict(os.environ,
                    AIRO_DATA=str(Path(self.tmp.name) / "data"),
                    AIRO_CONFIG=str(Path(self.tmp.name) / "config.json"),
                    HOME=str(Path(self.tmp.name) / "home"))




class TestTheEdgesOfTheArithmetic(CorrelateCase):
    """The guards that keep a bad row from becoming a confident number."""

    def test_a_flat_series_has_no_correlation_rather_than_zero(self):
        """0.0 reads as "measured, and unrelated". None reads as "not
        measurable", which is the truth when nothing varies."""
        self.assertIsNone(analyse.pearson([1.0] * 10, list(range(10))))
        self.assertIsNone(analyse.pearson(list(range(10)), [5.0] * 10))

    def test_too_few_points_is_not_a_correlation(self):
        self.assertIsNone(analyse.pearson([1.0, 2.0], [2.0, 4.0]))

    def test_missing_values_are_dropped_in_pairs(self):
        """Dropping one side only would silently shift every later point
        against the wrong partner."""
        self.assertAlmostEqual(
            1.0, analyse.pearson([1.0, None, 3.0, 4.0],
                                 [2.0, 9.0, 6.0, 8.0]), places=6)

    def test_a_perfect_relationship_reads_as_one(self):
        self.assertAlmostEqual(1.0, analyse.pearson([1, 2, 3, 4], [2, 4, 6, 8]))
        self.assertAlmostEqual(-1.0, analyse.pearson([1, 2, 3, 4], [8, 6, 4, 2]))

    def test_a_bearing_that_is_not_a_number_is_refused(self):
        for junk in ("north", object(), [270]):
            with self.subTest(value=junk):
                self.assertIsNone(analyse.compass(junk))

    def test_a_bearing_past_the_circle_wraps(self):
        self.assertEqual("N", analyse.compass(720))
        self.assertEqual("E", analyse.compass(450))

    def test_with_no_location_it_says_so_rather_than_guessing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            analyse.correlate(self.conn, {"location": {}}, 90)
        self.assertIn("no location", buf.getvalue().lower())

    def test_the_join_can_be_narrowed_to_one_source(self):
        # Declared outdoor. This test is about narrowing by source_id, and
        # both sources are meant to be comparable outdoor monitors — a
        # PurpleAir whose placement nobody has established is 'unknown' and is
        # excluded from the weather join, which would make `both` one row for
        # a reason that has nothing to do with what is being tested.
        other = store.upsert_source(self.conn, "purpleair", "2", "Other",
                                    placement="outdoor")
        self.record(1, 10, 9.0, wind=0.3, temp=8.0)
        store.insert_readings(self.conn, other,
                              [{"observed_utc": self.hour(1, 10), "pm25": 20.0}])
        both = store.hourly_with_weather(self.conn, self.place)
        one = store.hourly_with_weather(self.conn, self.place,
                                        source_id=self.sid)
        self.assertEqual(2, len(both))
        self.assertEqual(1, len(one))
        self.assertEqual(self.sid, one[0]["source_id"])

    def test_the_join_can_exclude_extreme_air_when_asked(self):
        """The default keeps it. A caller wanting the ordinary range only has
        to say so, and then it is their statement rather than a hidden one."""
        self.record(1, 10, 9.0, wind=0.3, temp=8.0)
        store.insert_readings(self.conn, self.sid, [{
            "observed_utc": self.hour(1, 11), "pm25": 900.0,
            "pm25_a": 890.0, "pm25_b": 910.0}])
        store.insert_weather(self.conn, self.place, [{
            "observed_utc": self.hour(1, 11), "wind_speed_ms": 0.1,
            "temperature_c": 6.0}])
        self.assertEqual(2, len(store.hourly_with_weather(
            self.conn, self.place)))
        self.assertEqual(1, len(store.hourly_with_weather(
            self.conn, self.place, include_extreme=False)))


class TestWeatherWithoutOverlap(CorrelateCase):
    """Weather is held, readings are held, and none of the hours line up.

    A distinct case from "no weather at all", and a real one: somebody who
    backfilled weather for a window their readings do not cover, or moved
    `data_dir` and started a new database beside old weather. Telling them
    there is no weather would send them to re-fetch what they already have.
    """

    def test_it_says_no_hour_lines_up_rather_than_no_weather(self):
        # Weather two years ago; readings this week.
        old = (datetime.now(timezone.utc) - timedelta(days=700)).replace(
            minute=0, second=0, microsecond=0)
        store.insert_weather(self.conn, self.place, [{
            "observed_utc": old.isoformat(timespec="seconds"),
            "wind_speed_ms": 0.4, "temperature_c": 8.0}])
        for h in range(6):
            self.record(1, h, 10.0)

        out = self.report()
        self.assertIn("has both a reading", out,
                      f"it did not distinguish 'no overlap' from "
                      f"'no weather':\n{out}")
        self.assertIn("--backfill-weather", out)


class TestCorrelateThroughTheCommandLine(CorrelateCase):
    """main() routes it. The subcommand existing in --help does not mean the
    dispatch reaches it — that shape has cost this project four times."""

    def run_main(self, *argv):
        saved = sys.argv
        sys.argv = ["analyse.py", *argv]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                    unittest.mock.patch.object(poller, "load_config",
                                               lambda: self.cfg), \
                    unittest.mock.patch.object(
                        poller, "db_path",
                        lambda: Path(self.tmp.name) / "airo.db"):
                code = analyse.main()
        finally:
            sys.argv = saved
        return code, buf.getvalue()

    def test_correlate_runs_end_to_end(self):
        for day in range(1, 6):
            for h in range(24):
                self.record(day, h, 8.0, wind=0.5, temp=10.0, humidity=70.0,
                            direction=270)
        code, said = self.run_main("correlate")
        self.assertEqual(0, code)
        self.assertIn("Weather against particulates", said)

    def test_the_nights_argument_reaches_it(self):
        for h in range(24):
            self.record(80, h, 8.0, wind=0.5, temp=10.0)
        _, near = self.run_main("correlate", "--nights", "7")
        _, far = self.run_main("correlate", "--nights", "365")
        self.assertNotEqual(near, far,
                            "--nights made no difference to the report")


if __name__ == "__main__":
    unittest.main()
