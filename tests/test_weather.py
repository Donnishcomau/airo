"""Weather capture — ROADMAP #9 Phase A.

Airo has always recorded what the air did and never why. These cover the
capture half: fetching hourly weather, storing it beside the readings, and
refusing anything that would make a later correlation wrong.

The unit check is the one that matters most. Phase B's finding is stated in
metres per second — calm is below 0.5 — and the API returns km/h unless asked
otherwise. A silent change there would not fail anything: it would move every
threshold by 3.6x and quietly invert the conclusion. That is the same shape as
the QLD API ignoring an unknown query parameter and returning the wrong window
with no error.

Nothing here touches the network. The fetch is stubbed, because a test suite
that depends on somebody else's service is one that fails on their bad day.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store     # noqa: E402
import weather   # noqa: E402

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



def payload(hours=3, start="2026-06-01T00:00", units=None, **override):
    """A response shaped like Open-Meteo's, with the units it really sends."""
    base = datetime.fromisoformat(start)
    times = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
             for i in range(hours)]
    hourly = {
        "time": times,
        "temperature_2m": [10.0 + i for i in range(hours)],
        "relative_humidity_2m": [70 + i for i in range(hours)],
        "surface_pressure": [1005.0 + i for i in range(hours)],
        "wind_speed_10m": [0.4 + i for i in range(hours)],
        "wind_direction_10m": [270 for _ in range(hours)],
    }
    hourly.update(override)
    return {"hourly_units": units or dict(weather.EXPECTED_UNITS),
            "hourly": hourly}


class TestUnitsAreNeverAssumed(unittest.TestCase):
    """The response's declared units are compared against what we asked for.

    Trusting either alone is the failure: the request names m/s and the
    response says what it actually sent, so only comparing them catches a
    default changing under us.
    """

    def test_a_good_response_parses(self):
        rows = weather._parse(payload())
        self.assertEqual(3, len(rows))
        self.assertEqual(0.4, rows[0]["wind_speed_ms"])

    def test_kilometres_per_hour_is_refused_outright(self):
        """Not converted — refused. A silent conversion is another place to
        get the factor wrong, and this should never happen in normal
        operation: it means the API stopped honouring the request."""
        units = dict(weather.EXPECTED_UNITS)
        units["wind_speed_10m"] = "km/h"
        with self.assertRaises(weather.WeatherUnavailable) as caught:
            weather._parse(payload(units=units))
        self.assertIn("km/h", str(caught.exception))
        self.assertIn("Phase B", str(caught.exception),
                      "the message does not say what breaks")

    def test_fahrenheit_is_refused_too(self):
        units = dict(weather.EXPECTED_UNITS)
        units["temperature_2m"] = "°F"
        with self.assertRaises(weather.WeatherUnavailable):
            weather._parse(payload(units=units))

    def test_a_field_that_is_absent_is_not_a_unit_error(self):
        """Only what came back is checked. A response missing a field it was
        not asked for must not look like a unit change."""
        units = {"temperature_2m": "°C"}
        weather._parse(payload(units=units))     # must not raise


class TestMissingHoursStayMissing(unittest.TestCase):
    """A gap and a calm hour must never look the same.

    Storing zero for an unmeasured hour would put false calm into exactly the
    correlation the project exists to test, and calm is the condition that
    matters.
    """

    def test_a_null_stays_none(self):
        rows = weather._parse(payload(wind_speed_10m=[None, 1.0, 2.0]))
        self.assertIsNone(rows[0]["wind_speed_ms"])
        self.assertEqual(1.0, rows[1]["wind_speed_ms"])

    def test_an_hour_with_nothing_at_all_is_dropped(self):
        empty = {k: [None] * 3 for k in
                 ("temperature_2m", "relative_humidity_2m", "surface_pressure",
                  "wind_speed_10m", "wind_direction_10m")}
        self.assertEqual([], weather._parse(payload(**empty)))

    def test_a_partly_measured_hour_is_kept(self):
        """Partial weather still constrains a correlation. Dropping it would
        discard a real wind reading because the barometer was out."""
        rows = weather._parse(payload(hours=1, surface_pressure=[None]))
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0]["pressure_hpa"])
        self.assertEqual(0.4, rows[0]["wind_speed_ms"])


class TestTimestampsAreExplicitlyUtc(unittest.TestCase):
    def test_a_naive_stamp_becomes_aware(self):
        """The API returns naive stamps and we asked for UTC. Saying so here
        means nothing downstream has to assume it."""
        rows = weather._parse(payload(hours=1))
        parsed = datetime.fromisoformat(rows[0]["observed_utc"])
        self.assertEqual(timezone.utc, parsed.tzinfo)


class TestTheWindowIsSplitByWhatCanAnswerIt(unittest.TestCase):
    """Two endpoints with different reach, and neither errors on a range it
    cannot serve — it returns an empty series, which reads as "no weather".
    """

    NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)

    def test_an_old_window_uses_the_archive_then_the_recent_endpoint(self):
        plan = weather.plan_backfill(self.NOW - timedelta(days=40), now=self.NOW)
        self.assertEqual(["archive", "recent"], [k for k, _, _ in plan])

    def test_a_window_inside_the_lag_never_asks_the_archive(self):
        """The archive lags real time. Asking it for yesterday returns
        nothing, and nothing is indistinguishable from calm."""
        plan = weather.plan_backfill(self.NOW - timedelta(days=2), now=self.NOW)
        self.assertEqual(["recent"], [k for k, _, _ in plan])

    def test_an_ancient_window_never_asks_the_recent_endpoint(self):
        plan = weather.plan_backfill(self.NOW - timedelta(days=400),
                                     self.NOW - timedelta(days=300),
                                     now=self.NOW)
        self.assertEqual(["archive"], [k for k, _, _ in plan])

    def test_a_backwards_window_asks_for_nothing(self):
        self.assertEqual([], weather.plan_backfill(
            self.NOW, self.NOW - timedelta(days=5), now=self.NOW))


class TestStoringWeather(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.place = store.place_key(-33.5, 151.0)
        self.rows = weather._parse(payload(hours=6))

    def test_it_stores_and_is_idempotent(self):
        """Backfill windows overlap by design, and a poll every fifteen
        minutes re-fetches the same hour four times."""
        self.assertEqual(6, store.insert_weather(self.conn, self.place, self.rows))
        self.assertEqual(0, store.insert_weather(self.conn, self.place, self.rows))

    def test_a_nearby_coordinate_lands_on_the_same_series(self):
        """Three decimals is about 100 m. A re-geocode that moves the
        configured location by metres must not silently start a second
        series that nothing ever joins against."""
        store.insert_weather(self.conn, self.place, self.rows)
        near = store.place_key(-33.5001, 151.0004)
        self.assertEqual(self.place, near)
        self.assertEqual(0, store.insert_weather(self.conn, near, self.rows))

    def test_a_reading_finds_the_hour_it_falls_in(self):
        """Readings arrive every ten minutes, weather every hour. The join is
        'which hour was this in', because an hourly model has no opinion about
        18:37 specifically."""
        store.insert_weather(self.conn, self.place, self.rows)
        got = store.weather_at(self.conn, self.place, "2026-06-01T03:37:00+00:00")
        self.assertIsNotNone(got)
        self.assertEqual("2026-06-01T03:00:00+00:00", got["observed_utc"])

    def test_an_hour_with_no_weather_returns_none_not_zero(self):
        store.insert_weather(self.conn, self.place, self.rows)
        self.assertIsNone(
            store.weather_at(self.conn, self.place, "2020-01-01T00:00:00+00:00"))

    def test_gaps_are_reported_so_only_the_missing_is_fetched(self):
        store.insert_weather(self.conn, self.place, self.rows[:3])
        gaps = store.weather_gaps(
            self.conn, self.place,
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(3, len(gaps))
        self.assertTrue(gaps[0].startswith("2026-06-01T03"))

    def test_the_span_says_what_is_held(self):
        store.insert_weather(self.conn, self.place, self.rows)
        span = store.weather_span(self.conn, self.place)
        self.assertEqual(6, span["hours"])
        self.assertLess(span["first"], span["last"])


class TestWeatherNeverCostsAReading(unittest.TestCase):
    """A weather service being down must not stop a poll.

    The asymmetry is deliberate: a missing reading is the product failing, a
    missing hour of wind is not. Rule 5 is about readings.
    """

    def setUp(self):
        import poller
        self.poller = poller
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.cfg = {"location": {"latitude": -33.5, "longitude": 151.0}}

    def _stub(self, fn):
        real = weather.recent
        weather.recent = fn
        self.addCleanup(lambda: setattr(weather, "recent", real))

    def test_an_unavailable_service_returns_zero_rather_than_raising(self):
        def boom(*a, **kw):
            raise weather.WeatherUnavailable("the service is down")
        self._stub(boom)
        self.assertEqual(0, self.poller.capture_weather(self.conn, self.cfg))

    def test_an_unexpected_error_is_also_contained(self):
        """Not only the exception the module defines. Anything raised inside a
        supplementary step must not reach the poll."""
        def boom(*a, **kw):
            raise ValueError("something else entirely")
        self._stub(boom)
        self.assertEqual(0, self.poller.capture_weather(self.conn, self.cfg))

    def test_no_location_is_not_an_error(self):
        self._stub(lambda *a, **kw: self.fail("must not fetch without a location"))
        self.assertEqual(0, self.poller.capture_weather(self.conn, {}))

    def test_it_can_be_switched_off(self):
        self._stub(lambda *a, **kw: self.fail("capture_weather is disabled"))
        cfg = dict(self.cfg, capture_weather=False)
        self.assertEqual(0, self.poller.capture_weather(self.conn, cfg))

    def test_a_good_fetch_is_stored(self):
        self._stub(lambda *a, **kw: weather._parse(payload(hours=4)))
        self.assertEqual(4, self.poller.capture_weather(self.conn, self.cfg))


class TestAttributionTravelsWithTheData(unittest.TestCase):
    """CC BY 4.0, the same obligation the government feeds carry (rule 4)."""

    def setUp(self):
        import poller
        self.poller = poller
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.cfg = {"location": {"latitude": -33.5, "longitude": 151.0}}

    def test_the_module_declares_an_attribution_and_a_licence(self):
        self.assertIn("Open-Meteo", weather.ATTRIBUTION)
        self.assertIn("CC BY", weather.LICENCE)

    def test_an_install_with_no_weather_claims_none(self):
        """Crediting a source an install has never used is the same error as
        the footer once crediting PurpleAir to a Queensland-only user."""
        self.assertIsNone(self.poller.weather_summary(self.conn, self.cfg))

    def test_once_weather_is_stored_the_attribution_appears(self):
        store.insert_weather(self.conn, store.place_key(-33.5, 151.0),
                             weather._parse(payload(hours=2)))
        summary = self.poller.weather_summary(self.conn, self.cfg)
        self.assertEqual(2, summary["hours"])
        self.assertIn("Open-Meteo", summary["attribution"])

    def test_a_missing_location_claims_nothing(self):
        self.assertIsNone(self.poller.weather_summary(self.conn, {}))

    def test_the_credit_carries_the_link_to_its_source(self):
        """`HOMEPAGE` sat unused beside the attribution it belongs to.

        Deleting it was the other option, and the wrong one: it is the URL a
        CC BY credit points at, and rule 4 puts attribution among the things
        this project does not quietly reduce. Serving it beside the credit
        costs one key and lets any surface render the two together. Nothing
        renders it as a link yet — that is a page change, and the pages are
        another work item's.
        """
        store.insert_weather(self.conn, store.place_key(-33.5, 151.0),
                             weather._parse(payload(hours=2)))
        summary = self.poller.weather_summary(self.conn, self.cfg)
        self.assertEqual(weather.HOMEPAGE, summary["homepage"])
        self.assertTrue(summary["homepage"].startswith("https://"),
                        "an attribution link must not be plain http")




# The synthetic frame (rule 2b). These assert plumbing, not geography.
HOME_LAT, HOME_LON = -33.5000, 151.0000


class TestFetchWindowJoinsTheTwoEndpoints(unittest.TestCase):
    """The archive and the forecast endpoint overlap at the seam.

    Both can answer for the same hour and they disagree: the archive is a
    reanalysis, the other a running model. Whichever wins has to be decided
    once and deliberately, or a correlation is built on a value that depends
    on when the backfill happened to run.
    """

    def setUp(self):
        self._history, self._recent = weather.history, weather.recent
        self.addCleanup(lambda: (setattr(weather, "history", self._history),
                                 setattr(weather, "recent", self._recent)))
        self.asked = []

    def rows(self, hours, marker):
        return [{"observed_utc": h, "temperature_c": marker} for h in hours]

    def hour(self, days_ago, h=0):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
            hour=h, minute=0, second=0, microsecond=0)
        return d.isoformat(timespec="seconds")

    def test_the_archive_wins_where_both_have_an_answer(self):
        seam = self.hour(8)
        weather.history = lambda *a, **kw: (
            self.asked.append("archive") or self.rows([seam], 1.0))
        weather.recent = lambda *a, **kw: (
            self.asked.append("recent") or self.rows([seam], 99.0))

        got = weather.fetch_window(
            HOME_LAT, HOME_LON,
            datetime.now(timezone.utc) - timedelta(days=30))

        self.assertEqual([1.0], [r["temperature_c"] for r in got],
                         "the running model overwrote the reanalysis")

    def test_hours_only_the_recent_endpoint_reaches_are_kept(self):
        old, fresh = self.hour(20), self.hour(1)
        weather.history = lambda *a, **kw: self.rows([old], 1.0)
        weather.recent = lambda *a, **kw: self.rows([fresh], 2.0)
        got = weather.fetch_window(
            HOME_LAT, HOME_LON,
            datetime.now(timezone.utc) - timedelta(days=30))
        self.assertEqual({old, fresh}, {r["observed_utc"] for r in got},
                         "the newest hours were dropped at the seam")

    def test_the_result_is_in_time_order(self):
        """Stored in order, read in ranges. Rows arriving out of order from
        two endpoints must not reach the store that way."""
        a, b, c = self.hour(20), self.hour(10), self.hour(1)
        weather.history = lambda *a_, **kw: self.rows([b, a], 1.0)
        weather.recent = lambda *a_, **kw: self.rows([c], 2.0)
        got = weather.fetch_window(
            HOME_LAT, HOME_LON,
            datetime.now(timezone.utc) - timedelta(days=30))
        stamps = [r["observed_utc"] for r in got]
        self.assertEqual(sorted(stamps), stamps)

    def test_a_short_window_never_asks_the_archive(self):
        """The archive lags real time by days, so asking it for yesterday
        returns nothing and looks exactly like a gap in the weather."""
        weather.history = lambda *a, **kw: (
            self.asked.append("archive") or [])
        weather.recent = lambda *a, **kw: (
            self.asked.append("recent") or self.rows([self.hour(1)], 1.0))
        weather.fetch_window(HOME_LAT, HOME_LON,
                             datetime.now(timezone.utc) - timedelta(days=2))
        self.assertNotIn("archive", self.asked)


class TestForwardHours(unittest.TestCase):
    """ROADMAP #9 Phase C needs the weather that has not happened yet.

    The same endpoint as `recent()`, pointed the other way — and the same unit
    check, because a forecast in km/h scored against history in m/s would be
    wrong by 3.6x and fail nowhere.
    """

    def setUp(self):
        self._get = weather._get
        self.asked = {}
        self.addCleanup(lambda: setattr(weather, "_get", self._get))

    def serve(self, hours):
        base = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0)
        times, temps, winds = [], [], []
        for offset in hours:
            times.append((base + timedelta(hours=offset)).strftime(
                "%Y-%m-%dT%H:%M"))
            temps.append(8.0)
            winds.append(0.4)

        def fake(url, params, timeout=30):
            self.asked.update(params)
            return {"hourly": {"time": times, "temperature_2m": temps,
                               "wind_speed_10m": winds},
                    "hourly_units": {"temperature_2m": "°C",
                                     "wind_speed_10m": "m/s"}}
        weather._get = fake

    def test_it_returns_only_hours_still_to_come(self):
        self.serve([-3, -2, -1, 0, 1, 2, 3])
        got = weather.forward(HOME_LAT, HOME_LON, hours=6)
        now = datetime.now(timezone.utc)
        for row in got:
            self.assertGreater(
                datetime.fromisoformat(row["observed_utc"]), now,
                "a past hour was returned as forecast")

    def test_it_returns_no_more_than_asked_for(self):
        self.serve(list(range(1, 20)))
        self.assertEqual(4, len(weather.forward(HOME_LAT, HOME_LON, hours=4)))

    def test_it_asks_for_no_past_days(self):
        """Fetching history here would pay for data the archive already holds
        and dilute what the caller asked for."""
        self.serve([1, 2])
        weather.forward(HOME_LAT, HOME_LON, hours=2)
        self.assertEqual(0, self.asked.get("past_days"))

    def test_it_asks_for_metres_per_second(self):
        self.serve([1])
        weather.forward(HOME_LAT, HOME_LON, hours=1)
        self.assertEqual("ms", self.asked.get("wind_speed_unit"))

    def test_a_response_in_the_wrong_units_is_refused(self):
        """The same trap as stored weather: the API returns km/h by default
        and honours m/s only when asked, so the declared units are checked
        rather than trusted."""
        def wrong(url, params, timeout=30):
            base = datetime.now(timezone.utc) + timedelta(hours=1)
            return {"hourly": {"time": [base.strftime("%Y-%m-%dT%H:%M")],
                               "wind_speed_10m": [1.4]},
                    "hourly_units": {"wind_speed_10m": "km/h"}}
        weather._get = wrong
        with self.assertRaises(weather.WeatherUnavailable):
            weather.forward(HOME_LAT, HOME_LON, hours=1)

    def test_asking_for_no_hours_returns_nothing(self):
        self.serve([1, 2, 3])
        self.assertEqual([], weather.forward(HOME_LAT, HOME_LON, hours=0))


if __name__ == "__main__":
    unittest.main()
