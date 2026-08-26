# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The pages against the Python they render.

Two whole classes of silent breakage live in a single-file HTML surface, and
this project has shipped both.

**A colour that is not a colour.** `var(--bad)` was written three times in
dashboard.html and defined nowhere. Two of the three carried a fallback hex, so
they looked right and nobody noticed; the third did not, and the "N readings at
an extreme level" warning — the loudest thing this page ever says — rendered in
the body's grey. CSS has no error for an undefined custom property: the
declaration is simply dropped, the element inherits, and the page looks fine to
everyone except the person it was trying to warn.

**A literal that used to match.** Every judgement on these pages is made in
Python and matched here by string: `s.quality === 'suspect'`, a lookup keyed on
`trend.direction`, a wording table keyed on the indoor/outdoor verdict. None of
those enums is a shared constant — they cannot be, because one side is Python
and the other is JavaScript in a file with no build step — so a rename in
Python does not break the page. It degrades it: the tag stops appearing, the
arrow falls through to its default, the verdict prints as its own raw slug.
Everything still renders, and nothing says anything is wrong.

So the enums are read out of the Python source here, the literals are harvested
out of the page script, and the two are compared. Deliberately two-way: a
Python value the page no longer handles is one failure, and a page literal
Python no longer produces is the other. The second is the one a rename causes.

This file holds no copy of either list. Every name it checks is read at run
time from the module or the page that owns it — a hardcoded expectation here
would be a third copy, and the third copy is how the drift this file exists to
catch would arrive inside the guard against it.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fusion  # noqa: E402
import poller  # noqa: E402
import store  # noqa: E402

from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    # This suite only reads files off disk, so it has no obvious route to the
    # developer's ~/.airo. Installed anyway: importing poller resolves its
    # module-level paths against the real home, and the contract is blanket
    # on purpose — the last suite that "obviously" could not touch ~/.airo
    # deleted three of the maintainer's backups.
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()

#: Every HTML surface in the tree. Listed by glob rather than by name so a page
#: added later is covered without an edit here — the whole failure mode is a
#: surface nobody remembered to check.
PAGES = sorted(
    [ROOT / "dashboard.html", ROOT / "settings.html"]
    + sorted((ROOT / "tray" / "ui").glob("*.html")))


def page_script(path):
    """The page's last inline script — the one tools/check.py syntax-checks."""
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        path.read_text(encoding="utf-8"), re.S)
    return blocks[-1] if blocks else ""


class TestEveryColourNameResolves(unittest.TestCase):
    """`var(--x)` where nothing defines `--x` is silently dropped by CSS.

    Not hypothetical and not cosmetic: `--bad` at the extreme-readings warning
    had no fallback, so the one message on the page that means "this air may be
    smoke" was rendered in the same grey as the footnote beside it.
    """

    def custom_properties(self, path):
        """What the file defines, and what it asks for.

        Both sides are read from the whole file rather than the style block:
        a page sets colours from script too (`style="color:var(--poor)"` in a
        built row, `cssVar('--poor')` through getComputedStyle), and those are
        exactly the references that carry no fallback.
        """
        text = path.read_text(encoding="utf-8")
        defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", text))
        used = set(re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", text))
        used |= set(re.findall(r"cssVar\(\s*['\"](--[A-Za-z0-9_-]+)['\"]", text))
        return defined, used

    def test_every_referenced_colour_is_defined_in_its_own_page(self):
        """In its own page, because there is no shared stylesheet to fall back
        on — each file is served alone and standalone by design."""
        for path in PAGES:
            with self.subTest(page=path.name):
                defined, used = self.custom_properties(path)
                missing = sorted(used - defined)
                self.assertEqual([], missing,
                                 f"{path.name} paints with custom properties "
                                 f"nothing defines: {missing}")

    def test_the_pages_actually_use_custom_properties(self):
        """The guard above passes trivially against a page with no `var()` in
        it at all, which is also what it would report if the harvesting regex
        stopped matching. Assert it found something to check."""
        for path in PAGES:
            with self.subTest(page=path.name):
                _, used = self.custom_properties(path)
                self.assertTrue(used, f"{path.name}: no var() references found "
                                      f"— has the page changed shape?")


class TestThePageMatchesThePythonEnums(unittest.TestCase):
    """String enums the page matches by literal, read from both sides.

    Every one of these is a health-relevant judgement Python makes and the page
    only renders (rule 7, decision D8). The page is allowed not to *tag* a
    value — "ok" quality gets no badge, and that is a presentation choice — but
    it is not allowed to match a literal Python has stopped producing.
    """

    def setUp(self):
        self.script = page_script(ROOT / "dashboard.html")

    # ---- the Python side, read from the code that owns each enum ----

    def trend_directions(self):
        """Called rather than parsed: compute_trend's four thresholds are the
        definition, and driving them enumerates the answers without a regex
        that would break the moment the returns are reshaped."""
        deltas = [None, 6.0, 3.0, -6.0, 0.0]
        return {poller.compute_trend(
            {"10min": d, "60min": 0.0} if d is not None else
            {"10min": None, "60min": None}, "au")["direction"] for d in deltas}

    def trend_directions_with_text(self):
        """`unknown` carries no text, and renderTrend returns before either
        lookup when there is none. So it is correct for the page not to handle
        it, and that has to be encoded rather than papered over."""
        out = set()
        for d in (6.0, 3.0, -6.0, 0.0):
            t = poller.compute_trend({"10min": d, "60min": 0.0}, "au")
            if t["text"]:
                out.add(t["direction"])
        return out

    def verdicts(self):
        """Regex, because indoor_outdoor() needs a populated database and a
        fixture here would be a fourth place the vocabulary is written down."""
        src = (ROOT / "analyse.py").read_text(encoding="utf-8")
        found = set(re.findall(r'result\["verdict"\]\s*=\s*"([^"]+)"', src))
        self.assertTrue(found, "no verdicts found in analyse.py — has the "
                               "assignment changed shape?")
        return found

    def qualities(self):
        """Driven, like the trend: the thresholds are the definition."""
        return {store.assess_quality(1.0),
                store.assess_quality(store.SUSPECT_PM25 + 1),
                store.assess_quality(10.0, 1.0, 40.0)}

    def corroborations(self):
        """From corroborate()'s own docstring, which enumerates the set — the
        assignments are scattered across eight branches and the docstring is
        the one place that claims to be complete. If it drifts from the
        branches that is worth failing on too, so both are read."""
        doc = fusion.corroborate.__doc__ or ""
        named = set(re.findall(r"'([a-z_]+)'", doc))
        src = re.findall(r'"corroboration"\]?\s*[:=]\s*"([a-z_]+)"',
                         (ROOT / "fusion.py").read_text(encoding="utf-8"))
        self.assertTrue(named, "corroborate() no longer names its values")
        self.assertTrue(set(src) <= named,
                        f"corroborate() assigns values its docstring does not "
                        f"name: {sorted(set(src) - named)}")
        return named

    # ---- the page side, harvested from the script ----

    def keys_of_lookup_on(self, subject):
        """The keys of an object literal indexed by `subject`.

        `{rising_fast:'--poor', ...}[trend.direction]` — the page's idiom for
        "translate a served enum into presentation". Bare and quoted keys both,
        because the verdicts have spaces in them and the directions do not.
        """
        keys = set()
        for body in re.findall(r"\{([^{}]*)\}\s*\[\s*" + re.escape(subject) + r"\s*\]",
                               self.script, re.S):
            keys |= set(re.findall(r"['\"]([^'\"]+)['\"]\s*:", body))
            keys |= set(re.findall(r"(?:^|[,{\s])([A-Za-z_]\w*)\s*:", body))
        return keys

    def compared_against(self, field):
        """Every literal the page compares `.field` to, `===` and `!==` alike.

        The negations matter as much: `s.corroboration !== 'corroborated'`
        decides whether a note is shown at all, so a rename there hides the
        note rather than showing a stale one.
        """
        return set(re.findall(
            r"\." + re.escape(field) + r"\s*[!=]==\s*['\"]([^'\"]+)['\"]",
            self.script))

    # ---- and the comparison ----

    def assertHandled(self, page_values, python_values, what):
        """Two-way. A stale page literal is the rename this file exists for;
        an unhandled Python value is a surface that quietly stopped rendering
        a distinction the server still draws."""
        self.assertTrue(page_values, f"no {what} literals found in the page — "
                                     f"has the page changed shape?")
        stale = sorted(page_values - python_values)
        self.assertEqual([], stale,
                         f"dashboard.html matches {what} Python no longer "
                         f"produces: {stale}. A renamed value does not break "
                         f"the page, it silently stops matching.")

    def test_the_trend_arrow_and_colour_know_every_direction(self):
        directions = self.trend_directions()
        keys = self.keys_of_lookup_on("trend.direction")
        self.assertHandled(keys, directions, "trend directions")
        missing = sorted(self.trend_directions_with_text() - keys)
        self.assertEqual([], missing,
                         f"the trend lookup has no entry for {missing}, so it "
                         f"falls through to the default and reads as steady")

    def test_the_indoor_verdict_wording_covers_every_verdict(self):
        verdicts = self.verdicts()
        keys = self.keys_of_lookup_on("found.verdict")
        self.assertHandled(keys, verdicts, "indoor/outdoor verdicts")
        missing = sorted(verdicts - keys)
        self.assertEqual([], missing,
                         f"no wording for {missing} — the panel would print "
                         f"the raw slug at the reader")

    def test_the_source_tags_match_the_qualities_python_assigns(self):
        qualities = self.qualities()
        self.assertHandled(self.compared_against("quality"), qualities,
                           "reading qualities")
        for flagged in ("suspect", "extreme"):
            self.assertIn(flagged, qualities,
                          "the page tags a quality Python no longer has")
            self.assertIn(flagged, self.compared_against("quality"),
                          f"{flagged} readings would be shown untagged")

    def test_the_corroboration_notes_match_what_fusion_decides(self):
        self.assertHandled(self.compared_against("corroboration"),
                           self.corroborations(), "corroboration verdicts")

    def test_the_placement_labels_match_the_stores_vocabulary(self):
        """The one enum that *is* an importable constant. Checked the same way
        so that if the others ever become constants nothing here changes."""
        self.assertHandled(self.compared_against("placement"),
                           set(store.PLACEMENTS), "sensor placements")


if __name__ == "__main__":
    unittest.main()
