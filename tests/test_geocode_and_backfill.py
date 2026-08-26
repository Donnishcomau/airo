"""Two paths that read somebody else's JSON, neither of them covered.

`geocode()` turns what a person typed into coordinates. It is the only place
the user's address leaves the machine, and it parses a third-party response
whose shape varies by country -- Nominatim calls a suburb `suburb` here,
`neighbourhood` or `city_district` elsewhere.

`backfill_weather()` decides how far back to fetch and which endpoint answers
for each span. Getting the window wrong is invisible: it stores *something*,
and the correlation it exists to feed is quietly built on less evidence than
it appears to have.

Both were carried on the strength of having been run once by hand.
"""

import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poller   # noqa: E402
import store    # noqa: E402
import weather  # noqa: E402

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


HOME_LAT, HOME_LON = -33.5000, 151.0000


def setUpModule():
    redirect_airo_paths_for_module()
    block_outbound_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


class GeocodeCase(unittest.TestCase):
    """Nominatim's answer, stubbed at the HTTP boundary."""

    def serve(self, payload):
        import io
        import json as _json

        class Response:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        body = _json.dumps(payload).encode("utf-8")
        self.requested = []

        def fake_urlopen(req, *a, **kw):
            self.requested.append(getattr(req, "full_url", str(req)))
            return Response(body)

        return unittest.mock.patch(
            "urllib.request.urlopen", fake_urlopen)

    def item(self, **over):
        d = {"lat": str(HOME_LAT), "lon": str(HOME_LON),
             "display_name": "Example St, Example Suburb, Example State",
             "address": {"suburb": "Example Suburb"}}
        d.update(over)
        return d


class TestGeocodeNormalises(GeocodeCase):

    def test_it_returns_coordinates_as_numbers(self):
        with self.serve([self.item()]):
            got = poller.geocode("example street")
        self.assertEqual(1, len(got))
        self.assertIsInstance(got[0]["latitude"], float)
        self.assertEqual(HOME_LAT, got[0]["latitude"])

    def test_the_full_label_is_kept(self):
        """"9109" matches a place in Norway, one in Tunisia and one in South
        Africa. The short name alone gives the user no way to tell which is
        theirs, so the whole label travels."""
        with self.serve([self.item()]):
            got = poller.geocode("9109")
        self.assertIn("Example State", got[0]["label"])

    def test_the_short_name_survives_a_country_that_names_things_differently(self):
        for key in ("suburb", "neighbourhood", "village", "town",
                    "city_district", "city", "municipality", "county"):
            with self.subTest(key=key):
                with self.serve([self.item(address={key: "Somewhere"})]):
                    got = poller.geocode("x")
                self.assertEqual("Somewhere", got[0]["name"],
                                 f"a {key} was not recognised as a place name")

    def test_with_no_recognised_key_it_falls_back_to_the_label(self):
        """Always something: an empty name in the picker gives the user a row
        they cannot tell apart from the next one."""
        with self.serve([self.item(address={"unheard_of": "x"})]):
            got = poller.geocode("x")
        self.assertEqual("Example St", got[0]["name"])

    def test_an_entry_with_no_coordinates_is_skipped_not_crashed_on(self):
        with self.serve([{"display_name": "no coords"}, self.item()]):
            got = poller.geocode("x")
        self.assertEqual(1, len(got))

    def test_an_out_of_range_coordinate_is_refused(self):
        """A latitude of 991 is not a place. Storing it would put the user
        somewhere no provider can answer for, and the failure would look like
        every network being down."""
        with self.serve([self.item(lat="991"), self.item(lon="-500")]):
            self.assertEqual([], poller.geocode("x"))

    def test_a_non_numeric_coordinate_is_refused(self):
        with self.serve([self.item(lat="north-ish")]):
            self.assertEqual([], poller.geocode("x"))

    def test_an_empty_query_never_leaves_the_machine(self):
        """The one thing this function must not do is send nothing useful to
        a third party on the user's behalf."""
        self.requested = []
        with self.serve([]):
            self.assertEqual([], poller.geocode("   "))
        self.assertEqual([], self.requested)

    def test_a_response_that_is_not_a_list_is_survived(self):
        with self.serve({"error": "rate limited"}):
            self.assertEqual([], poller.geocode("x"))

    def test_it_identifies_itself_as_the_policy_asks(self):
        """Nominatim's usage policy requires an identifying User-Agent. Being
        blocked for anonymous use would break address lookup for everybody."""
        seen = {}

        def fake_urlopen(req, *a, **kw):
            seen.update(dict(getattr(req, "headers", {})))
            raise OSError("stop here")

        with unittest.mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(Exception):
                poller.geocode("x")
        agent = " ".join(f"{k}: {v}" for k, v in seen.items()).lower()
        self.assertIn("airo", agent, f"no identifying User-Agent: {seen}")


class BackfillCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.logged = []
        self._log = poller.log
        poller.log = self.logged.append
        self.addCleanup(lambda: setattr(poller, "log", self._log))

        self.calls = []
        self._history, self._recent = weather.history, weather.recent
        weather.history = lambda lat, lon, s, e, **kw: (
            self.calls.append(("archive", s, e)) or self.rows(s, e))
        weather.recent = lambda lat, lon, past_days=2, **kw: (
            self.calls.append(("recent", past_days, None)) or
            self.rows(datetime.now(timezone.utc) - timedelta(days=past_days),
                      datetime.now(timezone.utc)))
        self.addCleanup(lambda: (setattr(weather, "history", self._history),
                                 setattr(weather, "recent", self._recent)))

    def rows(self, start, end):
        if isinstance(start, datetime):
            s = start
        else:
            s = datetime.combine(start, datetime.min.time(), timezone.utc)
        out, t = [], s.replace(minute=0, second=0, microsecond=0)
        stop = end if isinstance(end, datetime) else datetime.combine(
            end, datetime.min.time(), timezone.utc)
        while t <= stop and len(out) < 500:
            out.append({"observed_utc": t.isoformat(timespec="seconds"),
                        "temperature_c": 15.0, "wind_speed_ms": 1.0})
            t += timedelta(hours=1)
        return out

    def cfg(self, **over):
        c = {"location": {"latitude": HOME_LAT, "longitude": HOME_LON}}
        c.update(over)
        return c

    def seed_readings(self, days_back):
        sid = store.upsert_source(self.conn, "qld", "a", "Site")
        first = datetime.now(timezone.utc) - timedelta(days=days_back)
        store.insert_readings(self.conn, sid, [
            {"observed_utc": first.isoformat(timespec="seconds"), "pm25": 5.0},
            {"observed_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"), "pm25": 6.0}])


class TestBackfillReachesTheWholeRecord(BackfillCase):

    def test_with_no_location_it_says_so_and_asks_nothing(self):
        stored, first = poller.backfill_weather(self.conn, {"location": {}})
        self.assertEqual(0, stored)
        self.assertIsNone(first)
        self.assertEqual([], self.calls)
        self.assertTrue(any("no location" in m for m in self.logged))

    def test_with_no_readings_it_says_so_rather_than_guessing_a_window(self):
        """The default reaches back to the oldest reading. With none, any
        window would be arbitrary, and a year of weather against no PM2.5 is
        a year of wasted requests."""
        stored, _ = poller.backfill_weather(self.conn, self.cfg())
        self.assertEqual(0, stored)
        self.assertEqual([], self.calls)
        self.assertTrue(any("no readings" in m for m in self.logged))

    def test_by_default_it_reaches_back_to_the_oldest_reading(self):
        """A correlation needs history on both sides. A year of PM2.5 against
        a week of wind is a week of evidence."""
        self.seed_readings(days_back=40)
        stored, _ = poller.backfill_weather(self.conn, self.cfg())
        self.assertGreater(stored, 0)
        starts = [c[1] for c in self.calls if c[0] == "archive"]
        self.assertTrue(starts, f"nothing was fetched from the archive: {self.calls}")
        oldest = min(s if isinstance(s, datetime)
                     else datetime.combine(s, datetime.min.time(), timezone.utc)
                     for s in starts)
        age = (datetime.now(timezone.utc) - oldest.replace(
            tzinfo=oldest.tzinfo or timezone.utc)).days
        self.assertGreaterEqual(age, 35,
                                "the backfill stopped short of the readings")

    def test_an_explicit_window_is_honoured(self):
        """`--days 3` against 400 days of readings must fetch three days.

        Asserted by contrast with the default, because "it called the recent
        endpoint" is true either way: the default *also* fetches recent hours
        on its way through. The first version of this test passed with the
        `days` argument ignored entirely.
        """
        self.seed_readings(days_back=400)

        poller.backfill_weather(self.conn, self.cfg(), days=3)
        asked_for_three = list(self.calls)
        self.calls.clear()

        poller.backfill_weather(self.conn, self.cfg())
        asked_for_everything = list(self.calls)

        self.assertLess(len(asked_for_three), len(asked_for_everything),
                        "a three-day window cost as much as four hundred days")
        self.assertFalse(any(c[0] == "archive" for c in asked_for_three),
                         "three days is inside the archive's lag; asking it "
                         f"returns nothing and looks like a gap: {asked_for_three}")

    def test_a_long_window_uses_both_endpoints(self):
        """The archive lags real time by several days. Splitting is the whole
        reason plan_backfill exists, and using one endpoint for both spans
        loses either the oldest hours or the newest."""
        self.seed_readings(days_back=60)
        poller.backfill_weather(self.conn, self.cfg())
        kinds = {c[0] for c in self.calls}
        self.assertEqual({"archive", "recent"}, kinds,
                         f"one endpoint was asked to cover everything: {kinds}")

    def test_one_span_failing_does_not_abandon_the_others(self):
        """Reported rather than swallowed -- this was asked for explicitly, so
        silence would look like success -- but the rest still runs."""
        self.seed_readings(days_back=60)
        real = weather.history

        def fail(lat, lon, s, e, **kw):
            raise weather.WeatherUnavailable("archive is down")

        weather.history = fail
        try:
            stored, _ = poller.backfill_weather(self.conn, self.cfg())
        finally:
            weather.history = real
        self.assertTrue(any("WARN" in m and "weather failed" in m
                            for m in self.logged),
                        "a failed span was not reported")
        self.assertGreater(stored, 0,
                           "one failing span abandoned the whole backfill")

    def test_it_is_idempotent(self):
        """Polling every fifteen minutes against an hourly service must
        re-store nothing, or a year of backfill doubles every run."""
        self.seed_readings(days_back=20)
        first, _ = poller.backfill_weather(self.conn, self.cfg())
        second, _ = poller.backfill_weather(self.conn, self.cfg())
        self.assertGreater(first, 0)
        self.assertEqual(0, second, "a repeated backfill stored rows again")

    def test_it_reports_where_the_weather_now_starts(self):
        self.seed_readings(days_back=20)
        _, first = poller.backfill_weather(self.conn, self.cfg())
        self.assertTrue(first, "it did not say what the record now covers")




class TestTheNetworkBoundary(unittest.TestCase):
    """`http_get` and `http_post_json` are the only two functions in the
    project that speak to somebody else's server. Everything a provider does
    goes through them, and neither had a test."""

    def capture(self, body=b'{"ok": true}', status=200):
        self.seen = {}

        class Response:
            def __init__(self, data):
                self._d = data
            def read(self):
                return self._d
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            self.seen["url"] = req.full_url
            self.seen["headers"] = dict(req.headers)
            self.seen["timeout"] = timeout
            self.seen["data"] = req.data
            self.seen["method"] = req.get_method()
            return Response(body)

        return unittest.mock.patch("urllib.request.urlopen", fake_urlopen)

    def test_a_key_travels_in_a_header_never_in_the_url(self):
        """A query string is logged verbatim by every proxy and server in the
        path. A key in one is a key in somebody else's log file."""
        with self.capture():
            poller.http_get("https://example.invalid/v1/x", "secret-key")
        self.assertNotIn("secret-key", self.seen["url"])
        self.assertIn("secret-key", " ".join(self.seen["headers"].values()))

    def test_a_keyless_provider_sends_no_key_header_at_all(self):
        """Government open-data feeds take no key, and urllib rejects a None
        header value outright -- so sending an empty one is not harmless."""
        with self.capture():
            poller.http_get("https://example.invalid/v1/x", "")
        joined = " ".join(self.seen["headers"]).lower()
        self.assertNotIn("api-key", joined)

    def test_it_identifies_itself(self):
        with self.capture():
            poller.http_get("https://example.invalid/v1/x", "")
        self.assertTrue(any("airo" in v.lower()
                            for v in self.seen["headers"].values()),
                        f"no User-Agent: {self.seen['headers']}")

    def test_json_is_parsed_and_csv_is_not(self):
        with self.capture(body=b'{"n": 1}'):
            self.assertEqual({"n": 1},
                             poller.http_get("https://example.invalid/", ""))
        with self.capture(body=b"a,b\n1,2\n"):
            got = poller.http_get("https://example.invalid/", "", as_text=True)
        self.assertEqual("a,b\n1,2\n", got)

    def test_the_accept_header_matches_what_is_asked_for(self):
        with self.capture():
            poller.http_get("https://example.invalid/", "", as_text=True)
        self.assertIn("csv", " ".join(self.seen["headers"].values()).lower())

    def test_a_timeout_is_always_set(self):
        """Without one, a provider that accepts the connection and never
        answers hangs the poll forever -- and the scheduler starts another."""
        with self.capture():
            poller.http_get("https://example.invalid/", "")
        self.assertIsNotNone(self.seen["timeout"])
        self.assertGreater(self.seen["timeout"], 0)

    def test_undecodable_bytes_do_not_take_down_the_poll(self):
        """`errors="replace"`: one malformed byte from a provider must not
        cost every reading in that response."""
        with self.capture(body=b'{"n": "\xff\xfe"}'):
            got = poller.http_get("https://example.invalid/", "")
        self.assertIn("n", got)

    def test_post_sends_a_json_body_and_says_so(self):
        with self.capture():
            poller.http_post_json("https://example.invalid/q", {"site": 39})
        self.assertEqual("POST", self.seen["method"])
        self.assertIn(b"39", self.seen["data"])
        self.assertIn("json", " ".join(
            self.seen["headers"].values()).lower())

    def test_post_also_sets_a_timeout(self):
        with self.capture():
            poller.http_post_json("https://example.invalid/q", {})
        self.assertIsNotNone(self.seen["timeout"])


class TestTheWizardReadsTheShapeGeocodeReturns(unittest.TestCase):
    """The wizard and the settings page share one geocoder, and only one of
    them agreed with it.

    `geocode()` normalises Nominatim into `{name, label, latitude, longitude}`
    -- that normalising is the whole reason it exists, so the page and the
    wizard cannot disagree about what a place is called. The wizard then read
    `display_name`, `lat` and `lon`: Nominatim's *raw* keys, which the
    normaliser had already replaced. The candidate list rendered every match
    as `?`, and the line that turns the chosen match into coordinates raised
    `KeyError`, so typing a place name into the first-run wizard could not
    finish.

    Invisible to every test in the tree, because nothing drove the wizard's
    place-name path -- the coordinate path and the IP path were covered, and
    both build the location dict themselves.
    """

    def results(self):
        return [{"name": "Riverside",
                 "label": "Riverside, Example Province, Farland",
                 "latitude": HOME_LAT, "longitude": HOME_LON}]

    def run_wizard(self, answers, results=None):
        """Drive choose_location() with a stubbed geocoder and a script.

        `answers` is consumed in order by `ask`; a prompt that runs off the
        end of the script gets the default it offered, which is what pressing
        Enter does. `ask_yes` always declines, which skips the IP path and
        leaves the place-name path as the only one under test. Returns
        (location, everything the wizard printed).
        """
        import contextlib
        import io
        import setup

        rows = self.results() if results is None else results
        script = list(answers)
        patches = {
            "geocode": lambda place, limit=5: list(rows),
            "ask": lambda prompt, default=None: (
                script.pop(0) if script else default),
            "ask_yes": lambda prompt, default=True: False,
        }
        saved = {k: getattr(setup, k) for k in patches}
        for k, v in patches.items():
            setattr(setup, k, v)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                loc = setup.choose_location()
        finally:
            for k, v in saved.items():
                setattr(setup, k, v)
        return loc, buf.getvalue()

    def test_typing_a_place_name_produces_a_location(self):
        loc, _ = self.run_wizard(["example place", "1"])
        self.assertEqual(HOME_LAT, loc["latitude"])
        self.assertEqual(HOME_LON, loc["longitude"])
        self.assertEqual("search", loc["_lookup"])

    def test_the_offered_name_comes_from_the_normalised_keys(self):
        """The short name defaulted to the first comma-separated part of a key
        that is no longer there, so it fell back to the raw query."""
        loc, _ = self.run_wizard(["example place", "1"])
        self.assertEqual("Riverside", loc["name"])

    def test_the_candidate_list_shows_the_full_label(self):
        """A row reading `?` is the same bug one step earlier: the user is
        asked to pick between matches they cannot tell apart."""
        _, printed = self.run_wizard(["example place", "1"])
        self.assertIn("Riverside, Example Province, Farland", printed)
        self.assertNotIn("1. ?", printed)


if __name__ == "__main__":
    unittest.main()
