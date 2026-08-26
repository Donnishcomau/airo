# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forecast guardrails.

ROADMAP #9 Phase C does not exist yet. These tests exist anyway, because both
constraints on it -- Australian Consumer Law s4 and PurpleAir ToS s4.4 -- are
cheap to honour before the feature and impossible to retrofit after a model
has been trained and a number is on screen.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forecast  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



GOOD_SKILL = 0.4


class TestReasonableGrounds(unittest.TestCase):
    """ACL s4 deems a representation about a future matter misleading unless
    the maker had reasonable grounds -- and puts the burden on the maker."""

    def test_a_defensible_statement_passes_and_carries_its_basis(self):
        out = forecast.phrase(
            "Tonight looks like a trapping night",
            "light northerly, clear, forecast min 8 degrees",
            GOOD_SKILL)
        self.assertIn("trapping night", out)
        self.assertIn("northerly", out, "the basis must reach the user")

    def test_certainty_is_refused(self):
        for claim in ("PM2.5 will be high tonight",
                      "It is going to spike after sunset",
                      "The air is safe tonight",
                      "You won't need to close up"):
            with self.assertRaises(forecast.NoReasonableGrounds, msg=claim):
                forecast.phrase(claim, "wind and temperature", GOOD_SKILL)

    def test_a_statement_with_no_likelihood_wording_is_refused(self):
        with self.assertRaises(forecast.NoReasonableGrounds):
            forecast.phrase("A trapping night", "calm and cold", GOOD_SKILL)

    def test_a_forecast_with_no_stated_basis_is_refused(self):
        with self.assertRaises(forecast.NoReasonableGrounds):
            forecast.phrase("Tonight looks like a trapping night", "", GOOD_SKILL)

    def test_nothing_may_be_forecast_before_accuracy_is_measured(self):
        """'We haven't measured it' is precisely the absence of reasonable
        grounds. None is not the same as zero."""
        with self.assertRaises(forecast.NoReasonableGrounds):
            forecast.phrase("Tonight looks calm", "light winds", None)

    def test_a_model_no_better_than_persistence_may_not_speak(self):
        with self.assertRaises(forecast.NoReasonableGrounds):
            forecast.phrase("Tonight looks calm", "light winds", 0.0)
        with self.assertRaises(forecast.NoReasonableGrounds):
            forecast.phrase("Tonight looks calm", "light winds", -0.3)


class TestSkill(unittest.TestCase):
    def ledger(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return forecast.Skill(Path(self.tmp.name) / "skill.json")

    def test_score_is_none_until_enough_outcomes(self):
        s = self.ledger()
        for i in range(forecast.MIN_VERIFIED - 1):
            s.record(predicted=10, persistence=12, actual=10)
        self.assertIsNone(s.score(), "an opinion formed from too few outcomes")

    def test_a_perfect_model_scores_one(self):
        s = self.ledger()
        for i in range(forecast.MIN_VERIFIED):
            s.record(predicted=10, persistence=20, actual=10)
        self.assertAlmostEqual(s.score(), 1.0)

    def test_a_model_matching_persistence_scores_zero(self):
        s = self.ledger()
        for i in range(forecast.MIN_VERIFIED):
            s.record(predicted=15, persistence=15, actual=10)
        self.assertAlmostEqual(s.score(), 0.0)

    def test_a_worse_than_persistence_model_scores_negative(self):
        s = self.ledger()
        for i in range(forecast.MIN_VERIFIED):
            s.record(predicted=30, persistence=12, actual=10)
        self.assertLess(s.score(), 0)

    def test_the_ledger_survives_a_restart(self):
        s = self.ledger()
        for i in range(forecast.MIN_VERIFIED):
            s.record(predicted=10, persistence=20, actual=10)
        again = forecast.Skill(s.path)
        self.assertEqual(len(again.records), forecast.MIN_VERIFIED)
        self.assertAlmostEqual(again.score(), 1.0)

    def test_the_summary_is_honest_before_there_is_anything_to_say(self):
        self.assertIn("Not forecasting yet", self.ledger().summary())

    def test_the_summary_publishes_accuracy_once_measured(self):
        s = self.ledger()
        for i in range(forecast.MIN_VERIFIED):
            s.record(predicted=11, persistence=20, actual=10)
        text = s.summary()
        self.assertIn("mean error", text)
        self.assertIn("versus assuming no change", text)

    def test_a_corrupt_ledger_does_not_crash_the_poller(self):
        s = self.ledger()
        s.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(forecast.Skill(s.path).records, [])


class TestModelLicence(unittest.TestCase):
    """PurpleAir ToS s4.4 grants them a perpetual, sublicensable licence over
    models derived from their data. Avoided by construction, not by memory."""

    def test_purpleair_may_not_train_a_model(self):
        self.assertFalse(forecast.licence_permits_modelling("purpleair"))
        self.assertFalse(forecast.licence_permits_modelling("PurpleAir"))

    def test_government_open_data_may(self):
        for slug in ("qld", "nsw", "openaq"):
            self.assertTrue(forecast.licence_permits_modelling(slug), slug)

    def test_training_excludes_encumbered_sources_and_says_so(self):
        srcs = [{"provider": "purpleair", "site_id": 1},
                {"provider": "qld", "site_id": "fer"},
                {"provider": "nsw", "site_id": 9}]
        usable, excluded = forecast.training_sources(srcs)
        self.assertEqual([s["provider"] for s in usable], ["qld", "nsw"])
        self.assertEqual([s["provider"] for s in excluded], ["purpleair"])

    def test_the_exclusion_is_explained_rather_than_silent(self):
        """Dropping someone's nearest sensor without a word would be worse
        than the licence problem it avoids."""
        _, excluded = forecast.training_sources([{"provider": "purpleair"}])
        note = forecast.explain_exclusion(excluded)
        self.assertIn("purpleair", note)
        self.assertIn("perpetual", note)
        self.assertIn("still used for live readings", note)

    def test_no_exclusion_note_when_nothing_was_excluded(self):
        self.assertEqual(forecast.explain_exclusion([]), "")


class TestNoForecastHasSlippedIn(unittest.TestCase):
    """If forward-looking output ever appears outside this module, these
    guardrails have been bypassed rather than used."""

    def test_the_poller_does_not_forecast_on_its_own(self):
        import re
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        # compute_time_hint() describes a recurring daily pattern already in
        # the record; it is not a prediction of a specific future value.
        for m in re.finditer(r"def (forecast|predict)\w*\(", src):
            self.fail(f"poller.py defines {m.group(1)} without forecast.phrase")




class TestGuardsThatWereNotActuallyGuarded(unittest.TestCase):
    """Found by removing each guard and seeing what failed.

    The certainty check survived: every existing test fed it a sentence with
    no hedge in it, so `phrase()` refused for the missing-hedge reason instead
    and passed whether or not certainty wording was detected at all. Under
    ACL s4 that is the guard that matters most — it is the one stopping a
    representation about a future matter going out as a statement of fact.
    """

    GOOD_SKILL = 0.5

    def test_certainty_is_refused_even_when_the_sentence_hedges(self):
        for wording in ("it will be hazardous later, which looks likely",
                        "conditions for smoke; it is certainly coming",
                        "this is likely and the air is safe"):
            with self.subTest(wording=wording):
                with self.assertRaises(forecast.NoReasonableGrounds) as caught:
                    forecast.phrase(wording, basis="calm, cold, northerly",
                                    skill=self.GOOD_SKILL)
                self.assertIn("future fact", str(caught.exception),
                              "refused for the wrong reason")

    def test_a_properly_hedged_sentence_is_allowed(self):
        """The control. Without it every test above could pass because
        phrase() refuses everything."""
        said = forecast.phrase("tonight looks likely to trap particulates",
                               basis="calm, cold, northerly", skill=self.GOOD_SKILL)
        self.assertIn("likely", said)

    def test_a_persistence_baseline_that_never_moved_scores_nothing(self):
        """mse_p of zero divides. Without the guard this raises inside the
        thing whose whole job is to refuse to speak without grounds."""
        with tempfile.TemporaryDirectory() as td:
            s = forecast.Skill(Path(td) / "skill.json")
            for _ in range(forecast.MIN_VERIFIED):
                s.record(predicted=9.0, actual=10.0, persistence=10.0)
            self.assertIsNone(s.score())


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
