# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ROADMAP #9 Phase C — a six-hour outlook, and the right to give one.

The guardrails shipped a year before the feature, deliberately. `forecast.py`
refuses certainty wording, demands a stated basis, and will not speak at all
until skill has been measured over 30 verified outcomes. This is the part that
earns its way past them.

Two constraints shape every decision here.

**Australian Consumer Law s4.** A forecast is a representation about a future
matter and the burden of showing reasonable grounds sits with whoever makes
it. So the grounds are the *user's own record*: the prediction comes from the
wind bands Phase B measured in their data, not from constants somebody chose,
and the basis says how many of their hours it rests on.

**PurpleAir ToS §4.4** grants them a licence over models derived from their
data, so `training_sources()` excludes them by construction. A rules engine
fitted to a user's own government-feed history avoids that entirely.

The thing being protected is somebody deciding whether to open a window. A
forecast that is confidently wrong is worse than none, which is why the honest
answer here is usually silence.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forecast  # noqa: E402
import poller    # noqa: E402
import store     # noqa: E402

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


class OutlookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "qld", "wbk", "Reference")
        self.place = store.place_key(HOME_LAT, HOME_LON)
        self.skill_path = Path(self.tmp.name) / "skill.json"
        self.cfg = {"aqi_scale": "au",
                    "location": {"latitude": HOME_LAT, "longitude": HOME_LON,
                                 "timezone": "Australia/Brisbane"}}

    def hour(self, days_ago, h):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
            hour=h, minute=0, second=0, microsecond=0)
        return d.isoformat(timespec="seconds")

    def history(self, calm_pm=14.0, breezy_pm=4.0, days=20):
        """A record with the project's premise in it, so the rules have
        measured ground to stand on."""
        for day in range(1, days + 1):
            for h in range(24):
                calm = h % 2 == 0
                when = self.hour(day, h)
                store.insert_readings(self.conn, self.sid, [
                    {"observed_utc": when,
                     "pm25": calm_pm if calm else breezy_pm}])
                store.insert_weather(self.conn, self.place, [{
                    "observed_utc": when,
                    "wind_speed_ms": 0.2 if calm else 2.5,
                    "temperature_c": 7.0 if calm else 19.0,
                    "humidity_pct": 82.0 if calm else 48.0,
                    "wind_dir_deg": 270}])

    def verified(self, n, model_error=1.0, persistence_error=6.0):
        """A skill ledger with `n` outcomes, the model beating persistence."""
        s = forecast.Skill(self.skill_path)
        for i in range(n):
            s.record(predicted=10.0 + model_error, persistence=10.0 + persistence_error,
                     actual=10.0, when=f"2026-07-{(i % 28) + 1:02d}T20:00:00+00:00")
        return s


class TestItWillNotSpeakWithoutGrounds(OutlookCase):
    """The default answer, and the one that protects somebody deciding
    whether to open a window."""

    def test_with_no_history_it_declines(self):
        got = forecast.outlook(self.conn, self.cfg, [], self.skill_path)
        self.assertIsNone(got.get("text"),
                          "it forecast with nothing to forecast from")
        self.assertTrue(got.get("why"), "it declined without saying why")

    def test_with_history_but_no_verified_skill_it_declines(self):
        """Thirty outcomes is the bar `forecast.py` set before this feature
        existed. Having data to predict from is not the same as having shown
        the prediction is any good."""
        self.history()
        forward = [{"observed_utc": self.hour(-1, 20), "wind_speed_ms": 0.2,
                    "temperature_c": 7.0, "wind_dir_deg": 270}]
        got = forecast.outlook(self.conn, self.cfg, forward, self.skill_path)
        self.assertIsNone(got.get("text"))
        self.assertIn("verif", (got.get("why") or "").lower())

    def test_at_one_short_of_the_bar_it_still_declines(self):
        self.history()
        self.verified(forecast.MIN_VERIFIED - 1)
        forward = [{"observed_utc": self.hour(-1, 20), "wind_speed_ms": 0.2,
                    "temperature_c": 7.0, "wind_dir_deg": 270}]
        got = forecast.outlook(self.conn, self.cfg, forward, self.skill_path)
        self.assertIsNone(got.get("text"),
                          f"it spoke at {forecast.MIN_VERIFIED - 1} outcomes")

    def test_at_the_bar_it_speaks(self):
        """The other side of the same line: a gate that never opens is not a
        gate, it is a disabled feature."""
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        forward = [{"observed_utc": self.hour(-1, 20), "wind_speed_ms": 0.2,
                    "temperature_c": 7.0, "wind_dir_deg": 270}]
        got = forecast.outlook(self.conn, self.cfg, forward, self.skill_path)
        self.assertTrue(got.get("text"),
                        f"it stayed silent with skill measured: {got}")

    def test_a_model_no_better_than_persistence_stays_silent(self):
        """Beating nothing is not skill. Autocorrelation is not a forecast."""
        self.history()
        s = forecast.Skill(self.skill_path)
        for i in range(forecast.MIN_VERIFIED):
            s.record(predicted=16.0, persistence=10.0, actual=10.0)
        forward = [{"observed_utc": self.hour(-1, 20), "wind_speed_ms": 0.2,
                    "temperature_c": 7.0, "wind_dir_deg": 270}]
        got = forecast.outlook(self.conn, self.cfg, forward, self.skill_path)
        self.assertIsNone(got.get("text"))


class TestWhatItSaysWhenItDoesSpeak(OutlookCase):
    def setUp(self):
        super().setUp()
        self.history()
        self.verified(forecast.MIN_VERIFIED)

    def speak(self, **over):
        f = {"observed_utc": self.hour(-1, 20), "wind_speed_ms": 0.2,
             "temperature_c": 7.0, "wind_dir_deg": 270}
        f.update(over)
        return forecast.outlook(self.conn, self.cfg, [f], self.skill_path)

    def test_it_reads_as_a_likelihood_not_a_fact(self):
        """`phrase()` enforces this, so this is really a test that outlook
        goes through phrase() rather than around it."""
        text = self.speak()["text"].lower()
        self.assertTrue(any(h in text for h in forecast.HEDGES),
                        f"no likelihood wording: {text!r}")

    def test_it_states_the_grounds_from_the_users_own_record(self):
        """ACL s4: the burden is on the maker to show reasonable grounds. The
        grounds are the user's measured hours, and the number of them."""
        text = self.speak()["text"]
        self.assertIn("—", text, "phrase() did not append a basis")
        self.assertRegex(text, r"\d+\s+(hour|hours)",
                         f"the basis does not say how much data it rests on: "
                         f"{text!r}")

    def test_calm_and_cold_reads_worse_than_breezy(self):
        """The premise, applied. Both must be reachable or the rule is a
        constant wearing a costume."""
        calm = self.speak(wind_speed_ms=0.2, temperature_c=7.0)
        breezy = self.speak(wind_speed_ms=3.0, temperature_c=19.0)
        self.assertGreater(calm["pm25"], breezy["pm25"],
                           "a calm cold hour did not forecast worse air than "
                           "a breezy warm one")

    def test_the_prediction_is_a_number_that_can_be_scored(self):
        """A forecast that cannot be verified cannot earn skill, and would
        keep the gate shut forever."""
        got = self.speak()
        self.assertIsInstance(got["pm25"], float)
        self.assertGreater(got["pm25"], 0)

    def test_it_carries_a_persistence_baseline_to_be_judged_against(self):
        got = self.speak()
        self.assertIn("persistence", got)
        self.assertIsInstance(got["persistence"], float)

    def test_it_publishes_its_accuracy(self):
        """ROADMAP #9: publish accuracy. A forecast whose track record is
        private is a claim nobody can check."""
        got = self.speak()
        self.assertTrue(got.get("accuracy"))
        self.assertIn("µg/m³", got["accuracy"])


class TestVerifyingWhatWasPredicted(OutlookCase):
    """A prediction nobody checks is a claim, not a forecast."""

    def test_a_prediction_is_recorded_as_pending(self):
        self.history()
        pending = Path(self.tmp.name) / "pending.json"
        forecast.remember(pending, when=self.hour(-1, 20),
                          predicted=12.0, persistence=8.0)
        self.assertTrue(pending.exists())
        self.assertEqual(1, len(json.loads(pending.read_text(encoding="utf-8"))))

    def test_it_is_verified_once_the_hour_has_happened(self):
        self.history()
        pending = Path(self.tmp.name) / "pending.json"
        when = self.hour(1, 10)          # an hour that already exists
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 11.0}])
        forecast.remember(pending, when=when, predicted=12.0, persistence=8.0)

        n = forecast.verify_pending(self.conn, pending, self.skill_path)
        self.assertEqual(1, n)
        s = forecast.Skill(self.skill_path)
        self.assertEqual(1, len(s.records))
        self.assertEqual(11.0, s.records[0]["actual"])

    def test_an_hour_with_no_reading_stays_pending(self):
        """The station may have been offline. Scoring against a reading that
        does not exist would invent skill out of a gap."""
        pending = Path(self.tmp.name) / "pending.json"
        forecast.remember(pending, when=self.hour(1, 10),
                          predicted=12.0, persistence=8.0)
        n = forecast.verify_pending(self.conn, pending, self.skill_path)
        self.assertEqual(0, n)
        self.assertEqual(1, len(json.loads(pending.read_text(encoding="utf-8"))),
                         "an unverifiable prediction was discarded")

    def test_a_future_hour_is_not_verified_early(self):
        """A reading is planted at the future hour on purpose.

        Without it the test could not tell "has not happened yet" from "no
        reading for that hour" — both keep it pending, so it passed with the
        time check deleted. With a reading present, the clock is the only
        thing that can stop it being scored.
        """
        when = self.hour(-1, 10)          # tomorrow
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 11.0}])
        pending = Path(self.tmp.name) / "pending.json"
        forecast.remember(pending, when=when, predicted=12.0, persistence=8.0)

        self.assertEqual(
            0, forecast.verify_pending(self.conn, pending, self.skill_path),
            "a prediction was scored before the hour it is about")
        self.assertEqual(1, len(json.loads(pending.read_text(encoding="utf-8"))),
                         "the pending prediction was consumed early")

    def test_verifying_twice_does_not_double_count(self):
        self.history()
        pending = Path(self.tmp.name) / "pending.json"
        when = self.hour(1, 10)
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 11.0}])
        forecast.remember(pending, when=when, predicted=12.0, persistence=8.0)
        forecast.verify_pending(self.conn, pending, self.skill_path)
        forecast.verify_pending(self.conn, pending, self.skill_path)
        self.assertEqual(1, len(forecast.Skill(self.skill_path).records),
                         "one prediction was scored twice, inflating skill")


class TestPurpleAirIsNotTrainedOn(OutlookCase):
    """ToS §4.4 grants PurpleAir a licence over models derived from their
    data. The exclusion is by construction rather than by remembering."""

    def test_a_purpleair_only_setup_cannot_forecast(self):
        # A database with *only* PurpleAir in it. The base fixture registers a
        # government source, and leaving it in place made this pass for the
        # wrong reason -- the refusal was "no paired data" rather than the
        # licence, which is a different sentence and a different bug.
        self.conn.execute("DELETE FROM sources WHERE provider != 'purpleair'")
        self.conn.commit()
        pa = store.upsert_source(self.conn, "purpleair", "pa-1", "Backyard")
        for day in range(1, 21):
            for h in range(24):
                when = self.hour(day, h)
                store.insert_readings(self.conn, pa,
                                      [{"observed_utc": when, "pm25": 10.0}])
                store.insert_weather(self.conn, self.place, [{
                    "observed_utc": when, "wind_speed_ms": 0.2,
                    "temperature_c": 7.0, "wind_dir_deg": 270}])
        self.verified(forecast.MIN_VERIFIED)
        got = forecast.outlook(self.conn, self.cfg,
                               [{"observed_utc": self.hour(-1, 20),
                                 "wind_speed_ms": 0.2, "temperature_c": 7.0}],
                               self.skill_path)
        self.assertIsNone(got.get("text"))
        self.assertIn("purpleair", (got.get("why") or "").lower())


class TestEveryRefusalSaysWhich(OutlookCase):
    """Each way of having nothing to say is a different sentence.

    "No weather", "no reading", "no hours in that band" and "not allowed to
    model your only source" send somebody to four different places. Collapsing
    them into one message would be tidier and would waste their afternoon.
    """

    def test_no_location_configured(self):
        got = forecast.outlook(self.conn, {"location": {}}, [], self.skill_path)
        self.assertIsNone(got["text"])
        self.assertTrue(got["why"])

    def test_no_forward_weather_to_reason_about(self):
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        got = forecast.outlook(self.conn, self.cfg, [], self.skill_path)
        self.assertIn("no forecast weather", got["why"])

    def test_a_forward_hour_with_no_timestamp_is_skipped(self):
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        got = forecast.outlook(self.conn, self.cfg,
                               [{"wind_speed_ms": 0.2}], self.skill_path)
        self.assertIn("no forecast weather", got["why"])

    def test_a_wind_band_your_record_has_never_seen(self):
        """Only calm and breezy hours exist in the fixture, so a forecast in
        the middle band has nothing measured to stand on. Saying "no data"
        would be wrong — there is plenty, just not of that kind."""
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        got = forecast.outlook(self.conn, self.cfg,
                               [{"observed_utc": self.hour(-1, 20),
                                 "wind_speed_ms": 0.75}], self.skill_path)
        self.assertIsNone(got["text"])
        self.assertIn("band", got["why"])

    def test_the_persistence_baseline_is_always_available_when_means_are(self):
        """There is deliberately no "no current reading" branch.

        `band_means()` and the baseline draw from the same rows — a reading
        with a value, not marked suspect — so means being non-empty guarantees
        a reading exists. A guard for that case would be unreachable, and dead
        defensive code reads as a handled case that is not one. This asserts
        the invariant the missing guard relies on.
        """
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        got = forecast.outlook(self.conn, self.cfg,
                               [{"observed_utc": self.hour(-1, 20),
                                 "wind_speed_ms": 0.2}], self.skill_path)
        self.assertIsNotNone(got["persistence"])
        self.assertIsInstance(got["persistence"], float)

    def test_a_clearing_outlook_reads_as_clearing(self):
        """Both directions, or "worse" is a constant rather than a finding."""
        self.history(calm_pm=14.0, breezy_pm=2.0)
        self.verified(forecast.MIN_VERIFIED)
        self.conn.execute(
            "INSERT INTO readings (source_id, observed_utc, pm25, quality) "
            "VALUES (?, ?, 30.0, 'ok')", (self.sid, self.hour(0, 23)))
        self.conn.commit()
        got = forecast.outlook(self.conn, self.cfg,
                               [{"observed_utc": self.hour(-1, 20),
                                 "wind_speed_ms": 3.0}], self.skill_path)
        self.assertIn("clearer", got["text"])


class TestTheLedgersSurviveBadInput(OutlookCase):
    """These files sit in the user's data directory and are read on every
    poll. A corrupt one must not stop the poller."""

    def test_a_corrupt_pending_file_is_treated_as_empty(self):
        pending = Path(self.tmp.name) / "pending.json"
        pending.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            0, forecast.verify_pending(self.conn, pending, self.skill_path))

    def test_a_corrupt_pending_file_does_not_stop_a_new_prediction(self):
        pending = Path(self.tmp.name) / "pending.json"
        pending.write_text("{not json", encoding="utf-8")
        forecast.remember(pending, when=self.hour(-1, 20),
                          predicted=9.0, persistence=8.0)
        self.assertEqual(1, len(json.loads(
            pending.read_text(encoding="utf-8"))))

    def test_promising_twice_for_one_hour_keeps_the_first(self):
        """Otherwise a poll every fifteen minutes writes four predictions for
        the same hour and scores all of them, inflating the ledger."""
        pending = Path(self.tmp.name) / "pending.json"
        when = self.hour(-1, 20)
        forecast.remember(pending, when=when, predicted=9.0, persistence=8.0)
        forecast.remember(pending, when=when, predicted=99.0, persistence=8.0)
        held = json.loads(pending.read_text(encoding="utf-8"))
        self.assertEqual(1, len(held))
        self.assertEqual(9.0, held[0]["predicted"])

    def test_an_unparseable_hour_is_dropped_rather_than_scored(self):
        pending = Path(self.tmp.name) / "pending.json"
        pending.write_text(json.dumps(
            [{"when": "not a time", "predicted": 9.0, "persistence": 8.0}]),
            encoding="utf-8")
        self.assertEqual(
            0, forecast.verify_pending(self.conn, pending, self.skill_path))
        self.assertEqual([], json.loads(pending.read_text(encoding="utf-8")))


class TestTheOutlookThroughTheCommandLine(OutlookCase):
    """`poller.py --forecast`. The function being right is not the same as
    anything running it — four helpers in this project have been fully tested
    while their call site was gone."""

    def run_cli(self, *argv):
        saved = sys.argv
        sys.argv = ["poller.py", *argv]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out), \
                    unittest.mock.patch.object(poller, "load_config",
                                               lambda: self.cfg), \
                    unittest.mock.patch.object(
                        poller, "open_store", lambda: self.conn), \
                    unittest.mock.patch.object(
                        poller, "FORECAST_PENDING_PATH",
                        Path(self.tmp.name) / "pending.json"), \
                    unittest.mock.patch.object(
                        poller, "FORECAST_SKILL_PATH", self.skill_path):
                code = poller.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.argv = saved
        return code, out.getvalue()

    def stub_forward(self, hours):
        import weather
        saved = weather.forward
        weather.forward = lambda lat, lon, hours=6, **kw: hours_list
        hours_list = hours
        self.addCleanup(lambda: setattr(weather, "forward", saved))

    def test_it_explains_itself_when_it_cannot_speak(self):
        """A fresh install runs this and must not see silence or a traceback."""
        self.stub_forward([])
        code, said = self.run_cli("--forecast")
        self.assertEqual(0, code, said)
        self.assertIn("No outlook yet", said)

    def test_it_speaks_once_skill_is_measured(self):
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        self.stub_forward([{"observed_utc": self.hour(-1, 20),
                            "wind_speed_ms": 0.2, "temperature_c": 7.0}])
        code, said = self.run_cli("--forecast")
        self.assertEqual(0, code, said)
        self.assertIn("likely", said.lower())

    def test_it_writes_down_what_it_promised(self):
        """So it can be scored later. A forecast nobody records can never earn
        skill, and the gate would stay shut forever."""
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        self.stub_forward([{"observed_utc": self.hour(-1, 20),
                            "wind_speed_ms": 0.2, "temperature_c": 7.0}])
        self.run_cli("--forecast")
        pending = Path(self.tmp.name) / "pending.json"
        self.assertTrue(pending.exists(), "the prediction was not recorded")
        self.assertEqual(1, len(json.loads(
            pending.read_text(encoding="utf-8"))))

    def test_it_publishes_its_accuracy(self):
        self.history()
        self.verified(forecast.MIN_VERIFIED)
        self.stub_forward([{"observed_utc": self.hour(-1, 20),
                            "wind_speed_ms": 0.2, "temperature_c": 7.0}])
        _, said = self.run_cli("--forecast")
        self.assertIn("µg/m³", said)

    def test_a_weather_service_that_is_down_does_not_crash_it(self):
        """Rule: a weather service being down must never cost a reading, and
        it must not cost the command either."""
        import weather
        saved = weather.forward
        weather.forward = lambda *a, **kw: (_ for _ in ()).throw(
            weather.WeatherUnavailable("the service is down"))
        self.addCleanup(lambda: setattr(weather, "forward", saved))
        code, said = self.run_cli("--forecast")
        self.assertEqual(0, code, said)
        self.assertIn("could not fetch", said.lower())

    def test_it_verifies_what_has_since_happened(self):
        """Every run scores anything now measurable, so the ledger the gate
        depends on keeps up without a separate command to remember."""
        self.history()
        when = self.hour(1, 10)
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 11.0}])
        pending = Path(self.tmp.name) / "pending.json"
        forecast.remember(pending, when=when, predicted=12.0, persistence=8.0)
        self.stub_forward([])
        _, said = self.run_cli("--forecast")
        self.assertIn("verified 1", said)
        self.assertEqual(1, len(forecast.Skill(self.skill_path).records))


class TestTheEdgesOfTheBandAndTheLedger(OutlookCase):
    def test_a_missing_wind_reading_belongs_to_no_band(self):
        """Not to `calm`. An hour with no anemometer reading is unknown, and
        rule 5a's shape says unknown is not the same as zero — calling it calm
        would train the model on weather nobody measured."""
        self.assertIsNone(forecast._band_for(None))

    def test_every_real_wind_speed_lands_in_a_band(self):
        """Enumerated from analyse.WIND_BANDS, so adding a band cannot leave a
        hole that only shows up as a forecast that quietly never fires."""
        import analyse
        edges = [b[0] for b in analyse.WIND_BANDS]
        edges += [b[1] for b in analyse.WIND_BANDS if b[1] is not None]
        for edge in edges:
            for wind in (edge, edge + 0.001, edge + 5.0):
                self.assertIsNotNone(
                    forecast._band_for(wind),
                    f"{wind} m/s falls between the bands")

    def test_a_naive_timestamp_in_the_ledger_is_still_scored(self):
        """Read as UTC, matching store.canonical_utc. Treated as local instead,
        an Australian install would read every past prediction as up to eleven
        hours in the future and defer it forever — the ledger would grow, skill
        would never be measured, and the forecast would stay silent with no
        way to tell that from having nothing to say."""
        when = self.hour(3, 10)          # in the past, so it is scoreable
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 9.0}])
        pending = Path(self.tmp.name) / "pending.json"
        pending.write_text(json.dumps([{
            "when": when.replace("Z", "").replace("+00:00", ""),
            "predicted": 10.0, "persistence": 6.0}]), encoding="utf-8")

        scored = forecast.verify_pending(self.conn, pending, self.skill_path)

        self.assertEqual(1, scored, "a naive timestamp was never scored")
        self.assertEqual([], json.loads(pending.read_text(encoding="utf-8")),
                         "it stayed in the ledger after being scored")


class TestTheLedgerHoldsOneTimestampFormat(OutlookCase):
    """Section 1's shape, applied to the prediction ledger. `readings` had two
    timestamp forms and dedup broke across the boundary; this file is the same
    risk in a different place, and it decides whether the forecast is ever
    allowed to speak."""

    def test_a_promise_is_written_in_canonical_form(self):
        pending = Path(self.tmp.name) / "pending.json"
        forecast.remember(pending, when="2026-08-09T10:00:00Z",
                          predicted=10.0, persistence=6.0)
        held = json.loads(pending.read_text(encoding="utf-8"))[0]["when"]
        self.assertEqual(store.canonical_utc(held), held,
                         f"the ledger holds a non-canonical timestamp: {held}")

    def test_one_hour_written_two_ways_is_promised_once(self):
        """`Z` and `+00:00` are the same instant. Two entries would score the
        hour twice, and skill is the number the silence gate turns on."""
        pending = Path(self.tmp.name) / "pending.json"
        forecast.remember(pending, when="2026-08-09T10:00:00Z",
                          predicted=10.0, persistence=6.0)
        forecast.remember(pending, when="2026-08-09T10:00:00+00:00",
                          predicted=99.0, persistence=1.0)
        self.assertEqual(
            1, len(json.loads(pending.read_text(encoding="utf-8"))),
            "one hour was promised twice under two spellings")

    def test_a_promise_recorded_with_an_offset_is_still_scored(self):
        """The hour key and the has-it-happened test must come off the same
        parse. Slicing the raw string asked for hour 10 when the reading was
        stored under hour 00, so the entry sat pending forever."""
        when = self.hour(3, 0)                    # canonical, hour 00 UTC
        store.insert_readings(self.conn, self.sid,
                              [{"observed_utc": when, "pm25": 9.0}])
        offset_form = when.replace("T00:00:00+00:00", "T10:00:00+10:00")
        self.assertNotEqual(when, offset_form, "the fixture proves nothing")

        pending = Path(self.tmp.name) / "pending.json"
        pending.write_text(json.dumps([{"when": offset_form, "predicted": 10.0,
                                        "persistence": 6.0}]), encoding="utf-8")

        scored = forecast.verify_pending(self.conn, pending, self.skill_path)

        self.assertEqual(1, scored, "an offset timestamp was never scored")
        self.assertEqual(9.0, forecast.Skill(self.skill_path).records[0]["actual"])

    def test_a_ledger_left_by_an_older_version_is_not_promised_twice(self):
        """The pending file survives an upgrade. If it holds `...Z` and the new
        writer produces `...+00:00`, a raw string dedup sees two different
        hours and promises both -- so the hour is scored twice and skill,
        which decides whether the forecast may speak at all, is inflated by
        exactly the rows an upgrade happened to straddle."""
        pending = Path(self.tmp.name) / "pending.json"
        old = "2026-08-09T10:00:00Z"
        pending.write_text(json.dumps([{"when": old, "predicted": 10.0,
                                        "persistence": 6.0}]), encoding="utf-8")
        self.assertNotEqual(store.canonical_utc(old), old,
                            "the fixture is already canonical; it proves nothing")

        forecast.remember(pending, when="2026-08-09T10:00:00+00:00",
                          predicted=99.0, persistence=1.0)

        self.assertEqual(
            1, len(json.loads(pending.read_text(encoding="utf-8"))),
            "an upgrade turned one promised hour into two")
