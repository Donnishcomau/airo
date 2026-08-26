# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The user's timezone, rather than the machine's.

`config.json` has carried a `location.timezone` field since the first version.
Nothing ever read it. Six places resolved local time independently, each by
calling `.astimezone()` with no argument, which means *the machine's* zone --
and the machine is not always where the user is:

  * a NAS, a Raspberry Pi or a home server is very often left on UTC
  * a laptop carried across a border reports the zone it woke up in
  * a VM inherits its host's

Two of the six decide health-relevant behaviour. Quiet hours of 22:00-07:00 on
a UTC machine in Brisbane suppress alerts from 08:00 to 17:00 -- daytime -- and
notify at 3am. The evening window that the whole project is built around moves
by the same amount, so "is it worse after sunset" is asked about the wrong ten
hours.

There is one honest gap and it is stated rather than hidden: `zoneinfo` needs a
system timezone database, and Windows has none without a package. A runtime
dependency is out of the question (rule 1), so there the configured zone cannot
be applied and the machine's is used instead. That degradation is *reported* --
by `--doctor`, and in the log -- rather than left to be discovered.
"""

import sys
import unittest
import unittest.mock
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


# A synthetic reference point. Nothing here describes where anyone lives
# (rule 2b), and no test below needs a real coordinate: they assert
# rounding and zone plumbing, not geography.
HOME_LAT, HOME_LON = -33.5000, 151.0000

BRISBANE = "Australia/Brisbane"     # UTC+10, no DST
LOS_ANGELES = "America/Los_Angeles"  # DST
KOLKATA = "Asia/Kolkata"            # UTC+5:30


def zoneinfo_works(name=BRISBANE):
    """Does this platform have a timezone database at all?"""
    tz, _ = poller.resolve_zone(name)
    return tz is not None


class TestResolvingAZone(unittest.TestCase):

    def test_no_configured_zone_means_the_machines_own(self):
        tz, note = poller.resolve_zone("")
        self.assertIsNone(tz)
        self.assertIn("machine", note.lower())

    def test_a_configured_zone_resolves_where_there_is_a_database(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")
        tz, note = poller.resolve_zone(BRISBANE)
        self.assertIsNotNone(tz)
        self.assertEqual("", note, "a working resolution should say nothing")

    def test_a_zone_that_does_not_exist_never_raises(self):
        """A typo in a config file must not stop the poller. It records air
        quality; it can record it in the machine's zone and complain."""
        tz, note = poller.resolve_zone("Mars/Olympus_Mons")
        self.assertIsNone(tz)
        self.assertTrue(note, "it failed silently")
        self.assertIn("Mars/Olympus_Mons", note,
                      "the note does not say which zone failed")

    def test_the_note_explains_windows_rather_than_blaming_the_user(self):
        """The most likely reader of this message is on Windows with a
        perfectly correct config, and the honest answer is that we cannot
        apply it without taking a dependency this project will not take."""
        tz, note = poller.resolve_zone("Mars/Olympus_Mons")
        self.assertIn("machine", note.lower())

    def test_resolution_is_cached_not_reread_every_poll(self):
        a, _ = poller.resolve_zone(BRISBANE)
        b, _ = poller.resolve_zone(BRISBANE)
        self.assertIs(a, b)


class TestTheConfiguredZoneIsWhatGetsUsed(unittest.TestCase):

    def setUp(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")

    def cfg(self, name):
        return {"location": {"latitude": HOME_LAT, "longitude": HOME_LON,
                             "timezone": name}}

    def test_local_now_is_in_the_configured_zone(self):
        got = poller.local_now(self.cfg(BRISBANE))
        self.assertEqual(timedelta(hours=10), got.utcoffset())

    def test_a_different_zone_gives_a_different_wall_clock(self):
        bne = poller.local_now(self.cfg(BRISBANE))
        lax = poller.local_now(self.cfg(LOS_ANGELES))
        self.assertNotEqual(bne.utcoffset(), lax.utcoffset())
        # Same instant, different clocks.
        self.assertLess(abs((bne - lax).total_seconds()), 5)

    def test_a_half_hour_offset_is_honoured(self):
        got = poller.local_now(self.cfg(KOLKATA))
        self.assertEqual(timedelta(hours=5, minutes=30), got.utcoffset())

    def test_no_configuration_falls_back_to_the_machine(self):
        got = poller.local_now({})
        self.assertEqual(datetime.now().astimezone().utcoffset(),
                         got.utcoffset())


class TestQuietHoursBelongToTheUserNotTheServer(unittest.TestCase):
    """The failure that gets the whole feature switched off.

    A Pi left on UTC in Brisbane, quiet hours 22:00-07:00: the suppression runs
    against UTC, so it silences 08:00 to 17:00 local -- the middle of the day,
    when someone would want to know -- and notifies at 3am, which is how a
    person ends up disabling alerts entirely.
    """

    def setUp(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")

    def cfg(self, tz_name):
        return {"location": {"timezone": tz_name},
                "alerts": {"enabled": True, "quiet_hours": [22, 7]}}

    def at_utc(self, hour):
        return datetime(2026, 7, 15, hour, 0, tzinfo=timezone.utc)

    def test_a_utc_machine_still_suppresses_the_users_night(self):
        # 16:00 UTC is 02:00 in Brisbane: the middle of the quiet window.
        self.assertTrue(
            poller._in_quiet_hours(self.cfg(BRISBANE), self.at_utc(16)),
            "an alert fired at 2am local")

    def test_and_still_notifies_during_the_users_day(self):
        # 02:00 UTC is 12:00 in Brisbane: noon, and nothing to suppress.
        self.assertFalse(
            poller._in_quiet_hours(self.cfg(BRISBANE), self.at_utc(2)),
            "the middle of the day was treated as quiet hours")

    def test_the_machines_zone_is_not_consulted_when_one_is_configured(self):
        """Same instant, two configured zones, two different answers -- which
        can only be true if the configuration is what decided."""
        instant = self.at_utc(16)
        self.assertTrue(poller._in_quiet_hours(self.cfg(BRISBANE), instant))
        self.assertFalse(poller._in_quiet_hours(self.cfg(KOLKATA), instant),
                         "21:30 in Kolkata is before a 22:00 quiet window")


class TestTheEveningWindowIsTheUsersEvening(unittest.TestCase):
    """The window the entire project is built around. On a UTC machine in
    Brisbane, "after sunset" is asked about 01:00-11:00 local."""

    def setUp(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")

    def cfg(self, tz_name):
        return {"location": {"timezone": tz_name},
                "risk_window": {"enabled": True,
                                "start_hour": 15, "end_hour": 1}}

    def hint(self, tz_name, utc_hour):
        return poller.compute_time_hint(
            self.cfg(tz_name),
            now=datetime(2026, 7, 15, utc_hour, 0, tzinfo=timezone.utc))

    def test_local_evening_counts_as_the_risk_window(self):
        # 09:00 UTC is 19:00 in Brisbane: inside 15:00-01:00.
        self.assertEqual("active", self.hint(BRISBANE, 9)["state"])

    def test_local_morning_does_not(self):
        # 23:00 UTC is 09:00 next day in Brisbane: nowhere near it.
        self.assertEqual("clear", self.hint(BRISBANE, 23)["state"])

    def test_the_same_instant_differs_by_configured_zone(self):
        """Only the configuration can explain two answers for one instant."""
        self.assertEqual("active", self.hint(BRISBANE, 9)["state"])
        # 09:00 UTC is 14:30 in Kolkata -- half an hour before the window.
        self.assertEqual("approaching", self.hint(KOLKATA, 9)["state"])


class TestTheLocalHourGoesThroughTheOneResolver(unittest.TestCase):
    """`_local_hour()` re-resolved the zone by hand, beside `resolve_zone()`.

    Its own `from zoneinfo import ZoneInfo` behind a bare `except Exception`,
    which is the shape CONVENTIONS warns about twice over: a silent fallback,
    and a platform fallback that returns something plausible. A configured
    zone that cannot be loaded is not exotic — Windows ships no timezone
    database at all, and the project takes no dependency to supply one — so on
    that platform this quietly used the machine's clock while every other
    caller got `resolve_zone()`'s explanatory note.

    Same answers, one implementation, and the degradation is now reported
    where the rest of it is.
    """

    def setUp(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")

    def at(self, tz_name, utc_hour, utc_minute=0):
        return poller._local_hour(
            {"location": {"timezone": tz_name}},
            now=datetime(2026, 7, 15, utc_hour, utc_minute,
                         tzinfo=timezone.utc))

    def test_it_reads_the_configured_zone(self):
        # 09:00 UTC is 19:00 in Brisbane.
        self.assertEqual(19.0, self.at(BRISBANE, 9))

    def test_the_same_instant_differs_by_configured_zone(self):
        """The property the function exists for: only the configuration can
        explain two answers for one instant."""
        self.assertEqual(19.0, self.at(BRISBANE, 9))
        self.assertEqual(14.5, self.at(KOLKATA, 9))

    def test_minutes_survive_as_a_fraction(self):
        """The risk window compares against a fractional hour; rounding to the
        hour here would move its edges by up to thirty minutes."""
        self.assertAlmostEqual(19.5, self.at(BRISBANE, 9, 30))

    def test_an_unloadable_zone_falls_back_without_raising(self):
        """It must still answer — a poll that cannot name the hour is worth
        more than no poll — but through resolve_zone(), which says so."""
        got = self.at("Mars/Olympus_Mons", 9)
        self.assertIsInstance(got, float)
        self.assertTrue(0 <= got < 24)
        self.assertIn("Mars/Olympus_Mons", " ".join(poller.timezone_report(
            {"location": {"timezone": "Mars/Olympus_Mons"}})))

    def test_no_configured_zone_uses_the_machines(self):
        got = poller._local_hour(
            {}, now=datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc))
        expected = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc).astimezone()
        self.assertAlmostEqual(expected.hour + expected.minute / 60.0, got)

    def test_it_agrees_with_local_now(self):
        """The canonical resolver is the definition; this is a view of it."""
        cfg = {"location": {"timezone": BRISBANE}}
        now = datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)
        here = poller.local_now(cfg, now)
        self.assertAlmostEqual(here.hour + here.minute / 60.0,
                               poller._local_hour(cfg, now=now))

    def test_it_no_longer_imports_zoneinfo_itself(self):
        import inspect
        src = inspect.getsource(poller._local_hour)
        self.assertNotIn("ZoneInfo", src,
                         "_local_hour resolves the zone by hand again")
        self.assertIn("local_now", src)


class TestTheDegradationIsReported(unittest.TestCase):
    """Rule: a fallback nobody is told about is a bug that looks like a
    feature. If the configured zone cannot be applied, --doctor says so."""

    def test_doctor_reports_the_zone_in_force(self):
        lines = poller.timezone_report(
            {"location": {"timezone": BRISBANE}})
        self.assertTrue(any(BRISBANE in ln for ln in lines))

    def test_doctor_says_when_it_could_not_apply_the_configured_zone(self):
        lines = poller.timezone_report(
            {"location": {"timezone": "Mars/Olympus_Mons"}})
        joined = " ".join(lines)
        self.assertIn("Mars/Olympus_Mons", joined)
        self.assertIn("machine", joined.lower())

    def test_doctor_says_when_none_is_configured_at_all(self):
        lines = poller.timezone_report({})
        self.assertTrue(lines, "it said nothing about an unset timezone")
        self.assertIn("not set", " ".join(lines).lower())

    def test_it_names_the_machine_zone_so_the_two_can_be_compared(self):
        """Knowing the configured zone is not enough to spot the problem; the
        useful report is 'you configured Brisbane, this machine thinks UTC'."""
        lines = poller.timezone_report({"location": {"timezone": BRISBANE}})
        joined = " ".join(lines)
        self.assertIn("machine", joined.lower())




class TestTheEveningAnalysisUsesTheConfiguredZone(unittest.TestCase):
    """`analyse.py evening` buckets by local hour. Run on a server in another
    zone it re-buckets every night in the record, which changes the answer to
    the only question the tool asks."""

    def setUp(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")
        import tempfile
        import store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store.upsert_source(self.conn, "qld", "wbk", "Site")

    def seed_at_utc(self, hours, pm):
        import store
        store.insert_readings(self.conn, self.sid, [
            {"observed_utc": f"2026-07-1{d}T{h:02d}:00:00+00:00", "pm25": pm}
            for d in (4, 5, 6) for h in hours])

    def report(self, tz_name):
        import contextlib
        import io
        import analyse
        cfg = {"aqi_scale": "au", "location": {"timezone": tz_name}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            analyse.evening(self.conn, cfg, 3650)
        return buf.getvalue()

    def test_the_same_readings_bucket_differently_in_different_zones(self):
        """08:00-10:00 UTC is early evening in Brisbane (18:00-20:00) and
        early afternoon in Kolkata (13:30-15:30). Same rows, and the ratio
        must reflect where the user is, not where the query ran."""
        self.seed_at_utc((8, 9, 10), 40.0)      # Brisbane evening
        self.seed_at_utc((23, 0, 1), 10.0)      # Brisbane morning
        bne = self.report(BRISBANE)
        kol = self.report(KOLKATA)
        self.assertNotEqual(bne, kol,
                            "the report ignored the configured timezone")
        self.assertIn("4.00x", bne)


class TestDoctorSurfacesIt(unittest.TestCase):

    def run_doctor_lines(self, cfg):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                poller.run_doctor(cfg)
            except Exception:
                # Doctor probes providers over the network; the timezone block
                # is printed before any of that, so whatever it does after is
                # not this test's business.
                pass
        return buf.getvalue()

    def test_doctor_prints_the_timezone_block(self):
        out = self.run_doctor_lines({"location": {"timezone": BRISBANE},
                                     "sources": []})
        self.assertIn("timezone", out.lower())
        self.assertIn(BRISBANE, out)


class TestLatestJsonCarriesTheUsersClock(unittest.TestCase):
    """`fetched_local` is what the tray and the dashboard show as "as of".

    On a server in another zone it said something that disagreed with the
    clock on the user's wall, which reads as stale data rather than as a
    timezone problem -- the sort of thing that makes somebody stop trusting
    the number next to it.
    """

    def setUp(self):
        if not zoneinfo_works():
            self.skipTest("no timezone database on this platform")
        import tempfile
        import store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        sid = store.upsert_source(self.conn, "qld", "wbk", "Site",
                                  latitude=HOME_LAT, longitude=HOME_LON)
        store.insert_readings(self.conn, sid, [
            {"observed_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"), "pm25": 8.0}])

    def cfg(self, tz_name):
        return {"aqi_scale": "au", "fusion": {"rule": "nearest"},
                "location": {"name": "Somewhere", "latitude": HOME_LAT,
                             "longitude": HOME_LON, "timezone": tz_name}}

    def offset_of(self, tz_name):
        got = poller.build_latest(self.conn, self.cfg(tz_name))["fetched_local"]
        return datetime.fromisoformat(got).utcoffset()

    def test_the_local_stamp_is_in_the_configured_zone(self):
        self.assertEqual(timedelta(hours=10), self.offset_of(BRISBANE))

    def test_a_different_zone_gives_a_different_stamp(self):
        """Two zones, one machine: only the configuration can explain it."""
        self.assertNotEqual(self.offset_of(BRISBANE),
                            self.offset_of(LOS_ANGELES))

    def test_a_half_hour_offset_survives_the_round_trip(self):
        self.assertEqual(timedelta(hours=5, minutes=30),
                         self.offset_of(KOLKATA))

    def test_the_utc_stamp_is_untouched(self):
        """`fetched_utc` is the record and must stay UTC whatever the user's
        zone -- rule 6's habit applied to time: store canonical, derive for
        display."""
        got = poller.build_latest(self.conn, self.cfg(KOLKATA))
        self.assertEqual(timedelta(0),
                         datetime.fromisoformat(got["fetched_utc"]).utcoffset())


class TestAPlatformWithNoZoneDatabaseIsNotAMisconfiguration(unittest.TestCase):
    """CI taught this one, which is the argument for running it on Windows.

    The first version counted any unresolvable zone as a problem for --doctor.
    On Windows *every* name raises ZoneInfoNotFoundError, because CPython ships
    no timezone database there and the package that supplies one would be a
    runtime dependency rule 1 forbids. So a perfectly healthy Windows install
    reported a permanent fault it could do nothing about — and a --doctor that
    always shows a problem is a --doctor nobody reads.

    A typo and a missing database look identical from one lookup. They are told
    apart by trying a zone that exists in every copy of the IANA data: if even
    that fails, the platform has no database and the user's config is not what
    is wrong.
    """

    def test_the_control_zone_resolves_wherever_there_is_a_database(self):
        if not poller.timezone_database_available():
            self.skipTest("this platform has no timezone database")
        tz, note = poller.resolve_zone(poller._KNOWN_ZONE)
        self.assertIsNotNone(tz)

    def test_a_typo_is_a_problem_where_the_platform_could_have_resolved_it(self):
        if not poller.timezone_database_available():
            self.skipTest("this platform has no timezone database")
        joined = " ".join(poller.timezone_report(
            {"location": {"timezone": "Mars/Olympus_Mons"}}))
        self.assertIn("typo", joined.lower())

    @staticmethod
    def as_if_windows():
        """Both halves of what Windows actually does.

        Patching only the availability flag left the zone still resolving on a
        machine that has a database, so the report took its success path and
        the test asserted nothing. On Windows the lookup fails *and* the
        control zone fails; a simulation of one without the other is not a
        simulation of anything.
        """
        return (
            unittest.mock.patch.object(poller, "timezone_database_available",
                                       lambda: False),
            unittest.mock.patch.object(
                poller, "resolve_zone",
                lambda name: (None, "no timezone database entry for "
                                    f"{name} (ZoneInfoNotFoundError)")),
        )

    def test_a_missing_database_is_reported_without_blaming_the_config(self):
        """Simulated, so it runs on the platforms that have a database too --
        otherwise this test only ever executes on the machines that cannot
        report whether it passed."""
        avail, resolve = self.as_if_windows()
        with avail, resolve:
            joined = " ".join(poller.timezone_report(
                {"location": {"timezone": BRISBANE}}))
        self.assertIn("no", joined.lower())
        self.assertIn("timezone database", joined.lower())
        self.assertNotIn("typo", joined.lower(),
                         "it blamed the user for a platform limitation")
        self.assertIn("nothing here is wrong", joined.lower())

    def test_a_typo_counts_against_the_install(self):
        if not poller.timezone_database_available():
            self.skipTest("this platform has no timezone database")
        self.assertTrue(poller.timezone_is_a_problem(
            {"location": {"timezone": "Mars/Olympus_Mons"}}))

    def test_a_missing_database_does_not(self):
        avail, resolve = self.as_if_windows()
        with avail, resolve:
            self.assertFalse(
                poller.timezone_is_a_problem(
                    {"location": {"timezone": BRISBANE}}),
                "a platform limitation counted against the user")

    def test_a_zone_that_resolves_is_never_a_problem(self):
        if not poller.timezone_database_available():
            self.skipTest("this platform has no timezone database")
        self.assertFalse(poller.timezone_is_a_problem(
            {"location": {"timezone": BRISBANE}}))

    def test_an_unset_zone_is_not_counted_against_anyone(self):
        """Reported by the report, but not a fault: plenty of installs sit on
        a machine that is in the right zone already."""
        self.assertFalse(poller.timezone_is_a_problem({}))

    def test_doctor_actually_asks(self):
        """The predicate being right does not mean run_doctor consults it."""
        import inspect
        self.assertIn("timezone_is_a_problem",
                      inspect.getsource(poller.run_doctor))


class TestTheAvailabilityProbeActuallyProbes(unittest.TestCase):
    """A check that cannot fail on the machine running it is not a check.

    `timezone_database_available()` returns True on macOS and Linux whatever it
    does inside, so replacing its body with `return True` left every test in
    this file green -- while on Windows it would put back exactly the bug CI
    had just found. The lookup is made to fail instead, which is a thing every
    platform can do.
    """

    def failing_zoneinfo(self):
        import zoneinfo
        return unittest.mock.patch.object(
            zoneinfo, "ZoneInfo",
            side_effect=zoneinfo.ZoneInfoNotFoundError("no tz database"))

    def test_it_says_no_when_the_lookup_raises(self):
        with self.failing_zoneinfo():
            self.assertFalse(poller.timezone_database_available(),
                             "it answered from something other than a lookup")

    def test_and_the_report_then_stops_blaming_the_config(self):
        """End to end from a failing lookup, so the two halves are shown to
        agree rather than assumed to."""
        poller._ZONE_CACHE.clear()
        self.addCleanup(poller._ZONE_CACHE.clear)
        with self.failing_zoneinfo():
            joined = " ".join(poller.timezone_report(
                {"location": {"timezone": BRISBANE}}))
            problem = poller.timezone_is_a_problem(
                {"location": {"timezone": BRISBANE}})
        self.assertIn("timezone database", joined.lower())
        self.assertNotIn("typo", joined.lower())
        self.assertFalse(problem)

    def test_a_working_lookup_still_says_yes(self):
        """The control. Without it the test above passes against a probe that
        always answers False -- just as wrong in the other direction, because
        a real typo would stop being reported.

        Whether to skip is decided by doing the lookup here, not by asking the
        function under test. The first version asked it, so `return False`
        made the test skip itself and the fault went unnoticed: a check that
        opts out on the evidence it is meant to be checking.
        """
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(poller._KNOWN_ZONE)
        except Exception:
            self.skipTest("this platform genuinely has no timezone database")
        self.assertTrue(poller.timezone_database_available())


class TestTheZoneIsDerivedFromWhereTheUserSaid(unittest.TestCase):
    """Nobody types an IANA timezone name, for the same reason nobody types
    their own latitude.

    Setup already turns a street address into coordinates. Open-Meteo answers
    with the zone for those coordinates, without a key, and is already a
    dependency of this project -- so the field can be filled from something the
    user has already given us rather than by asking a question whose wrong
    answer is invisible.
    """

    def setUp(self):
        import weather
        self.weather = weather
        self._get = weather._get
        self.asked = []

        def fake_get(url, params, timeout=30):
            self.asked.append((url, params))
            return dict(self.answer)

        weather._get = fake_get
        self.addCleanup(lambda: setattr(weather, "_get", self._get))
        self.answer = {"timezone": BRISBANE, "utc_offset_seconds": 36000}

    def test_it_returns_the_zone_for_those_coordinates(self):
        self.assertEqual(BRISBANE,
                         self.weather.timezone_at(HOME_LAT, HOME_LON))

    def test_it_asks_the_provider_to_choose_the_zone(self):
        self.weather.timezone_at(HOME_LAT, HOME_LON)
        _, params = self.asked[0]
        self.assertEqual("auto", params.get("timezone"),
                         "it did not ask for the zone at those coordinates")

    def test_it_asks_for_no_weather_at_all(self):
        """A timezone lookup that also pulls a forecast is a bigger request
        than the question needs, against a service that asks to be used
        politely and is doing this for free."""
        self.weather.timezone_at(HOME_LAT, HOME_LON)
        _, params = self.asked[0]
        self.assertNotIn("hourly", params)

    def test_the_location_is_coarsened_before_it_is_sent(self):
        """Coarser than anywhere else coordinates leave this machine.

        Elsewhere four decimals go out because a provider is matching a
        sensor. Here the question is only which zone a point is in, and two
        decimals -- about 1.1 km -- answers it without describing a street.
        """
        self.weather.timezone_at(HOME_LAT - 0.0012345, HOME_LON + 0.0012345)
        _, params = self.asked[0]
        self.assertEqual(round(HOME_LAT, 2), params["latitude"])
        self.assertEqual(round(HOME_LON, 2), params["longitude"])

    def test_it_is_not_coarsened_so_far_that_it_crosses_a_border(self):
        """One decimal would be 11 km. Coolangatta and Tweed Heads are closer
        than that across a state line, and one side observes daylight saving
        while the other does not -- the exact failure this feature exists to
        prevent. Asserted as a property of the rounding, not of one place.
        """
        self.weather.timezone_at(HOME_LAT + 0.045, HOME_LON + 0.04)
        _, params = self.asked[0]
        self.assertNotEqual(round(HOME_LAT + 0.045, 1), params["latitude"])

    def test_a_response_with_no_zone_returns_nothing_rather_than_guessing(self):
        self.answer = {"utc_offset_seconds": 36000}
        self.assertIsNone(self.weather.timezone_at(HOME_LAT, HOME_LON))

    def test_a_nonsense_zone_is_refused(self):
        """An IANA name has a shape. Storing whatever arrived would put a
        value in the config that every later lookup fails on, and the failure
        would be reported as the user's typo."""
        for junk in ("", "   ", 17, "not a zone", "Australia/Brisbane; rm -rf"):
            with self.subTest(value=junk):
                self.answer = {"timezone": junk}
                self.assertIsNone(self.weather.timezone_at(HOME_LAT, HOME_LON))

    def test_the_network_failing_is_not_fatal(self):
        """A timezone is a nicety; a location is the product. Setup must not
        fall over because a weather API is down."""
        def boom(url, params, timeout=30):
            raise self.weather.WeatherUnavailable("down")
        self.weather._get = boom
        self.assertIsNone(self.weather.timezone_at(HOME_LAT, HOME_LON))


class TestSetupFillsItInWithoutSpendingPrivacy(unittest.TestCase):
    """Derived where a lookup has already happened, and not where one has not.

    The location screen promises that typing coordinates directly sends
    nothing to anyone. A timezone lookup is a lookup, so somebody who chose
    that path keeps it -- they are told what to do by hand instead. Spending
    someone's privacy to save them a question is a small betrayal, and small
    betrayals are what make a tool not worth trusting.
    """

    def setUp(self):
        import setup as setup_mod
        self.setup = setup_mod
        import weather
        self.weather = weather
        self.looked_up = []
        self._at = weather.timezone_at
        weather.timezone_at = lambda lat, lon, **kw: (
            self.looked_up.append((lat, lon)) or BRISBANE)
        self.addCleanup(lambda: setattr(weather, "timezone_at", self._at))
        # setup prints; nothing here is about what it prints.
        self._say, self._ok, self._warn = (setup_mod.say, setup_mod.ok,
                                           setup_mod.warn)
        self.said = []
        setup_mod.say = lambda *a, **k: self.said.append(" ".join(map(str, a)))
        setup_mod.ok = lambda *a, **k: self.said.append(" ".join(map(str, a)))
        setup_mod.warn = lambda *a, **k: self.said.append(" ".join(map(str, a)))
        self.addCleanup(lambda: (setattr(setup_mod, "say", self._say),
                                 setattr(setup_mod, "ok", self._ok),
                                 setattr(setup_mod, "warn", self._warn)))

    def loc(self, lookup):
        return {"name": "Home", "latitude": HOME_LAT, "longitude": HOME_LON,
                "_lookup": lookup}

    def test_a_searched_address_gets_its_timezone(self):
        got = self.setup.resolve_timezone(self.loc("search"))
        self.assertEqual(BRISBANE, got["timezone"])
        self.assertEqual(1, len(self.looked_up))

    def test_an_ip_guess_does_too(self):
        got = self.setup.resolve_timezone(self.loc("ip"))
        self.assertEqual(BRISBANE, got["timezone"])

    def test_typed_coordinates_are_never_sent_anywhere(self):
        got = self.setup.resolve_timezone(self.loc("manual"))
        self.assertEqual([], self.looked_up,
                         "coordinates were sent by a path that promised not to")
        self.assertNotIn("timezone", got)

    def test_and_that_user_is_told_what_to_do_instead(self):
        """Declining to act silently is the same as forgetting to."""
        self.setup.resolve_timezone(self.loc("manual"))
        said = " ".join(self.said).lower()
        self.assertIn("timezone", said)
        self.assertIn("doctor", said)

    def test_the_internal_tag_never_reaches_the_config(self):
        for lookup in ("search", "ip", "manual"):
            with self.subTest(lookup=lookup):
                got = self.setup.resolve_timezone(self.loc(lookup))
                self.assertNotIn("_lookup", got)

    def test_a_lookup_that_fails_does_not_stop_setup(self):
        self.weather.timezone_at = lambda lat, lon, **kw: None
        got = self.setup.resolve_timezone(self.loc("search"))
        self.assertNotIn("timezone", got)
        self.assertIn("doctor", " ".join(self.said).lower())

    def test_a_lookup_that_explodes_does_not_stop_setup_either(self):
        def boom(lat, lon, **kw):
            raise RuntimeError("network on fire")
        self.weather.timezone_at = boom
        got = self.setup.resolve_timezone(self.loc("search"))
        self.assertNotIn("timezone", got)

    def test_a_location_with_no_coordinates_is_left_alone(self):
        got = self.setup.resolve_timezone({"name": "Home", "_lookup": "search"})
        self.assertEqual([], self.looked_up)
        self.assertNotIn("timezone", got)

    def test_setup_actually_calls_it(self):
        """The function being right does not mean the wizard runs it.

        Deleting the call left every test in this class green while setup went
        back to writing a config with no timezone in it -- correct code, never
        reached. The same shape as the chart's axis helper, which was also
        fully tested and briefly dead.
        """
        import inspect
        src = inspect.getsource(self.setup.main)
        self.assertIn("resolve_timezone(", src,
                      "setup no longer derives the timezone")


class TestTheSettingsPageCanChangeIt(unittest.TestCase):
    """Setup writes the timezone once. Nothing could change it afterwards.

    That is the gap that matters for the people most likely to need it: anyone
    who moved, anyone whose install predates the field existing, and anyone
    running the poller on a server in another country. `--doctor` told them
    something was wrong and offered no way to fix it but a text editor.
    """

    def test_the_schema_accepts_a_timezone(self):
        clean, errors = poller.validate_settings(
            {"location": {"timezone": BRISBANE}})
        self.assertEqual({}, errors)
        self.assertEqual(BRISBANE, clean["location"]["timezone"])

    def test_a_zone_this_platform_cannot_resolve_is_refused(self):
        """Storing it would put a value in the config that every later lookup
        fails on, and --doctor would then report it back as the user's typo --
        which it would be, except that the page accepted it without a word."""
        if not poller.timezone_database_available():
            self.skipTest("this platform has no timezone database")
        _, errors = poller.validate_settings(
            {"location": {"timezone": "Mars/Olympus_Mons"}})
        self.assertIn("location.timezone", errors)
        self.assertIn("Mars/Olympus_Mons", errors["location.timezone"])

    def test_clearing_it_is_allowed(self):
        """Empty means "follow this machine", which is a legitimate choice and
        the behaviour every install had before the field was read at all."""
        clean, errors = poller.validate_settings({"location": {"timezone": ""}})
        self.assertEqual({}, errors)
        self.assertEqual("", clean["location"]["timezone"])

    def test_a_platform_with_no_database_accepts_any_name(self):
        """On Windows nothing resolves, so validating against resolution would
        refuse every zone including the correct one -- locking a user out of a
        setting because of a platform limitation they cannot do anything about.
        The value is stored and --doctor explains why it is not in force.

        Both halves are simulated, not just the availability flag. Patching one
        leaves the zone still resolving on a machine that has a database, so
        the validator takes its success path and the test asserts nothing --
        it passed with the guard deleted.
        """
        avail = unittest.mock.patch.object(
            poller, "timezone_database_available", lambda: False)
        resolve = unittest.mock.patch.object(
            poller, "resolve_zone", lambda name: (None, "no database"))
        with avail, resolve:
            clean, errors = poller.validate_settings(
                {"location": {"timezone": BRISBANE}})
        self.assertEqual({}, errors,
                         "a platform limitation blocked a correct setting")
        self.assertEqual(BRISBANE, clean["location"]["timezone"])

    def test_the_payload_separates_configured_from_in_force(self):
        """`in_force` must be empty when the name cannot be applied. Reporting
        it as in force is the lie the field exists to prevent, and on a machine
        that resolves everything the two are indistinguishable -- so the
        unresolvable case is simulated rather than waited for."""
        resolve = unittest.mock.patch.object(
            poller, "resolve_zone", lambda name: (None, "no database"))
        with resolve:
            payload = poller.settings_payload(
                {"location": {"timezone": BRISBANE}, "sources": []})
        self.assertEqual(BRISBANE, payload["timezone"]["configured"])
        self.assertEqual("", payload["timezone"]["in_force"],
                         "a zone that cannot be applied was reported as in force")

    def test_the_payload_says_whether_it_is_actually_in_force(self):
        """A page that shows the configured zone and nothing else is lying by
        omission on any machine that cannot apply it."""
        payload = poller.settings_payload(
            {"location": {"timezone": BRISBANE}, "sources": []})
        tz = payload.get("timezone")
        self.assertIsInstance(tz, dict, "the payload does not describe the zone")
        self.assertEqual(BRISBANE, tz.get("configured"))
        self.assertIn("in_force", tz)
        self.assertIn("machine", tz)

    def test_the_payload_reports_the_machine_zone_for_comparison(self):
        payload = poller.settings_payload({"location": {}, "sources": []})
        self.assertTrue(payload["timezone"]["machine"],
                        "nothing to compare a configured zone against")
        self.assertFalse(payload["timezone"]["configured"])


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main()
