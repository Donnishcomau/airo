"""An indoor sensor never speaks for the air outside.

`nearest` is the default fusion rule and a sensor in your house is ~0 km away,
so without these exclusions the headline becomes a reading from a kitchen —
rendered with outdoor advice ("avoid outdoor exertion"), raising outdoor alerts
when somebody cooks, and marking the *real* outdoor sensors uncorroborated for
disagreeing with it.

Quieter and worse: Phase B correlates PM2.5 against outdoor wind and
temperature, and Phase C fits its forecast to Phase B's bands. Indoor air
against outdoor wind is not a weak signal, it is a meaningless one, and one
contaminated join reaches every claim this project makes about the future.

Five exclusions, and each test here is written so that removing the exclusion
turns it red. Nothing is discarded: an indoor reading is still stored, still
served and still shown — it is only stopped from meaning something it does not,
which is rule 5a's shape one level up.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyse  # noqa: E402
import forecast  # noqa: E402
import fusion  # noqa: E402
import poller  # noqa: E402
import store  # noqa: E402
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)
import notifyguard  # noqa: E402

HOME_LAT, HOME_LON = -33.5000, 151.0000


def setUpModule():
    block_outbound_for_module()
    notifyguard.block_notifications_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    notifyguard.restore_notifications_for_module()
    restore_outbound_for_module()


class IndoorCase(unittest.TestCase):
    """One outdoor sensor reading dirty air, one indoor sensor reading clean.

    The numbers are deliberately far apart. A test where indoor and outdoor
    agree cannot tell an exclusion from a coincidence, and this whole file is
    about which of the two a number came from.
    """

    OUTDOOR_PM = 30.0
    INDOOR_PM = 2.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.conn = store.connect(self.data / "airo.db")
        self.addCleanup(self.conn.close)

        self.place = store.place_key(HOME_LAT, HOME_LON)
        self.cfg = {
            "location": {"name": "Testville", "latitude": HOME_LAT,
                         "longitude": HOME_LON,
                         "timezone": "Australia/Brisbane"},
            "sources": [], "aqi_scale": "au",
            "fusion": {"rule": "nearest"},
        }

        # The indoor sensor is nearer, which is the whole problem: `nearest`
        # would pick it every time.
        self.indoor = store.upsert_source(
            self.conn, "purpleair", "indoor-1", "Kitchen",
            latitude=HOME_LAT, longitude=HOME_LON, placement="indoor")
        self.outdoor = store.upsert_source(
            self.conn, "qld", "out-1", "Regulatory station",
            latitude=HOME_LAT + 0.05, longitude=HOME_LON + 0.05,
            placement="outdoor")

    def hours(self, n=120):
        """`n` hours of both sensors, plus the weather that went with them.

        The series ends at the current hour, not `n` hours ago. Fusion treats
        a reading older than the provider's cadence as stale and declines to
        headline it, so a fixture that stops an hour short produces "no
        reading" and every assertion below fails for a reason that has nothing
        to do with placement.
        """
        now = datetime.now(timezone.utc)
        base = now.replace(minute=0, second=0, microsecond=0) - timedelta(
            hours=n - 1)
        for i in range(n):
            when = (base + timedelta(hours=i)).isoformat(timespec="seconds")
            store.insert_readings(self.conn, self.outdoor,
                                  [{"observed_utc": when,
                                    "pm25": self.OUTDOOR_PM}])
            store.insert_readings(self.conn, self.indoor,
                                  [{"observed_utc": when,
                                    "pm25": self.INDOOR_PM}])
            store.insert_weather(self.conn, self.place, [{
                "observed_utc": when, "wind_speed_ms": 0.2,
                "temperature_c": 8.0, "humidity_pct": 60.0,
                "wind_dir_deg": 270.0}])

        # And one reading at the actual current moment, for both sensors.
        #
        # The hourly series above is what the weather join needs, and it is
        # *not* enough for fusion: the newest row sits at the top of the hour,
        # so how old it looks depends on how far into the hour the suite runs.
        # These tests passed at five past and failed at five to, which is a
        # test whose behaviour depends on the wall clock — the exact shape
        # that made this project's coverage gate flaky once already.
        current = now.replace(microsecond=0).isoformat(timespec="seconds")
        store.insert_readings(self.conn, self.outdoor,
                              [{"observed_utc": current,
                                "pm25": self.OUTDOOR_PM}])
        store.insert_readings(self.conn, self.indoor,
                              [{"observed_utc": current,
                                "pm25": self.INDOOR_PM}])
        return base


class TestTheHeadlineIsOutdoorAir(IndoorCase):
    def test_the_nearest_rule_does_not_pick_the_indoor_sensor(self):
        """The headline claim. `nearest` sorts by distance and the indoor
        sensor is at the house — it wins on distance every time, and would put
        kitchen air under "avoid outdoor exertion"."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        self.assertEqual(self.OUTDOOR_PM, latest["pm25_10min"],
                         "the headline came from the indoor sensor")
        self.assertEqual("Regulatory station",
                         (latest.get("source") or {}).get("site_name"))

    def test_an_install_with_only_an_indoor_sensor_says_so(self):
        """It must not fall back to indoor air when there is nothing else.
        Silence with a reason is an answer; a kitchen reading labelled as the
        local air is not."""
        store.upsert_source(self.conn, "qld", "out-1", "Regulatory station",
                            enabled=False)
        self.conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?",
                          (self.outdoor,))
        self.conn.commit()
        self.hours(3)

        latest = poller.build_latest(self.conn, self.cfg)
        self.assertIsNone(latest["pm25_10min"],
                          "indoor air was reported as the outdoor headline")

    def test_the_indoor_reading_is_still_collected_and_served(self):
        """Rule 5a. Excluded from meaning something it does not, not thrown
        away — a reading nobody can see is a reading that was discarded."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        names = [s.get("site_name") for s in latest.get("sources") or []]
        self.assertIn("Kitchen", names,
                      "the indoor sensor vanished from the surfaces entirely")

    def test_every_served_source_says_where_it_is(self):
        """A surface cannot separate indoor from outdoor unless it is told,
        and a surface that has to infer it from the site name will get it
        wrong."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)
        for s in latest.get("sources") or []:
            self.assertIn(s.get("placement"), store.PLACEMENTS,
                          f"{s.get('site_name')} is served without a placement")


class TestCorroborationIgnoresIndoorAir(IndoorCase):
    def test_an_outdoor_sensor_is_not_accused_by_an_indoor_one(self):
        """The quiet one. `fusion` cross-checks peers, so an indoor sensor
        reading 2 against an outdoor 30 would mark the *outdoor* sensor as the
        liar — and the user would be told their real instrument is unreliable
        because their kitchen is clean."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        outdoor = [s for s in latest["sources"]
                   if s.get("site_name") == "Regulatory station"][0]
        self.assertNotEqual(
            "uncorroborated", outdoor.get("corroboration"),
            "the indoor sensor was used to discredit the outdoor one")

    def test_the_indoor_sensor_is_not_offered_to_fusion_at_all(self):
        """Checked at the seam rather than through its consequences: fuse()
        decides both the headline and corroboration, so what it is handed is
        the single fact both depend on."""
        self.hours(1)
        rows = store.latest_per_source(self.conn)
        offered = [r for r in rows if store.is_outdoor(r["placement"])]
        self.assertEqual([self.outdoor], [r["source_id"] for r in offered])


class TestTheWeatherCorrelationIsOutdoorOnly(IndoorCase):
    def test_phase_b_does_not_pair_indoor_air_with_outdoor_wind(self):
        self.hours(120)
        rows = store.hourly_with_weather(self.conn, self.place)
        sources = {r["source_id"] for r in rows}

        self.assertNotIn(self.indoor, sources,
                         "indoor hours were paired with outdoor weather")
        self.assertIn(self.outdoor, sources, "the join found nothing at all")

    def test_the_correlation_report_uses_only_outdoor_hours(self):
        """Through `analyse.correlate`, not just the join it calls — a helper
        can be right while its caller asks the wrong question."""
        import contextlib
        import io
        self.hours(120)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            analyse.correlate(self.conn, self.cfg, nights=90)
        printed = out.getvalue()

        self.assertNotIn("Kitchen", printed,
                         "the indoor sensor appears in the weather analysis")

    def test_the_comparison_can_still_ask_for_both(self):
        """The exclusion is a default, not a wall. The indoor/outdoor
        comparison needs exactly the rows this hides, and says so."""
        self.hours(24)
        both = store.hourly_with_weather(self.conn, self.place,
                                         outdoor_only=False)
        self.assertIn(self.indoor, {r["source_id"] for r in both})


class TestTheForecastIsNotFittedToIndoorAir(IndoorCase):
    def test_phase_c_wind_bands_exclude_the_indoor_sensor(self):
        """Phase C fits to Phase B's bands, so a contaminated band is a
        contaminated forecast. With indoor included the calm-band mean would
        be dragged from 30 toward 16 by air from inside a house."""
        self.hours(120)
        means = forecast.band_means(self.conn, self.cfg, nights=90)
        self.assertTrue(means, "no bands were produced at all")

        for band, (mean, hours) in means.items():
            self.assertAlmostEqual(
                self.OUTDOOR_PM, mean, delta=0.5,
                msg=f"the {band} band mean is {mean:.1f}, which is between "
                    f"the outdoor {self.OUTDOOR_PM} and the indoor "
                    f"{self.INDOOR_PM} — indoor hours are in the fit")


class TestAlertsAreAboutOutdoorAir(IndoorCase):
    def test_cooking_does_not_raise_an_outdoor_air_alert(self):
        """An indoor spike is a grill, not a smoke event. Alerting on it tells
        somebody the air outside is dangerous when it is not, which is the
        failure that costs trust fastest."""
        # `now`, not the top of the hour. Truncating makes the reading up to
        # 59 minutes old depending on when the suite runs, and fusion declines
        # to headline a stale one — so this passed at five past and failed at
        # five to.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        store.insert_readings(self.conn, self.outdoor, [{
            "observed_utc": now.isoformat(timespec="seconds"), "pm25": 4.0}])
        store.insert_readings(self.conn, self.indoor, [{
            "observed_utc": now.isoformat(timespec="seconds"), "pm25": 400.0}])

        cfg = dict(self.cfg, alerts={"enabled": True, "threshold_aqi": 67})
        latest = poller.build_latest(self.conn, cfg)
        fired = poller.maybe_alert(latest, cfg)

        self.assertIsNone(fired,
                          "a 400 µg/m³ indoor reading raised an outdoor alert")

    def test_a_real_outdoor_event_still_alerts(self):
        """The other half. An exclusion that also silences the real thing has
        not fixed anything."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        store.insert_readings(self.conn, self.outdoor, [{
            "observed_utc": now.isoformat(timespec="seconds"), "pm25": 400.0}])

        cfg = dict(self.cfg, alerts={"enabled": True, "threshold_aqi": 67})
        latest = poller.build_latest(self.conn, cfg)
        guard = notifyguard.current()
        before = len(guard.sent)

        self.assertIsNotNone(poller.maybe_alert(latest, cfg),
                             "a genuine outdoor event did not alert")

        # And the guard caught it, rather than the desktop.
        #
        # Without this the guard could stop intercepting entirely and nothing
        # here would notice: `maybe_alert` would return the same value while
        # delivering a real notification. That is not hypothetical — writing
        # this file sent "Air quality: Hazardous — AQI 1600" to the
        # maintainer's screen on an ordinary afternoon.
        self.assertGreater(
            len(guard.sent), before,
            "the alert was not intercepted — it went to a real desktop")
        self.assertIn("400", guard.messages[-1])


class TestWhereASensorIsComesFromTheApiNotTheUser(unittest.TestCase):
    def test_purpleairs_location_type_is_read(self):
        self.assertEqual("outdoor", poller.purpleair_placement(0))
        self.assertEqual("indoor", poller.purpleair_placement(1))

    def test_a_code_nobody_has_seen_is_unknown_not_a_guess(self):
        """PurpleAir may add a value. Mapping it to outdoor by default is how
        a new code becomes a wrong answer instead of an admitted gap."""
        self.assertEqual("unknown", poller.purpleair_placement(2))
        self.assertEqual("unknown", poller.purpleair_placement("nonsense"))

    def test_a_field_the_api_omitted_is_not_an_assertion(self):
        """None and 'unknown' differ where it matters: None leaves a stored
        answer alone, 'unknown' overwrites it. A missing field is not a claim
        that nobody knows where the sensor is."""
        self.assertIsNone(poller.purpleair_placement(None))

    def test_the_user_beats_the_api(self):
        """They can see the sensor. The API knows only how it was registered,
        and somebody who mounted an 'indoor' unit under the eaves has the
        better view."""
        self.assertEqual("indoor", poller.placement_for(
            {"placement": "indoor"}, {"placement": "outdoor"}))

    def test_the_api_beats_the_provider_default(self):
        self.assertEqual("indoor", poller.placement_for(
            {}, {"placement": "indoor"}, poller.PROVIDERS["purpleair"]))

    def test_a_regulatory_network_needs_no_detection(self):
        """A government agency does not site a compliance monitor in a
        kitchen. Without this a fresh install of a government feed registers
        as 'unknown', which is excluded from describing outdoor air — so the
        headline vanishes on an ordinary install."""
        for slug in ("qld", "nsw", "openaq"):
            self.assertEqual("outdoor", poller.placement_for(
                {}, {}, poller.PROVIDERS[slug]), slug)

    def test_purpleair_declines_to_guess(self):
        self.assertIsNone(poller.PROVIDERS["purpleair"].default_placement)

    def test_every_provider_declares_one_deliberately(self):
        """Enumerated from PROVIDERS. A network added later that says nothing
        would default to whatever the base class happens to hold, which is a
        decision worth making rather than inheriting."""
        for slug, provider in poller.PROVIDERS.items():
            value = provider.default_placement
            self.assertTrue(
                value is None or value in store.PLACEMENTS,
                f"{slug} declares {value!r}, which is not a placement")

    def test_the_request_actually_asks_for_it(self):
        """The call-site check. Mapping `location_type` is useless if the
        field is never requested — five helpers in this project have been
        fully tested while nothing called them."""
        self.assertIn("location_type", poller.PurpleAirProvider.FIELDS)


class TestPlacementSurvivesTheThingsThatGoWrong(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "airo.db"

    def test_a_poll_that_learns_nothing_does_not_forget(self):
        """A network failure must not downgrade a sensor somebody identified.
        The column is NOT NULL, so 'I have nothing to say' arrives as
        'unknown' — and a plain COALESCE would overwrite the real answer every
        time the API was unreachable."""
        conn = store.connect(self.path)
        store.upsert_source(conn, "purpleair", "1", "Kitchen",
                            placement="indoor")
        store.upsert_source(conn, "purpleair", "1", "Kitchen")
        self.assertEqual("indoor", conn.execute(
            "SELECT placement FROM sources").fetchone()[0])
        conn.close()

    def test_moving_a_sensor_outdoors_is_respected(self):
        """Forgetting takes a deliberate act; changing the answer does not."""
        conn = store.connect(self.path)
        store.upsert_source(conn, "purpleair", "1", "S", placement="indoor")
        store.upsert_source(conn, "purpleair", "1", "S", placement="outdoor")
        self.assertEqual("outdoor", conn.execute(
            "SELECT placement FROM sources").fetchone()[0])
        conn.close()

    def test_an_unrecognised_placement_lands_as_unknown(self):
        conn = store.connect(self.path)
        store.upsert_source(conn, "purpleair", "1", "S", placement="loft?")
        self.assertEqual("unknown", conn.execute(
            "SELECT placement FROM sources").fetchone()[0])
        conn.close()

    def test_unknown_is_not_outdoor(self):
        """The default direction, and the one that matters. Treating unknown
        as outdoor is precisely how an unidentified consumer sensor in a
        kitchen ends up as the headline."""
        self.assertFalse(store.is_outdoor("unknown"))
        self.assertFalse(store.is_outdoor(None))
        self.assertTrue(store.is_outdoor("outdoor"))

    def test_upgrading_an_old_database_keeps_it_working(self):
        """Every source that predates this could only have been added by
        discovery, which returns outdoor sensors only, or is a government
        monitor. Marking them 'unknown' would look cautious and would take the
        headline away from every existing install on upgrade."""
        conn = store.connect(self.path)
        store.upsert_source(conn, "qld", "fer", "Fernway")
        store.upsert_source(conn, "purpleair", "pa-far", "Backyard")
        conn.execute("ALTER TABLE sources RENAME TO old")
        conn.execute("""CREATE TABLE sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
            site_id TEXT NOT NULL, site_name TEXT, latitude REAL,
            longitude REAL, resolution_minutes INTEGER NOT NULL DEFAULT 10,
            enabled INTEGER NOT NULL DEFAULT 1, added_utc TEXT,
            UNIQUE(provider, site_id))""")
        conn.execute("""INSERT INTO sources (id, provider, site_id, site_name,
            latitude, longitude, resolution_minutes, enabled, added_utc)
            SELECT id, provider, site_id, site_name, latitude, longitude,
                   resolution_minutes, enabled, added_utc FROM old""")
        conn.execute("DROP TABLE old")
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()

        conn = store.connect(self.path)
        try:
            placements = {r[0]: r[1] for r in conn.execute(
                "SELECT provider, placement FROM sources")}
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual({"qld": "outdoor", "purpleair": "outdoor"}, placements)
        self.assertEqual(str(store.SCHEMA_VERSION), str(version))


class TestCorroborationAtTheSeam(IndoorCase):
    """Proved against `fuse()` directly, not only through the poller.

    The headline and corroboration are protected by the same filter — what
    `fuse()` is handed — so a fault removing it turns both red together and
    neither test can say which property it holds. This one asks fusion the
    question twice, with and without the indoor sensor, and shows the
    difference the exclusion actually makes.
    """

    def fused(self, rows):
        return fusion.fuse(rows, "nearest", self.cfg["location"], history={})

    def rows(self):
        self.hours(2)
        return store.latest_per_source(self.conn)

    def test_including_indoor_would_discredit_the_outdoor_sensor(self):
        """The damage, demonstrated. If this stops being true the exclusion
        has become decoration and this file would keep passing."""
        everything = self.rows()
        result = self.fused(everything)

        outdoor = [s for s in result["sources"]
                   if s.get("site_name") == "Regulatory station"][0]
        self.assertEqual(
            "uncorroborated", outdoor.get("corroboration"),
            "an indoor sensor reading 2 against an outdoor 30 no longer "
            "discredits it — the premise of this exclusion has changed")

    def test_excluding_it_leaves_the_outdoor_sensor_alone(self):
        rows = [r for r in self.rows() if store.is_outdoor(r["placement"])]
        result = self.fused(rows)

        outdoor = [s for s in result["sources"]
                   if s.get("site_name") == "Regulatory station"][0]
        self.assertNotEqual("uncorroborated", outdoor.get("corroboration"))


class TestAddingASensorYouOwn(unittest.TestCase):
    """By sensor id, including a private one.

    `discover()` answers "what is near me" and sends `location_type: 0`, so it
    returns outdoor sensors only — and a private sensor is absent from those
    results whatever it is set to. Anything somebody owns has to be added by
    id, and the id is the only thing they have.
    """

    class Fake(poller.Provider):
        slug = "fake"
        label = "Fake network"
        needs_key = False
        resolution_minutes = 10
        default_placement = None
        answer = None
        raises = None

        def current(self, src, key):
            if self.raises:
                raise self.raises
            return self.answer

    def setUp(self):
        self.provider = self.Fake()
        poller.PROVIDERS["fake"] = self.provider
        self.addCleanup(lambda: poller.PROVIDERS.pop("fake", None))

    def answering(self, placement=None, pm25=7.4, name="Kitchen"):
        meta = {"site_id": "42", "site_name": name,
                "latitude": HOME_LAT, "longitude": HOME_LON}
        if placement is not None:
            meta["placement"] = placement
        self.provider.answer = ({"headline": pm25, "now": pm25}, meta)

    def test_a_sensor_that_answers_is_described_before_it_is_added(self):
        """Verified first, the same discipline discovery already follows. The
        nearest station published nothing and was once chosen on distance
        alone, and the first poll reported that every source had failed —
        a mistyped id produces exactly that silence a day later."""
        self.answering(placement="indoor")
        found = poller.probe_source("fake", "42")

        self.assertTrue(found["ok"], found)
        self.assertEqual("Kitchen", found["site_name"])
        self.assertEqual("indoor", found["placement"])
        self.assertAlmostEqual(7.4, found["pm25"])

    def test_it_says_where_the_sensor_is_in_words(self):
        """The user is about to decide whether to add it, and placement
        decides what it will be allowed to mean. A code is not an answer."""
        self.answering(placement="indoor")
        note = poller.probe_source("fake", "42")["placement_note"]
        self.assertIn("indoor", note.lower())
        self.assertIn("headline", note.lower())

    def test_an_unidentifiable_sensor_is_treated_as_indoor_would_be(self):
        """Not as outdoor. The whole failure this prevents is an unidentified
        consumer sensor in a kitchen becoming the headline."""
        self.answering(placement=None)
        found = poller.probe_source("fake", "42")
        self.assertEqual("unknown", found["placement"])
        self.assertFalse(store.is_outdoor(found["placement"]))
        self.assertIn("kept out of the outdoor headline",
                      found["placement_note"])

    def test_a_mistyped_id_fails_here_rather_than_silently_tomorrow(self):
        self.provider.raises = RuntimeError("HTTP 404 not found")
        found = poller.probe_source("fake", "99")
        self.assertFalse(found["ok"])
        self.assertIn("check the sensor id", found["error"])

    def test_a_private_sensor_without_its_key_says_so(self):
        """The two things that actually go wrong are a mistyped id and a
        private sensor with no read key. "403" tells nobody anything."""
        self.provider.raises = RuntimeError("HTTP 403 Forbidden")
        found = poller.probe_source("fake", "42")
        self.assertFalse(found["ok"])
        self.assertIn("private", found["error"])
        self.assertIn("read key", found["error"])

    def test_a_read_key_is_used_and_never_returned(self):
        """Storing a credential stays the business of /api/keys. This one uses
        it in flight and must not hand it back — the reply may end up in a
        log, which is the assumption every credential path here is written
        under."""
        seen = {}
        self.answering(placement="outdoor")
        real = self.provider.current
        self.provider.current = lambda src, key: (
            seen.update(src) or real(src, key))

        found = poller.probe_source("fake", "42", read_key="SECRET-KEY-VALUE")

        self.assertEqual("SECRET-KEY-VALUE", seen.get("read_key"),
                         "the read key never reached the provider")
        self.assertNotIn("SECRET-KEY-VALUE", json.dumps(found),
                         "the probe echoed the credential back")

    def test_an_unknown_network_lists_the_ones_that_exist(self):
        found = poller.probe_source("nonsense", "42")
        self.assertFalse(found["ok"])
        self.assertIn("fake", found["networks"])

    def test_the_route_exists_and_reports_failure_as_a_failure(self):
        """The call-site check. A probe nothing calls is a helper with tests."""
        import inspect
        src = inspect.getsource(poller)
        self.assertIn('"/api/sources/probe"', src)
        self.assertIn("200 if probed.get(\"ok\") else 400", src,
                      "a sensor that could not be read returns 200")

    def test_the_page_offers_every_network_without_being_edited(self):
        """Served from PROVIDERS, so a network added later appears in the page
        with no HTML change — the same reason the scales are served."""
        payload = poller.settings_payload({"location": {}, "sources": []})
        offered = {p["name"] for p in payload["choices"]["providers"]}
        self.assertEqual(set(poller.PROVIDERS), offered)


class TestInsideAgainstOutside(IndoorCase):
    """The point of the whole feature: is indoors staying clean, and if not,
    which way is it failing.

    Two answers with opposite remedies. Reporting the wrong one tells somebody
    to open a window during a smoke event, or to seal the house around a fire
    they lit — so each is proven separately, and each by a fixture that could
    not be mistaken for the other.
    """

    def series(self, pattern, days=3):
        """`pattern(hour_index) -> (indoor, outdoor)`, hourly, ending now.

        clock-independent: hour-aligned on purpose. This feeds
        `indoor_outdoor`, which groups by hour and never asks whether a
        reading is fresh — unlike fusion, which declines to headline a stale
        one and made three fixtures in this file depend on the minute the
        suite ran at.
        """
        from datetime import datetime, timedelta, timezone
        hours = days * 24
        base = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
        for i in range(hours):
            when = (base + timedelta(hours=i)).isoformat(timespec="seconds")
            inside, outside = pattern(i)
            store.insert_readings(self.conn, self.indoor,
                                  [{"observed_utc": when, "pm25": inside}])
            store.insert_readings(self.conn, self.outdoor,
                                  [{"observed_utc": when, "pm25": outside}])

    def test_a_house_that_is_holding_is_reported_as_holding(self):
        """Inside far cleaner than outside, and staying there."""
        self.series(lambda i: (2.0 + (i % 3) * 0.1, 30.0 + (i % 5)))
        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.assertEqual("holding", got["verdict"], got)
        self.assertLess(got["ratio"], analyse.INFILTRATION_RATIO)
        self.assertIn("staying cleaner", got["advice"])

    def test_outdoor_air_getting_in_is_named_and_the_advice_is_to_close_up(self):
        """Indoor tracking outdoor. The remedy is to close up and filter —
        the opposite of the other case, and the one that matters during a
        smoke event."""
        self.series(lambda i: (0.85 * (20.0 + (i % 7) * 3), 20.0 + (i % 7) * 3))
        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.assertEqual("outdoor air getting in", got["verdict"], got)
        self.assertIn("Closing up", got["advice"])
        self.assertNotIn("Ventilat", got["advice"],
                         "told to ventilate while outdoor air is getting in")

    def test_an_indoor_source_is_named_and_ventilating_is_qualified(self):
        """Indoor well above outdoor. Ventilating helps — but only while
        outside is cleaner, and saying so unconditionally is how somebody
        opens a window into smoke."""
        self.series(lambda i: (40.0 + (i % 4), 8.0 + (i % 3)))
        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.assertEqual("indoor source", got["verdict"], got)
        self.assertIn("Ventilating", got["advice"])
        self.assertIn("outdoor reading first", got["advice"],
                      "ventilating is recommended without checking outside")

    def test_the_two_failures_are_never_confused(self):
        """The pair, side by side. A classifier that got these the wrong way
        round would still pass each test above if they were written loosely
        enough, so they are asserted against each other."""
        self.series(lambda i: (40.0 + (i % 4), 8.0 + (i % 3)))
        indoor_source = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.setUp()
        self.series(lambda i: (0.85 * (20.0 + (i % 7) * 3), 20.0 + (i % 7) * 3))
        getting_in = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.assertNotEqual(indoor_source["verdict"], getting_in["verdict"])
        self.assertGreater(indoor_source["ratio"], getting_in["ratio"])

    def test_it_refuses_to_judge_a_building_from_a_few_hours(self):
        """A claim about somebody's house from nine hours is not a claim worth
        making. Silence with a reason is an answer this project already
        gives."""
        self.series(lambda i: (2.0, 30.0), days=1)
        self.conn.execute("DELETE FROM readings WHERE observed_utc < ?",
                          (sorted(r["observed_utc"] for r in
                                  self.conn.execute(
                                      "SELECT observed_utc FROM readings"
                                  ).fetchall())[-18],))
        self.conn.commit()

        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)
        self.assertIsNone(got["verdict"])
        self.assertIn("hour(s)", got["why"])
        self.assertIn(str(analyse.MIN_PAIRED_HOURS_INSIDE), got["why"])

    def test_it_says_when_there_is_no_indoor_sensor_rather_than_nothing(self):
        # Readings first, then the source goes — ON DELETE CASCADE takes its
        # rows with it. Deleting first and writing afterwards is a foreign key
        # violation, which is the database being right about an impossible
        # state rather than anything to work around.
        self.series(lambda i: (2.0, 30.0))
        self.conn.execute("DELETE FROM sources WHERE id = ?", (self.indoor,))
        self.conn.commit()

        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)
        self.assertIsNone(got["verdict"])
        self.assertIn("no indoor sensor", got["why"])

    def test_air_too_clean_to_divide_by_is_said_rather_than_reported(self):
        """Two numbers near the instrument's floor produce a ratio that swings
        wildly and means nothing. Reporting it would be inventing precision."""
        self.series(lambda i: (0.4, 0.5))
        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.assertIsNone(got["verdict"])
        self.assertIn("too clean", got["why"])

    def test_the_lag_is_measured_rather_than_assumed(self):
        """Indoor follows outdoor by about an hour, but a house is not a
        published constant. Asserting one offset would be inventing a number
        about somebody's building."""
        self.series(lambda i: (0.8 * (10.0 + (i % 6) * 6) if i else 8.0,
                               10.0 + (i % 6) * 6))
        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)
        self.assertIn(got["lag_hours"], analyse.LAGS_HOURS)

    def test_every_verdict_states_its_grounds_and_disclaims_cause(self):
        """The standard `correlate()` already holds itself to. A statement
        about somebody's house from a few days of two sensors needs its
        grounds visible."""
        self.series(lambda i: (2.0, 30.0))
        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)

        self.assertIsNotNone(got["verdict"])
        self.assertIn("paired hours", got["basis"])
        self.assertIn("not a claim about cause", got["basis"])
        self.assertIn(str(got["hours"]), got["basis"])

    def test_hours_are_paired_by_time_not_by_position(self):
        """A sensor dropping out for an hour is ordinary. Zipping two lists
        would pair the wrong hours from that point on and produce a confident
        number from nonsense."""
        self.series(lambda i: (2.0, 30.0))
        self.conn.execute(
            "DELETE FROM readings WHERE source_id = ? AND observed_utc IN "
            "(SELECT observed_utc FROM readings WHERE source_id = ? LIMIT 5)",
            (self.outdoor, self.outdoor))
        self.conn.commit()

        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)
        self.assertEqual("holding", got["verdict"],
                         "a gap on one side shifted the pairing")

    def test_it_says_when_no_outdoor_sensor_is_reporting(self):
        """The mirror of the missing-indoor case. Somebody whose only outdoor
        sensor has gone quiet should be told that, not shown nothing."""
        self.series(lambda i: (2.0, 30.0))
        self.conn.execute("DELETE FROM readings WHERE source_id = ?",
                          (self.outdoor,))
        self.conn.commit()

        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)
        self.assertIsNone(got["verdict"])
        self.assertIn("no outdoor sensor", got["why"])

    def test_an_unparseable_hour_is_skipped_rather_than_fatal(self):
        """One malformed timestamp must not cost the whole comparison. A row
        that cannot be read is a gap, and the rest of the week is still worth
        answering from — the same reasoning that keeps a bad timestamp from
        blanking the series endpoint."""
        self.series(lambda i: (2.0, 30.0))
        rows = analyse.store.hourly_by_placement(self.conn)
        rows["indoor"]["not-a-timestamp"] = 5.0

        pairs = analyse._paired(rows["indoor"], rows["outdoor"], lag_hours=1)
        self.assertTrue(pairs, "the bad hour took every good one with it")
        self.assertNotIn("not-a-timestamp", [p[0] for p in pairs])

    def test_a_lag_with_too_few_pairs_is_skipped_not_used(self):
        """Shifting by two hours drops the first two, and near the minimum
        that can take a lag below what is honest. Skipping it is right;
        reporting a correlation from eleven hours would not be."""
        hours = analyse.MIN_PAIRED_HOURS_INSIDE
        indoor = {f"2026-08-0{1 + h // 24}T{h % 24:02d}:00:00+00:00": 2.0 + h
                  for h in range(hours)}
        outdoor = dict(indoor)

        full = analyse._paired(indoor, outdoor, 0)
        shifted = analyse._paired(indoor, outdoor, 2)
        self.assertEqual(hours, len(full))
        self.assertLess(len(shifted), analyse.MIN_PAIRED_HOURS_INSIDE,
                        "the fixture no longer exercises the short-lag branch")

    def test_a_lag_that_would_be_too_short_is_skipped_inside_the_analysis(self):
        """Exercised through `indoor_outdoor`, not just `_paired`.

        With exactly the minimum number of hours, shifting by one or two drops
        below it — so those lags are skipped and the answer comes from lag 0.
        A version that used them anyway would report a correlation from fewer
        hours than the function's own threshold allows.

        clock-independent: hour-aligned on purpose, for the same reason as
        `series()` above — the analysis buckets by hour and staleness plays no
        part in it.
        """
        from datetime import datetime, timedelta, timezone
        hours = analyse.MIN_PAIRED_HOURS_INSIDE
        base = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
        for i in range(hours):
            when = (base + timedelta(hours=i)).isoformat(timespec="seconds")
            store.insert_readings(self.conn, self.indoor,
                                  [{"observed_utc": when, "pm25": 3.0 + i}])
            store.insert_readings(self.conn, self.outdoor,
                                  [{"observed_utc": when, "pm25": 30.0 + i}])

        got = analyse.indoor_outdoor(self.conn, self.cfg, days=3)
        self.assertEqual(hours, got["hours"])
        self.assertEqual(0, got["lag_hours"],
                         "a lag with fewer than the minimum pairs was used")


class TestBothFrontEndsAddASensorTheSameWay(unittest.TestCase):
    """`setup.py` and the settings page must not disagree about what a valid
    sensor is.

    They already share one validator for settings, for exactly this reason:
    two front ends with two ideas of validity is two behaviours to keep in
    step, and the one nobody uses is the one that rots.
    """

    def test_setup_probes_rather_than_trusting_what_was_typed(self):
        import inspect
        import setup as setup_module
        src = inspect.getsource(setup_module.choose_your_own_sensors)
        self.assertIn("poller.probe_source(", src,
                      "setup accepts a sensor id without reading the sensor")

    def test_setup_shows_where_the_sensor_is_before_adding_it(self):
        """The user is the only one who can correct a wrong placement, and
        they can only do that if they are told what was detected."""
        import inspect
        import setup as setup_module
        src = inspect.getsource(setup_module.choose_your_own_sensors)
        self.assertIn("placement_note", src)
        self.assertIn("placement", src)

    def test_setup_offers_it_at_all(self):
        """The call-site check: a helper nothing reaches is a helper with
        tests. Five of those have shipped here."""
        import inspect
        import setup as setup_module
        src = inspect.getsource(setup_module.choose_sources)
        self.assertIn("choose_your_own_sensors()", src)

    def test_a_read_key_typed_at_setup_is_carried_to_the_config(self):
        """Otherwise a private sensor is added and then cannot be read, which
        looks like the sensor being broken."""
        import inspect
        import setup as setup_module
        src = inspect.getsource(setup_module.choose_your_own_sensors)
        self.assertIn('entry["read_key"] = read_key', src)


class TestANewSensorArrivesWithEnoughHistoryToBeUseful(unittest.TestCase):
    """How much history a newly added sensor fetches, decided rather than
    inherited.

    `do_poll()` seeds `backfill_days_on_first_run` days for any source with no
    rows, so adding a sensor to an existing install already backfills. The
    question the spec asks is whether that default is *enough*, and the answer
    has to hold against the one thing that consumes it.
    """

    def test_the_default_backfill_covers_what_the_comparison_needs(self):
        """Asserted as a relationship, not as two numbers that happen to
        agree today. Lowering the backfill default, or raising the comparison's
        minimum, would otherwise leave a newly added indoor sensor unable to
        say anything until a day of live polling had passed — and the user
        would be looking at "not enough to say yet" with no idea it was
        temporary.
        """
        days = poller.DEFAULT_CONFIG["backfill_days_on_first_run"]
        self.assertGreaterEqual(
            days * 24, analyse.MIN_PAIRED_HOURS_INSIDE,
            f"a new sensor is seeded with {days} day(s) of history, which is "
            f"less than the {analyse.MIN_PAIRED_HOURS_INSIDE} paired hours "
            f"the inside/outside comparison needs before it will speak")

    def test_a_newly_added_source_is_backfilled_at_all(self):
        """The mechanism, not just the number. A source with no rows takes the
        first-run path; one with rows takes the gap path."""
        import inspect
        src = inspect.getsource(poller.do_poll)
        self.assertIn("last = store.last_observed(conn, sid)", src)
        self.assertIn("backfill_days_on_first_run", src)

    def test_more_history_is_a_setting_rather_than_a_rebuild(self):
        """Somebody who wants a longer record for a building they are trying
        to characterise can ask for it, and the validator accepts up to ten
        years."""
        clean, errors = poller.validate_settings(
            {"backfill_days_on_first_run": 365})
        self.assertEqual({}, errors)
        self.assertEqual(365, clean["backfill_days_on_first_run"])


class TestASingleChannelSensorIsNotBroken(unittest.TestCase):
    """PurpleAir's `confidence` is derived from how far its two laser counters
    disagree. A sensor reporting one channel has nothing to disagree with, so
    a low figure there is not evidence about the instrument — it is the
    absence of a second opinion, which is a different thing.

    An indoor PA-I reporting channel A only, at confidence 30, had every live
    reading filed as an instrument fault: excluded from the chart, the evening
    analysis and the inside-against-outside comparison, while PurpleAir's own
    map showed it healthy and reporting.

    `assess_quality`'s own docstring already stated the principle this broke —
    *a single-value government feed has no way to self-check, and that is not
    suspicious* — and a single-channel PurpleAir is that case wearing a
    confidence figure.
    """

    def test_one_channel_and_low_confidence_is_not_a_fault(self):
        self.assertEqual("ok", store.assess_quality(0.1, 0.0, None, 30.0))

    def test_two_channels_and_low_confidence_still_is(self):
        """The signal is real when there are two counters to disagree. Losing
        that would trade one false positive for a missed fault."""
        self.assertEqual("suspect",
                         store.assess_quality(10.0, 10.0, 10.2, 30.0))

    def test_two_channels_disagreeing_is_still_a_fault(self):
        self.assertEqual("suspect", store.assess_quality(10.0, 20.0, 2.0, 90.0))

    def test_a_single_valued_feed_is_still_not_suspicious(self):
        """Most of the regulatory network reports one number and no channels.
        That was already true and must stay true."""
        self.assertEqual("ok", store.assess_quality(7.0, None, None, None))

    def test_extreme_air_is_still_not_called_a_fault(self):
        """The reason this function checks instrument evidence first. Readings
        past 350 µg/m³ during Black Summer were filed as faults and dropped
        from every aggregate on exactly the days they mattered."""
        self.assertEqual("extreme",
                         store.assess_quality(900.0, 890.0, 910.0, 95.0))

    def test_whether_the_instrument_could_check_itself_is_reportable(self):
        """Not silently trusted either. "No fault found" must not imply two
        channels agreed when there was only one."""
        self.assertFalse(store.self_checked(0.0, None))
        self.assertTrue(store.self_checked(1.0, 1.1))
        self.assertFalse(store.self_checked(None, None))


class TestTheMislabelledReadingsAreRepaired(unittest.TestCase):
    """v9. The verdicts already stored are wrong on every install that has a
    single-channel sensor, and they are what the chart and the comparison
    read — fixing the function alone would leave the data hidden."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "airo.db"

    def seeded(self, rows):
        conn = store.connect(self.path)
        sid = store.upsert_source(conn, "purpleair", "1", "Indoor",
                                  placement="indoor")
        for when, pm, a, b, conf, quality in rows:
            conn.execute(
                "INSERT INTO readings (source_id, observed_utc, pm25, pm25_a,"
                " pm25_b, confidence, quality) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, when, pm, a, b, conf, quality))
        conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()

    def verdicts(self):
        conn = store.connect(self.path)
        try:
            return [r[0] for r in conn.execute(
                "SELECT quality FROM readings ORDER BY observed_utc")]
        finally:
            conn.close()

    def test_a_single_channel_fault_is_corrected_on_upgrade(self):
        self.seeded([("2026-08-01T00:00:00+00:00", 0.1, 0.0, None, 30.0,
                      "suspect")])
        self.assertEqual(["ok"], self.verdicts())

    def test_a_genuine_channel_disagreement_is_left_alone(self):
        self.seeded([("2026-08-01T00:00:00+00:00", 10.0, 20.0, 2.0, 90.0,
                      "suspect")])
        self.assertEqual(["suspect"], self.verdicts())

    def test_nothing_that_was_ok_can_become_a_fault(self):
        """Only rows already marked suspect are looked at. A reassessment that
        could newly condemn readings would be a different and riskier thing
        than the one this is."""
        self.seeded([("2026-08-01T00:00:00+00:00", 10.0, 20.0, 2.0, 90.0,
                      "ok")])
        self.assertEqual(["ok"], self.verdicts())

    def test_the_repaired_readings_reach_the_comparison(self):
        """The point of repairing them. `hourly_by_placement` excludes
        suspect rows, so a mislabelled indoor sensor is invisible to the
        inside-against-outside panel however healthy it is."""
        rows = []
        for hour in range(30):
            rows.append((f"2026-08-01T{hour % 24:02d}:00:00+00:00"
                         if hour < 24 else
                         f"2026-08-02T{hour - 24:02d}:00:00+00:00",
                         0.5, 0.5, None, 30.0, "suspect"))
        self.seeded(rows)

        conn = store.connect(self.path)
        try:
            indoor = store.hourly_by_placement(conn).get("indoor") or {}
        finally:
            conn.close()
        self.assertEqual(30, len(indoor),
                         "repaired readings are still hidden from the "
                         "inside-against-outside comparison")


class TestAnIndoorSensorStillReportsItsOwnHealth(IndoorCase):
    """Excluded from the outdoor claim, not from its own diagnostics.

    The first version of the exclusion split the readings *before*
    `fusion.annotate()` rather than after, so the indoor sensor never had its
    age, distance or staleness computed at all. The dashboard renders those
    three straight from the payload, so the row read `— / —` with no stale tag
    however long the sensor had been dead.

    That is the exclusion cutting the wrong way. `annotate()` records facts
    about an instrument — when it last spoke, how far away it is, whether it
    has gone quiet. `fuse()` makes a claim about the air outside. Only the
    second must exclude indoor air; applying the first to outdoor sensors only
    means a failed indoor sensor is invisible, which is worse than showing
    nothing, because a stale reading with no age looks current.
    """

    def quiet_indoor(self, hours_ago=6):
        """An indoor sensor whose last word was `hours_ago` hours back."""
        self.conn.execute("DELETE FROM readings WHERE source_id = ?",
                          (self.indoor,))
        when = (datetime.now(timezone.utc)
                - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        store.insert_readings(self.conn, self.indoor,
                              [{"observed_utc": when, "pm25": self.INDOOR_PM}])
        self.conn.commit()

    def indoor_row(self, latest):
        for s in latest.get("sources") or []:
            if s.get("site_name") == "Kitchen":
                return s
        # Raised rather than self.fail(), which reads to a human and to a
        # static analyser as falling through to an implicit None -- and a
        # helper that returns None here would fail the caller's assertion for
        # the wrong reason, reporting a blank field instead of a missing row.
        raise AssertionError(
            "the indoor sensor is not in the served sources at all")

    def test_the_indoor_row_says_when_it_last_reported(self):
        """The question a user actually asks of this row: is it collecting?
        A blank age cannot answer it, and reads as "no data" beside a live
        PM2.5 figure."""
        self.hours(3)
        row = self.indoor_row(poller.build_latest(self.conn, self.cfg))

        self.assertIsNotNone(row.get("age_minutes"),
                             "the indoor sensor's age is blank, so the row "
                             "cannot say whether data is arriving")

    def test_the_indoor_row_says_how_far_away_it_is(self):
        self.hours(3)
        row = self.indoor_row(poller.build_latest(self.conn, self.cfg))

        self.assertIsNotNone(row.get("distance_km"),
                             "the indoor sensor's distance is blank")

    def test_an_indoor_sensor_that_has_gone_quiet_is_marked_stale(self):
        """The hazard this closes. Without it a dead indoor sensor keeps
        showing its last reading, undated and untagged, indefinitely — and
        the inside-against-outside verdict is drawn from a sensor that
        stopped reporting."""
        self.quiet_indoor(hours_ago=6)
        row = self.indoor_row(poller.build_latest(self.conn, self.cfg))

        self.assertTrue(row.get("stale"),
                        "an indoor sensor silent for six hours is not "
                        "flagged stale, so nobody is told it died")

    def test_a_live_indoor_sensor_is_not_marked_stale(self):
        """The other direction, so the test above cannot pass by hard-coding
        staleness on every indoor row."""
        self.hours(3)
        row = self.indoor_row(poller.build_latest(self.conn, self.cfg))

        self.assertFalse(row.get("stale"),
                         "a live indoor sensor is being reported as quiet")

    def test_knowing_its_health_does_not_let_it_speak_for_outside(self):
        """The discriminating half. Widening `annotate()` must not widen
        `fuse()` — if a later change "simplifies" this by passing every
        reading into fusion, the indoor sensor is back in corroboration and
        back in the running for the headline, which is the contamination the
        split exists to prevent."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)
        row = self.indoor_row(latest)

        self.assertIsNone(row.get("corroboration"),
                          "the indoor sensor is being corroborated against "
                          "outdoor peers")
        self.assertIsNone(row.get("peer_ratio"),
                          "the indoor sensor has a peer ratio, so it is "
                          "inside the corroboration set")
        self.assertEqual(self.OUTDOOR_PM, latest["pm25_10min"],
                         "the indoor sensor became the headline")

    def test_the_outdoor_sensors_keep_their_own_corroboration(self):
        """Annotating everything must not disturb what fusion returns for the
        sources that are in it."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)
        out = [s for s in latest["sources"]
               if s.get("site_name") == "Regulatory station"][0]

        self.assertIsNotNone(out.get("corroboration"),
                             "the outdoor sensor lost its corroboration")
        self.assertIsNotNone(out.get("age_minutes"))


class TestTheRowExplainsItself(IndoorCase):
    """The blank cells are correct; unexplained they read as a broken sensor.

    An indoor sensor has no outdoor peers, so its "vs peers" cell is empty by
    design. The first question this dashboard was asked about one was "is data
    being collected?" — it was, and the blanks were what prompted it. The
    wording is reused from the settings probe rather than written again here,
    because one relationship described by two surfaces in two ways is how the
    two drift apart.
    """

    def row_for(self, latest, name):
        for s in latest.get("sources") or []:
            if s.get("site_name") == name:
                return s
        raise AssertionError(f"no served row for {name!r}")

    def test_an_indoor_row_carries_the_reason_it_has_no_peers(self):
        self.hours(3)
        row = self.row_for(poller.build_latest(self.conn, self.cfg), "Kitchen")

        self.assertIsNotNone(row.get("placement_note"),
                             "the indoor row is served with no explanation "
                             "for its empty peer column")
        self.assertIn("outdoor headline", row["placement_note"])

    def test_an_outdoor_row_carries_no_note(self):
        """A note on every row is a note nobody reads, including the one that
        means something."""
        self.hours(3)
        row = self.row_for(poller.build_latest(self.conn, self.cfg),
                           "Regulatory station")

        self.assertIsNone(row.get("placement_note"),
                          "an ordinary outdoor row is carrying a note")

    def test_a_sensor_of_unknown_placement_is_explained_too(self):
        """`unknown` is excluded from the headline like indoor is, so it has
        the same unexplained blank and needs the same sentence."""
        self.conn.execute("UPDATE sources SET placement = 'unknown' WHERE id = ?",
                          (self.indoor,))
        self.conn.commit()
        self.hours(3)
        row = self.row_for(poller.build_latest(self.conn, self.cfg), "Kitchen")

        self.assertIsNotNone(row.get("placement_note"),
                             "a sensor of unknown placement is served with no "
                             "explanation, though it is excluded like indoor")

    def test_the_wording_is_the_settings_page_wording(self):
        """Enumerated from the function both surfaces call, so a change to one
        cannot silently leave the other saying something else."""
        self.hours(3)
        row = self.row_for(poller.build_latest(self.conn, self.cfg), "Kitchen")

        self.assertEqual(poller._placement_note("indoor"),
                         row.get("placement_note"),
                         "the dashboard and the settings page describe an "
                         "indoor sensor differently")


class TestTheRecordPanelCountsTheWholeRecord(IndoorCase):
    """"Readings on disk" must be the readings on disk.

    Found by adding a nearby sensor against the maintainer's own record. It
    became the headline immediately — correctly, `nearest` is the rule and it
    is the nearest — and every historical panel follows the headline source,
    so the page went from describing years of readings to describing a
    handful.

    Three of those panels are honest about it: they show one instrument and
    now say so. "Readings on disk" is not one of them. It promises a fact
    about the database and was reporting the length of whatever series the
    page happened to be holding, so it read a handful with tens of thousands
    of rows stored.

    A number that looks like catastrophic data loss, on a page whose whole
    purpose is to be trusted about the record.
    """

    def totals(self, latest):
        return latest.get("record") or {}

    def test_the_total_is_every_reading_stored(self):
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        stored = self.conn.execute(
            "SELECT COUNT(*) FROM readings").fetchone()[0]
        self.assertEqual(stored, self.totals(latest).get("readings_total"),
                         "the served total is not the number of readings held")

    def test_the_total_counts_sources_the_headline_is_not_from(self):
        """The failure exactly: the indoor sensor and every outdoor one that
        is not the headline still occupy disk, and the reader is entitled to
        know it."""
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        headline_only = self.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE source_id = ?",
            (self.outdoor,)).fetchone()[0]

        self.assertGreater(
            self.totals(latest).get("readings_total"), headline_only,
            "the total equals the headline source's own count, which is the "
            "bug: it is reporting one instrument as though it were the record")

    def test_the_span_is_the_whole_record_not_one_sensor(self):
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)
        rec = self.totals(latest)

        first = self.conn.execute(
            "SELECT MIN(observed_utc) FROM readings").fetchone()[0]
        self.assertEqual(first, rec.get("first_utc"))

    def test_a_source_with_no_readings_does_not_break_the_count(self):
        """A sensor added a minute ago has none, and that must not make the
        total null — which is how it would present as "no data at all"."""
        store.upsert_source(self.conn, "purpleair", "brand-new", "Just added",
                            placement="outdoor")
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        self.assertIsNotNone(self.totals(latest).get("readings_total"))
        self.assertGreater(self.totals(latest)["readings_total"], 0)

    def test_an_empty_install_says_zero_rather_than_nothing(self):
        latest = poller.build_latest(self.conn, self.cfg)

        self.assertEqual(0, self.totals(latest).get("readings_total"))


class TestTheWindowDecisionIsServed(IndoorCase):
    """The page renders this and decides nothing. If the server stops sending
    it, the panel goes blank — which is safe, and silent, and nobody would
    notice for weeks."""

    def test_the_view_carries_a_window_decision(self):
        self.hours(3)
        latest = poller.build_latest(self.conn, self.cfg)

        self.assertIn("window_advice", latest,
                      "the page has nothing to render and will show nothing")

    def test_the_decision_has_words_in_it(self):
        self.hours(3)
        w = poller.build_latest(self.conn, self.cfg)["window_advice"]

        self.assertTrue(w.get("headline"))
        self.assertTrue(w.get("advice"))

    def test_dirty_air_is_not_told_to_ventilate(self):
        """End to end, through the real builder rather than the helper: the
        reading reaches the decision."""
        now = datetime.now(timezone.utc)
        store.insert_readings(self.conn, self.outdoor, [
            {"observed_utc": (now - timedelta(minutes=i)).isoformat(
                timespec="seconds"), "pm25": 40.0} for i in range(12)])
        self.conn.commit()

        w = poller.build_latest(self.conn, self.cfg)["window_advice"]

        self.assertNotIn("ventilate", (w.get("advice") or "").lower(),
                         "40 ug/m3 and the panel still says open a window")
