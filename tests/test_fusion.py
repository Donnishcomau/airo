# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fusion tests.

This decides what single number a user sees, and they may open or close a
window because of it. The properties that matter most are the negative ones:
stale data must never be presented as current, faults must never be chosen,
and only the explicitly opt-in 'blend' rule may report a value no instrument
measured.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fusion  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)


# A fixed instant. Every call that judges staleness must be anchored to it --
# a test that reads the wall clock passes on the day it was written and fails
# the next, which is worse than no test because the failure looks like a
# regression in the code rather than in the fixture.
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

# A synthetic reference point. Coordinates in a test suite should not
# describe where any real person lives, so the whole frame is shifted;
# relative geometry between the fixtures is what the tests actually check.
HOME = {"latitude": -33.5000, "longitude": 151.0000}


def reading(pm25, age_min, *, provider="purpleair", site="a", lat=None, lon=None,
            resolution=10, quality="ok"):
    return {
        "provider": provider,
        "site_id": site,
        "site_name": site,
        "pm25": pm25,
        "observed_utc": (NOW - timedelta(minutes=age_min)).isoformat(timespec="seconds"),
        "latitude": lat,
        "longitude": lon,
        "resolution_minutes": resolution,
        "quality": quality,
    }


class TestDistance(unittest.TestCase):
    def test_haversine_matches_known_distance(self):
        # Two points ~6 km apart.
        km = fusion.haversine_km(-33.5000, 151.0000, -33.5500, 151.0050)
        self.assertTrue(5.0 < km < 7.0, f"got {km}")

    def test_missing_coordinates_give_none(self):
        self.assertIsNone(fusion.haversine_km(None, 151.0, -33.4, 151.0))

    def test_zero_distance(self):
        self.assertAlmostEqual(
            fusion.haversine_km(-33.4, 151.0, -33.4, 151.0), 0.0, places=6)


class TestStaleness(unittest.TestCase):
    def test_staleness_is_judged_against_the_source_interval(self):
        """40 minutes is an outage for a 10-min sensor, normal for an hourly feed."""
        fast = reading(5.0, 40, resolution=10)
        slow = reading(5.0, 40, resolution=60)
        self.assertTrue(fusion.is_stale(fast, NOW))
        self.assertFalse(fusion.is_stale(slow, NOW))

    def test_one_missed_report_is_tolerated(self):
        self.assertFalse(fusion.is_stale(reading(5.0, 15, resolution=10), NOW))

    def test_missing_timestamp_is_stale(self):
        self.assertTrue(fusion.is_stale({"resolution_minutes": 10}, NOW))


class TestNearest(unittest.TestCase):
    def test_picks_the_closest_source(self):
        near = reading(20.0, 5, site="near", lat=-33.5001, lon=151.0001)
        far = reading(4.0, 5, site="far", lat=-33.5500, lon=151.1000)
        r = fusion.fuse([far, near], "nearest", HOME, NOW)
        self.assertEqual(r["pm25"], 20.0)
        self.assertEqual(r["source"]["site_id"], "near")

    def test_skips_a_nearer_but_stale_source(self):
        """A dead sensor next door must not beat a live one down the road."""
        near_dead = reading(20.0, 300, site="near", lat=-33.5001, lon=151.0001)
        far_live = reading(4.0, 5, site="far", lat=-33.5500, lon=151.1000)
        r = fusion.fuse([near_dead, far_live], "nearest", HOME, NOW)
        self.assertEqual(r["source"]["site_id"], "far")
        self.assertTrue(r["degraded"])

    def test_skips_a_nearer_but_faulty_source(self):
        faulty = reading(4176.0, 2, site="near", lat=-33.5001, lon=151.0001,
                         quality="suspect")
        good = reading(6.0, 5, site="far", lat=-33.5000, lon=151.0800)
        r = fusion.fuse([faulty, good], "nearest", HOME, NOW)
        self.assertEqual(r["pm25"], 6.0)

    def test_falls_back_to_freshest_without_coordinates(self):
        a = reading(9.0, 30, site="a")
        b = reading(3.0, 2, site="b")
        r = fusion.fuse([a, b], "nearest", HOME, NOW)
        self.assertEqual(r["source"]["site_id"], "b")
        self.assertIn("coordinates", r["note"])

    def test_is_the_default_rule(self):
        self.assertEqual(fusion.DEFAULT_RULE, "nearest")
        near = reading(20.0, 5, site="near", lat=-33.5001, lon=151.0001)
        far = reading(4.0, 1, site="far", lat=-33.5500, lon=151.1000)
        default = fusion.fuse([far, near], location=HOME, now=NOW)
        self.assertEqual(default["source"]["site_id"], "near")


class TestFreshest(unittest.TestCase):
    def test_picks_the_most_recent(self):
        old = reading(9.0, 50, site="old", resolution=60)
        new = reading(3.0, 2, site="new")
        r = fusion.fuse([old, new], "freshest", HOME, NOW)
        self.assertEqual(r["source"]["site_id"], "new")

    def test_ignores_distance(self):
        far_fresh = reading(3.0, 1, site="far", lat=-34.0, lon=152.0)
        near_older = reading(9.0, 8, site="near", lat=-33.5001, lon=151.0001)
        r = fusion.fuse([far_fresh, near_older], "freshest", HOME, NOW)
        self.assertEqual(r["source"]["site_id"], "far")


class TestAll(unittest.TestCase):
    def test_returns_every_usable_source(self):
        a = reading(5.0, 2, site="a", lat=-33.50, lon=151.00)
        b = reading(8.0, 3, site="b", lat=-33.45, lon=151.05)
        r = fusion.fuse([a, b], "all", HOME, NOW)
        self.assertEqual(len(r["contributing"]), 2)

    def test_still_yields_one_headline_for_the_tray(self):
        a = reading(5.0, 2, site="a", lat=-33.5001, lon=151.0001)
        b = reading(8.0, 1, site="b", lat=-33.9, lon=151.5)
        r = fusion.fuse([a, b], "all", HOME, NOW)
        self.assertEqual(r["pm25"], 5.0, "headline should be the nearest usable")


class TestBlend(unittest.TestCase):
    def test_blend_lands_between_the_inputs(self):
        a = reading(4.0, 2, site="a", lat=-33.5001, lon=151.0001)
        b = reading(10.0, 2, site="b", lat=-33.4600, lon=151.0300)
        r = fusion.fuse([a, b], "blend", HOME, NOW)
        self.assertTrue(4.0 < r["pm25"] < 10.0, f"got {r['pm25']}")

    def test_nearer_source_dominates(self):
        near = reading(4.0, 2, site="near", lat=-33.5001, lon=151.0001)
        far = reading(40.0, 2, site="far", lat=-34.5, lon=152.0)
        r = fusion.fuse([near, far], "blend", HOME, NOW)
        self.assertLess(r["pm25"], 22.0, "distance weighting had no effect")

    def test_is_labelled_as_computed(self):
        a = reading(4.0, 2, site="a", lat=-33.50, lon=151.00)
        b = reading(10.0, 2, site="b", lat=-33.46, lon=151.03)
        r = fusion.fuse([a, b], "blend", HOME, NOW)
        self.assertIn("computed", r["note"])

    def test_single_source_blend_returns_that_value(self):
        a = reading(7.0, 2, site="a", lat=-33.50, lon=151.00)
        r = fusion.fuse([a], "blend", HOME, NOW)
        self.assertAlmostEqual(r["pm25"], 7.0, places=6)


class TestNoUsableData(unittest.TestCase):
    def test_all_stale_returns_none_not_a_stale_number(self):
        a = reading(5.0, 500, site="a", lat=-33.50, lon=151.00)
        r = fusion.fuse([a], "nearest", HOME, NOW)
        self.assertIsNone(r["pm25"], "stale data must never be shown as current")
        self.assertIn("recently", r["note"])

    def test_last_known_is_surfaced_for_display(self):
        a = reading(5.0, 500, site="a", lat=-33.50, lon=151.00)
        r = fusion.fuse([a], "nearest", HOME, NOW)
        self.assertIn("last_known", r)
        self.assertEqual(r["last_known"]["pm25"], 5.0)

    def test_no_sources_at_all(self):
        r = fusion.fuse([], "nearest", HOME, NOW)
        self.assertIsNone(r["pm25"])
        self.assertIn("no data", r["note"])

    def test_null_readings_are_not_chosen(self):
        a = reading(None, 2, site="a", lat=-33.50, lon=151.00)
        r = fusion.fuse([a], "nearest", HOME, NOW)
        self.assertIsNone(r["pm25"])


class TestRuleValidation(unittest.TestCase):
    def test_unknown_rule_falls_back_to_default(self):
        a = reading(5.0, 2, site="a", lat=-33.50, lon=151.00)
        r = fusion.fuse([a], "nonsense", HOME, NOW)
        self.assertEqual(r["rule"], fusion.DEFAULT_RULE)

    def test_all_advertised_rules_work(self):
        a = reading(5.0, 2, site="a", lat=-33.5001, lon=151.0001)
        b = reading(9.0, 3, site="b", lat=-33.4600, lon=151.0300)
        for rule in fusion.RULES:
            r = fusion.fuse([a, b], rule, HOME, NOW)
            self.assertIsNotNone(r["pm25"], f"rule {rule} produced nothing")


class TestDescribe(unittest.TestCase):
    def test_names_the_source_and_its_age(self):
        a = reading(5.0, 3, site="Example sensor", lat=-33.5001, lon=151.0001)
        text = fusion.describe(fusion.fuse([a], "nearest", HOME, NOW))
        self.assertIn("Example sensor", text)
        self.assertIn("3 min ago", text)

    def test_reports_no_data_honestly(self):
        self.assertEqual(fusion.describe(fusion.fuse([], "nearest", HOME, NOW)),
                         "no data from any configured source")

    def test_blend_says_it_is_a_blend(self):
        a = reading(4.0, 2, site="a", lat=-33.50, lon=151.00)
        b = reading(10.0, 2, site="b", lat=-33.46, lon=151.03)
        text = fusion.describe(fusion.fuse([a, b], "blend", HOME, NOW))
        self.assertIn("blend", text)


class TestCorroboration(unittest.TestCase):
    """Distinguishing a real local event from a false positive.

    The motivating case: a sensor read 11x its neighbours. Every other sensor
    in the city was in single digits. That is either a fire next door or a
    fault -- and either way it is not the regional air quality the number
    implies.
    """

    def test_agreeing_sources_are_corroborated(self):
        a = reading(20.0, 2, site="a", lat=-33.50, lon=151.00)
        b = reading(18.0, 2, site="b", lat=-33.45, lon=151.05)
        for r in fusion.corroborate([a, b], now=NOW):
            self.assertEqual(r["corroboration"], "corroborated", r["site_id"])

    def test_lone_high_source_is_flagged(self):
        hot = reading(57.2, 2, site="hot", lat=-33.50, lon=151.00)
        hot["source_id"] = 1
        calm = reading(4.8, 2, site="calm", lat=-33.56, lon=151.07, resolution=60)
        calm["source_id"] = 2
        out = fusion.corroborate([hot, calm], now=NOW)
        flagged = [r for r in out if r["site_id"] == "hot"][0]
        self.assertEqual(flagged["corroboration"], "uncorroborated")
        self.assertGreater(flagged["peer_ratio"], 10)

    def test_history_rescues_a_site_that_always_runs_high(self):
        """A valley sensor that habitually reads 4x must not be called a fault."""
        hot = reading(20.0, 2, site="valley", lat=-33.50, lon=151.00)
        hot["source_id"] = 1
        calm = reading(5.0, 2, site="ridge", lat=-33.56, lon=151.07, resolution=60)
        calm["source_id"] = 2
        history = {1: {"n": 200, "median": 3.0, "p90": 4.0, "max": 6.0}}
        out = fusion.corroborate([hot, calm], history, now=NOW)
        v = [r for r in out if r["site_id"] == "valley"][0]
        self.assertEqual(v["corroboration"], "typical_for_site")

    def test_history_does_not_excuse_a_genuine_outlier(self):
        hot = reading(57.2, 2, site="valley", lat=-33.50, lon=151.00)
        hot["source_id"] = 1
        calm = reading(4.8, 2, site="ridge", lat=-33.56, lon=151.07, resolution=60)
        calm["source_id"] = 2
        # Normally up to 2.6x; today 11.9x.
        history = {1: {"n": 168, "median": 1.11, "p90": 2.59, "max": 4.75}}
        out = fusion.corroborate([hot, calm], history, now=NOW)
        v = [r for r in out if r["site_id"] == "valley"][0]
        self.assertEqual(v["corroboration"], "uncorroborated")
        self.assertIn("2.6x", v["corroboration_note"])

    def test_thin_history_is_admitted_not_guessed(self):
        hot = reading(57.2, 2, site="a", lat=-33.50, lon=151.00)
        hot["source_id"] = 1
        calm = reading(4.8, 2, site="b", lat=-33.56, lon=151.07, resolution=60)
        calm["source_id"] = 2
        out = fusion.corroborate([hot, calm], {1: {"n": 3, "p90": 9.0}}, now=NOW)
        v = [r for r in out if r["site_id"] == "a"][0]
        self.assertEqual(v["corroboration"], "uncorroborated")
        self.assertIn("not enough history", v["corroboration_note"])

    def test_low_absolute_values_never_trigger_a_flag(self):
        """2 vs 0.2 ug/m3 is a 10x ratio and completely meaningless."""
        a = reading(2.0, 2, site="a", lat=-33.50, lon=151.00)
        a["source_id"] = 1
        b = reading(0.2, 2, site="b", lat=-33.45, lon=151.05)
        b["source_id"] = 2
        out = fusion.corroborate([a, b], now=NOW)
        self.assertEqual([r for r in out if r["site_id"] == "a"][0]["corroboration"],
                         "corroborated")

    def test_single_source_says_so_rather_than_claiming_agreement(self):
        a = reading(57.0, 2, site="a", lat=-33.50, lon=151.00)
        a["source_id"] = 1
        out = fusion.corroborate([a], now=NOW)
        self.assertEqual(out[0]["corroboration"], "single_source")

    def test_stale_peers_do_not_count_as_corroboration(self):
        hot = reading(57.0, 2, site="hot", lat=-33.50, lon=151.00)
        hot["source_id"] = 1
        dead = reading(55.0, 900, site="dead", lat=-33.45, lon=151.05)
        dead["source_id"] = 2
        out = fusion.corroborate([hot, dead], now=NOW)
        self.assertEqual([r for r in out if r["site_id"] == "hot"][0]["corroboration"],
                         "single_source")

    def test_fuse_surfaces_the_flag_on_the_headline(self):
        hot = reading(57.2, 2, site="hot", lat=-33.5001, lon=151.0001)
        hot["source_id"] = 1
        calm = reading(4.8, 2, site="calm", lat=-33.56, lon=151.07, resolution=60)
        calm["source_id"] = 2
        r = fusion.fuse([hot, calm], "nearest", HOME, NOW,
                        history={1: {"n": 168, "p90": 2.59}})
        self.assertEqual(r["source"]["site_id"], "hot")
        self.assertTrue(r["uncorroborated"])
        self.assertIn("nearby sources", r["corroboration_note"])

    def test_uncorroborated_reading_is_still_reported(self):
        """Flagged, never hidden -- a fire next door is real air."""
        hot = reading(57.2, 2, site="hot", lat=-33.5001, lon=151.0001)
        hot["source_id"] = 1
        calm = reading(4.8, 2, site="calm", lat=-33.56, lon=151.07, resolution=60)
        calm["source_id"] = 2
        r = fusion.fuse([hot, calm], "nearest", HOME, NOW)
        self.assertEqual(r["pm25"], 57.2)


class TestChannelFaults(unittest.TestCase):
    """A PurpleAir's two laser counters disagreeing is an instrument fault."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import store
        self.store = store

    def test_agreeing_channels_pass(self):
        self.assertEqual(self.store.assess_quality(53.7, 56.5, 50.9, 100), "ok")

    def test_disagreeing_channels_are_suspect(self):
        self.assertEqual(self.store.assess_quality(53.7, 260.0, 4.0, 100), "suspect")

    def test_one_dead_channel_is_suspect(self):
        self.assertEqual(self.store.assess_quality(30.0, 60.0, 0.0, 100), "suspect")

    def test_low_provider_confidence_is_suspect(self):
        self.assertEqual(self.store.assess_quality(20.0, 20.0, 20.0, 12), "suspect")

    def test_small_values_are_not_false_flagged(self):
        """0.5 vs 2.0 is a 4x ratio on noise, not a fault."""
        self.assertEqual(self.store.assess_quality(1.2, 2.0, 0.5, 100), "ok")

    def test_absurd_value_is_extreme_without_channel_data(self):
        """With no channels and no confidence figure there is no evidence
        about the *instrument* -- only an enormous number, which is a claim
        about the air. Marked 'extreme': shown and counted, so a chart draws
        it and an alert fires, while fusion's corroboration decides whether a
        lone site reading this far above its peers should be believed."""
        self.assertEqual(self.store.assess_quality(4176.0), "extreme")

    def test_missing_channel_data_does_not_flag(self):
        self.assertEqual(self.store.assess_quality(25.0, None, None, None), "ok")




class TestASourceWithoutCoordinatesIsNotSilentlyIgnored(unittest.TestCase):
    """The default rule is 'nearest', so a source with no stored position can
    never be chosen however close it actually is. That meant the headline came
    from a station 3.1 km away while the one at 1.9 km sat in the list unused,
    with nothing said. On the reference install two
    sources differ 4x — picking wrong shows 'Very good' to someone breathing
    'Fair' air."""

    def rows(self):
        # "Near" has no coordinates at all, which is the real condition: a
        # source added by editing the config rather than through setup.
        return [
            reading(20.0, 5, provider="qld", site="Near"),
            reading(4.0, 5, provider="qld", site="Far",
                    lat=HOME["latitude"] + 0.07, lon=HOME["longitude"]),
        ]

    def test_the_headline_still_comes_from_a_located_source(self):
        out = fusion.fuse(self.rows(), "nearest", HOME, NOW)
        self.assertEqual(out["pm25"], 4.0)

    def test_but_the_excluded_source_is_named(self):
        out = fusion.fuse(self.rows(), "nearest", HOME, NOW)
        self.assertIsNotNone(out.get("note"), "the exclusion was silent")
        self.assertIn("Near", out["note"])
        self.assertIn("coordinates", out["note"].lower())

    def test_the_note_says_how_to_fix_it(self):
        out = fusion.fuse(self.rows(), "nearest", HOME, NOW)
        self.assertIn("setup", out["note"].lower())

    def test_no_note_when_every_source_is_located(self):
        rows = [
            reading(20.0, 5, provider="qld", site="Near",
                    lat=HOME["latitude"] + 0.01, lon=HOME["longitude"]),
            reading(4.0, 5, provider="qld", site="Far",
                    lat=HOME["latitude"] + 0.07, lon=HOME["longitude"]),
        ]
        out = fusion.fuse(rows, "nearest", HOME, NOW)
        self.assertIsNone(out.get("note"))

    def test_all_unlocated_still_falls_back_to_freshest(self):
        rows = [reading(20.0, 40, provider="qld", site="Older"),
                reading(4.0, 5, provider="qld", site="Newer")]
        out = fusion.fuse(rows, "nearest", HOME, NOW)
        self.assertEqual(out["pm25"], 4.0, "did not fall back to the freshest")


class TestCorroborationCannotDefeatItself(unittest.TestCase):
    """Guards inside corroborate() that no test exercised.

    Found by removing each guard in turn and seeing whether anything went red.
    These five did not, which means corroboration -- the mechanism that decides
    whether a reading is supported by its neighbours -- could have lost any of
    them in a refactor without a single failure.
    """

    def test_a_lone_source_does_not_corroborate_itself(self):
        """Without the identity check a reading is its own peer: the median of
        one value is that value, the ratio is exactly 1.0, and every single
        source install reports 'in line with nearby sources' while having no
        nearby sources at all. That is the most reassuring answer the tool has,
        given for no evidence."""
        out = fusion.corroborate([reading(200.0, 5, site="only")], now=NOW)
        self.assertEqual("single_source", out[0]["corroboration"])
        self.assertIsNone(out[0]["peer_pm25"])

    def test_two_sources_without_ids_still_see_each_other(self):
        """The inverse, and a bug that already shipped: comparing source_id
        when both are None gives None != None, which is False, so every peer
        was excluded and a two-source setup reported single_source."""
        rows = [reading(10.0, 5, site="a"), reading(10.5, 5, site="b")]
        out = fusion.corroborate(rows, now=NOW)
        for r in out:
            self.assertNotEqual("single_source", r["corroboration"],
                                "a real peer was excluded")
            self.assertIsNotNone(r["peer_pm25"])

    def test_two_readings_from_one_source_do_not_corroborate_each_other(self):
        """Same source_id means the same instrument. An instrument agreeing
        with itself is not corroboration, however many rows it produced."""
        rows = [reading(150.0, 5, site="a"), reading(150.0, 4, site="a")]
        for r in rows:
            r["source_id"] = 7
        out = fusion.corroborate(rows, now=NOW)
        for r in out:
            self.assertEqual("single_source", r["corroboration"])

    def test_a_stale_reading_is_never_given_a_corroboration_verdict(self):
        """Stale data must never be presented as confirmed-current. A stale
        reading labelled 'corroborated' is exactly the failure ARCHITECTURE
        2.5c exists to prevent."""
        rows = [reading(10.0, 5, site="fresh"),
                reading(11.0, 600, site="ancient", resolution=10)]
        out = fusion.corroborate(rows, now=NOW)
        old = [r for r in out if r["site_id"] == "ancient"][0]
        self.assertTrue(old["stale"])
        self.assertEqual("unknown", old["corroboration"])
        self.assertIsNone(old["corroboration_note"])

    def test_a_suspect_reading_is_never_given_a_corroboration_verdict(self):
        rows = [reading(10.0, 5, site="ok"),
                reading(9000.0, 5, site="broken", quality="suspect")]
        out = fusion.corroborate(rows, now=NOW)
        bad = [r for r in out if r["site_id"] == "broken"][0]
        self.assertEqual("unknown", bad["corroboration"])

    def test_a_reading_with_no_value_is_never_given_a_verdict(self):
        rows = [reading(10.0, 5, site="ok"), reading(None, 5, site="empty")]
        out = fusion.corroborate(rows, now=NOW)
        empty = [r for r in out if r["site_id"] == "empty"][0]
        self.assertEqual("unknown", empty["corroboration"])

    def test_peers_reading_zero_produce_unknown_rather_than_a_crash(self):
        """peer_level of 0 divides. Without the guard this raises inside the
        fusion path, which takes down the poll rather than the panel."""
        rows = [reading(50.0, 5, site="high"),
                reading(0.0, 5, site="zero1"), reading(0.0, 5, site="zero2")]
        out = fusion.corroborate(rows, now=NOW)
        high = [r for r in out if r["site_id"] == "high"][0]
        self.assertEqual("unknown", high["corroboration"])
        self.assertIn("no usable value", high["corroboration_note"])

    def test_the_median_of_nothing_is_nothing(self):
        self.assertIsNone(fusion._median([]))
        self.assertEqual(2, fusion._median([1, 2, 3]))


class TestTimestampNormalisation(unittest.TestCase):
    """_aware() feeds every staleness decision. Both its early returns were
    unexercised: removing either leaves the round trip through a string, which
    happens to produce the same answer for the timestamps a test usually
    writes — so the wrong code passes."""

    def test_no_timestamp_is_not_a_timestamp(self):
        self.assertIsNone(fusion._aware(None))

    def test_a_datetime_is_used_as_given_not_reparsed(self):
        """Asserting identity, not equality, on purpose. Re-parsing an aware
        datetime via str() gives an equal value for ordinary timestamps, so
        equality cannot tell the guard from its absence."""
        dt = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.assertIs(dt, fusion._aware(dt))

    def test_a_naive_datetime_is_assumed_utc(self):
        naive = datetime(2026, 7, 31, 12, 0)
        self.assertEqual(timezone.utc, fusion._aware(naive).tzinfo)

    def test_nonsense_is_none_rather_than_an_exception(self):
        self.assertIsNone(fusion._aware("not a time at all"))


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
