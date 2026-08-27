# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Provider parsing, against realistic payloads.

Nothing had ever stubbed `http_get`. The providers were checked for shape --
that they declare a slug, a licence, an attribution, a resolution -- and never
for whether they read a response correctly. That is where every trap in
ARCHITECTURE §3 and CONVENTIONS.md lives: PurpleAir nests its rolling averages
under `stats`, Queensland signals "offline" with -9999, NSW stamps hours 1..24
where 24 means midnight *ending* that date, and the QLD API silently ignores
unknown query parameters.

Found by mutation: fourteen guards inside the four providers could be removed
without a test noticing.

Coordinates here are a shifted synthetic frame, per CONVENTIONS.md rule 2b. What
matters is the relative geometry, never a real place.
"""

import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poller  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)


# A synthetic reference point. Nothing here describes where anyone lives.
HOME_LAT, HOME_LON = -33.5000, 151.0000


class UnexpectedRequest(BaseException):
    """Raised when a provider asks for a URL the test never queued.

    A BaseException, not an Exception, and that is the whole point. Provider
    code is deliberately forgiving about network failures -- six broad
    `except Exception` handlers wrap http_get, because one refused connection
    must not cost the poll. Those handlers caught this guard too, so a test
    could make an unasserted extra API call, have the harness object, have the
    objection swallowed and logged as a warning, and still pass.

    That is exactly what was happening: the PurpleAir history test asked for a
    window built from two separate now() calls, which is 2 days plus a few
    microseconds, so the 2-day chunking issued a second request of epsilon
    width. It went unanswered 495 times in 500 -- and the flake in poller.py's
    coverage was the other five, when both now() calls landed in the same
    microsecond and the second request never happened.

    `except Exception` cannot catch this. `except BaseException` can, and
    nothing here does one.
    """


class ProviderCase(unittest.TestCase):
    """Replace the one function that touches the network."""

    def setUp(self):
        self._http = poller.http_get
        self._log = poller.log
        self.logged = []
        poller.log = self.logged.append
        self.responses = []          # consumed in order
        self.requested = []

        def fake_http_get(url, key=None, **kw):
            self.requested.append(url)
            if not self.responses:
                raise UnexpectedRequest(f"unexpected request: {url}")
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        poller.http_get = fake_http_get
        # NSW posts a query document rather than issuing a GET. Stubbing only
        # http_get left its calls raising into a handler that logs and returns
        # [], so a test asserting "no usable rows" passed for the wrong reason
        # entirely -- there were no rows because the request never happened.
        self._post = poller.http_post_json
        poller.http_post_json = lambda url, payload=None, **kw: fake_http_get(url)

        # Queensland caches its station list on the class for the life of the
        # process, which in production is seconds because the poller runs and
        # exits. A test process lives for the whole suite, so without this the
        # first test to populate it decides whether every later test issues
        # the request at all -- and the answer changes with test order.
        self._stations = poller.PROVIDERS["qld"].__class__._STATIONS
        poller.PROVIDERS["qld"].__class__._STATIONS = None

        # Providers sleep between requests to be polite to a public API. That
        # is right in production and pure cost in a test: asserting the
        # chunking of a six-day window paid nine real seconds. Recorded
        # instead, so the politeness is still checkable and the suite does not
        # buy it. A test that quietly adds seconds gets the whole suite run
        # less often, which costs more than the test is worth.
        self._sleep = poller.time.sleep
        self.slept = []
        poller.time.sleep = self.slept.append

    def tearDown(self):
        poller.http_get = self._http
        poller.http_post_json = self._post
        poller.log = self._log
        poller.time.sleep = self._sleep
        poller.PROVIDERS["qld"].__class__._STATIONS = self._stations

    def serve(self, *payloads):
        self.responses = list(payloads)


class TestTheHarnessCanReportAnUnexpectedRequest(ProviderCase):
    """The guard guarding the guard.

    Every provider wraps its network calls in `except Exception`, correctly:
    one refused connection must not cost a poll. For as long as the harness
    objected with an AssertionError, those handlers caught the objection too,
    so a test could issue an unasserted API call, be told off, have the
    telling-off logged as a warning, and pass. Three tests were doing exactly
    that.

    Nothing else fails if the base class is changed back, because no test
    makes an unexpected request any more -- which is precisely why this exists.
    """

    def provider(self):
        return poller.PROVIDERS["qld"]

    def test_an_unqueued_request_is_not_swallowed_by_provider_error_handling(self):
        self.serve()   # nothing queued at all
        now = datetime.now(timezone.utc)
        with self.assertRaises(UnexpectedRequest):
            # history() ends in `except Exception: log(...); return []`.
            # An Exception-derived guard returns [] here and says nothing.
            self.provider().history({"site_id": "abc"}, None,
                                    now - timedelta(days=1), now)

    def test_it_names_the_url_nobody_queued(self):
        self.serve()
        with self.assertRaises(UnexpectedRequest) as caught:
            self.provider().current({"site_id": "abc"}, None)
        self.assertIn("http", str(caught.exception))


class TestPurpleAirReadsTheRollingAverage(ProviderCase):
    """ARCHITECTURE §3.1. `pm2.5_10minute` lives inside a `stats` object; read
    from the top level it silently returns None, and the tool quietly falls
    back to an instantaneous value while looking exactly the same."""

    def provider(self):
        return poller.PROVIDERS["purpleair"]

    def sensor(self, **over):
        s = {"sensor_index": 42, "name": "Synthetic sensor",
             "latitude": HOME_LAT, "longitude": HOME_LON,
             "pm2.5": 30.0,
             "stats": {"pm2.5": 30.0, "pm2.5_10minute": 12.0,
                       "pm2.5_60minute": 11.0, "pm2.5_24hour": 9.0},
             "last_seen": int(datetime.now(timezone.utc).timestamp())}
        s.update(over)
        return {"sensor": s}

    def test_the_ten_minute_average_is_found_inside_stats(self):
        self.serve(self.sensor())
        measures, meta = self.provider().current({"site_id": 42}, "k")
        self.assertEqual(12.0, measures["headline"],
                         "read the instantaneous value instead of the "
                         "10-minute average")

    def test_a_missing_average_falls_back_and_says_that_it_did(self):
        """The fallback is fine. The fallback happening silently is not — the
        flag is the only thing that makes it visible."""
        payload = self.sensor()
        payload["sensor"]["stats"] = {"pm2.5": 30.0}
        self.serve(payload)

        measures, meta = self.provider().current({"site_id": 42}, "k")

        self.assertEqual(30.0, measures["headline"])
        self.assertTrue(meta.get("headline_is_fallback"),
                        "fell back to the instantaneous value without saying so")

    def test_discovery_skips_a_sensor_with_no_position(self):
        """The default fusion rule ranks by distance. A sensor with no
        coordinates cannot be ranked, and silently sorts last."""
        self.serve({"fields": ["sensor_index", "name", "latitude", "longitude"],
                    "data": [[1, "Has position", HOME_LAT, HOME_LON],
                             [2, "No position", None, None]]})

        found = self.provider().discover(HOME_LAT, HOME_LON, 25, "k")

        self.assertEqual([1], [f["site_id"] for f in found])

    def test_history_skips_a_row_with_no_timestamp_or_no_value(self):
        now = int(datetime.now(timezone.utc).timestamp())
        self.serve("time_stamp,pm2.5_atm\n"
                   f"{now - 3600},10.0\n"
                   ",11.0\n"
                   f"{now - 1800},\n")
        # One now(), used for both ends. Two calls are microseconds apart, so
        # the window is two days *and a bit* -- and history() chunks at two
        # days, so the remainder became a second request of a few microseconds
        # that this test never queued a response for. It went unanswered 495
        # times in 500; the other five were when both now() calls landed in
        # the same microsecond. That was the whole of poller.py's coverage
        # flake, and the harness could not report it because the provider's
        # `except Exception` ate the objection. See UnexpectedRequest.
        anchor = datetime.now(timezone.utc)
        start, end = anchor - timedelta(days=1), anchor + timedelta(days=1)

        rows = self.provider().history({"site_id": 42}, "k", start, end)

        self.assertEqual(1, len(rows), f"kept unusable rows: {rows}")
        self.assertEqual(10.0, rows[0]["pm25"])
        self.assertEqual(1, len(self.requested),
                         f"a two-day window is one call: {self.requested}")

    def test_a_window_longer_than_one_chunk_is_split_and_no_further(self):
        """The chunking contract, asserted rather than assumed.

        PurpleAir limits how much history one call may return, so the window
        is cut into two-day pieces. Too few requests silently loses the tail
        of a backfill; too many is a wasted call against a rate limit, and the
        spurious one above was invisible for exactly that reason.
        """
        anchor = datetime.now(timezone.utc)
        for days, expected in ((2, 1), (4, 2), (5, 3), (6, 3)):
            with self.subTest(days=days):
                self.requested.clear()
                self.serve(*["time_stamp,pm2.5_atm\n"] * expected)
                self.slept.clear()
                self.provider().history({"site_id": 42}, "k",
                                        anchor, anchor + timedelta(days=days))
                self.assertEqual(expected, len(self.requested),
                                 f"{days} days should be {expected} call(s)")
                # Politeness to a rate-limited public API, asserted rather
                # than waited for: a backfill that hammers PurpleAir is how an
                # account gets suspended, and nothing else would notice.
                self.assertEqual(expected, len(self.slept),
                                 "a chunk was fetched without pausing")
                self.assertTrue(all(s >= 1 for s in self.slept), self.slept)


class TestQueenslandSentinelAndPaging(ProviderCase):
    """A station that is offline reports -9999. Stored as a concentration it
    became AQI −39,996, which falls below the first breakpoint and rendered as
    "Very good" — the most reassuring label there is, for air nobody
    measured."""

    def provider(self):
        return poller.PROVIDERS["qld"]

    def measurement(self, value, when="2026-07-31T10:00:00"):
        return {"date_measured": when, "mvalue": value,
                "mvalue_running_avg": value}

    def test_a_station_with_no_measurements_is_a_fault_not_a_blank(self):
        """RuntimeError is the provider's "no data" signal, and it is what
        makes probe_reporting() exclude a dead station from a suggestion."""
        self.serve([])
        with self.assertRaises(RuntimeError):
            self.provider().current({"site_id": "abc"}, None)

    def stations(self, station="abc"):
        """The station list current() fetches for coordinates.

        Queued explicitly because current() makes *two* requests, and until
        the harness could report an unanswered one these tests were queueing
        only the first. The station lookup failed every time, was swallowed by
        its own `except Exception`, and the source came back with no position
        -- which the comment above _station_position calls out as the thing
        that makes the nearest instrument get passed over.
        """
        return [{"station_id": station,
                 "latitude": HOME_LAT, "longitude": HOME_LON}]

    def test_the_newest_measurement_wins_regardless_of_feed_order(self):
        self.serve([self.measurement(5.0, "2026-07-31T09:00:00"),
                    self.measurement(9.0, "2026-07-31T11:00:00"),
                    self.measurement(7.0, "2026-07-31T10:00:00")],
                   self.stations())
        measures, _ = self.provider().current({"site_id": "abc"}, None)
        self.assertEqual(9.0, measures["headline"])

    def test_a_station_arrives_with_the_position_that_ranks_it(self):
        """The default fusion rule is "nearest". A source with no coordinates
        sorts behind every source that has them, so a silent failure here
        passes over the closest instrument in favour of a farther one."""
        self.serve([self.measurement(5.0)], self.stations())
        _, meta = self.provider().current({"site_id": "abc"}, None)
        self.assertEqual(HOME_LAT, meta["latitude"])
        self.assertEqual(HOME_LON, meta["longitude"])

    def test_a_station_list_that_cannot_be_fetched_costs_a_position_not_a_reading(self):
        """The failure is tolerated on purpose: a reading with no coordinates
        is worth far more than no reading. It must be tolerated *visibly*."""
        self.serve([self.measurement(5.0)], RuntimeError("stations are down"))
        measures, meta = self.provider().current({"site_id": "abc"}, None)
        self.assertEqual(5.0, measures["headline"])
        self.assertIsNone(meta["latitude"])

    def test_a_sentinel_never_survives_the_provider_boundary(self):
        """The first of three independent layers. Each is tested where it
        lives, so removing only one fails only its own test."""
        self.serve([self.measurement(-9999.0)], self.stations())
        measures, _ = self.provider().current({"site_id": "abc"}, None)
        cleaned, rejected = poller.clean_measures(measures)
        for channel, value in cleaned.items():
            self.assertFalse(value is not None and value < 0,
                             f"{channel} kept a sentinel: {value}")
        self.assertTrue(rejected,
                        "a sentinel was dropped without being reported — "
                        "rule 5a: flagged and shown, never hidden")

    def test_an_unparseable_date_is_dropped_rather_than_guessed(self):
        self.assertIsNone(self.provider()._iso("not a date"))
        self.assertIsNone(self.provider()._iso(None))
        self.assertIsNone(self.provider()._iso(""))

    def test_a_timestamp_with_an_offset_is_trusted_as_given(self):
        self.assertEqual("2026-07-31T10:00:00+00:00",
                         self.provider()._iso("2026-07-31T10:00:00Z"))

    def test_a_naive_timestamp_is_queensland_time_not_the_readers_time(self):
        """The feed publishes local time with no offset, and a naive datetime
        passed to astimezone() is interpreted in the *machine's* zone. That is
        right in Brisbane and wrong by the reader's offset anywhere else — the
        same reading landed at a different instant depending on who fetched
        it, which silently misplaces every evening comparison for a user
        outside Queensland.

        Asserting the absolute instant, so this fails on any machine where the
        old behaviour returns: 10:00 in Queensland is 00:00 UTC, wherever the
        test is run.
        """
        self.assertEqual("2026-07-31T00:00:00+00:00",
                         self.provider()._iso("2026-07-31T10:00:00"))

    def test_paging_stops_on_a_short_batch(self):
        """A full page means there may be more; a short one means there is
        not. Without that, the loop asks forever."""
        full = [self.measurement(5.0) for _ in range(2000)]
        self.serve(full, [self.measurement(6.0)])
        rows = self.provider()._measurements("abc", {})
        self.assertEqual(2001, len(rows))
        self.assertEqual(2, len(self.requested))

    def test_paging_stops_on_an_empty_batch(self):
        self.serve([])
        self.assertEqual([], self.provider()._measurements("abc", {}))

    def test_paging_stops_on_a_response_that_is_not_a_list(self):
        """The API answering with an error object rather than rows must end
        the loop, not be treated as a page of data."""
        self.serve({"error": "rate limited"})
        self.assertEqual([], self.provider()._measurements("abc", {}))

    def test_history_is_bounded_even_though_the_api_filters_by_day(self):
        """The QLD API filters by date, not instant, so a request for a
        partial day returns the whole of it — and it silently ignores query
        parameters it does not recognise, which is how from_date/to_date
        returned the most recent 1000 rows instead of the window asked for."""
        self.serve([self.measurement(5.0, "2026-07-30T09:00:00"),
                    self.measurement(6.0, "2026-07-31T20:00:00"),
                    self.measurement(7.0, "2026-08-01T21:00:00")])
        # Queensland time in, UTC out: 20:00 on the 31st is 10:00 UTC.
        start = datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)

        rows = self.provider().history({"site_id": "abc"}, None, start, end)

        self.assertEqual([6.0], [r["pm25"] for r in rows],
                         "readings outside the requested window were returned")

    def test_discovery_skips_a_station_with_no_position(self):
        self.serve([{"station_id": "a", "station_name": "Has position",
                     "latitude": HOME_LAT, "longitude": HOME_LON},
                    {"station_id": "b", "station_name": "No position",
                     "latitude": None, "longitude": None}])
        found = self.provider().discover(HOME_LAT, HOME_LON, 50, None)
        self.assertEqual(["a"], [f["site_id"] for f in found])


class TestNewSouthWalesHourTwentyFour(ProviderCase):
    """NSW stamps hours 1..24, where 24 means midnight *ending* that date —
    00:00 the next day. Using it as an hour value raises; clamping it to 23
    silently misplaces every midnight reading by an hour."""

    def provider(self):
        return poller.PROVIDERS["nsw"]

    def test_hour_twenty_four_is_midnight_the_following_day(self):
        at24 = self.provider()._observed_utc({"Date": "2026-07-31", "Hour": 24})
        at1 = self.provider()._observed_utc({"Date": "2026-08-01", "Hour": 1})
        self.assertIsNotNone(at24)
        self.assertEqual(timedelta(hours=1), at1 - at24,
                         "hour 24 was not read as midnight ending the date")

    def test_hours_are_converted_from_nsw_standard_time(self):
        at10 = self.provider()._observed_utc({"Date": "2026-07-31", "Hour": 10})
        self.assertEqual(datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc), at10)

    def test_an_unusable_date_is_none_rather_than_an_exception(self):
        for rec in ({"Date": "31/07/2026", "Hour": 3}, {"Date": None},
                    {"Hour": 5}):
            with self.subTest(rec=rec):
                self.assertIsNone(self.provider()._observed_utc(rec))

    def test_a_site_with_no_usable_observation_is_a_fault(self):
        self.serve([{"Value": None, "Date": "2026-07-31", "Hour": 3},
                    {"Value": 5.0, "Date": "rubbish", "Hour": 3}])
        with self.assertRaises(RuntimeError):
            self.provider().current({"site_id": 99}, None)

    def test_discovery_skips_a_site_with_no_position(self):
        self.serve([{"Site_Id": 1, "SiteName": "HAS POSITION",
                     "Latitude": HOME_LAT, "Longitude": HOME_LON},
                    {"Site_Id": 2, "SiteName": "NO POSITION",
                     "Latitude": None, "Longitude": None}])
        found = self.provider().discover(HOME_LAT, HOME_LON, 50, None)
        self.assertEqual([1], [f["site_id"] for f in found])


class TestOpenAqPicksThePmSensor(ProviderCase):
    """A location may host several instruments. A site id alone does not say
    what it measures, so an ozone sensor must never be offered as PM2.5."""

    def provider(self):
        return poller.PROVIDERS["openaq"]

    def test_a_sensor_with_no_results_is_a_fault(self):
        self.serve({"results": []})
        with self.assertRaises(RuntimeError):
            self.provider().current({"site_id": 7}, "k")

    def test_discovery_offers_only_pm25_instruments(self):
        self.serve({"results": [{
            "name": "Synthetic location",
            "coordinates": {"latitude": HOME_LAT, "longitude": HOME_LON},
            "sensors": [{"id": 1, "parameter": {"name": "o3"}},
                        {"id": 2, "parameter": {"name": "pm25"}},
                        {"id": 3, "parameter": {"name": "no2"}}],
        }]})

        found = self.provider().discover(HOME_LAT, HOME_LON, 25, "k")

        self.assertEqual([2], [f["site_id"] for f in found],
                         "offered an instrument that does not measure PM2.5")


class TestHistoryReturnsComparableTimestamps(ProviderCase):
    """Every history row's `utc` must be a timezone-aware datetime.

    Not documented anywhere and not enforced, which is how a provider
    returning an ISO string got as far as being compared against a window --
    "'<=' not supported between datetime and str", surfacing as "history
    failed" in the doctor rather than as the type error it is.
    """

    def test_queensland_history_rows_carry_datetimes(self):
        self.serve([{"date_measured": "2026-07-31T20:00:00", "mvalue": 5.0,
                     "mvalue_running_avg": 5.0}])
        rows = poller.PROVIDERS["qld"].history(
            {"site_id": "abc"}, None,
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertTrue(rows)
        for r in rows:
            self.assertIsInstance(r["utc"], datetime)
            self.assertIsNotNone(r["utc"].tzinfo, "a naive datetime cannot be "
                                                  "compared across sources")

    def test_new_south_wales_history_rows_carry_datetimes(self):
        self.serve([{"Value": 5.0, "Date": "2026-07-31", "Hour": 20}])
        rows = poller.PROVIDERS["nsw"].history(
            {"site_id": 1}, None,
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc))
        for r in rows:
            self.assertIsInstance(r["utc"], datetime)
            self.assertIsNotNone(r["utc"].tzinfo)


class TestHistoryDropsRowsItCannotUse(ProviderCase):
    """A history row missing its timestamp or its value cannot be stored: one
    has no place in the record, the other has nothing to put there. Dropped
    per row rather than failing the whole fetch, because one bad row in a
    backfill of thousands must not lose the other thousands."""

    def test_queensland_skips_rows_with_no_time_or_no_value(self):
        self.serve([{"date_measured": "2026-07-31T20:00:00", "mvalue": 5.0},
                    {"date_measured": "rubbish", "mvalue": 6.0},
                    {"date_measured": "2026-07-31T21:00:00", "mvalue": None}])
        rows = poller.PROVIDERS["qld"].history(
            {"site_id": "abc"}, None,
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual([5.0], [r["pm25"] for r in rows])

    def test_new_south_wales_skips_rows_with_no_time_or_no_value(self):
        self.serve([{"Value": 5.0, "Date": "2026-07-31", "Hour": 20},
                    {"Value": 6.0, "Date": "not-a-date", "Hour": 20},
                    {"Value": None, "Date": "2026-07-31", "Hour": 21}])
        rows = poller.PROVIDERS["nsw"].history(
            {"site_id": 1}, None,
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual([5.0], [r["pm25"] for r in rows])

    def test_openaq_skips_rows_with_no_time_or_no_value(self):
        self.serve({"results": [
            {"period": {"datetimeFrom": {"utc": "2026-07-31T20:00:00Z"}},
             "value": 5.0},
            {"period": {"datetimeFrom": {"utc": None}}, "value": 6.0},
            {"period": {"datetimeFrom": {"utc": "2026-07-31T21:00:00Z"}},
             "value": None},
        ]})
        rows = poller.PROVIDERS["openaq"].history(
            {"site_id": 7}, "k",
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual([5.0], [r["pm25"] for r in rows])




class TestOpenAqReadsALiveSensor(ProviderCase):
    """`current()` had one test, for the empty case. What it does with an
    answer went unchecked -- including the coordinates, which decide whether
    the nearest-source rule can rank this site at all."""

    def provider(self):
        return poller.PROVIDERS["openaq"]

    def sensor(self, **over):
        latest = {"value": 12.5,
                  "datetime": {"utc": "2026-07-31T09:00:00Z"},
                  "coordinates": {"latitude": HOME_LAT, "longitude": HOME_LON}}
        latest.update(over.pop("latest", {}))
        d = {"name": "Synthetic station", "latest": latest}
        d.update(over)
        return {"results": [d]}

    def test_the_reading_and_its_position_both_arrive(self):
        self.serve(self.sensor())
        measures, meta = self.provider().current({"site_id": 7}, "k")
        self.assertEqual(12.5, measures["headline"])
        self.assertEqual(HOME_LAT, meta["latitude"],
                         "a source with no position sorts behind every other")
        self.assertEqual("2026-07-31T09:00:00Z", meta["last_seen_utc"])

    def test_a_missing_position_is_none_rather_than_zero(self):
        """0,0 is the Gulf of Guinea. A source placed there is not unranked,
        it is ranked as extremely far away, which is a different bug."""
        self.serve(self.sensor(latest={"coordinates": {}}))
        _, meta = self.provider().current({"site_id": 7}, "k")
        self.assertIsNone(meta["latitude"])
        self.assertIsNone(meta["longitude"])

    def test_a_null_value_does_not_become_a_reading(self):
        self.serve(self.sensor(latest={"value": None}))
        measures, _ = self.provider().current({"site_id": 7}, "k")
        self.assertIsNone(measures["headline"])

    def test_the_sensor_id_may_be_named_either_way(self):
        """Setup writes `site_id`; a hand-edited config may say `sensor_id`.
        Both are the same thing and neither should 404."""
        for field in ("site_id", "sensor_id"):
            with self.subTest(field=field):
                self.serve(self.sensor())
                self.provider().current({field: 7}, "k")
                self.assertIn("/sensors/7", self.requested[-1])

    def test_history_asks_for_the_window_it_was_given(self):
        self.serve({"results": []})
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 3, tzinfo=timezone.utc)
        self.provider().history({"site_id": 7}, "k", start, end)
        url = self.requested[-1]
        self.assertIn("2026-07-01", url)
        self.assertIn("2026-07-03", url)

    def test_history_bounds_the_rows_it_keeps(self):
        """The API returns the hour bucket straddling the start boundary, so
        a request for exactly N days comes back with a reading fractionally
        outside it. Every provider must honour the same contract or gap
        detection reasons about a different window per source."""
        self.serve({"results": [
            {"period": {"datetimeFrom": {"utc": "2026-06-30T23:00:00Z"}},
             "value": 5.0},                                    # before
            {"period": {"datetimeFrom": {"utc": "2026-07-01T06:00:00Z"}},
             "value": 6.0},                                    # inside
            {"period": {"datetimeFrom": {"utc": "2026-07-09T00:00:00Z"}},
             "value": 7.0},                                    # after
        ]})
        rows = self.provider().history(
            {"site_id": 7}, "k",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 3, tzinfo=timezone.utc))
        self.assertEqual([6.0], [r["pm25"] for r in rows])

    def test_history_survives_a_row_it_cannot_read(self):
        self.serve({"results": [
            {"period": {}, "value": 5.0},
            {"period": {"datetimeFrom": {"utc": "not a date"}}, "value": 5.0},
            {"period": {"datetimeFrom": {"utc": "2026-07-02T00:00:00Z"}},
             "value": None},
            {"period": {"datetimeFrom": {"utc": "2026-07-02T01:00:00Z"}},
             "value": 8.0},
        ]})
        rows = self.provider().history(
            {"site_id": 7}, "k",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 3, tzinfo=timezone.utc))
        self.assertEqual([8.0], [r["pm25"] for r in rows])

    def test_a_failed_history_returns_nothing_rather_than_raising(self):
        """Backfill runs per source in a loop. One network being down must
        not stop the others being repaired."""
        self.serve(RuntimeError("openaq is down"))
        rows = self.provider().history(
            {"site_id": 7}, "k",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 3, tzinfo=timezone.utc))
        self.assertEqual([], rows)


class TestNewSouthWalesReadsItsFeed(ProviderCase):
    """NSW posts a query document rather than issuing a GET, which is the
    other common government-feed shape. Its parsing was covered only for the
    hour-24 quirk."""

    def provider(self):
        return poller.PROVIDERS["nsw"]

    def obs(self, value=11.0, date="2026-07-31", hour=9, **over):
        d = {"Value": value, "Date": date, "Hour": hour,
             "Parameter": {"ParameterCode": "PM2.5",
                           "Frequency": "Hourly average"}}
        d.update(over)
        return d

    def test_a_reading_arrives_with_its_time(self):
        self.serve([self.obs()])
        measures, meta = self.provider().current({"site_id": "39"}, None)
        self.assertEqual(11.0, measures["headline"])
        self.assertTrue(meta["last_seen_utc"],
                        "a reading with no time cannot be judged stale")

    def test_no_usable_observation_is_a_fault_not_a_blank(self):
        """RuntimeError is the provider's "no data" signal, and it is what
        keeps a dead site out of a suggestion."""
        self.serve([])
        with self.assertRaises(RuntimeError):
            self.provider().current({"site_id": "39"}, None)

    def test_the_newest_observation_wins(self):
        self.serve([self.obs(value=5.0, hour=7),
                    self.obs(value=9.0, hour=11),
                    self.obs(value=7.0, hour=9)])
        measures, _ = self.provider().current({"site_id": "39"}, None)
        self.assertEqual(9.0, measures["headline"])

    def test_history_bounds_its_window(self):
        self.serve([self.obs(value=5.0, date="2026-06-01", hour=9),
                    self.obs(value=6.0, date="2026-07-02", hour=9)])
        rows = self.provider().history(
            {"site_id": "39"}, None,
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 3, tzinfo=timezone.utc))
        self.assertEqual([6.0], [r["pm25"] for r in rows])

    def test_a_failed_history_returns_nothing_rather_than_raising(self):
        self.serve(RuntimeError("nsw is down"))
        rows = self.provider().history(
            {"site_id": "39"}, None,
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 3, tzinfo=timezone.utc))
        self.assertEqual([], rows)


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


class TestOpenAqStoresOnlyWhatItAskedFor(unittest.TestCase):
    """A sensor id is a user-editable field, and it does not say what it
    measures.

    `discover()` filters to `parameter.name == "pm25"`, so a source added
    through setup is right. `current()` then re-fetches by id and never asks
    again — and §3d put source editing behind a settings page, so an id can be
    typed, pasted or corrected by hand at any time afterwards.

    An id pointing at an NO2 or an ozone instrument would look entirely
    ordinary: plausible small numbers, no error, and straight into
    corroboration and Phase B's bands. Same shape as `weather._check_units`,
    which exists because storing km/h in a column named `_ms` fails nowhere.
    """

    def sensor(self, name="pm25", units="µg/m³", value=7.4):
        param = {}
        if name is not None:
            param["name"] = name
        if units is not None:
            param["units"] = units
        return {"results": [{
            "id": 42, "name": "Somewhere", "parameter": param,
            "latest": {"value": value, "datetime": {"utc": "2026-08-01T10:00:00Z"},
                       "coordinates": {"latitude": HOME_LAT,
                                       "longitude": HOME_LON}},
        }]}

    def read(self, payload):
        provider = poller.OpenAQProvider()
        saved = poller.http_get
        poller.http_get = lambda *a, **kw: payload
        self.addCleanup(lambda: setattr(poller, "http_get", saved))
        return provider.current({"site_id": 42}, "key")

    def test_a_pm25_sensor_is_read_normally(self):
        measures, _ = self.read(self.sensor())
        self.assertAlmostEqual(7.4, measures["headline"], places=2)

    def test_a_sensor_measuring_something_else_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            self.read(self.sensor(name="no2"))
        self.assertIn("no2", str(caught.exception))
        self.assertIn("Settings", str(caught.exception),
                      "the message does not say how to fix it")

    def test_a_sensor_reporting_another_unit_is_refused(self):
        """Rule 6: raw µg/m³ is canonical. A number in ppm stored as µg/m³ is
        not a small error — it is a different quantity."""
        with self.assertRaises(RuntimeError) as caught:
            self.read(self.sensor(units="ppm"))
        self.assertIn("ppm", str(caught.exception))

    def test_the_ascii_spelling_of_the_unit_is_accepted(self):
        """OpenAQ aggregates many networks and they do not agree on whether
        to use the micro sign. Refusing `ug/m3` would break working installs
        over a character."""
        measures, _ = self.read(self.sensor(units="ug/m3"))
        self.assertAlmostEqual(7.4, measures["headline"], places=2)

    def test_a_sensor_that_declares_nothing_is_accepted(self):
        """An absent field is missing metadata, not a contradiction. Refusing
        on silence would break working installs to guard a case nobody has
        seen — and rule 5's direction is to keep the reading."""
        measures, _ = self.read(self.sensor(name=None, units=None))
        self.assertAlmostEqual(7.4, measures["headline"], places=2)

    def test_a_backfill_refuses_the_wrong_instrument_too(self):
        """The live path and the history path reach the same store. Fixing one
        and not the other leaves the column wrong for everything older than
        today, which is the half that went unnoticed in known issue C."""
        provider = poller.OpenAQProvider()
        payload = {"results": [{
            "parameter": {"name": "o3", "units": "ppm"},
            "period": {"datetimeFrom": {"utc": "2026-08-01T10:00:00Z"}},
            "value": 0.03,
        }]}
        saved = poller.http_get
        poller.http_get = lambda *a, **kw: payload
        self.addCleanup(lambda: setattr(poller, "http_get", saved))
        with self.assertRaises(RuntimeError):
            provider.history({"site_id": 42}, "key",
                             datetime(2026, 8, 1, tzinfo=timezone.utc),
                             datetime(2026, 8, 2, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
