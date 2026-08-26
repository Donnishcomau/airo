# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Display units follow the reader; the stored number never moves.

Rule 6 says raw µg/m³ is canonical, and the reasoning generalises: a database
that stores what somebody's screen happened to show is a database nobody can
compare across machines, across a house move, or against itself a year later.

So the property under test has two halves, and the second is the one that is
easy to lose: the right unit reaches the screen, *and* nothing about the stored
value changed to get it there.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import poller  # noqa: E402
import store  # noqa: E402
import units  # noqa: E402
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)


def setUpModule():
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


AU = {"LANG": "en_AU.UTF-8"}
US = {"LANG": "en_US.UTF-8"}
GB = {"LANG": "en_GB.UTF-8"}
NONE = {"LANG": "C"}


class TestReadingTheRegion(unittest.TestCase):
    def test_it_reads_the_region_from_the_usual_variables(self):
        for env, expected in ((AU, "AU"), (US, "US"), (GB, "GB")):
            self.assertEqual(expected, units.region(env))

    def test_lc_measurement_wins_because_it_is_the_one_that_means_this(self):
        """Somebody working in English on a machine set to metric measurement
        has said two different things, and only one of them is about units."""
        self.assertEqual("DE", units.region(
            {"LANG": "en_US.UTF-8", "LC_MEASUREMENT": "de_DE.UTF-8"}))

    def test_the_no_locale_locales_are_not_a_region(self):
        """`C` and `POSIX` mean "no locale configured", not a country. Reading
        a region out of them would be inventing one."""
        for value in ("C", "POSIX"):
            self.assertIsNone(units.region({"LANG": value}, fallback=None))

    def test_nothing_set_is_none_rather_than_a_guess(self):
        self.assertIsNone(units.region({}, fallback=None))


class TestWhatEachRegionSees(unittest.TestCase):
    def test_an_australian_sees_celsius_and_kilometres_per_hour(self):
        chosen = units.resolve(environ=AU)
        self.assertEqual("c", chosen["temperature"])
        self.assertEqual("kmh", chosen["wind"])

    def test_an_american_sees_fahrenheit_and_miles_per_hour(self):
        chosen = units.resolve(environ=US)
        self.assertEqual("f", chosen["temperature"])
        self.assertEqual("mph", chosen["wind"])
        self.assertEqual("mi", chosen["distance"])

    def test_the_united_kingdom_is_celsius_and_miles(self):
        """The case a single metric/imperial flag cannot express, and the
        reason the table is per quantity. Getting this wrong is invisible: a
        wind speed in the wrong unit is still a plausible number."""
        chosen = units.resolve(environ=GB)
        self.assertEqual("c", chosen["temperature"])
        self.assertEqual("mph", chosen["wind"])

    def test_an_unlisted_region_gets_metric(self):
        chosen = units.resolve(environ={"LANG": "fr_FR.UTF-8"})
        self.assertEqual(units.METRIC, chosen)

    def test_no_region_at_all_gets_metric(self):
        self.assertEqual(units.METRIC, units.resolve(environ=NONE))

    def test_every_region_names_only_quantities_that_exist(self):
        """Enumerated from the table rather than checked by hand, so a region
        added with a typo fails here instead of silently doing nothing."""
        for code, mapping in units.BY_REGION.items():
            for quantity, unit in mapping.items():
                self.assertIn(quantity, units.QUANTITIES, f"{code}")
                self.assertIn((quantity, unit), units.CONVERSIONS, f"{code}")

    def test_every_quantity_has_a_metric_default_and_a_conversion(self):
        for quantity in units.QUANTITIES:
            self.assertIn(quantity, units.METRIC)
            self.assertIn((quantity, units.METRIC[quantity]), units.CONVERSIONS)


class TestTheConversionsThemselves(unittest.TestCase):
    def test_the_fixed_points_everybody_knows(self):
        for celsius, fahrenheit in ((0, 32.0), (100, 212.0), (-40, -40.0),
                                    (20, 68.0), (37, 98.6)):
            got, label = units.convert("temperature", celsius,
                                       {"temperature": "f"})
            self.assertAlmostEqual(fahrenheit, got, places=6, msg=f"{celsius}C")
            self.assertEqual("°F", label)

    def test_wind_conversions(self):
        got, _ = units.convert("wind", 10.0, {"wind": "kmh"})
        self.assertAlmostEqual(36.0, got, places=6)
        got, _ = units.convert("wind", 10.0, {"wind": "mph"})
        self.assertAlmostEqual(22.369362920544, got, places=6)

    def test_a_round_trip_returns_the_original(self):
        """The property that makes converting at display time safe. If it were
        lossy, a figure quoted on screen could not be compared with the stored
        one, and somebody would eventually store the screen's version.
        """
        back = {"f": lambda v: (v - 32.0) * 5.0 / 9.0,
                "kmh": lambda v: v / 3.6,
                "mph": lambda v: v / 2.2369362920544,
                "mi": lambda v: v / 0.621371192237334,
                "inhg": lambda v: v / 0.029529983071445}
        for (quantity, unit), (forward, _) in units.CONVERSIONS.items():
            if unit not in back:
                continue
            for original in (0.0, 0.5, 7.3, 123.456, -12.0):
                self.assertAlmostEqual(
                    original, back[unit](forward(original)), places=9,
                    msg=f"{quantity} in {unit} did not survive a round trip")

    def test_nothing_measured_stays_nothing(self):
        """None is not zero. An hour with no anemometer reading is not an hour
        of no wind, and every other layer of this project says so."""
        value, label = units.convert("wind", None, {"wind": "mph"})
        self.assertIsNone(value)
        self.assertEqual("mph", label)
        self.assertIn("—", units.show("wind", None, {"wind": "mph"}))

    def test_a_shown_value_carries_its_unit(self):
        self.assertEqual("68.0 °F", units.show("temperature", 20.0,
                                               {"temperature": "f"}))
        self.assertEqual("20.0 °C", units.show("temperature", 20.0,
                                               {"temperature": "c"}))

    def test_pm25_is_not_convertible_because_it_does_not_vary(self):
        """µg/m³ is the unit everywhere, including in the US, where the EPA
        reports concentrations in µg/m³ and puts its index on top. Offering a
        conversion would invent a difference that does not exist."""
        self.assertNotIn("pm25", units.QUANTITIES)
        self.assertNotIn("concentration", units.QUANTITIES)


class TestTheUserGetsTheLastWord(unittest.TestCase):
    def test_an_explicit_choice_beats_the_region(self):
        """Somebody working abroad for a month did not ask for their history
        to change units. Somebody who set it deliberately did."""
        chosen = units.resolve({"units": {"temperature": "c"}}, environ=US)
        self.assertEqual("c", chosen["temperature"])
        self.assertEqual("mph", chosen["wind"],
                         "an override of one quantity moved another")

    def test_the_shorthand_is_accepted_because_people_will_write_it(self):
        chosen = units.resolve({"units": "us"}, environ=AU)
        self.assertEqual("f", chosen["temperature"])

    def test_nonsense_in_the_config_does_not_take_the_display_down(self):
        """`resolve` runs on the way to a screen. It refusing to return would
        cost somebody the reading, which rule 5's direction forbids — the
        loud refusal belongs in the validator, and it is there."""
        for junk in ({"temperature": "kelvin"}, "imperial", 7, None, []):
            chosen = units.resolve({"units": junk}, environ=AU)
            self.assertEqual("c", chosen["temperature"])

    def test_the_validator_refuses_what_resolve_ignores(self):
        """The pair matters: silently ignoring a bad setting leaves somebody
        staring at Celsius wondering why their change did nothing."""
        for junk in ({"temperature": "kelvin"}, {"wibble": "f"}, "imperial"):
            _, errors = poller.validate_settings({"units": junk})
            self.assertTrue(errors, f"{junk!r} was accepted")

    def test_a_good_setting_is_accepted(self):
        clean, errors = poller.validate_settings(
            {"units": {"temperature": "f", "wind": "mph"}})
        self.assertEqual({}, errors)
        self.assertEqual({"temperature": "f", "wind": "mph"}, clean["units"])


class TestChangingTheLocaleChangesTheUnits(unittest.TestCase):
    """The half of the request that is easy to miss: not just "show the right
    unit", but "and change when the reader does".

    Nothing is cached and nothing is written into the config at setup, so
    there is no stored answer to go stale and nothing to migrate when somebody
    moves or changes their Mac's region.
    """

    def test_the_same_stored_value_is_shown_differently_either_side(self):
        stored = 20.0
        au = units.show("temperature", stored, units.resolve(environ=AU))
        us = units.show("temperature", stored, units.resolve(environ=US))
        self.assertEqual("20.0 °C", au)
        self.assertEqual("68.0 °F", us)

    def test_moving_changes_the_answer_with_no_migration(self):
        before = units.resolve(environ=AU)
        after = units.resolve(environ=US)
        self.assertNotEqual(before, after,
                            "changing region changed nothing")

    def test_the_settings_payload_reports_what_is_in_force_and_why(self):
        """So the page can say *why* it chose what it chose. "Because your Mac
        says en_US" is an answer somebody can act on; "°F" on its own is not.
        """
        payload = poller.settings_payload({"location": {}, "sources": []})
        for key in ("units", "units_region", "unit_labels"):
            self.assertIn(key, payload)
        self.assertEqual(set(units.QUANTITIES), set(payload["units"]))
        self.assertEqual(set(units.QUANTITIES), set(payload["unit_labels"]))


class TestNothingStoredEverChanges(unittest.TestCase):
    """The guarantee that makes all of the above safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "airo.db"

    def test_a_us_reader_does_not_change_the_database(self):
        conn = store.connect(self.path)
        sid = store.upsert_source(conn, "qld", "s1", "Site")
        store.insert_readings(conn, sid, [{
            "observed_utc": "2026-08-01T10:00:00+00:00", "pm25": 7.4,
            "temperature": 20.0, "temperature_unit": "C"}])
        before = conn.execute(
            "SELECT pm25, temperature, temperature_unit FROM readings"
        ).fetchone()
        conn.close()

        # Everything a US reader's screen would do.
        chosen = units.resolve(environ=US)
        units.show("temperature", before["temperature"], chosen)
        units.show("wind", 3.0, chosen)

        conn = store.connect(self.path)
        after = conn.execute(
            "SELECT pm25, temperature, temperature_unit FROM readings"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(before), tuple(after),
                         "displaying a reading changed what was stored")
        self.assertEqual("C", after["temperature_unit"],
                         "the stored unit followed the reader's locale")

    def test_the_config_is_not_written_just_by_looking(self):
        """Resolution is a read. A settings file rewritten as a side effect of
        opening a page is how a hand-edited value disappears."""
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps({"location": {}, "sources": []}),
                          encoding="utf-8")
        before = config.read_bytes()
        units.resolve(json.loads(before), environ=US)
        self.assertEqual(before, config.read_bytes())


class TestTheFallbackToTheInterpretersOwnLocale(unittest.TestCase):
    """What happens on a Mac, where `LC_MEASUREMENT` is usually unset.

    In production `resolve()` is called with no environ, and the environment
    variables often say nothing — a launchd agent inherits a stripped one. The
    interpreter's own locale is the last thing that knows, and if that path is
    wrong every scheduled run shows metric to somebody who is not.
    """

    def test_the_interpreters_locale_is_used_when_the_environment_is_silent(self):
        self.assertEqual("US", units.region({}, fallback="en_US.UTF-8"))
        self.assertEqual("DE", units.region({}, fallback="de_DE"))

    def test_the_environment_still_wins_over_it(self):
        """The fallback is a last resort, not a preference. A machine set to
        one region running a shell configured for another has been told twice,
        and the shell is the more specific answer."""
        self.assertEqual("GB", units.region({"LANG": "en_GB.UTF-8"},
                                            fallback="en_US.UTF-8"))

    def test_a_fallback_that_names_no_region_is_not_forced_into_one(self):
        for junk in ("C", "POSIX", "en", "", "nonsense"):
            self.assertIsNone(units.region({}, fallback=junk), junk)

    def test_a_broken_locale_setting_does_not_stop_the_display(self):
        """`locale.getlocale()` raises on some malformed settings. A units
        lookup must never be the thing that costs somebody their reading —
        rule 5's direction, applied to the way out.
        """
        import locale as stdlocale
        saved = stdlocale.getlocale
        stdlocale.getlocale = lambda *a: (_ for _ in ()).throw(
            ValueError("unknown locale: garbage"))
        self.addCleanup(lambda: setattr(stdlocale, "getlocale", saved))

        self.assertIsNone(units.system_locale())

        # The environment has to be cleared too, or this asserts nothing about
        # the broken locale: LANG still says en_AU on this machine, the env
        # wins as it should, and the metric fallback is never reached. First
        # version of this test proved only that the developer is Australian.
        saved_env = dict(os.environ)
        for name in units.ENV_ORDER:
            os.environ.pop(name, None)
        self.addCleanup(lambda: os.environ.update(saved_env))

        self.assertEqual(units.METRIC, units.resolve())

    def test_production_resolve_consults_the_locale_at_all(self):
        """Guards the wiring rather than the helper. `resolve(environ=...)`
        deliberately ignores the interpreter's locale, so if the no-environ
        call did too, the fallback would be fully tested and never used —
        which is the call-site trap this project has hit four times."""
        import locale as stdlocale
        saved = stdlocale.getlocale
        stdlocale.getlocale = lambda *a: ("en_US", "UTF-8")
        self.addCleanup(lambda: setattr(stdlocale, "getlocale", saved))

        saved_env = dict(os.environ)
        for name in units.ENV_ORDER:
            os.environ.pop(name, None)
        self.addCleanup(lambda: os.environ.update(saved_env))

        self.assertEqual("f", units.resolve()["temperature"],
                         "the production call ignores the system locale")

    def test_asking_for_metric_gets_metric_even_in_a_km_h_country(self):
        """`metric` mapped to an empty override once, so an Australian who
        asked for metric still got km/h — the region kept winning because
        there was nothing to override it with. A setting that silently does
        nothing is worse than one that is refused: they said what they wanted
        and were ignored, with no way to tell."""
        chosen = units.resolve({"units": "metric"}, environ=AU)
        self.assertEqual("ms", chosen["wind"])
        self.assertEqual(units.METRIC, chosen)

    def test_asking_for_us_gets_us_in_a_metric_country(self):
        chosen = units.resolve({"units": "us"}, environ=AU)
        self.assertEqual("f", chosen["temperature"])
        self.assertEqual("mph", chosen["wind"])
