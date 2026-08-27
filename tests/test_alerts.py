# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Notifications, and the quiet hours that suppress them.

Untested until now, which is uncomfortable for the one feature that wakes
someone at 3am. The failure modes are asymmetric: an alert that does not fire
loses information the user asked for, while an alert that fires during quiet
hours loses their trust in the whole feature and gets notifications turned off
entirely — so the suppression has to be exactly right at the boundaries.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fusion  # noqa: E402
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



def at(hour, minute=30):
    return datetime(2026, 8, 1, hour, minute)


class TestQuietHours(unittest.TestCase):

    def cfg(self, quiet):
        return {"alerts": {"enabled": True, "quiet_hours": quiet}}

    def test_a_same_day_window_suppresses_only_inside_itself(self):
        c = self.cfg([1, 7])
        for h in (1, 3, 6):
            self.assertTrue(poller._in_quiet_hours(c, at(h)), f"{h}:30 not quiet")
        for h in (0, 7, 12, 23):
            self.assertFalse(poller._in_quiet_hours(c, at(h)), f"{h}:30 wrongly quiet")

    def test_a_window_that_wraps_midnight_works(self):
        """22:00-07:00 is the obvious setting and the one that breaks a naive
        start <= h < end comparison — it would suppress nothing at all."""
        c = self.cfg([22, 7])
        for h in (22, 23, 0, 3, 6):
            self.assertTrue(poller._in_quiet_hours(c, at(h)), f"{h}:30 not quiet")
        for h in (21, 7, 12):
            self.assertFalse(poller._in_quiet_hours(c, at(h)), f"{h}:30 wrongly quiet")

    def test_the_start_hour_is_inclusive_and_the_end_exclusive(self):
        """Otherwise a window is an hour longer or shorter than it reads, which
        nobody notices until an alert arrives at 07:00 sharp."""
        c = self.cfg([22, 7])
        self.assertTrue(poller._in_quiet_hours(c, at(22, 0)))
        self.assertFalse(poller._in_quiet_hours(c, at(21, 59)))
        self.assertTrue(poller._in_quiet_hours(c, at(6, 59)))
        self.assertFalse(poller._in_quiet_hours(c, at(7, 0)))

    def test_no_quiet_hours_means_never_suppressed(self):
        for value in (None, [], [1]):
            c = self.cfg(value)
            self.assertFalse(poller._in_quiet_hours(c, at(3)),
                             f"{value!r} suppressed an alert")

    def test_a_missing_alerts_block_does_not_crash(self):
        self.assertFalse(poller._in_quiet_hours({}, at(3)))

    def test_an_all_day_window_suppresses_everything(self):
        """[0, 0] reads as 'always quiet' to a user setting it. It must not
        silently mean 'never quiet'."""
        c = self.cfg([0, 24])
        for h in (0, 12, 23):
            self.assertTrue(poller._in_quiet_hours(c, at(h)))


class TestSuppressionKeepsTheStateHonest(unittest.TestCase):
    """Suppressing the notification must not suppress the bookkeeping. If the
    band change is not recorded, the next crossing is not detected and the
    user gets no alert when quiet hours end either."""

    def test_state_is_written_even_when_suppressed(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("if _in_quiet_hours(cfg, now):")
        block = src[i:i + 700]
        self.assertIn("alert suppressed", block)
        self.assertIn("write_json_atomic(ALERT_STATE_PATH", block,
                      "suppression skips the state write, so the next "
                      "crossing will not be detected")

    def test_suppression_is_logged_not_silent(self):
        """A user wondering why they heard nothing needs somewhere to look."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("if _in_quiet_hours(cfg, now):")
        self.assertIn("log(", src[i:i + 300])

    def test_a_test_alert_ignores_quiet_hours(self):
        """Someone checking that notifications work at 2am must not be told
        nothing is wrong by silence."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("if args.test_alert:")
        block = src[i:i + 500]
        self.assertNotIn("_in_quiet_hours", block)
        self.assertIn("notify(", block)


class TestTheAlertItselfActuallyFires(unittest.TestCase):
    """maybe_alert() decides whether anyone is told. Nothing ran it.

    The tests above this one check `_in_quiet_hours` behaviourally and then
    check *the source text* of maybe_alert for the right strings. Source text
    is not behaviour: it survives any change that keeps the words and alters
    the logic, which is the change most likely to be made by accident.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (poller.ALERT_STATE_PATH, poller.notify, poller.log)
        poller.ALERT_STATE_PATH = Path(self.tmp.name) / "alert_state.json"
        self.sent = []
        poller.notify = lambda *a, **kw: (self.sent.append((a, kw)) or True)
        self.logged = []
        poller.log = self.logged.append

    def tearDown(self):
        (poller.ALERT_STATE_PATH, poller.notify, poller.log) = self._saved
        self.tmp.cleanup()

    def cfg(self, **alerts):
        base = {"enabled": True, "threshold_aqi": 67, "rising_delta": 12,
                "cooldown_minutes": 60, "notify_when_clear": True}
        base.update(alerts)
        return {"alerts": base, "aqi_scale": "au"}

    def reading(self, aqi, **over):
        r = {"aqi": aqi, "band": "Fair", "pm25_10min": 20.0,
             "averages": {"10min": 20.0, "60min": 19.0}}
        r.update(over)
        return r

    def test_crossing_the_threshold_notifies(self):
        out = poller.maybe_alert(self.reading(80), self.cfg())
        self.assertIsNotNone(out, "nobody was told the air had worsened")
        self.assertTrue(self.sent)

    def test_staying_below_the_threshold_notifies_nobody(self):
        """The control. Without it every test here could pass because
        maybe_alert notifies unconditionally."""
        self.assertIsNone(poller.maybe_alert(self.reading(20), self.cfg()))
        self.assertFalse(self.sent)

    def test_alerts_switched_off_means_switched_off(self):
        self.assertIsNone(poller.maybe_alert(self.reading(200),
                                             self.cfg(enabled=False)))
        self.assertFalse(self.sent)

    def test_a_reading_with_no_value_alerts_nobody(self):
        """No number is not a low number. Alerting on it would fire on every
        outage; treating it as safe would be worse."""
        self.assertIsNone(poller.maybe_alert(self.reading(None), self.cfg()))

    def test_the_cooldown_stops_a_second_alert_immediately_after(self):
        """Conditions swinging around the threshold must not produce a
        notification per poll — that is how the feature gets switched off.

        Tested with a *clearing* second reading, not another high one. A
        second high reading is already blocked by `was_over`, so a test using
        one passes with the cooldown deleted — which is what the first version
        of this did.
        """
        poller.maybe_alert(self.reading(80), self.cfg())
        first = len(self.sent)
        self.assertEqual(1, first)

        # Low enough to trigger the all-clear, and inside the cooldown.
        poller.maybe_alert(self.reading(20), self.cfg())

        self.assertEqual(first, len(self.sent), "the cooldown did not hold")

    def test_the_all_clear_does_arrive_once_the_cooldown_has_passed(self):
        """The control for the test above: without it, "no second alert" could
        mean the all-clear never works at all."""
        poller.maybe_alert(self.reading(80), self.cfg())
        poller.maybe_alert(self.reading(20), self.cfg(cooldown_minutes=0))
        self.assertEqual(2, len(self.sent), "the all-clear never arrived")

    def test_a_threshold_in_micrograms_wins_over_one_in_index_units(self):
        """threshold_aqi means different air under different national scales.
        The raw threshold is the scale-independent one and must take
        precedence when both are set."""
        low = poller.maybe_alert(self.reading(80),
                                 self.cfg(threshold_aqi=200, threshold_pm25=5.0))
        self.assertIsNotNone(low, "the microgram threshold was ignored")

    def test_quiet_hours_suppress_the_notification_but_not_the_bookkeeping(self):
        """The failure this guards: suppression skipping the state write, so
        the band change goes unrecorded and the next crossing is not detected
        either — no alert now, and none when quiet hours end."""
        import json
        cfg = self.cfg(quiet_hours=[0, 24])
        out = poller.maybe_alert(self.reading(80), cfg)

        self.assertFalse(self.sent, "an alert fired inside quiet hours")
        self.assertTrue(poller.ALERT_STATE_PATH.exists(),
                        "suppression skipped the state write")
        state = json.loads(poller.ALERT_STATE_PATH.read_text(encoding="utf-8"))
        self.assertTrue(state.get("over_threshold"),
                        "the crossing was not recorded, so the next one will "
                        "not be detected")
        self.assertTrue(any("suppress" in line for line in self.logged),
                        "suppression was silent")


class TestASourceGoingDark(unittest.TestCase):
    """A provider changing its API, revoking a key or simply disappearing
    shows up as a warning in a log file. Meanwhile the record develops a hole
    nobody notices until they go looking for a night that was never
    recorded."""

    def setUp(self):
        self.sent = []
        self._notify, self._log = poller.notify, poller.log
        poller.notify = lambda *a, **kw: (self.sent.append(a) or True)
        self.logged = []
        poller.log = self.logged.append

    def tearDown(self):
        poller.notify, poller.log = self._notify, self._log

    def cfg(self):
        return {"poll_minutes": 15}

    def test_failures_below_the_threshold_do_not_nag(self):
        """One failed poll is a blip. Alerting on it trains the user to ignore
        the alert that matters."""
        state = {}
        for _ in range(3):
            poller.record_source_result(state, "qld/abc", False, 4, self.cfg())
        self.assertFalse(self.sent)
        self.assertEqual(3, state["qld/abc"]["consecutive"])

    def test_crossing_the_threshold_notifies_once_and_only_once(self):
        state = {}
        for _ in range(8):
            poller.record_source_result(state, "qld/abc", False, 4, self.cfg())
        self.assertEqual(1, len(self.sent),
                         "a dark source nagged once per poll")
        self.assertIn("--doctor", self.sent[0][2])

    def test_a_source_that_comes_back_says_so_and_resets(self):
        """Without the recovery message the user is left believing a source is
        still broken, and the counter never resets so the next outage is never
        announced."""
        state = {}
        for _ in range(5):
            poller.record_source_result(state, "qld/abc", False, 4, self.cfg())
        self.sent.clear()

        poller.record_source_result(state, "qld/abc", True, 4, self.cfg())

        self.assertEqual(1, len(self.sent))
        self.assertIn("working again", self.sent[0][1])
        self.assertEqual(0, state["qld/abc"]["consecutive"])
        self.assertFalse(state["qld/abc"]["notified"])

    def test_a_source_that_was_never_broken_is_not_announced_as_recovered(self):
        """The control: a success must not produce a notification of its
        own."""
        state = {}
        poller.record_source_result(state, "qld/abc", True, 4, self.cfg())
        self.assertFalse(self.sent)

    def test_the_next_outage_is_announced_after_a_recovery(self):
        state = {}
        for _ in range(5):
            poller.record_source_result(state, "qld/abc", False, 4, self.cfg())
        poller.record_source_result(state, "qld/abc", True, 4, self.cfg())
        self.sent.clear()

        for _ in range(5):
            poller.record_source_result(state, "qld/abc", False, 4, self.cfg())

        self.assertEqual(1, len(self.sent), "the second outage was never told")


class TestASensorDarkBehindAWorkingProvider(unittest.TestCase):
    """The outage the failure counter cannot see.

    `record_source_result` counts polls that *raised*. PurpleAir does not stop
    answering when a sensor drops off the network — it serves that sensor's
    last known reading, with its original timestamp, indefinitely. So the fetch
    succeeds, the counter resets on every poll, and the detector never fires
    while the record develops exactly the hole its docstring exists to prevent.

    This is not hypothetical. On the maintainer's own install the nearest
    sensor — the headline source — went dark for about two days. Every poll
    in that window logged `0 new` against the same unmoving reading, and
    nothing was ever said. The only visible symptom was blank cells on a
    heatmap, days later.

    What makes it invisible to the existing tests: they drive
    `record_source_result` directly with `ok_now=False`, so they prove the
    counter and the message work. Nothing asserted what `ok_now` is *derived
    from*, which is where the bug was. Fifth instance of that shape here.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "purpleair", "1", "Riverside",
                                       resolution_minutes=10)
        self.provider = poller.PROVIDERS["purpleair"]
        self.cfg = {"poll_minutes": 15}

    def reading(self, minutes_ago):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        store.insert_readings(self.conn, self.sid, [
            {"observed_utc": when.isoformat(timespec="seconds"), "pm25": 5.0}])

    def test_a_fresh_reading_counts_as_reporting(self):
        """The control. Without it the fix could mark everything dark."""
        self.reading(minutes_ago=2)
        self.assertTrue(poller.source_is_reporting(
            self.conn, self.sid, self.provider, self.cfg))

    def test_a_reading_that_stopped_hours_ago_is_not_reporting(self):
        """The production case: the provider answers, the sensor does not."""
        self.reading(minutes_ago=8 * 60)
        self.assertFalse(
            poller.source_is_reporting(self.conn, self.sid, self.provider,
                                       self.cfg),
            "a sensor silent for eight hours is still counted as reporting, "
            "so nobody is told it died")

    def test_a_source_with_no_readings_at_all_is_not_reporting(self):
        self.assertFalse(poller.source_is_reporting(
            self.conn, self.sid, self.provider, self.cfg))

    def test_the_window_scales_with_the_provider_cadence(self):
        """Twenty minutes of silence is an outage for a ten-minute consumer
        sensor and completely normal for an hourly regulatory feed. A fixed
        window would either nag about the second or miss the first."""
        self.reading(minutes_ago=40)
        hourly = poller.PROVIDERS["qld"]

        self.assertFalse(
            poller.source_is_reporting(self.conn, self.sid, self.provider,
                                       self.cfg),
            "forty minutes of silence from a ten-minute sensor is an outage")
        self.assertTrue(
            poller.source_is_reporting(self.conn, self.sid, hourly, self.cfg),
            "forty minutes is normal for an hourly feed and must not alert")


class TestTheMessageSaysWhichKindOfSilence(unittest.TestCase):
    """"Your network is down" and "your sensor is dead" call for opposite
    responses, and the user cannot act on the wrong one."""

    def setUp(self):
        self.sent = []
        self._notify, self._log = poller.notify, poller.log
        poller.notify = lambda *a, **kw: (self.sent.append(a) or True)
        poller.log = lambda *a, **kw: None

    def tearDown(self):
        poller.notify, poller.log = self._notify, self._log

    def dark(self, reason):
        state = {}
        for _ in range(5):
            poller.record_source_result(state, "purpleair/1", False, 4,
                                        {"poll_minutes": 15}, reason=reason)
        return self.sent[0]

    def test_an_unreachable_provider_says_so(self):
        self.assertIn("stopped responding", self.dark("unreachable")[1])

    def test_a_stale_sensor_says_the_provider_is_still_answering(self):
        """Otherwise the user checks their network, their key and their
        internet, and none of them is the problem."""
        message = " ".join(str(p) for p in self.dark("stale"))

        self.assertIn("still answering", message,
                      "a dark sensor is reported as an unreachable provider, "
                      "sending the user after the wrong fault")

    def test_the_default_reason_is_the_old_behaviour(self):
        """Called without a reason, from anywhere not yet updated, it must
        still say something true rather than nothing."""
        state = {}
        for _ in range(5):
            poller.record_source_result(state, "qld/abc", False, 4,
                                        {"poll_minutes": 15})
        self.assertTrue(self.sent)




class TestDangerousAirStillReachesTheAlert(unittest.TestCase):
    """The path from a 900 µg/m³ reading to somebody's screen.

    Every step of it used to stop at the first: assess_quality called anything
    over 350 a sensor fault, fusion drops faults before choosing a headline,
    and maybe_alert only sees what fusion chose. So the alert was guaranteed
    to stay silent in precisely the conditions it exists for, and nothing
    tested it because each layer was doing exactly what it said.

    These run the layers together, because the failure lived between them.
    """

    def readings(self, quality):
        # A real observation time: annotate() derives staleness from it, and
        # a reading with none is stale by default -- which would drop the row
        # for a reason that has nothing to do with the verdict under test.
        from datetime import timedelta, timezone
        now = datetime.now(timezone.utc)
        return [{"source_id": 1, "site_id": "1", "provider": "purpleair",
                 "observed_utc": (now - timedelta(minutes=2)).isoformat(),
                 "pm25": 900.0, "quality": quality,
                 "latitude": -33.5, "longitude": 151.0,
                 "averages": {"10min": 900.0, "60min": 900.0}}]

    def test_extreme_air_survives_fusion_and_becomes_the_headline(self):
        out = fusion.fuse(self.readings("extreme"), "nearest",
                          {"latitude": -33.5, "longitude": 151.0})
        self.assertEqual(900.0, out.get("pm25"),
                         "the worst reading was dropped before anyone saw it")

    def test_a_broken_instrument_does_not(self):
        out = fusion.fuse(self.readings("suspect"), "nearest",
                          {"latitude": -33.5, "longitude": 151.0})
        self.assertIsNone(out.get("pm25"),
                          "a faulty sensor was promoted to the headline")

    def test_the_verdict_travels_with_the_reading(self):
        """A surface cannot mark what it is not told."""
        out = fusion.fuse(self.readings("extreme"), "nearest",
                          {"latitude": -33.5, "longitude": 151.0})
        qualities = [s.get("quality") for s in (out.get("sources") or [])]
        self.assertIn("extreme", qualities)


class TestExtremeAirActuallyNotifies(TestTheAlertItselfActuallyFires):
    """Inherits the harness above, which stubs notify() and the state file.

    Kept separate from the fusion tests on purpose: those prove the reading
    reaches the headline, and this proves the headline reaches a person. Both
    were assumed and neither was checked.
    """

    def test_an_extreme_reading_notifies(self):
        out = poller.maybe_alert(
            self.reading(3600, band="Hazardous", pm25_10min=900.0,
                         quality="extreme"), self.cfg())
        self.assertIsNotNone(out, "nobody was told the air was dangerous")
        self.assertTrue(self.sent)

    def test_the_message_carries_the_concentration(self):
        poller.maybe_alert(
            self.reading(3600, band="Hazardous", pm25_10min=900.0,
                         quality="extreme"), self.cfg())
        said = " ".join(str(a) for args, _ in self.sent for a in args)
        self.assertIn("900", said,
                      "the alert did not say how bad the air actually was")


class TestExtremeAirIsStillCheckedAgainstItsNeighbours(unittest.TestCase):
    """Counting a reading is not the same as trusting it.

    Quality answers "is this instrument working". Corroboration answers "does
    anything else see this". Both have to run: with extreme air now reaching
    the headline and the alert, corroboration is the only thing left standing
    between a lone sensor reading 900 and a notification saying the suburb is
    hazardous. If it skipped these readings the dashboard's promise -- that a
    value no neighbour can confirm is marked -- would be untrue exactly when
    it is load-bearing.
    """

    def two_sources(self, headline_quality="extreme"):
        from datetime import timedelta, timezone
        now = datetime.now(timezone.utc)
        seen = (now - timedelta(minutes=2)).isoformat()
        return [
            {"source_id": 1, "site_id": "1", "provider": "purpleair",
             "observed_utc": seen, "pm25": 900.0, "quality": headline_quality,
             "latitude": -33.5, "longitude": 151.0},
            {"source_id": 2, "site_id": "2", "provider": "qld",
             "observed_utc": seen, "pm25": 10.0, "quality": "ok",
             "latitude": -33.51, "longitude": 151.01},
        ]

    def verdicts(self, readings):
        out = fusion.fuse(readings, "nearest",
                          {"latitude": -33.5, "longitude": 151.0})
        return {s["site_id"]: s.get("corroboration")
                for s in (out.get("sources") or [])}

    def test_a_lone_extreme_reading_is_marked_unconfirmed(self):
        self.assertEqual("uncorroborated", self.verdicts(self.two_sources())["1"])

    def test_it_is_not_marked_when_a_neighbour_agrees(self):
        both = self.two_sources()
        both[1]["pm25"] = 850.0
        self.assertNotEqual("uncorroborated", self.verdicts(both)["1"])


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


class TestVentilationAdviceNeverFightsTheAir(unittest.TestCase):
    """"Open up and ventilate" during a pollution episode.

    The dashboard's window panel was a pure function of the clock. On a
    morning during a local smoke event it said "Cleanest part of the day —
    good window to open up and ventilate" while the headline beside it sat
    well into the third band and rising sharply. It never looked at the air.

    That is the register's own named risk — telling somebody to ventilate
    during a smoke event — arriving on a second surface that had none of the
    protection built for the first. It is also rule 7: the advice was being
    decided in a renderer, where a second copy of a health decision drifts
    from the one that was tested.

    The rule the tests below encode: **the air comes first, the clock
    second.** A time of day is a prior about when air is usually clean. A
    reading is evidence about whether it is clean now, and evidence wins.
    """

    def advice(self, hour, index, direction="steady"):
        return poller.ventilation_advice(
            hour=hour, index=index, scale_name="au",
            trend={"direction": direction})

    # ---- the failure that prompted this -------------------------------

    def test_it_does_not_say_ventilate_during_an_episode(self):
        """Mid-morning, well into the third band, rising sharply."""
        got = self.advice(hour=10.2, index=99.0, direction="rising_fast")

        self.assertNotIn("ventilate", got["advice"].lower(),
                         "told to open the windows during a smoke event")
        self.assertNotIn("open up", got["advice"].lower())

    def test_it_says_to_keep_closed_instead(self):
        """Silence would be safe but useless. The reader is standing at the
        window deciding."""
        got = self.advice(hour=10.2, index=99.0, direction="rising_fast")

        self.assertIn("clos", got["advice"].lower(),
                      f"no actionable advice given: {got['advice']!r}")

    def test_it_says_why_the_usual_advice_is_withheld(self):
        """Somebody who knows mornings are normally the clean window needs to
        know why today is not, or they will open the window anyway."""
        got = self.advice(hour=10.2, index=99.0, direction="rising_fast")

        self.assertTrue(got.get("why"), "no reason given for the change")

    # ---- and the control, which matters just as much ------------------

    def test_clean_air_in_the_morning_still_says_ventilate(self):
        """The advice is genuinely useful on an ordinary day, and a panel that
        never recommends anything is one nobody reads."""
        got = self.advice(hour=10.2, index=12.0)

        self.assertIn("ventilate", got["advice"].lower(),
                      "the ordinary case lost its advice")

    def test_clean_but_climbing_fast_does_not_invite_the_window_open(self):
        """Low now and rising is how an episode starts. Indoor lags outdoor,
        so an open window at the start of one is the worst timing available."""
        got = self.advice(hour=10.2, index=20.0, direction="rising_fast")

        self.assertNotIn("ventilate", got["advice"].lower())

    def test_the_evening_risk_window_is_unchanged_when_air_is_clean(self):
        """The time-of-day finding this project was built on still stands: the
        evening advice is about what is coming, not about what is measured."""
        got = self.advice(hour=17.5, index=10.0)

        self.assertIn("purifier", got["advice"].lower() + got["headline"].lower(),
                      f"the risk-window advice is gone: {got}")

    def test_an_unknown_reading_does_not_invent_confidence(self):
        """No reading is not a clean reading. The safe direction is to stop
        recommending an open window, not to assume the best."""
        got = self.advice(hour=10.2, index=None)

        self.assertNotIn("ventilate", got["advice"].lower(),
                         "recommended ventilating with no reading at all")

    def test_every_hour_of_the_day_produces_advice(self):
        """A gap in the hour ranges would render an empty panel, which reads
        as a broken page rather than as no advice."""
        for tenth in range(0, 240):
            hour = tenth / 10.0
            got = self.advice(hour=hour, index=10.0)
            self.assertTrue(got["headline"], f"no headline at {hour:.1f}")
            self.assertTrue(got["advice"], f"no advice at {hour:.1f}")

    def test_dirty_air_withholds_the_window_at_every_hour(self):
        """The rule is not "except in the morning". It is the air first."""
        for tenth in range(0, 240):
            hour = tenth / 10.0
            got = self.advice(hour=hour, index=99.0)
            self.assertNotIn(
                "ventilate", got["advice"].lower(),
                f"ventilating recommended at {hour:.1f} with AQI 99")


class TestTheEveningWindowWordingSurvivedTheMove(unittest.TestCase):
    """The risk-window advice moved from the page into Python. These are the
    assertions the page-level tests used to make, kept at the new address so
    the move did not quietly cost the coverage.

    The hours encode this project's originating finding — the climb from 5pm,
    the peak at 7-8pm — and they are advice about what is *coming*, so they
    stand whatever the current reading is.
    """

    def at(self, hour, index=10.0):
        return poller.ventilation_advice(hour=hour, index=index,
                                         scale_name="au",
                                         trend={"direction": "steady"})

    def test_before_the_window_it_says_to_prepare(self):
        self.assertIn("Have the house shut", self.at(15.5)["advice"])

    def test_inside_the_close_up_window_it_says_to_act_now(self):
        self.assertIn("Shut up", self.at(16.75)["advice"])

    def test_during_the_risk_window_it_says_to_keep_purifiers_running(self):
        self.assertIn("purifiers running", self.at(19.0)["advice"])

    def test_the_small_hours_are_still_inside_the_window(self):
        """It ends at 1am, so 00:30 must not fall through into "overnight".
        A wrapped window that leaves a gap renders an empty panel."""
        self.assertIn("purifiers running", self.at(0.5)["advice"])

    def test_the_advice_changes_across_the_boundary(self):
        """The whole value of the panel. Reading the same at 15:30 and 16:45
        would make it decoration."""
        self.assertNotEqual(self.at(15.5)["advice"], self.at(16.75)["advice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
