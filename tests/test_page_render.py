# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dashboard's own JavaScript, executed against a payload.

Until now the pages were only syntax-checked: `tools/check.py` runs
`node --check` over the inline script, which catches a page that will not
parse and nothing else. A row that renders the wrong cell, or silently drops a
field the server started sending, parses perfectly.

That gap is the JavaScript version of the mistake this project has made five
times in Python — a helper fully tested while its call site is gone. The
served payload has tests. What the page *does* with it had none, and the bug
that prompted this file was exactly there: the server stopped sending
`age_minutes` for one sensor and the page rendered an em dash, which is
correct behaviour for a missing field and completely wrong as an answer to
"is this sensor collecting data?".

Node is not a project dependency and must not become one -- these skip when it
is absent, which is the same bargain `tools/check.py` already makes for the
syntax check. CI has node on every runner.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import poller  # noqa: E402

from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def setUpModule():
    # This suite reads two .html files and runs node on a temp script, so it
    # has no route to the developer's ~/.airo and no reason to reach the
    # network. Installed anyway: the contract is deliberately blanket, because
    # the last suite that "obviously" could not touch ~/.airo deleted three of
    # the maintainer's backups.
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()

#: A minimal DOM. Only what the page touches on the path under test -- growing
#: this to a real DOM implementation would be reimplementing a browser, and the
#: point here is the row-building logic, which is ordinary string work.
SHIM = """
const __els = new Map();
const mk = () => ({ innerHTML:'', textContent:'', dataset:{},
  style:{setProperty(){}, removeProperty(){}},
  classList:{add(){},remove(){},toggle(){},contains:()=>false},
  addEventListener(){}, removeEventListener(){}, appendChild(){},
  setAttribute(){}, removeAttribute(){}, focus(){}, remove(){},
  querySelector:()=>mk(), querySelectorAll:()=>[],
  getContext:()=>null, getBoundingClientRect:()=>({width:800,height:400}) });
globalThis.document = {
  getElementById: id => { if(!__els.has(id)) __els.set(id, mk()); return __els.get(id); },
  querySelector: () => mk(), querySelectorAll: () => [],
  addEventListener(){}, createElement: mk,
  documentElement: mk(), body: mk(), readyState: 'complete',
};
globalThis.window = {
  addEventListener(){}, removeEventListener(){},
  matchMedia: () => ({matches:false, addEventListener(){}, addListener(){}}),
  location:{href:'http://127.0.0.1:8787/dashboard.html', search:'', reload(){}},
  getComputedStyle: () => ({getPropertyValue: () => '#888888'}),
};
globalThis.getComputedStyle = window.getComputedStyle;
// Bare `location`, not `window.location`: settings.html reloads the page
// after a successful write, and a test about whether it reloaded needs
// something to observe. Replaced per-test where that is the point.
globalThis.location = window.location;
// showFieldErrors() builds an attribute selector with it. Identity is enough
// here -- the field names are the config's own keys, not user text.
globalThis.CSS = { escape: s => String(s) };
globalThis.setTimeout = () => 0;
globalThis.setInterval = () => 0;
globalThis.requestAnimationFrame = () => 0;
globalThis.fetch = async () => ({ ok:true, status:200, json: async () => ({}) });
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.__els = __els;
"""


def page_script(name):
    """The page's last inline <script>, the same one tools/check.py checks."""
    html = (ROOT / name).read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        raise AssertionError(f"{name} has no inline script to test")
    return blocks[-1]


def render(payload, name="dashboard.html", call="renderSources()",
           host="sourcesPanel", into="latest", expr=None, setup="",
           extra_env=None):
    """Run the page against `payload` and return what `host` was filled with.

    The payload is assigned to one of the page's own globals -- `latest` for
    the served view, `readings` for the primary source's series -- so this
    exercises the real render path rather than a function called with a
    hand-made argument.

    `expr` returns a JSON value from a page function instead of a host
    element's HTML, for the data-preparation steps behind the canvas panels.
    There is nothing honest to assert about a drawn chart; there is plenty to
    assert about the numbers handed to it.
    """
    if into == "readings":
        # The page holds readings as {t: Date, v, pm25, quality}; the payload
        # carries ISO strings because JSON has no date type.
        assign = ("readings = JSON.parse(process.env.AIRO_PAYLOAD)"
                  ".map(r => ({...r, t: new Date(r.t)}));\n")
    else:
        assign = f"  {into} = JSON.parse(process.env.AIRO_PAYLOAD);\n"
    # innerHTML *or* textContent. The page uses whichever suits the panel --
    # a table is built as markup, a single figure is set as text -- and a
    # harness that only read one of them returned an empty string for half the
    # dashboard, which every `assertNotIn` would have passed.
    emit = (f"console.log('\\u0001' + JSON.stringify({expr}));" if expr else
            f"console.log('\\u0001' + (__els.get('{host}').innerHTML"
            f" || __els.get('{host}').textContent || ''));")
    # `setup` fills in a second global where a panel reads more than one --
    # drawHeatmap() needs both `latest` (whose sensor is this?) and `readings`
    # (the figures). Kept as raw JS rather than a second payload parameter,
    # because the alternative is a signature that grows a slot per panel.
    # Wrapped in an async IIFE so a panel that fetches its own data can be
    # awaited. drawInsideOutside() is one: it calls `api/indoor` itself rather
    # than being handed a payload, so a synchronous driver read the element
    # before the panel had written to it and every assertion saw an empty
    # string -- passing the negative ones.
    driver = ("\n;(async () => {\n  " + assign + setup
              + f"  await {call};\n  {emit}\n" + "})();\n")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.js"
        script.write_text(SHIM + page_script(name) + driver, encoding="utf-8")
        env = dict(os.environ, AIRO_PAYLOAD=json.dumps(payload))
        # A second payload where one is not enough -- the sensor picker
        # needs both the served view and the whole series list.
        env.update(extra_env or {})
        # encoding, not just text=True. Node writes UTF-8 on every platform;
        # `text=True` alone decodes with the *locale's* encoding, which on the
        # Windows runner is cp1252 -- so "µg" came back as "Âµg" and the em
        # dash as "â€”", and every assertion about a rendered cell compared
        # against mojibake. The page is full of both characters, so this was
        # never going to be a small discrepancy.
        out = subprocess.run([NODE, str(script)], capture_output=True,
                             text=True, encoding="utf-8", env=env, timeout=60)
        # The page logs render-step failures for panels this test does not
        # drive (charts want a real canvas). Only the marked line is ours.
        for line in out.stdout.splitlines():
            if line.startswith(""):
                return line[1:]
        raise AssertionError(
            f"the page did not render {host}.\nstdout: {out.stdout[-2000:]}\n"
            f"stderr: {out.stderr[-2000:]}")


def strip_js_comments(src):
    """Block and line comments removed, so a check about *code* is not
    answered by prose. Crude on purpose: it does not need to handle a `//`
    inside a string literal, and the alternative is a JavaScript parser."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"^\s*//.*$", " ", src, flags=re.M)


def cells(row_html):
    """The visible text of each <td>, markup and whitespace removed."""
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
            for c in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row_html)]


def rows(html):
    return ["<tr" + r for r in html.split("<tr")[1:]]


def source(pm25=1.0, name="Outside", placement="outdoor", **kw):
    """One entry shaped like `as_view` in poller.py serves it."""
    row = {
        "provider": "qld", "site_id": "s1", "site_name": name, "pm25": pm25,
        "aqi": pm25 * 4, "band": "Very good", "observed_utc": None,
        "age_minutes": 3, "distance_km": 2.0, "stale": False, "quality": "ok",
        "pm25_a": None, "pm25_b": None, "confidence": None,
        "corroboration": "corroborated", "corroboration_note": None,
        "peer_ratio": 1.0, "peer_pm25": pm25, "resolution_minutes": 60,
        "humidity": None, "temperature": None, "temperature_unit": "C",
        "placement": placement, "placement_note": None,
    }
    row.update(kw)
    return row


def payload(*sources, **kw):
    view = {
        "pm25_10min": 1.0, "aqi": 4.0, "band": "Very good", "scale": "au",
        "sources": list(sources), "source": dict(sources[0]) if sources else {},
        "generated_utc": None, "fusion_rule": "nearest", "provenance": None,
    }
    view.update(kw)
    return view


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheSourcesTable(unittest.TestCase):
    """What a person actually reads, built by the code that actually builds it."""

    def row_for(self, html, name):
        for r in rows(html):
            if name in r:
                return r
        raise AssertionError(f"no row for {name!r} in the rendered table")

    def test_a_live_sensor_shows_its_age_rather_than_a_dash(self):
        """The question this file exists for. A dash in the age column is the
        page's way of saying "the server told me nothing", and it is
        indistinguishable to a reader from "this sensor is dead"."""
        html = render(payload(source(name="Kitchen", placement="indoor",
                                     age_minutes=4, distance_km=0.9,
                                     corroboration=None, peer_ratio=None)))
        got = cells(self.row_for(html, "Kitchen"))

        self.assertIn("4 min", got,
                      f"the indoor row does not show its age: {got}")
        self.assertIn("0.9 km", got,
                      f"the indoor row does not show its distance: {got}")

    def test_an_indoor_sensor_is_labelled_indoor(self):
        """Otherwise it is a row with no peer ratio and no reason given, which
        reads as a broken sensor rather than an excluded one."""
        html = render(payload(source(name="Kitchen", placement="indoor",
                                     corroboration=None, peer_ratio=None)))
        self.assertIn("indoor", self.row_for(html, "Kitchen").lower(),
                      "nothing on the row says the sensor is indoors")

    def test_an_outdoor_sensor_is_not_labelled_indoor(self):
        """So the test above cannot pass by tagging everything."""
        html = render(payload(source(name="Roof", placement="outdoor")))
        row = self.row_for(html, "Roof")
        self.assertNotIn(">indoor<", row,
                         "an outdoor sensor is tagged as indoor")

    def test_the_blank_peer_column_is_explained_in_words(self):
        """The blank is correct -- an indoor sensor has no outdoor peers. An
        unexplained blank is what prompted "is data being collected?"."""
        note = ("This is an indoor sensor. It will be shown and charted, and "
                "kept out of the outdoor headline, alerts and analysis.")
        html = render(payload(source(name="Kitchen", placement="indoor",
                                     corroboration=None, peer_ratio=None,
                                     placement_note=note)))
        self.assertIn("kept out of the outdoor headline", html,
                      "the indoor row's empty peer cell is never explained")

    def test_an_ordinary_outdoor_row_carries_no_note(self):
        """A note on every row is a note nobody reads, including the one that
        means something."""
        html = render(payload(source(name="Roof", placement="outdoor")))
        self.assertNotIn("srcNoteRow", html,
                         "an unremarkable outdoor row is carrying a note")

    def test_a_placement_note_and_a_peer_note_can_both_appear(self):
        """They answer different questions and one must not hide the other."""
        html = render(payload(source(
            name="Kitchen", placement="unknown",
            placement_note="Where this sensor is could not be established.",
            corroboration="uncorroborated",
            corroboration_note="It reads far above its neighbours.")))

        self.assertIn("could not be established", html,
                      "the placement note was dropped")
        self.assertIn("far above its neighbours", html,
                      "the corroboration note was dropped")

    def test_the_headline_row_is_still_marked(self):
        """Existing behaviour, asserted here because this is now the file that
        would notice it breaking."""
        html = render(payload(source(name="Roof", placement="outdoor")))
        self.assertIn("headline", self.row_for(html, "Roof"),
                      "the headline tag is gone")


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheHarnessItself(unittest.TestCase):
    """A harness that silently renders nothing would pass every test above."""

    def test_a_missing_field_is_visible_as_a_dash(self):
        """Proves the assertions above are reading real output: the same row
        with `age_minutes` removed must render the dash they rule out."""
        html = render(payload(source(name="Kitchen", placement="indoor",
                                     age_minutes=None)))
        self.assertIn("—", self.row_cells(html, "Kitchen"),
                      "a null age did not render as a dash, so these tests "
                      "are not reading the cells they claim to")

    def row_cells(self, html, name):
        for r in rows(html):
            if name in r:
                return cells(r)
        raise AssertionError(f"no row for {name!r}")

    def test_what_node_writes_survives_the_trip_back(self):
        """Stated as a claim rather than left to the em dash to enforce.

        Node writes UTF-8 everywhere; `text=True` decodes with the locale's
        encoding, which is UTF-8 on this machine and cp1252 on the Windows
        runner. The page is full of "µg" and em dashes, so under the locale
        default every cell assertion compared against mojibake — and passed
        locally while failing on Windows only.
        """
        html = render(payload(source(name="Ångström µ—site")))
        self.assertIn("Ångström µ—site", html,
                      "non-ASCII came back mangled, so every assertion about "
                      "a rendered cell is comparing against mojibake")

    def test_the_renderer_is_actually_called(self):
        """If `render()` returned the empty string, every assertNotIn above
        would pass and every assertIn would fail loudly -- but a harness that
        returned a fixed non-empty blob would pass both. Assert the payload's
        own data reaches the output."""
        html = render(payload(source(name="Distinctive Site Name")))
        self.assertIn("Distinctive Site Name", html,
                      "the harness is not rendering the payload it was given")


def evening_readings(nights, level=10.0, skip=()):
    """One reading an hour, 3pm to midnight, for `nights` consecutive nights.

    Dates are fixed rather than relative to now: a fixture that anchors on the
    current date produces a different grid depending on the hour it runs at,
    and this file already carries one lesson about that.

    clock-independent: the dates are literals and the page keys nights by
    local date, so nothing here depends on when the suite runs.
    """
    out = []
    for day in range(1, nights + 1):
        date = f"2026-03-{day:02d}"
        if date in skip:
            continue
        for hour in list(range(15, 24)):
            out.append({"t": f"{date}T{hour:02d}:30:00",
                        "v": level, "pm25": level / 4, "quality": "ok"})
    return out


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheEveningGrid(unittest.TestCase):
    """Nights by hours, drawn from one instrument.

    `primarySeries()` hands every downstream panel the headline source's
    series alone, and that is right — mixing instruments in one trend would
    compare calibrations against each other. What was missing is that the grid
    never said so, so a gap in *one sensor* read as a gap in the record.

    The maintainer asked why cells were blank. They were blank because their
    nearest sensor was dark for about two days; their other three sources
    reported normally throughout.
    """

    def grid(self, readings):
        return render(readings, call="1", into="readings",
                      expr="eveningGrid()")

    def test_a_night_with_no_readings_is_still_a_row(self):
        """The finding. A night the sensor missed entirely produced no entry
        at all, so the outage was not shown as a gap — it was not shown. An
        absent row hides an outage; an empty row is the evidence of one."""
        got = json.loads(self.grid(evening_readings(4, skip=("2026-03-02",))))
        dates = [r["date"] for r in got]

        self.assertIn("2026-03-02", dates,
                      "a night with no readings vanished from the grid "
                      "entirely rather than rendering as empty")

    def test_the_empty_night_has_no_invented_numbers(self):
        """Present as a row, absent as data. Filling it with a neighbour's
        figures would be fabricating a night that was never measured."""
        got = json.loads(self.grid(evening_readings(4, skip=("2026-03-02",))))
        blank = [r for r in got if r["date"] == "2026-03-02"][0]

        self.assertIsNone(blank["mean"])
        self.assertIsNone(blank["peak"])
        self.assertEqual(0, blank["n"])
        self.assertTrue(all(v is None for v in blank["cells"].values()),
                        "an unmeasured night was given hourly values")

    def test_a_run_of_dark_nights_is_all_shown(self):
        """The real outage spanned three consecutive nights."""
        got = json.loads(self.grid(
            evening_readings(6, skip=("2026-03-02", "2026-03-03", "2026-03-04"))))
        dates = [r["date"] for r in got]

        for missing in ("2026-03-02", "2026-03-03", "2026-03-04"):
            self.assertIn(missing, dates, f"{missing} is missing from the grid")

    def test_the_nights_stay_in_order(self):
        got = json.loads(self.grid(evening_readings(5, skip=("2026-03-03",))))
        dates = [r["date"] for r in got]

        self.assertEqual(sorted(dates), dates, "the grid is out of order")

    def test_a_measured_night_keeps_its_numbers(self):
        """The control: filling gaps must not disturb the nights that have
        data."""
        got = json.loads(self.grid(evening_readings(3, level=20.0)))
        first = [r for r in got if r["date"] == "2026-03-01"][0]

        self.assertEqual(20.0, first["mean"])
        self.assertEqual(20.0, first["peak"])
        self.assertEqual(9, first["n"])

    def test_no_gap_is_invented_before_the_record_starts(self):
        """Padding must run between the first and last night observed, not
        back to the epoch."""
        got = json.loads(self.grid(evening_readings(3)))

        self.assertEqual(3, len(got),
                         f"the grid grew nights it never had: "
                         f"{[r['date'] for r in got]}")

    def test_midnight_belongs_to_the_evening_before(self):
        """Existing behaviour, asserted here because this is now the file that
        would notice it breaking. A reading at 00:30 on the 2nd is part of the
        night of the 1st, and filing it under the 2nd would split every
        evening in half."""
        got = json.loads(self.grid([
            {"t": "2026-03-01T22:30:00", "v": 10.0, "pm25": 2.5, "quality": "ok"},
            {"t": "2026-03-02T00:30:00", "v": 30.0, "pm25": 7.5, "quality": "ok"},
        ]))

        self.assertEqual(1, len(got), "midnight started a new night")
        self.assertEqual("2026-03-01", got[0]["date"])


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheGridSaysWhoseReadingsTheseAre(unittest.TestCase):
    """A blank that does not say what it means gets read as the worst thing it
    could mean. Here that was "Airo collected nothing", when the truth was
    "one sensor was quiet and the other three were fine" — a different fact
    and a different worry."""

    ONE_NIGHT = ("  readings = [{t: new Date('2026-03-01T18:30:00'), v: 10, "
                 "pm25: 2.5, quality: 'ok'}];\n")

    def drawn(self, source_name="Riverside"):
        payload = {"sources": [], "scale": "au",
                   "source": {"site_name": source_name} if source_name else {},
                   "provenance": source_name}
        return render(payload, call="drawHeatmap()", host="hmWrap",
                      setup=self.ONE_NIGHT)

    def test_the_panel_names_the_instrument(self):
        self.assertIn("Riverside", self.drawn(),
                      "the grid never says which sensor it describes, so a "
                      "gap in one instrument reads as a gap in the record")

    def test_the_panel_says_what_a_blank_cell_means(self):
        self.assertIn("did not report", self.drawn())

    def test_it_says_the_other_sources_are_unaffected(self):
        """The specific reassurance the maintainer needed and did not get."""
        self.assertIn("unaffected", self.drawn())

    def test_an_unnamed_source_still_explains_the_blanks(self):
        """Provenance can be absent on a fresh install, and the sentence that
        matters must not depend on it."""
        self.assertIn("did not report", self.drawn(source_name=None),
                      "with no source name the panel explains nothing at all")

    def test_the_grid_itself_is_still_drawn(self):
        """The note must not have replaced the thing it annotates."""
        self.assertIn("<table", self.drawn())


def view(**kw):
    """A served `/api/latest` view, with only what a panel actually reads."""
    v = {"aqi": 20.0, "band": "Very good", "pm25_10min": 5.0, "scale": "au",
         "scale_label": "Australian AQI", "location_name": "Northfield",
         "sources": [], "source": {}, "fetched_utc": None, "poll_minutes": 15,
         "trend": None, "headline_explained": None}
    v.update(kw)
    return v


#: A fixed clock, for the two panels whose output is a function of the hour.
#: Injected rather than worked around: `updateStatus` says "close up now"
#: between 16:35 and 17:15 and something else the rest of the day, so a test
#: that does not pin the hour asserts whatever happened to be true when it ran.
def frozen_clock(iso):
    return (
        "  const __real = Date;\n"
        f"  const __at = new __real('{iso}').getTime();\n"
        "  Date = class extends __real {\n"
        "    constructor(...a){ super(...(a.length ? a : [__at])); }\n"
        "    static now(){ return __at; }\n"
        "  };\n")


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheHeadline(unittest.TestCase):
    """The number somebody acts on. A wrong figure here is the worst bug this
    project can ship, and it had no test between the served payload and the
    reader's eye."""

    def big(self, **kw):
        return render(view(**kw), call="renderHeadline()", host="big")

    def test_the_index_is_shown_rounded(self):
        self.assertEqual("20", self.big(aqi=20.4))

    def test_a_missing_index_is_a_dash_not_a_zero(self):
        """A zero is a claim about the air. A dash is an admission."""
        self.assertEqual("—", self.big(aqi=None))

    def test_the_band_and_advice_come_from_the_index(self):
        html = render(view(aqi=180.0), call="renderHeadline()", host="ugm")
        self.assertIn("µg/m³", html)

    def test_the_concentration_is_shown_beside_the_index(self):
        """Rule 6's shape at the surface: the index is derived, the µg/m³ is
        what was measured, and the reader is shown both."""
        self.assertIn("5.0 µg/m³",
                      render(view(pm25_10min=5.0), call="renderHeadline()",
                             host="ugm"))

    def test_the_band_label_is_named(self):
        self.assertEqual("Very good",
                         render(view(aqi=10.0), call="renderHeadline()",
                                host="bandLabel"))


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheAdviceUnderTheHeadlineIsServed(unittest.TestCase):
    """The sentence a reader acts on, and where it is decided.

    The page held six of them and joined them to the served band table BY
    ARRAY INDEX. Every scale has six bands, so the join always produced
    something: a `raw` install reading 20 ug/m3 — above the WHO 24-hour
    guideline — was shown "Above WHO guideline" and, underneath it, advice
    written for an Australian band that stops at 16.5 ug/m3.
    """

    def ugm(self, scale="au", **kw):
        v = view(scale=scale, scale_label=poller.SCALES[scale]["label"],
                 bands=poller.scale_bands(scale), **kw)
        return render(v, call="renderHeadline()", host="ugm",
                      setup="adoptBands(latest.bands);")

    def band_label(self, scale="au", **kw):
        v = view(scale=scale, bands=poller.scale_bands(scale), **kw)
        return render(v, call="renderHeadline()", host="bandLabel",
                      setup="adoptBands(latest.bands);")

    def test_the_australian_headline_shows_the_advice_it_was_sent(self):
        self.assertIn("Enjoy normal activities.",
                      self.ugm("au", aqi=10.0, pm25_10min=2.5))

    def test_the_above_guideline_band_is_not_told_to_enjoy_normal_activities(
            self):
        """The bug, at the surface a reader actually sees. 20 ug/m3 on the raw
        scale is the second band, and second in the old JS table was the
        cleanest-but-one Australian sentence."""
        self.assertEqual(
            "Above WHO guideline", self.band_label("raw", aqi=20.0))
        self.assertNotIn("Enjoy normal activities",
                         self.ugm("raw", aqi=20.0, pm25_10min=20.0))

    def test_a_scale_with_no_advice_shows_the_measurement_alone(self):
        """Degrading cleanly: the line loses a half, not its punctuation. The
        separator used to be baked onto the end of the measurement, so an
        absent sentence left a dangling " · " reading as a truncated one."""
        got = self.ugm("raw", aqi=20.0, pm25_10min=20.0)
        self.assertEqual("≈ 20.0 µg/m³", got.strip())

    def test_an_absent_sentence_is_absent_and_not_a_word_for_absence(self):
        for scale in ("raw", "us_epa"):
            got = self.ugm(scale, aqi=20.0, pm25_10min=20.0)
            for artefact in ("undefined", "null", "NaN"):
                self.assertNotIn(artefact, got,
                                 f"{scale} renders {artefact!r} where the "
                                 f"advice would be")

    def test_with_neither_half_the_line_is_empty_rather_than_punctuation(self):
        self.assertEqual(
            "", self.ugm("raw", aqi=20.0, pm25_10min=None).strip())

    def test_the_words_are_the_servers_and_the_page_has_no_copy(self):
        """Mutate the served string and the page must change with it. If any
        copy survived in the JS, this reads back the original — which is the
        state the page was in before the table was moved into Python.

        The sentinel is deliberately not health wording: the assertion is
        about where the text comes from, and inventing advice to test with
        would be authoring advice.
        """
        sentinel = "SERVED SENTINEL WORDING"
        bands = poller.scale_bands("au")
        bands[0] = dict(bands[0], advice=sentinel)
        got = render(view(aqi=10.0, pm25_10min=2.5, bands=bands),
                     call="renderHeadline()", host="ugm",
                     setup="adoptBands(latest.bands);")

        self.assertIn(sentinel, got)
        self.assertNotIn("Enjoy normal activities", got)

    def test_removing_the_advice_removes_the_line_even_on_a_known_scale(self):
        """The other direction of the same proof: a served band that drops its
        advice must not be topped up from anything the page remembers."""
        bands = [{"max": b["max"], "name": b["name"]}
                 for b in poller.scale_bands("au")]
        got = render(view(aqi=10.0, pm25_10min=2.5, bands=bands),
                     call="renderHeadline()", host="ugm",
                     setup="adoptBands(latest.bands);")

        self.assertEqual("≈ 2.5 µg/m³", got.strip())


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheTrendLine(unittest.TestCase):
    """Rising and clearing call for opposite actions."""

    def trend(self, **kw):
        return render(view(trend=kw or None), call="renderTrend()",
                      host="trend")

    def test_rising_is_shown_with_the_words_the_server_chose(self):
        self.assertIn("climbing fast",
                      self.trend(direction="rising_fast",
                                 text="climbing fast"))

    def test_clearing_and_rising_do_not_look_alike(self):
        """They are opposite advice; an arrow that pointed the same way for
        both would be worse than no arrow."""
        up = self.trend(direction="rising", text="climbing")
        down = self.trend(direction="clearing", text="clearing")

        self.assertNotEqual(up.replace("climbing", ""),
                            down.replace("clearing", ""),
                            "rising and clearing render identically")

    def test_no_trend_says_nothing_rather_than_guessing(self):
        self.assertEqual("", self.trend())


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheHeaderAndTheRecord(unittest.TestCase):

    def test_the_scale_is_named_so_the_number_is_interpretable(self):
        """"48" means nothing without the scale it is on — the maintainer
        asked exactly this question about exactly this number."""
        self.assertEqual("Australian AQI",
                         render(view(), call="renderHeader()",
                                host="scaleName"))

    def test_a_scale_with_no_label_falls_back_rather_than_blanking(self):
        self.assertEqual("AQI", render(view(scale_label=None),
                                       call="renderHeader()", host="scaleName"))

    def test_the_location_is_named(self):
        self.assertIn("Northfield", render(view(), call="renderHeader()",
                                            host="locName"))

    def test_the_row_count_is_the_readings_it_was_given(self):
        rows = [{"t": f"2026-03-01T{h:02d}:00:00", "v": 10, "pm25": 2.5,
                 "quality": "ok"} for h in range(5)]
        self.assertEqual("5", render(rows, call="renderRecordStats()",
                                     host="s_rows", into="readings"))

    def test_an_empty_record_says_zero_rather_than_failing(self):
        self.assertEqual("0", render([], call="renderRecordStats()",
                                     host="s_rows", into="readings"))


@unittest.skipIf(NODE is None, "node is not installed")
class TestThePollCountdown(unittest.TestCase):
    """How the reader knows the figures are live. It is also the panel that
    tells them the agent has stopped."""

    def info(self, iso, **kw):
        return render(view(fetched_utc="2026-03-01T10:00:00+00:00", **kw),
                      call="pollInfo()", host="pollInfo",
                      setup=frozen_clock(iso))

    def test_a_recent_poll_reads_as_just_now(self):
        self.assertIn("just now", self.info("2026-03-01T10:00:20Z"))

    def test_an_older_poll_says_how_long_ago(self):
        self.assertIn("30 min ago", self.info("2026-03-01T10:30:00Z"))

    def test_the_cadence_is_stated(self):
        self.assertIn("every 15 min", self.info("2026-03-01T10:00:20Z"))

    def test_nothing_is_claimed_before_the_first_poll(self):
        self.assertEqual("", render(view(fetched_utc=None), call="pollInfo()",
                                    host="pollInfo"))


@unittest.skipIf(NODE is None, "node is not installed")
class TestInsideAgainstOutsideIsServerDecided(unittest.TestCase):
    """Rule 7 at the surface. Two failure modes with opposite remedies, and
    every word of the verdict comes from the server -- the page must render
    what it is told and must not decide anything itself."""

    def panel(self, host="inoutVerdict", **kw):
        """The panel fetches `api/indoor` itself, so the answer is stubbed at
        the boundary it actually uses rather than handed in as an argument."""
        payload = {"verdict": "holding",
                   "advice": "Inside is staying cleaner than outside.",
                   "basis": "75 paired hours over 7 days."}
        payload.update(kw)
        stub = ("  globalThis.fetch = async () => ({ok: true, status: 200, "
                "json: async () => (" + json.dumps(payload) + ")});\n")
        return render(view(), call="drawInsideOutside()", host=host,
                      setup=stub)

    def all_of_it(self, **kw):
        return " ".join(self.panel(host=h, **kw) for h in
                        ("inoutVerdict", "inoutAdvice", "inoutBasis"))

    def test_the_verdict_is_shown_verbatim(self):
        self.assertIn("holding", self.all_of_it().lower())

    def test_the_advice_is_shown_verbatim(self):
        self.assertIn("staying cleaner than outside", self.all_of_it())

    def test_the_grounds_are_shown_with_the_claim(self):
        """A statement about somebody's house needs its basis visible, not a
        tooltip away."""
        self.assertIn("75 paired hours", self.all_of_it())

    def test_the_opposite_verdict_renders_its_own_advice(self):
        """The page must not have the advice baked in -- that is how a reader
        gets told to ventilate during a smoke event."""
        got = self.all_of_it(
            verdict="outdoor air getting in",
            advice="Closing up and filtering helps; opening windows does not.")

        self.assertIn("opening windows does not", got)
        self.assertNotIn("staying cleaner", got)


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheRecordPanelIsAboutTheRecord(unittest.TestCase):
    """"Readings on disk" is a claim about the database, and the page had been
    answering it with the length of the series it happened to be holding.

    Adding a sensor nearer the house than the previous one moves the headline,
    every historical panel follows the headline, and the panel went from the
    whole stored record to 4 with nothing lost. On a page whose job is to be
    trusted about the record, that is the worst available kind of wrong.
    """

    FOUR_POINTS = ("  readings = [1,2,3,4].map(i => ({t: new Date('2026-08-14T0"
                   "8:0'+i+':00'), v: 10, pm25: 2.5, quality: 'ok'}));\n")

    def panel(self, host, **record):
        return render(view(record=record or None), call="renderRecordStats()",
                      host=host, setup=self.FOUR_POINTS)

    def test_the_count_is_the_whole_database(self):
        self.assertEqual("1,234",
                         self.panel("s_rows", readings_total=1234),
                         "the panel reported the series it was holding rather "
                         "than the readings on disk")

    def test_the_first_reading_is_the_records_first(self):
        """Not the headline sensor's first, which for a sensor installed this
        morning would date the whole record to this morning."""
        got = self.panel("s_first", readings_total=1234,
                         first_utc="2026-07-29T21:20:00+00:00",
                         last_utc="2026-08-14T09:14:00+00:00")

        self.assertIn("Jul", got, f"the record starts in July; panel said {got!r}")

    def test_it_falls_back_to_the_series_when_no_total_is_served(self):
        """An older server sends no `record`. Showing four is then honest —
        it is all the page knows — and better than a dash."""
        self.assertEqual("4", self.panel("s_rows"))

    def test_an_empty_install_is_not_a_crash(self):
        self.assertEqual("0", render(view(record={"readings_total": 0}),
                                     call="renderRecordStats()", host="s_rows",
                                     setup="  readings = [];\n"))


def series_payload(*entries):
    """An `/api/series` payload: one entry per source, each with points."""
    return {"scale": "au", "series": [
        {"provider": p, "site_id": i, "site_name": n,
         "points": [{"t": f"2026-08-15T0{h}:00:00", "pm25": v, "aqi": v * 4}
                    for h, v in enumerate(vals)]}
        for p, i, n, vals in entries]}


@unittest.skipIf(NODE is None, "node is not installed")
class TestChoosingWhichSensorToLookAt(unittest.TestCase):
    """One instrument at a time, and the reader picks which.

    Mixing sources in a trend would compare calibrations against each other,
    so the panels stay single-source. But *which* source is a view choice, and
    it was hard-wired to the headline — so adding a sensor nearer the house
    silently replaced the chart with one that had thirty minutes of data, and
    there was no way to look at the old one.

    The load-bearing constraint is in the other direction: the headline is a
    fusion decision made in Python from distance, freshness and corroboration.
    A dropdown must be able to change what you are *looking at* and never what
    Airo *claims*.
    """

    PAYLOAD = None

    def setUp(self):
        self.data = series_payload(
            ("purpleair", "pa-near", "Northfield", [5, 6, 7]),
            ("purpleair", "pa-far", "Riverside", [20, 21, 22]),
            ("purpleair", "pa-inside", "Indoor", [1, 1, 1]),
        )

    def chose(self, key, expr):
        """Run with `viewSource` set, as the change handler leaves it."""
        setup = (f"  viewSource = {json.dumps(key)};\n"
                 "  seriesData = JSON.parse(process.env.AIRO_SERIES);\n")
        return render(view(source={"provider": "purpleair",
                                   "site_id": "pa-near",
                                   "site_name": "Northfield"}),
                      call="1", setup=setup, expr=expr,
                      extra_env={"AIRO_SERIES": json.dumps(self.data)})

    def test_by_default_it_follows_the_headline(self):
        """The behaviour before there was a choice, kept."""
        got = json.loads(self.chose(None, "primarySeries(seriesData, latest)"))
        self.assertEqual([20, 24, 28], [p["v"] for p in got],
                         "the default is no longer the headline source")

    def test_choosing_a_sensor_shows_that_sensor(self):
        got = json.loads(self.chose("purpleair/pa-far",
                                    "primarySeries(seriesData, latest)"))
        self.assertEqual([80, 84, 88], [p["v"] for p in got],
                         "picking Riverside did not change the series")

    def test_an_indoor_sensor_can_be_looked_at(self):
        """Excluded from speaking for the outdoor air; not excluded from being
        looked at. Somebody who fits an indoor sensor wants its history."""
        got = json.loads(self.chose("purpleair/pa-inside",
                                    "primarySeries(seriesData, latest)"))
        self.assertEqual([4, 4, 4], [p["v"] for p in got])

    def test_a_source_that_has_gone_away_falls_back_to_the_headline(self):
        """Rather than blanking the page. A reader who cannot see why the
        chart is empty concludes the data is gone."""
        got = json.loads(self.chose("purpleair/does-not-exist",
                                    "primarySeries(seriesData, latest)"))
        self.assertEqual([20, 24, 28], [p["v"] for p in got],
                         "a stale saved choice emptied the chart")

    def test_the_picker_offers_every_source(self):
        html = self.chose(None, "(renderSourcePicker(), "
                                "__els.get('srcPick').innerHTML)")
        for name in ("Northfield", "Riverside", "Indoor"):
            self.assertIn(name, html, f"{name} is not offered")

    def test_the_picker_marks_the_headline(self):
        html = self.chose(None, "(renderSourcePicker(), "
                                "__els.get('srcPick').innerHTML)")
        self.assertIn("headline", html,
                      "nothing says which of these Airo is actually using")

    def test_looking_at_an_indoor_sensor_does_not_make_it_the_headline(self):
        """The constraint that matters. A view choice must not become a claim
        about the air — that is the contamination the placement column exists
        to prevent, arriving through a dropdown instead."""
        payload = view(source={"provider": "purpleair", "site_id": "pa-near",
                               "site_name": "Northfield"})
        setup = ('  viewSource = "purpleair/pa-inside";\n'
                 "  seriesData = JSON.parse(process.env.AIRO_SERIES);\n"
                 "  readings = primarySeries(seriesData, latest);\n")
        got = render(payload, call="renderHeadline()", host="big", setup=setup,
                     extra_env={"AIRO_SERIES": json.dumps(self.data)})

        self.assertEqual("20", got,
                         "selecting a sensor changed the headline figure")


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheWindowPanelRendersWhatItIsTold(unittest.TestCase):
    """It used to decide. On a morning with a fire nearby it said
    "Cleanest part of the day — good window to open up and ventilate" beside
    a headline well into the third band and rising sharply, because it read
    the clock and never the air."""

    def panel(self, host="statusTxt", **advice):
        return render(view(window_advice=advice or None),
                      call="updateStatus()", host=host)

    def test_the_served_advice_is_shown_verbatim(self):
        got = self.panel(headline="Not the window to open up",
                         advice="Keep the house closed and run purifiers.",
                         why="The reading is 99.", may_ventilate=False)

        self.assertIn("Keep the house closed", got)

    def test_the_reason_is_shown_with_the_advice(self):
        got = self.panel(headline="Not the window to open up",
                         advice="Keep the house closed.",
                         why="Above the clean band.", may_ventilate=False)

        self.assertIn("Above the clean band", got)

    def test_the_page_invents_nothing_when_the_server_is_silent(self):
        """An older server sends no decision. Saying nothing is safe; falling
        back to the clock is the behaviour that caused this."""
        self.assertEqual("", self.panel())

    def test_the_page_holds_no_ventilation_wording_of_its_own(self):
        """The discriminating check. If the words are still in the page, a
        future edit can reach them and the server stops being the only voice.
        """
        script = page_script("dashboard.html")
        i = script.index("function updateStatus()")
        body = strip_js_comments(script[i:i + 1600])

        # Comments stripped first. The function's own comment quotes the old
        # wording to explain why it is gone, and a check that greps raw text
        # cannot tell a comment from code -- a trap this repository has fallen
        # into three times, once matching the comment that explained why the
        # thing it forbids is avoided.
        # The *sentences*, not the word. Reading a served field called
        # `may_ventilate` is the correct behaviour and must not be forbidden
        # by a check that was aiming at prose.
        for phrase in ("open up and ventilate", "Cleanest part of the day",
                       "Close up now", "Risk window active",
                       "purifiers on by"):
            self.assertNotIn(
                phrase.lower(), body.lower(),
                f"the page still carries its own advice text: {phrase!r}")


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheChartKeyFollowsTheServedScale(unittest.TestCase):
    """The legend under the history chart, rendered rather than typed.

    It was five rows of static markup — "0–33 Very good" through "150–200
    Very poor". `adoptBands()` replaced the JS band table from the served
    payload and could not touch markup, so a us_epa install recoloured the
    chart around EPA breakpoints and left the key beneath it naming Australian
    bands: two contradictory statements about the same colours inside one
    glance. On an AU install the list simply stopped short, so the Hazardous
    band was a colour on the chart with no name anywhere on the page.
    """

    def legend(self, scale):
        view = payload(source(), bands=poller.scale_bands(scale),
                       scale=scale, scale_label=poller.SCALES[scale]["label"])
        return render(view, call="renderLegend()", host="legend",
                      setup="adoptBands(latest.bands);")

    def test_the_australian_key_names_the_australian_bands(self):
        got = self.legend("au")
        for band in ("Very good", "Good", "Fair", "Poor", "Very poor"):
            self.assertIn(band, got)

    def test_the_top_band_is_named_at_last(self):
        """Hazardous had a colour and no label. The band a reader most needs
        to recognise was the one the key left out."""
        self.assertIn("Hazardous", self.legend("au"))

    def test_an_epa_install_gets_epa_bands(self):
        """The finding. Nothing Australian may survive into a us_epa key."""
        got = self.legend("us_epa")
        self.assertIn("Unhealthy", got)
        self.assertIn("Moderate", got)
        for australian in ("Very good", "Very poor"):
            self.assertNotIn(australian, got,
                             "the key still names Australian bands on a US "
                             "EPA install")

    def test_the_raw_scale_key_is_not_an_index(self):
        """The raw scale has a fractional boundary — 37.5 — which is why the
        key states ceilings rather than inventing a range of "38–75"."""
        got = self.legend("raw")
        self.assertIn("WHO guideline", got)
        self.assertIn("37.5", got)

    def test_the_boundaries_are_the_served_ones(self):
        got = self.legend("us_epa")
        for band in poller.scale_bands("us_epa"):
            if band["max"] is not None:
                # `%g`, because the payload carries floats and JSON.stringify
                # writes 50.0 as 50 — the page prints the number the browser
                # parsed, not Python's repr of it.
                self.assertIn(f"{band['max']:g}", got,
                              f"{band['name']} is missing its boundary")

    def test_the_caption_names_the_served_scale(self):
        """"Colour is the Australian AQI band" was printed under the heatmap
        whatever scale was configured."""
        view = payload(source(), scale="us_epa", scale_label="US EPA AQI")
        got = render(view, call="renderCaptions()", host="hmColourNote")

        self.assertIn("US EPA AQI", got)
        self.assertNotIn("Australian", got)

    def test_no_scale_is_named_when_none_is_served(self):
        """Omitted rather than guessed. A caption asserting the wrong national
        standard for the colours above it is worse than no caption."""
        got = render(payload(source(), scale_label=None),
                     call="renderCaptions()", host="hmColourNote")

        self.assertIn("Rows are nights", got)
        self.assertNotIn("band", got)


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheRiskWindowIsConfigured(unittest.TestCase):
    """The evening window, taken from the payload instead of assumed.

    `risk_window` has been a setting since the time-of-day hint was written
    and the page ignored it: 15 and 1 were spelled out in the chart shading,
    the night key, the heatmap's hour columns, today's evening/daytime split
    and three captions. A user who moved their window to 6pm was advised about
    6pm by the menu bar and shown charts about 3pm here, with nothing on
    either surface saying they disagreed.

    The window used throughout is 18:00–02:00 — it wraps midnight like the
    default but shares no boundary with it, so a page that had quietly kept
    the old numbers cannot pass by coincidence.
    """

    WINDOW = "adoptRiskWindow({start_hour: 18, end_hour: 2});"

    def grid(self, readings, setup=WINDOW):
        return json.loads(render(readings, call="1", into="readings",
                                 expr="eveningGrid()", setup=setup))

    def all_hours(self, level=10.0):
        """One reading an hour, right round the clock, for two days."""
        return [{"t": f"2026-03-{day:02d}T{hour:02d}:30:00",
                 "v": level, "pm25": level / 4, "quality": "ok"}
                for day in (1, 2) for hour in range(24)]

    def test_the_columns_are_the_configured_hours(self):
        got = self.grid(self.all_hours())
        hours = sorted(int(h) for h in got[0]["cells"])

        self.assertEqual([0, 1, 18, 19, 20, 21, 22, 23], hours)

    def test_the_default_window_is_still_the_default_window(self):
        """The control. Without a served window the page behaves exactly as it
        did — the payload is an override, not a requirement."""
        got = self.grid(self.all_hours(), setup="")
        hours = sorted(int(h) for h in got[0]["cells"])

        self.assertEqual([0, 15, 16, 17, 18, 19, 20, 21, 22, 23], hours)

    def test_an_hour_outside_the_window_is_not_bucketed(self):
        """15:30 is inside the default window and outside an 18:00 one. Under
        the old literals it was counted either way."""
        got = self.grid([
            {"t": "2026-03-01T15:30:00", "v": 99.0, "pm25": 24.0, "quality": "ok"},
            {"t": "2026-03-01T19:30:00", "v": 10.0, "pm25": 2.5, "quality": "ok"},
        ])

        self.assertEqual(1, len(got))
        self.assertEqual(10.0, got[0]["peak"],
                         "a reading from outside the configured window was "
                         "counted in the evening figures")

    def test_the_night_rolls_over_at_the_configured_end(self):
        """01:30 belongs to the previous evening under an 02:00 end, and to
        the *following* one under the default 01:00 end. The rollover hour is
        the window's, not a literal 1."""
        got = self.grid([
            {"t": "2026-03-01T20:30:00", "v": 10.0, "pm25": 2.5, "quality": "ok"},
            {"t": "2026-03-02T01:30:00", "v": 30.0, "pm25": 7.5, "quality": "ok"},
        ])

        self.assertEqual(1, len(got), "01:30 started a new night")
        self.assertEqual("2026-03-01", got[0]["date"])

    def test_a_window_that_does_not_wrap_never_rolls_over(self):
        """A morning inversion is a real configuration — 05:00 to 09:00 — and
        it has no "night before". The old code subtracted a day from anything
        before hour 1 unconditionally."""
        got = self.grid(self.all_hours(),
                        setup="adoptRiskWindow({start_hour: 5, end_hour: 9});")
        hours = sorted(int(h) for h in got[0]["cells"])

        self.assertEqual([5, 6, 7, 8], hours)
        self.assertEqual(["2026-03-01", "2026-03-02"],
                         [r["date"] for r in got],
                         "a non-wrapping window still rolled a night over")

    def test_the_captions_state_the_configured_window(self):
        for host, expected in (("riskCaption", "6pm–2am"),
                               ("hmCaption", "mean per hour, 6pm to 2am"),
                               ("premCaption", "6pm-onward"),
                               ("s_eveLabel", "(6pm+)")):
            with self.subTest(caption=host):
                got = render(payload(source()), call="renderCaptions()",
                             host=host, setup=self.WINDOW)
                self.assertIn(expected, got)
                self.assertNotIn("3pm", got)

    def test_the_captions_come_from_the_served_payload(self):
        """End to end: the page is handed a window shaped the way the server
        sends it, rather than having adoptRiskWindow() called by the test."""
        view = payload(source(), risk_window={"enabled": True,
                                              "start_hour": 18, "end_hour": 2})
        got = render(view, call="renderCaptions()", host="riskCaption",
                     setup="adoptRiskWindow(latest.risk_window);")

        self.assertIn("6pm–2am", got)

    def test_a_nonsense_window_is_refused_rather_than_obeyed(self):
        """An out-of-range hour would silently empty every column, and an
        empty grid reads as "this sensor collected nothing" rather than as a
        setting that needs fixing."""
        got = self.grid(self.all_hours(),
                        setup="adoptRiskWindow({start_hour: 47, end_hour: -3});")
        hours = sorted(int(h) for h in got[0]["cells"])

        self.assertEqual([0, 15, 16, 17, 18, 19, 20, 21, 22, 23], hours)


@unittest.skipIf(NODE is None, "node is not installed")
class TestTheStalenessBannerFollowsTheCadence(unittest.TestCase):
    """"The background poller may have stopped", said at the right time.

    The threshold was a flat 45 minutes against `fetched_utc`. Forty-five is
    three polls only on the default fifteen — `poll_minutes` is a setting, and
    an hourly poller is older than 45 minutes for most of every *normal*
    cycle. So the banner appeared on nearly every visit to an install where
    nothing was wrong: the same failure CONVENTIONS records for gap thresholds
    ("a fixed 25 minutes ... fires on every poll against an hourly feed"), and
    the surest way to have a warning ignored on the day it is true.
    """

    #: A fixed instant, so the answer does not depend on when the suite runs.
    FETCHED = "2026-03-01T00:00:00+00:00"

    def overdue(self, minutes, poll_minutes):
        at = f"new Date('{self.FETCHED}').getTime() + {minutes} * 60000"
        expr = (f"overdueMinutes({{fetched_utc: '{self.FETCHED}',"
                f" poll_minutes: {poll_minutes}}}, {at})")
        return json.loads(render(payload(source()), call="1", expr=expr))

    def test_an_hourly_poller_is_not_called_stopped_after_an_hour(self):
        """The finding. One cadence's ordinary freshness was another's alarm."""
        self.assertIsNone(self.overdue(70, 60),
                          "an hourly poller was called stopped ten minutes "
                          "after a perfectly ordinary poll")

    def test_an_hourly_poller_that_really_has_stopped_is_reported(self):
        self.assertIsNotNone(self.overdue(200, 60))

    def test_the_default_install_behaves_exactly_as_before(self):
        """15 x 3 is the 45 this replaces. The factor exists so the number
        follows the setting, not to change the default."""
        self.assertIsNone(self.overdue(44, 15))
        self.assertIsNotNone(self.overdue(46, 15))

    def test_a_fast_poller_is_held_to_its_own_schedule(self):
        """The other direction, and the reason this is not simply a bigger
        constant: a five-minute cadence silent for half an hour has stopped,
        and the flat threshold said nothing about it for forty-five."""
        self.assertIsNotNone(self.overdue(30, 5))

    def test_a_missing_cadence_falls_back_rather_than_failing(self):
        expr = (f"overdueMinutes({{fetched_utc: '{self.FETCHED}'}},"
                f" new Date('{self.FETCHED}').getTime() + 46 * 60000)")
        self.assertIsNotNone(
            json.loads(render(payload(source()), call="1", expr=expr)))

    def test_nothing_is_claimed_before_the_first_poll(self):
        self.assertIsNone(
            json.loads(render(payload(source()), call="1",
                              expr="overdueMinutes({}, 0)")))


#: The settings page's "add your own sensor" flow, driven end to end: probe a
#: sensor, add it, store its read key. `KEYS_STATUS` is substituted so each
#: test can decide what the key POST answers.
ADD_OWN_SENSOR = """
let RELOADED = false;
globalThis.location = { reload(){ RELOADED = true; }, href: '' };
const BANNERS = [];
globalThis.fetch = async (path) => {
  if (path === '/api/sources/probe') return {ok: true, status: 200,
    json: async () => ({ok: true, provider: 'purpleair', site_id: '7',
      site_name: 'Backyard', placement: 'outdoor',
      placement_note: 'reported as outdoors', pm25: 5.0,
      latitude: -33.5, longitude: 151.0})};
  if (path === '/api/settings') return {ok: true, status: 200,
    json: async () => SETTINGS};
  if (path === '/api/keys') return {ok: KEYS_OK, status: KEYS_STATUS,
    json: async () => KEYS_BODY};
  return {ok: true, status: 200, json: async () => ({})};
};
const __banner = banner;
banner = (kind, html) => { BANNERS.push(html || ''); __banner(kind, html); };
for (const [id, v] of [['s-own-net', 'purpleair'], ['s-own-id', '7'],
                       ['s-own-key', 'a-read-key']])
  document.getElementById(id).value = v;
drawSources();
await document.getElementById('s-own-check').onclick();
await document.getElementById('s-own-add').onclick();
"""


@unittest.skipIf(NODE is None, "node is not installed")
class TestAFailedKeyIsNotLostInAReload(unittest.TestCase):
    """Adding a private sensor, when storing its read key fails.

    The page POSTed the key, dropped the answer, and reloaded unconditionally.
    A rejected key — a stale token, an unwritable key directory, a server that
    went away between the two requests — took its error message into the
    reload with it, and the user was left holding a sensor that was configured,
    listed among their sources, and permanently unreadable. A private sensor
    needs its read key; without one it reports nothing, which reads as a broken
    sensor rather than as a missing key, and nothing on the page ever said the
    key had not been stored.

    Driven through the page's own handlers rather than by calling the POST
    directly: the bug was in the sequencing — `await` then `reload()` with no
    branch between them — so a test that did not reach the reload could not
    have seen it.
    """

    SETTINGS = {"sources": [], "networks": [], "location": {},
                "data": {}, "alerts": {},
                "choices": {"providers": ["purpleair"], "aqi_scales": ["au"],
                            "fusion_rules": ["nearest"]},
                "fusion": {"rule": "nearest"}, "aqi_scale": "au"}

    def run_flow(self, ok="true", status=500,
                 body="{error: 'the key file could not be written'}"):
        setup = (f"const KEYS_OK = {ok}, KEYS_STATUS = {status};\n"
                 f"const KEYS_BODY = {body};\n" + ADD_OWN_SENSOR)
        out = render(self.SETTINGS, name="settings.html", call="1",
                     into="SETTINGS", setup=setup,
                     expr="[__els.get('s-own-result').innerHTML, RELOADED, BANNERS]")
        shown, reloaded, banners = json.loads(out)
        return shown, reloaded, banners

    def test_a_rejected_key_is_reported(self):
        shown, _, _ = self.run_flow(ok="false")

        self.assertIn("read key could not be saved", shown)
        self.assertIn("the key file could not be written", shown,
                      "the server's reason was not passed on")

    def test_a_rejected_key_does_not_reload_the_page(self):
        """The finding itself. Reloading discards the only message the user
        would ever get about it."""
        _, reloaded, _ = self.run_flow(ok="false")

        self.assertFalse(reloaded,
                         "the page reloaded away the error, exactly as it did "
                         "before — the failure is invisible again")

    def test_the_message_says_what_to_do_about_it(self):
        """The sensor *was* added. Saying only "something failed" leaves
        someone with a source they cannot tell is inert."""
        shown, _, _ = self.run_flow(ok="false")

        self.assertIn("Keys panel", shown)
        self.assertIn("cannot be read", shown)

    def test_a_stale_token_is_reported_as_a_reload_not_a_refusal(self):
        """403 is "Airo restarted", not "permission denied" — and the key was
        not saved, so it must not reload either."""
        shown, reloaded, banners = self.run_flow(ok="false", status=403,
                                                 body="({})")

        self.assertFalse(reloaded)
        joined = " ".join(banners)
        self.assertIn("Airo restarted since this page was opened", joined)
        self.assertIn("Your key was not saved.", joined,
                      "the banner did not say what the stale token cost")
        self.assertIn("Reload the page", joined)
        self.assertIn("read key could not be saved", shown)

    def test_an_unreachable_server_does_not_reload_either(self):
        """The transport failing is the same loss by a different route."""
        setup = ("const KEYS_OK = true, KEYS_STATUS = 200, KEYS_BODY = {};\n"
                 + ADD_OWN_SENSOR.replace(
                     "if (path === '/api/keys')",
                     "if (path === '/api/keys') throw new Error('offline');\n"
                     "  if (false)"))
        out = render(self.SETTINGS, name="settings.html", call="1",
                     into="SETTINGS", setup=setup,
                     expr="[__els.get('s-own-result').innerHTML, RELOADED, BANNERS]")
        shown, reloaded, _ = json.loads(out)

        self.assertFalse(reloaded)
        self.assertIn("Could not reach Airo", shown)

    def test_a_stored_key_still_reloads(self):
        """The control. The reload is how the new sensor appears in the list,
        so refusing to reload on success would be a different bug."""
        _, reloaded, _ = self.run_flow(ok="true", status=200,
                                       body="({path: '~/.airo/purpleair.key'})")

        self.assertTrue(reloaded,
                        "a successful add no longer refreshes the page")


class TestOneHelperOwnsEveryWrite(unittest.TestCase):
    """Every mutating request on the settings page goes through postJSON().

    Reads the script rather than running it, so unlike the rest of this file
    it needs no node and holds on every platform. Counting is the only way to
    ask this question: a second copy of the 403 branch is not a behaviour any
    payload can provoke — it is a behaviour that appears the day someone
    edits one copy and not the other.

    Six of the eight write paths hand-rolled fetch, the token header, a catch
    for an unreachable server, a 403 branch and a non-2xx branch. Two of them
    had no 403 branch at all, so a page left open across a restart reported
    the server's bare refusal on those paths and a clear "reload me" on the
    others — the same event, two stories, decided by which button was pressed.
    """

    def script(self):
        return strip_js_comments(page_script("settings.html"))

    def test_only_the_helper_sends_the_token(self):
        """The token header is the marker of a write. One occurrence means one
        place that can forget it."""
        self.assertEqual(
            1, self.script().count("X-Airo-Token"),
            "a write path builds its own headers again")

    def test_only_the_helper_posts(self):
        self.assertEqual(
            1, self.script().count("method:'POST'"),
            "a write path calls fetch directly again")

    def test_the_stale_token_banner_is_written_once(self):
        """The wording is pinned by test_settings_api; what matters here is
        that there is one copy of it to pin."""
        self.assertEqual(
            1, self.script().count("Airo restarted since this page was opened"),
            "the 403 wording has been copied again — that is how the two "
            "paths that lacked it drifted in the first place")

    def test_the_unreachable_server_is_described_once(self):
        script = self.script()
        self.assertEqual(1, script.count("Could not reach Airo"))
        self.assertNotIn("Could not reach Airo to open a folder chooser",
                         script)
