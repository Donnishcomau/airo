"""AQI scale conversion tests.

Getting a breakpoint wrong here misreports air quality to someone who may be
making a health decision, and the error would be invisible -- a wrong number
looks exactly like a right one.
"""

import json
import re
import sys
from datetime import datetime
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import poller  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



class TestAustralian(unittest.TestCase):
    def test_standard_is_aqi_100(self):
        """25 ug/m3 is the NEPM PM2.5 standard and must map to exactly 100."""
        self.assertEqual(poller.aqi_for(25.0, "au"), 100.0)

    def test_is_linear(self):
        self.assertEqual(poller.aqi_for(12.5, "au"), 50.0)
        self.assertEqual(poller.aqi_for(50.0, "au"), 200.0)

    def test_zero(self):
        self.assertEqual(poller.aqi_for(0.0, "au"), 0.0)

    def test_bands(self):
        for aqi, expected in ((0, "Very good"), (33, "Very good"),
                              (34, "Good"), (66, "Good"),
                              (67, "Fair"), (99, "Fair"),
                              (100, "Poor"), (149, "Poor"),
                              (150, "Very poor"), (200, "Very poor"),
                              (201, "Hazardous"), (5000, "Hazardous")):
            self.assertEqual(poller.band_for(aqi, "au"), expected, f"aqi {aqi}")


class TestUsEpa(unittest.TestCase):
    """2024 revision: the 'good' ceiling dropped from 12.0 to 9.0 ug/m3."""

    def test_good_ceiling_is_the_2024_value(self):
        self.assertEqual(poller.aqi_for(9.0, "us_epa"), 50.0)

    def test_category_boundaries(self):
        self.assertEqual(poller.aqi_for(35.4, "us_epa"), 100.0)
        self.assertEqual(poller.aqi_for(55.4, "us_epa"), 150.0)
        self.assertEqual(poller.aqi_for(125.4, "us_epa"), 200.0)
        self.assertEqual(poller.aqi_for(225.4, "us_epa"), 300.0)

    def test_interpolates_within_a_segment(self):
        mid = poller.aqi_for(22.25, "us_epa")  # midpoint of 9.1-35.4
        self.assertTrue(74 < mid < 77, f"got {mid}")

    def test_bands(self):
        self.assertEqual(poller.band_for(25, "us_epa"), "Good")
        self.assertEqual(poller.band_for(75, "us_epa"), "Moderate")
        self.assertEqual(poller.band_for(125, "us_epa"),
                         "Unhealthy for sensitive groups")
        self.assertEqual(poller.band_for(175, "us_epa"), "Unhealthy")

    def test_above_the_top_breakpoint_is_capped_not_negative(self):
        v = poller.aqi_for(9999.0, "us_epa")
        self.assertEqual(v, 500.0)

    def test_scales_genuinely_differ(self):
        """The same air must not give the same number on both scales."""
        self.assertNotEqual(poller.aqi_for(20.0, "au"),
                            poller.aqi_for(20.0, "us_epa"))


class TestRaw(unittest.TestCase):
    def test_is_identity(self):
        self.assertEqual(poller.aqi_for(12.3, "raw"), 12.3)

    def test_bands_reference_the_who_guideline(self):
        self.assertEqual(poller.band_for(10, "raw"), "At or below WHO guideline")
        self.assertEqual(poller.band_for(20, "raw"), "Above WHO guideline")


class TestIndexToMeasurement(unittest.TestCase):
    """Labelling an index with the µg/m³ behind it, without getting it wrong.

    The dashboard used to multiply by a hardcoded Australian standard of 25 in
    three places, whatever scale was configured. Air of 30 µg/m³ was labelled
    22.5 on a US EPA install and 7.5 on a `raw` one -- both understatements, on
    the figure a reader is most likely to check against a health guideline. The
    factor is now a server decision, because only the server knows the scale.
    """

    def test_the_australian_scale_is_a_quarter(self):
        """AQI 100 is the 25 µg/m³ NEPM standard, so one point is 0.25."""
        self.assertAlmostEqual(poller.ug_per_index("au"), 0.25)

    def test_the_australian_factor_round_trips(self):
        for pm in (5.0, 12.5, 30.0, 88.0):
            self.assertAlmostEqual(
                poller.aqi_for(pm, "au") * poller.ug_per_index("au"), pm,
                places=6, msg=f"{pm} µg/m³ does not survive the round trip")

    def test_raw_is_one_because_the_index_is_the_measurement(self):
        self.assertEqual(poller.ug_per_index("raw"), 1.0)
        self.assertAlmostEqual(
            poller.aqi_for(30.0, "raw") * poller.ug_per_index("raw"), 30.0)

    def test_a_piecewise_scale_has_no_factor_at_all(self):
        """None, not an approximation. US EPA breakpoints have a different
        slope in every band, so no single multiplier is correct anywhere. A
        caller must omit the figure rather than print a plausible wrong one."""
        self.assertIsNone(poller.ug_per_index("us_epa"))

    def test_every_scale_answers(self):
        """Enumerated, so a scale added tomorrow is already in scope -- a new
        one that silently fell through to the Australian factor is precisely
        the bug this replaces."""
        for name in poller.SCALES:
            factor = poller.ug_per_index(name)
            self.assertTrue(factor is None or factor > 0,
                            f"{name} reports a nonsensical factor {factor!r}")

    def test_an_unknown_scale_does_not_invent_one(self):
        self.assertEqual(poller.ug_per_index("nope"),
                         poller.ug_per_index(poller.DEFAULT_SCALE))


class TestEdges(unittest.TestCase):
    def test_none_in_none_out(self):
        for scale in poller.SCALES:
            self.assertIsNone(poller.aqi_for(None, scale))
            self.assertEqual(poller.band_for(None, scale), "No data")

    def test_unknown_scale_falls_back_rather_than_raising(self):
        self.assertEqual(poller.aqi_for(25.0, "klingon"),
                         poller.aqi_for(25.0, poller.DEFAULT_SCALE))

    def test_every_advertised_scale_converts(self):
        for scale in poller.SCALES:
            v = poller.aqi_for(15.0, scale)
            self.assertIsNotNone(v, f"scale {scale} returned nothing")
            self.assertGreaterEqual(v, 0)

    def test_monotonic(self):
        """More particulate must never produce a lower index."""
        for scale in poller.SCALES:
            vals = [poller.aqi_for(p, scale) for p in
                    (0, 1, 5, 9, 15, 25, 40, 60, 130, 240, 400)]
            self.assertEqual(vals, sorted(vals), f"{scale} is not monotonic")

    def test_band_matches_the_displayed_rounded_value(self):
        """ARCHITECTURE S3: colour must be chosen from the rounded number,
        or the band and the digits can disagree at a boundary."""
        pm = 16.75  # AU AQI 67.0 exactly -- the Fair boundary
        aqi = poller.aqi_for(pm, "au")
        self.assertEqual(poller.band_for(round(aqi), "au"), "Fair")


class TestConfigMigration(unittest.TestCase):
    def test_v03_flat_config_becomes_a_source_list(self):
        cfg = poller.migrate_config({
            "sensor_index": 1234, "sensor_name": "Example sensor", "read_key": "abc",
        })
        self.assertEqual(len(cfg["sources"]), 1)
        s = cfg["sources"][0]
        self.assertEqual(s["provider"], "purpleair")
        self.assertEqual(s["site_id"], 1234)
        self.assertEqual(s["read_key"], "abc")

    def test_v04_single_source_becomes_a_list(self):
        cfg = poller.migrate_config({
            "source": {"provider": "qld", "site_id": "station-a", "site_name": "Example station"},
        })
        self.assertEqual(len(cfg["sources"]), 1)
        self.assertEqual(cfg["sources"][0]["provider"], "qld")

    def test_migration_does_not_duplicate_an_existing_source(self):
        cfg = poller.migrate_config({
            "sources": [{"provider": "purpleair", "site_id": 1234}],
            "sensor_index": 1234,
        })
        self.assertEqual(len(cfg["sources"]), 1)

    def test_v05_list_passes_through_untouched(self):
        cfg = poller.migrate_config({
            "sources": [{"provider": "purpleair", "site_id": 1},
                        {"provider": "qld", "site_id": "station-a"}],
        })
        self.assertEqual(len(cfg["sources"]), 2)

    def test_location_name_falls_back_to_the_first_source(self):
        cfg = poller.migrate_config({
            "sources": [{"provider": "qld", "site_id": "station-a",
                         "site_name": "Example station"}],
        })
        self.assertEqual(cfg["location"]["name"], "Example station")

    def test_enabled_defaults_to_true(self):
        cfg = poller.migrate_config({
            "sources": [{"provider": "qld", "site_id": "station-a"}]})
        self.assertTrue(cfg["sources"][0]["enabled"])

    def test_disabled_sources_are_excluded(self):
        cfg = {"sources": [{"provider": "qld", "site_id": "a", "enabled": True},
                           {"provider": "qld", "site_id": "b", "enabled": False}]}
        self.assertEqual(len(poller.enabled_sources(cfg)), 1)


class TestProviderRegistry(unittest.TestCase):
    def test_every_provider_declares_its_contract(self):
        for slug, p in poller.PROVIDERS.items():
            self.assertTrue(p.label, f"{slug} has no label")
            self.assertTrue(p.licence, f"{slug} declares no licence")
            self.assertGreater(p.resolution_minutes, 0, f"{slug} resolution")
            if p.needs_key:
                self.assertTrue(p.key_env, f"{slug} needs a key but names no env var")
                self.assertTrue(p.key_url, f"{slug} needs a key but says not where")

    def test_unknown_provider_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            poller.get_provider({"provider": "nope"})

    def test_keyless_provider_returns_empty_key(self):
        self.assertEqual(poller.get_api_key({"provider": "qld"}), "")

    def test_every_provider_declares_an_accuracy_tier(self):
        """Tier decides what the user is told to believe when instruments
        disagree, so a provider without one is a silent correctness hazard."""
        valid = {"reference", "indicative", "consumer"}
        for slug, p in poller.PROVIDERS.items():
            self.assertIn(p.tier, valid, f"{slug} has tier {p.tier!r}")
            self.assertTrue(p.accuracy_note, f"{slug} explains no accuracy caveat")

    def test_both_reference_and_consumer_tiers_are_available(self):
        """The whole cross-check premise needs at least one of each."""
        tiers = {p.tier for p in poller.PROVIDERS.values()}
        self.assertIn("reference", tiers)
        self.assertIn("consumer", tiers)

    def test_keyless_government_feed_exists(self):
        """Someone with no API keys at all must still be able to use Airo."""
        keyless = [s for s, p in poller.PROVIDERS.items() if not p.needs_key]
        self.assertTrue(keyless, "no provider works without an account")

    def test_every_provider_can_be_asked_to_discover(self):
        """discover() is how setup finds sites. It may return nothing, but it
        must never raise on a provider that simply cannot search."""
        for slug, p in poller.PROVIDERS.items():
            self.assertTrue(hasattr(p, "discover"), f"{slug} has no discover()")


class TestSetupRefusesWithoutATerminal(unittest.TestCase):
    """Silently substituting defaults writes a plausible config for a location
    nobody chose, and reports success. A wrong config that claims to have
    worked is worse than a refusal -- and this is exactly the failure mode a
    piped or wrapped invocation produces."""

    def setUp(self):
        import setup
        self.setup = setup

    def test_ask_raises_rather_than_inventing_an_answer(self):
        # setup.ask() calls the builtin, so patch that rather than a module
        # attribute that only exists once something has assigned it.
        import builtins
        real = builtins.input
        builtins.input = lambda p="": (_ for _ in ()).throw(EOFError())
        try:
            with self.assertRaises(self.setup.NoTerminal):
                self.setup.ask("anything", "a-default")
        finally:
            builtins.input = real

    def test_wizard_refuses_when_stdin_is_not_a_tty(self):
        import io, contextlib, sys as _sys

        class NoTTY:
            def isatty(self):
                return False

        real_stdin, real_argv = _sys.stdin, _sys.argv
        _sys.stdin, _sys.argv = NoTTY(), ["setup.py"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = self.setup.main()
        finally:
            _sys.stdin, _sys.argv = real_stdin, real_argv
        self.assertEqual(rc, 2, "must refuse, not proceed with defaults")
        self.assertIn("interactive terminal", buf.getvalue())


class TestProviderContract(unittest.TestCase):
    """Every provider must behave the same way, or the code that consumes them
    reasons about different things depending on which one answered."""

    def test_history_honours_its_window(self):
        """A provider returning data outside the requested window means gap
        detection and backfill reason about different ranges per source.
        OpenAQ genuinely does this -- its datetime_from is inclusive of the
        hour bucket straddling the boundary -- so the adapter filters."""
        import inspect
        for slug, prov in poller.PROVIDERS.items():
            src = inspect.getsource(type(prov))
            if "def history" not in src:
                continue
            # Each adapter must either bound its own results or delegate to an
            # API that does. Assert the ones known to over-return filter.
            # Every provider, with no exemptions -- PurpleAir was left off
            # this list and carried the defect for three releases until
            # --doctor caught it against the live API.
            self.assertIn("start <= when <= end", src,
                          f"{slug} does not bound its history window")

    def test_history_results_are_sorted(self):
        """Callers merge these in order; unsorted input makes gap detection
        compare the wrong endpoints."""
        import inspect
        for slug, prov in poller.PROVIDERS.items():
            src = inspect.getsource(type(prov))
            if "def history" not in src:
                continue
            self.assertIn("sort", src, f"{slug} does not sort its history")


class TestConsoleSafety(unittest.TestCase):
    """Decoration must never break a command.

    Windows consoles default to cp1252, which cannot encode a tick. Printing
    one raised UnicodeEncodeError and took the command down, so a Windows user
    got a crash from `backup.py create` instead of a backup.
    """

    def test_symbols_are_defined(self):
        for name in ("TICK", "CROSS", "WARN"):
            self.assertTrue(getattr(poller, name), f"poller.{name} is empty")

    def test_symbols_encode_in_the_current_console(self):
        import sys
        enc = (getattr(sys.stdout, "encoding", "") or "utf-8")
        for name in ("TICK", "CROSS", "WARN"):
            try:
                getattr(poller, name).encode(enc)
            except (UnicodeEncodeError, LookupError):
                self.fail(f"poller.{name} cannot be printed in {enc}")

    def test_ascii_fallback_is_pure_ascii(self):
        """The fallback has to survive the narrowest encoding there is."""
        for candidate in ("OK", "X", "!"):
            candidate.encode("ascii")   # raises if not

    #: The glyphs `_console_safe()` exists to guard. A literal one anywhere
    #: else is a copy of the decoration without the fallback.
    GUARDED_GLYPHS = ("✓", "✗", "⚠")

    def test_no_shipped_module_prints_a_glyph_literal(self):
        """`poller.TICK`, not "✓".

        `setup.py` and `backup.py` each had their own `ok()`/`bad()` writing
        the character straight into an f-string. Both already imported poller,
        so the guarded version was one attribute away — and backup.py is the
        module the fallback was *written for*, after a Windows user got a
        UnicodeEncodeError from `backup.py create` rather than a backup. The
        crash was fixed in one place and left standing in the two callers.

        Matched by shape rather than by those two names, per rule 3's lesson:
        the next module to print a tick is caught by the same test.
        """
        sources = sorted(ROOT.glob("*.py")) + sorted((ROOT / "tools").glob("*.py"))
        offenders = []
        for path in sources:
            for n, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if any(g in line for g in self.GUARDED_GLYPHS):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")

        # `poller._console_safe()` is where the characters are allowed to
        # appear, because it is the thing that decides whether they can be
        # printed at all. It names them by escape, not as literals, so it does
        # not show up here -- and if it is ever rewritten to use literals, the
        # right answer is to escape them there, not to widen this.
        self.assertEqual(
            [], offenders,
            "a console glyph is written out as a literal instead of using "
            "poller.TICK / poller.CROSS / poller.WARN, which degrade to ASCII "
            "where the console cannot encode them:\n  " + "\n  ".join(offenders))


class TestProviderCoverage(unittest.TestCase):
    """A state feed offered to someone in another state is a dead end that
    reads as the tool being broken. A user in Hobart was defaulted onto the
    Queensland and NSW feeds and hunted outward to 200 km finding nothing."""

    HOBART = (-42.8825, 147.3281)
    BRISBANE = (-27.4698, 153.0251)
    SYDNEY = (-33.8688, 151.2093)
    BERLIN = (52.52, 13.405)

    def test_state_feeds_declare_a_bounded_area(self):
        for slug in ("qld", "nsw"):
            self.assertIsNotNone(poller.PROVIDERS[slug].coverage_box,
                                 f"{slug} claims worldwide coverage")

    def test_global_aggregators_are_unbounded(self):
        for slug in ("openaq", "purpleair"):
            self.assertIsNone(poller.PROVIDERS[slug].coverage_box)

    def test_queensland_feed_does_not_claim_tasmania(self):
        self.assertFalse(poller.PROVIDERS["qld"].covers(*self.HOBART))
        self.assertTrue(poller.PROVIDERS["qld"].covers(*self.BRISBANE))

    def test_nsw_feed_covers_sydney_only(self):
        self.assertTrue(poller.PROVIDERS["nsw"].covers(*self.SYDNEY))
        self.assertFalse(poller.PROVIDERS["nsw"].covers(*self.HOBART))
        self.assertFalse(poller.PROVIDERS["nsw"].covers(*self.BRISBANE))

    def test_somewhere_far_away_still_has_options(self):
        """Anywhere on earth must have at least one candidate network, or
        setup has nothing to offer."""
        for place in (self.HOBART, self.BERLIN):
            covering = [s for s, p in poller.PROVIDERS.items() if p.covers(*place)]
            self.assertTrue(covering, f"no network covers {place}")

    def test_unknown_location_does_not_exclude_anything(self):
        """Before setup runs there are no coordinates; nothing should be
        filtered out on the strength of a missing value."""
        for p in poller.PROVIDERS.values():
            self.assertTrue(p.covers(None, None))

    def test_every_provider_explains_its_coverage(self):
        for slug, p in poller.PROVIDERS.items():
            self.assertTrue(p.coverage_note, f"{slug} does not say where it works")


class TestSourceRecommendation(unittest.TestCase):
    """setup.recommend() pairs accuracy with proximity.

    'Closest' and 'most accurate' are different questions and usually
    different instruments. Recommending the two nearest sites would often
    mean two consumer sensors, which cannot cross-check each other.
    """

    def setUp(self):
        import setup
        self.setup = setup

    def _site(self, provider, distance, site_id):
        return {"provider": provider, "distance_km": distance,
                "site_id": site_id, "site_name": f"site-{site_id}"}

    def test_pairs_a_reference_with_a_consumer(self):
        found = [
            self._site("purpleair", 0.5, "a"),
            self._site("purpleair", 0.9, "b"),
            self._site("qld", 6.0, "c"),
        ]
        picks = self.setup.recommend(found)
        tiers = {poller.PROVIDERS[p["provider"]].tier for p in picks}
        self.assertEqual(tiers, {"consumer", "reference"})

    def test_picks_the_nearest_of_each_tier(self):
        found = [
            self._site("purpleair", 2.0, "far-consumer"),
            self._site("qld", 3.0, "near-ref"),
            self._site("purpleair", 0.4, "near-consumer"),
            self._site("qld", 9.0, "far-ref"),
        ]
        picks = {p["site_id"] for p in self.setup.recommend(found)}
        self.assertEqual(picks, {"near-consumer", "near-ref"})

    def test_single_tier_available_returns_just_that(self):
        found = [self._site("purpleair", 1.0, "a"),
                 self._site("purpleair", 2.0, "b")]
        picks = self.setup.recommend(found)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["site_id"], "a")

    def test_nothing_found_recommends_nothing(self):
        self.assertEqual(self.setup.recommend([]), [])




class TestSentinelNeverReadsAsSafe(unittest.TestCase):
    """The most dangerous failure this tool can have is telling someone the
    air is fine when it does not know.

    Observed live: two stations in the Queensland network report -9999 when
    the instrument is offline -- the synthetic slugs 'sou' and 'wbk' stand in
    for them here. Airo stored it, converted it (-9999 ug/m3 -> AQI
    -39996 on the Australian linear scale), and because -39996 falls below the
    first breakpoint's ceiling it rendered as:

        poll ok -- Australian AQI -39996.0 (Very good) via Southmoor

    "Very good" for air nobody measured.
    """

    SENTINELS = [-9999.0, -999.0, -1.0, -0.1]

    def test_no_scale_turns_a_sentinel_into_an_index(self):
        for scale in poller.SCALES:
            for bad in self.SENTINELS:
                self.assertIsNone(
                    poller.aqi_for(bad, scale),
                    f"{scale} converted {bad} into an index instead of None")

    def test_no_scale_bands_a_sentinel_as_anything_but_no_data(self):
        for scale in poller.SCALES:
            for bad in self.SENTINELS:
                band = poller.band_for(poller.aqi_for(bad, scale), scale)
                self.assertEqual(band, "No data",
                                 f"{scale} called {bad} {band!r}")

    def test_a_sentinel_never_lands_in_the_reassuring_band(self):
        """Stated as the property that actually matters, so it still holds if
        the band names or breakpoints are ever changed."""
        for scale, spec in poller.SCALES.items():
            best = None
            if "breakpoints" in spec:
                best = spec["breakpoints"][0][4]
            for bad in self.SENTINELS:
                band = poller.band_for(poller.aqi_for(bad, scale), scale)
                if best:
                    self.assertNotEqual(band, best,
                                        f"{scale}: {bad} read as {best!r}")

    def test_real_low_readings_still_work(self):
        """The guard must not swallow genuinely clean air."""
        self.assertEqual(poller.band_for(poller.aqi_for(0.0, "au"), "au"),
                         "Very good")
        self.assertIsNotNone(poller.aqi_for(0.0, "au"))

    def test_clean_measures_strips_sentinels_but_keeps_the_rest(self):
        measures = {"headline": -9999.0, "now": 4.2, "24hr": -9999.0,
                    "humidity": 55.0, "temperature": -3.0}
        cleaned, rejected = poller.clean_measures(measures)
        self.assertIsNone(cleaned["headline"])
        self.assertIsNone(cleaned["24hr"])
        self.assertEqual(cleaned["now"], 4.2)
        self.assertEqual(sorted(rejected), ["24hr", "headline"])

    def test_a_sub_zero_temperature_is_not_treated_as_a_sentinel(self):
        """Airo is not Australia-only. Below freezing is a real temperature."""
        cleaned, rejected = poller.clean_measures(
            {"now": 4.0, "temperature": -12.0, "humidity": 80.0})
        self.assertEqual(cleaned["temperature"], -12.0)
        self.assertEqual(rejected, [])

    def test_pm25num_rejects_a_sentinel(self):
        self.assertIsNone(poller.pm25num(-9999))
        self.assertEqual(poller.pm25num("4.2"), 4.2)
        self.assertEqual(poller.pm25num(0), 0.0)


class TestPollSourceRejectsSentinels(unittest.TestCase):
    """clean_measures() being correct is worth nothing if poll_source stops
    calling it. This covers the wiring, not the helper."""

    def setUp(self):
        import tempfile, store as store_mod
        self.store = store_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store_mod.connect(Path(self.tmp.name) / "airo.db")
        self.addCleanup(self.conn.close)
        self.sid = store_mod.upsert_source(self.conn, "qld", "sou", "Southmoor")

    def _provider(self, measures):
        outer = self

        class Fake(poller.Provider):
            slug = "qld"
            needs_key = False
            resolution_minutes = 60

            def current(self, src, key):
                return dict(measures), {"site_id": "sou", "site_name": "Southmoor",
                                        "last_seen_utc": "2026-08-01T02:00:00+00:00"}
        return Fake()

    def test_a_sentinel_reading_is_stored_as_no_measurement(self):
        poller.poll_source(
            self.conn, self.sid, {"provider": "qld", "site_id": "sou"},
            self._provider({"headline": -9999.0, "now": -9999.0, "24hr": -9999.0}),
            {})
        row = self.conn.execute(
            "SELECT pm25, pm25_now, pm25_24hr FROM readings").fetchone()
        self.assertIsNone(row["pm25"], "-9999 was stored as a concentration")
        self.assertIsNone(row["pm25_now"])
        self.assertIsNone(row["pm25_24hr"])

    def test_a_real_reading_is_untouched(self):
        poller.poll_source(
            self.conn, self.sid, {"provider": "qld", "site_id": "sou"},
            self._provider({"headline": 4.2, "now": 4.2, "24hr": 3.8}), {})
        row = self.conn.execute("SELECT pm25, pm25_24hr FROM readings").fetchone()
        self.assertEqual(row["pm25"], 4.2)
        self.assertEqual(row["pm25_24hr"], 3.8)

    def test_a_partial_fault_keeps_the_good_channel(self):
        """Rule 5a: flag and show, never hide. A station whose 24h average is
        offline but whose live value works is still telling us something."""
        poller.poll_source(
            self.conn, self.sid, {"provider": "qld", "site_id": "sou"},
            self._provider({"headline": 4.2, "now": 4.2, "24hr": -9999.0}), {})
        row = self.conn.execute("SELECT pm25, pm25_24hr FROM readings").fetchone()
        self.assertEqual(row["pm25"], 4.2)
        self.assertIsNone(row["pm25_24hr"])


class TestProvidersReportPositionForTheNearestRule(unittest.TestCase):
    """`nearest` is the default rule and depends on coordinates. Only
    discover() returned them, so a source added by editing the config — which
    is what adding a second source by hand looks like — never acquired a
    position and was permanently excluded from the headline decision."""

    def test_the_poller_learns_coordinates_from_a_provider(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def poll_source")
        block = src[i:i + 2000]
        self.assertIn('meta.get("latitude")', block,
                      "coordinates reported by a provider are never stored")

    def test_the_queensland_provider_reports_a_position(self):
        # Scoped to current(), not the whole class: discover() contains the
        # same key, so a class-wide search passed with current()'s position
        # stripped out — the exact bug this is meant to catch.
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("class QldProvider")
        c = src.index("def current", i)
        block = src[c:src.index("def _station_position", c)]
        self.assertIn("_station_position", block,
                      "current() never looks up the station position")
        self.assertIn('"latitude"', block,
                      "current() omits the position, so 'nearest' cannot rank it")
        self.assertIn('"longitude"', block)

    def test_the_station_list_is_fetched_once_not_per_poll(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("_station_position")
        block = src[i - 400:i + 900]
        self.assertIn("_STATIONS", block, "the station list is refetched every poll")


class TestDisplayHelpersNothingWasExercising(unittest.TestCase):
    """Small functions whose whole job is to choose the words a user reads.

    Every branch of all four was unexercised. They are the reason the tray,
    the dashboard and the menu bar cannot disagree about which way the air is
    going — which only holds if they say what they are supposed to say.
    """

    # -- trend ---------------------------------------------------------

    def test_a_sharp_rise_is_called_a_sharp_rise(self):
        out = poller.compute_trend({"10min": 30.0, "60min": 20.0}, "au")
        self.assertEqual("rising_fast", out["direction"])
        self.assertIn("Rising sharply", out["text"])

    def test_a_gentle_rise_is_distinguished_from_a_sharp_one(self):
        out = poller.compute_trend({"10min": 23.0, "60min": 20.0}, "au")
        self.assertEqual("rising", out["direction"])

    def test_clearing_air_is_reported_as_clearing(self):
        out = poller.compute_trend({"10min": 10.0, "60min": 20.0}, "au")
        self.assertEqual("clearing", out["direction"])

    def test_a_small_change_is_steady_rather_than_noise_amplified(self):
        out = poller.compute_trend({"10min": 20.5, "60min": 20.0}, "au")
        self.assertEqual("steady", out["direction"])

    def test_a_missing_average_is_unknown_not_steady(self):
        """"Steady" is a claim. Without both averages there is nothing to
        compare, and saying "steady" would be inventing reassurance."""
        for averages in ({"10min": 20.0}, {"60min": 20.0}, {}):
            with self.subTest(averages=averages):
                self.assertEqual("unknown",
                                 poller.compute_trend(averages, "au")["direction"])

    def test_the_boundaries_are_where_they_are_documented(self):
        """Each threshold tested from both sides, or the constant could drift
        and every test would still pass."""
        self.assertEqual("rising", poller.compute_trend(
            {"10min": 24.9, "60min": 20.0}, "au")["direction"])
        self.assertEqual("rising_fast", poller.compute_trend(
            {"10min": 25.0, "60min": 20.0}, "au")["direction"])
        self.assertEqual("steady", poller.compute_trend(
            {"10min": 21.9, "60min": 20.0}, "au")["direction"])

    # -- risk window ---------------------------------------------------

    def test_a_disabled_window_gives_no_advice_at_all(self):
        """Somewhere flat and coastal has no evening trapping window, and
        asserting one would be inventing advice."""
        self.assertIsNone(poller.compute_time_hint({"risk_window": {"enabled": False}}))

    def test_inside_the_window_says_keep_filtering(self):
        hint = poller.compute_time_hint(
            {"risk_window": {"enabled": True, "start_hour": 15, "end_hour": 1}},
            now=datetime(2026, 7, 31, 18, 0))
        self.assertEqual("active", hint["state"])

    def test_two_hours_before_the_window_is_a_warning(self):
        hint = poller.compute_time_hint(
            {"risk_window": {"enabled": True, "start_hour": 15, "end_hour": 1}},
            now=datetime(2026, 7, 31, 13, 30))
        self.assertEqual("approaching", hint["state"])

    def test_the_middle_of_the_day_is_a_good_time_to_ventilate(self):
        hint = poller.compute_time_hint(
            {"risk_window": {"enabled": True, "start_hour": 15, "end_hour": 1}},
            now=datetime(2026, 7, 31, 9, 0))
        self.assertEqual("clear", hint["state"])

    def test_a_window_crossing_midnight_is_handled(self):
        """15:00 to 01:00 is the shape the tool is actually for, and it is the
        shape a naive start <= h < end comparison gets wrong."""
        cfg = {"risk_window": {"enabled": True, "start_hour": 15, "end_hour": 1}}
        for hour in (15, 20, 23, 0):
            with self.subTest(hour=hour):
                hint = poller.compute_time_hint(cfg, now=datetime(2026, 7, 31, hour))
                self.assertEqual("active", hint["state"],
                                 f"{hour:02d}:00 was not inside 15:00-01:00")

    def test_no_advice_is_a_medical_claim(self):
        cfg = {"risk_window": {"enabled": True, "start_hour": 15, "end_hour": 1}}
        for hour in range(24):
            hint = poller.compute_time_hint(cfg, now=datetime(2026, 7, 31, hour))
            words = (hint or {}).get("text", "").lower()
            for banned in ("safe", "asthma", "health", "diagnos"):
                self.assertNotIn(banned, words)

    # -- next poll -----------------------------------------------------

    def test_minutes_hours_and_both_are_phrased_for_a_menu(self):
        self.assertEqual("in 15 min", poller._next_poll_text({"poll_minutes": 15}))
        self.assertEqual("in 2 hr", poller._next_poll_text({"poll_minutes": 120}))
        self.assertEqual("in 1 hr 30 min",
                         poller._next_poll_text({"poll_minutes": 90}))

    def test_no_interval_means_on_demand_not_in_zero_minutes(self):
        self.assertEqual("on demand", poller._next_poll_text({"poll_minutes": 0}))
        self.assertEqual("on demand", poller._next_poll_text({"poll_minutes": -5}))

    def test_a_nonsense_interval_falls_back_rather_than_raising(self):
        self.assertEqual("in 15 min", poller._next_poll_text({"poll_minutes": "soon"}))

    # -- number parsing ------------------------------------------------

    def test_an_empty_field_is_nothing_rather_than_zero(self):
        """0.0 is a measurement. None is the absence of one, and a chart
        cannot tell them apart once they are the same value."""
        self.assertIsNone(poller.fnum(""))
        self.assertIsNone(poller.fnum(None))
        self.assertIsNone(poller.fnum("not a number"))
        self.assertEqual(0.0, poller.fnum("0"))
        self.assertEqual(12.5, poller.fnum("12.5"))

    def test_measures_that_are_not_a_dict_are_passed_through_untouched(self):
        """Returned as given rather than coerced. A provider that answers with
        something unexpected is a fault to surface, not a shape to invent."""
        cleaned, rejected = poller.clean_measures("nonsense")
        self.assertEqual("nonsense", cleaned)
        self.assertEqual([], rejected)

    def test_a_scale_with_no_span_does_not_divide_by_zero(self):
        """A breakpoint segment whose two ends are equal divides by zero
        inside the conversion every surface calls -- so one malformed row in a
        scale table takes down the dashboard, the tray and the poll at once.

        Injected rather than hoped for: none of the shipped scales has such a
        segment, so asserting against them proves nothing about the guard.
        """
        poller.SCALES["degenerate"] = {
            "label": "Degenerate", "unit": "test",
            "breakpoints": [(0.0, 0.0, 0, 50, "Flat"),
                            (0.0, 25.0, 50, 100, "Normal")],
            "bands": poller.SCALES["au"].get("bands", {}),
        }
        try:
            self.assertEqual(0.0, poller.aqi_for(0.0, "degenerate"))
        finally:
            poller.SCALES.pop("degenerate", None)

    def test_a_reading_with_no_value_has_no_index(self):
        self.assertIsNone(poller.aqi_for(None, "au"))


class TestTheBandMatchesTheNumberOnScreen(unittest.TestCase):
    """ARCHITECTURE §3.4. Display rounds; classification must round the same
    way, or a reading shown as "33" is coloured as though it were 33.2.

    This was written as an instruction -- "callers must pass the displayed
    value" -- and followed in one place out of nine. band_for() rounds now, so
    a caller cannot get it wrong.
    """

    def test_a_value_just_over_a_boundary_bands_as_what_is_shown(self):
        """33.2 displays as 33, which is Very good. Banded unrounded it is
        Good: a different word and a different colour for the same number."""
        self.assertEqual(poller.band_for(33, "au"), poller.band_for(33.2, "au"))

    def test_a_value_that_rounds_up_bands_as_what_is_shown(self):
        """33.6 displays as 34, which is Good — so it must not be shown in the
        Very good colour."""
        self.assertEqual(poller.band_for(34, "au"), poller.band_for(33.6, "au"))
        self.assertNotEqual(poller.band_for(33.6, "au"), poller.band_for(33.2, "au"))

    def test_no_reading_is_not_a_band(self):
        self.assertEqual("No data", poller.band_for(None, "au"))


class TestBandsAreServedNotRestated(unittest.TestCase):
    """The boundaries and the names are a health-relevant judgement, and the
    dashboard had two copies of them — one for the colours and one for the
    chart background — both Australian whatever scale was configured."""

    def dashboard(self):
        return (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_every_scale_can_produce_its_own_bands(self):
        for name in poller.SCALES:
            with self.subTest(scale=name):
                bands = poller.scale_bands(name)
                self.assertTrue(bands, f"{name} has no bands")
                # "max" and "name" always; "advice" only where the scale names
                # one, and nothing else. Stated as a subset rather than an
                # equality so an advice-carrying band is legal, and as a
                # bounded subset so a new key cannot arrive unnoticed.
                for b in bands:
                    self.assertLessEqual(set(b), {"max", "name", "advice"},
                                         f"{name} serves an unexpected key")
                    self.assertLessEqual({"max", "name"}, set(b),
                                         f"{name} is missing max or name")

    def test_the_scales_do_not_share_band_names(self):
        """If they did, this test could not tell whether the dashboard was
        using the configured scale or the Australian one."""
        au = [b["name"] for b in poller.scale_bands("au")]
        us = [b["name"] for b in poller.scale_bands("us_epa")]
        self.assertNotEqual(au, us)

    def test_the_dashboard_takes_the_bands_from_the_response(self):
        html = self.dashboard()
        self.assertIn("adoptBands(", html)
        self.assertIn("latest.bands", html)

    def test_the_chart_background_is_derived_not_rewritten(self):
        """A second literal table is a second opinion about a health-relevant
        boundary."""
        html = self.dashboard()
        i = html.index("const BANDS_D")
        block = html[i:i + 400]
        self.assertIn("BANDS.map", block)
        for literal in ("'--fair','Fair'", "[66,99", "[99,149"):
            self.assertNotIn(literal, block,
                             "the chart background restates the boundaries")

    def static_markup(self):
        """The page with its script and its comments taken out.

        Both have to go. The script legitimately holds the Australian ceilings
        as a first-paint bootstrap — replaced by `adoptBands()` before anything
        is drawn — and the file's opening comment discusses the bug this class
        exists for by name. Neither is a restatement; only markup is, because
        markup is what a reader sees and nothing can update it.
        """
        html = self.dashboard()
        html = re.sub(r"<script(?![^>]*\bsrc=)[^>]*>.*?</script>", " ", html,
                      flags=re.S)
        return re.sub(r"<!--.*?-->", " ", html, flags=re.S)

    def test_the_legend_is_not_a_second_band_table_in_markup(self):
        """The finding this test was extended for.

        `adoptBands()` replaced the JS table from the served payload and the
        legend beside the chart kept its hand-written one: "0–33 Very good …
        150–200 Very poor", five Australian rows, in HTML. On a us_epa install
        the chart recoloured itself around EPA breakpoints while the key under
        it still read Australian — the two disagreeing in the same glance —
        and even on an AU install the list stopped at Very poor, so the
        Hazardous band had a colour on the chart and no name anywhere.

        Static markup cannot be corrected at run time, so the rule is that it
        must not carry the table at all. One band name in prose is survivable
        ("Good" is an ordinary English word); two from the same scale is a
        table someone typed out.
        """
        markup = self.static_markup()
        for name in poller.SCALES:
            with self.subTest(scale=name):
                present = [b["name"] for b in poller.scale_bands(name)
                           if re.search(r"(?<![A-Za-z])" + re.escape(b["name"])
                                        + r"(?![A-Za-z])", markup)]
                self.assertLess(
                    len(present), 2,
                    f"the {name} band table is written out in the page's "
                    f"markup: {present}. Markup cannot be corrected by "
                    f"adoptBands(), so it will be wrong on every other scale.")

    def test_no_boundary_is_printed_beside_a_band_name(self):
        """The shape of the old legend, caught directly: a numeric range
        followed by the name of the band it covers. A boundary in markup is
        the same second opinion as a boundary in the script and worse, because
        nothing on the page can overrule it."""
        found = re.findall(r"\d+\s*[–—-]\s*\d+\s+[A-Z][a-z]+(?:\s+[a-z]+)?",
                           self.static_markup())
        self.assertEqual([], found,
                         f"band boundaries are spelled out in markup: {found}")

    def test_no_scale_is_named_in_markup(self):
        """"Colour is the Australian AQI band" was printed under the heatmap
        whatever scale was configured — a caption asserting the wrong national
        standard for the colours directly above it. The label is served in
        every payload (`scale_label`); the page must take it from there."""
        markup = self.static_markup()
        for name, scale in poller.SCALES.items():
            with self.subTest(scale=name):
                # assertNotIn would print the whole page on failure.
                self.assertFalse(
                    scale["label"] in markup,
                    f"the page names {scale['label']} in markup, so it says so "
                    f"on installs configured for one of the other scales")

    def test_the_legend_is_rendered_from_the_served_table(self):
        """The other half: having removed the literal, the page has to build
        the key from `BANDS` — which is what `adoptBands()` fills in — or the
        chart has colours with nothing naming them."""
        html = self.dashboard()
        i = html.index("function renderLegend")
        block = html[i:i + 900]
        self.assertIn("BANDS", block,
                      "the legend is not built from the served band table")

    def test_band_for_rounds_inside_rather_than_asking_callers_to(self):
        import inspect
        src = inspect.getsource(poller.band_for)
        self.assertIn("round(aqi)", src)

    def test_the_page_rounds_inside_band_for_too(self):
        html = self.dashboard()
        i = html.index("const bandFor =")
        self.assertIn("Math.round(v)", html[i:i + 200],
                      "the page still relies on callers rounding first")


class TestAdviceIsServedNotJoinedByPosition(unittest.TestCase):
    """The sentence under the headline is a health decision (rule 7, D8).

    It was six sentences in a JS table in dashboard.html, handed to whichever
    served band arrived in the same array slot. Every scale has six bands, so
    the join never threw and never noticed: on the raw ug/m3 scale the band
    "Above WHO guideline" is second, and second in the table was "Enjoy normal
    activities." — advice written for an Australian band that stops at 16.5
    ug/m3, printed under a reading above the WHO guideline.

    The sentences now sit in SCALES beside the bands they were written for.
    Nothing here is new wording; the strings moved unchanged, which is why
    this class pins them literally rather than reading them back out of the
    table it is meant to be checking.
    """

    #: As they were written, in band order.
    AU_ADVICE = [
        "Enjoy normal activities.",
        "Enjoy normal activities.",
        "Sensitive people should reduce prolonged outdoor exertion.",
        "Close up and filter. Avoid outdoor exertion.",
        "Everyone should avoid outdoor exertion.",
        "Stay indoors with filtration running.",
    ]

    def served(self, scale):
        return poller.scale_bands(scale)

    def test_the_australian_bands_carry_the_advice_written_for_them(self):
        self.assertEqual(
            self.AU_ADVICE,
            [b.get("advice") for b in self.served("au")])

    def test_each_sentence_sits_beside_its_own_band(self):
        """Order is the whole bug. A list that matches as a set and not as a
        sequence is the same mistake in a new place."""
        paired = [(b["name"], b.get("advice")) for b in self.served("au")]
        self.assertEqual(
            [("Very good", self.AU_ADVICE[0]), ("Good", self.AU_ADVICE[1]),
             ("Fair", self.AU_ADVICE[2]), ("Poor", self.AU_ADVICE[3]),
             ("Very poor", self.AU_ADVICE[4]),
             ("Hazardous", self.AU_ADVICE[5])],
            paired)

    def test_the_raw_scale_serves_no_advice_at_all(self):
        """Absent, not empty. Nobody has written wording for a scale cut
        against a guideline rather than a national index, and the honest
        rendering of that is the band name alone."""
        for band in self.served("raw"):
            self.assertNotIn("advice", band,
                             f"raw band {band['name']!r} carries advice")

    def test_the_above_who_guideline_band_is_not_told_to_enjoy_itself(self):
        """The finding, stated as the thing that must never come back."""
        band = next(b for b in self.served("raw")
                    if b["name"] == "Above WHO guideline")
        self.assertNotIn("advice", band)
        self.assertNotIn("Enjoy normal activities.", json.dumps(band))

    def test_the_epa_scale_serves_no_advice_either(self):
        """"Moderate" reaches 35.4 ug/m3 where the Australian band carrying
        "Enjoy normal activities." stops at 16.5. The categories are not a
        one-to-one fit, so the list is not lent to them."""
        for band in self.served("us_epa"):
            self.assertNotIn("advice", band,
                             f"us_epa band {band['name']!r} carries advice")

    def test_no_other_scale_borrows_the_australian_sentences(self):
        """Enumerated over SCALES, so a scale added tomorrow is in scope. A
        new scale may have advice; it may not have *these*, which were written
        against Australian boundaries."""
        for name in poller.SCALES:
            if name == "au":
                continue
            served = json.dumps(self.served(name))
            for sentence in self.AU_ADVICE:
                self.assertNotIn(
                    sentence, served,
                    f"{name} is being served advice written for the "
                    f"Australian scale")

    def test_the_page_keeps_no_copy_of_any_sentence(self):
        """The discriminating check: if the words are still reachable in the
        page, a future edit reaches them and the server stops being the only
        voice. Comments are stripped first — the block explaining why the
        table is gone quotes the wording, and a check that greps raw text
        cannot tell a comment from code."""
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        script = re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)[-1]
        code = re.sub(r"/\*.*?\*/", " ", script, flags=re.S)
        code = re.sub(r"(?m)//.*$", " ", code)
        for sentence in self.AU_ADVICE:
            self.assertNotIn(sentence, code,
                             f"the page still carries {sentence!r}")

    def test_the_page_has_no_severity_table_left_to_join(self):
        """The table itself, by name. What survives is a colour ramp, and a
        colour is presentation; an ordinal ramp over six ordered bands is a
        join that means the same thing on every scale."""
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("SEVERITIES", html)
        self.assertIn("const BAND_CSS", html)


class TestWhatWeWriteIsActuallyJson(unittest.TestCase):
    """Python's json emits `Infinity` and `NaN` as bare literals and reads
    them back again, so nothing looks wrong from inside Python. They are not
    JSON. Every other parser rejects the *whole file*.

    That is not hypothetical: a band ceiling of infinity went into latest.json
    and the Rust tray showed "No reading yet" beside a database full of
    readings — a partial failure that reads as no data at all.
    """

    def test_no_scale_produces_a_value_json_cannot_represent(self):
        for name in poller.SCALES:
            with self.subTest(scale=name):
                json.dumps(poller.scale_bands(name), allow_nan=False)

    def test_the_open_ended_band_says_so_with_null(self):
        top = poller.scale_bands("au")[-1]
        self.assertIsNone(top["max"], "the top band has a numeric ceiling")
        self.assertEqual("Hazardous", poller.band_for(9_999, "au"),
                         "a null ceiling must still catch everything above it")

    def writing_into(self, td):
        """Repoint DATA *and everything under it*, as the harness rule
        requires -- write_json_atomic() creates DATA, and a test that moves
        only DATA leaves the rest pointing at the real ~/.airo."""
        saved = (poller.DATA, poller.LATEST_PATH, poller.LOG_PATH,
                 poller.CSV_PATH, poller.ALERT_STATE_PATH)
        poller.DATA = Path(td)
        poller.LATEST_PATH = poller.DATA / "latest.json"
        poller.FORECAST_PENDING_PATH = poller.DATA / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = poller.DATA / "forecast_skill.json"
        poller.LOG_PATH = poller.DATA / "poller.log"
        poller.CSV_PATH = poller.DATA / "readings.csv"
        poller.ALERT_STATE_PATH = poller.DATA / "alert_state.json"

        def restore():
            (poller.DATA, poller.LATEST_PATH, poller.LOG_PATH,
             poller.CSV_PATH, poller.ALERT_STATE_PATH) = saved
        self.addCleanup(restore)

    def test_the_writer_refuses_rather_than_writing_bad_json(self):
        """Loud is the right failure. A poll that cannot produce valid output
        must not leave a file the tray silently cannot read."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.writing_into(td)
            with self.assertRaises(ValueError):
                poller.write_json_atomic(Path(td) / "x.json",
                                         {"bad": float("inf")})
            self.assertFalse((Path(td) / "x.json").exists(),
                             "an unreadable file was left behind")

    def test_a_normal_payload_still_writes(self):
        """The control: without it the test above passes against a writer that
        refuses everything."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.writing_into(td)
            poller.write_json_atomic(Path(td) / "ok.json", {"aqi": 12.0})
            self.assertEqual({"aqi": 12.0}, json.loads(
                (Path(td) / "ok.json").read_text(encoding="utf-8")))


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
