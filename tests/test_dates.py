# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dashboard date-handling tests, run under two timezones.

ROADMAP #5. The chart x-axis is a linear scale over epoch-ms with custom tick
generation (Chart.js `type:'time'` needs a date adapter we deliberately don't
load — see ARCHITECTURE §3.3). That makes tick placement our problem, and
stepping by a fixed 86,400,000 ms across a daylight-saving boundary silently
shifts every subsequent label by an hour.

Queensland has no DST, so this was invisible to the original author. These
tests run the real extracted JavaScript under Australia/Sydney (DST) and
America/New_York (DST, opposite hemisphere) as well as Australia/Brisbane
(no DST), because the bug only appears when a tick range spans a transition.

Skipped rather than failed when Node isn't installed — the Python side has no
dependencies and must stay runnable anywhere.
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

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard.html"

NODE = shutil.which("node")


def extract_js():
    """Pull the dashboard's inline script, as CI does."""
    html = DASHBOARD.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return scripts[-1]


def _declaration(js, name):
    """One top-level declaration from the page's script, whole.

    Whole is the point. This used to take a single line for anything starting
    `const` and everything up to the next `\\n}` otherwise, which is fine until
    a helper is an arrow function with a body or a ternary that wraps. Then it
    silently took the first line, and the failure surfaced three tests away as
    "nightOf is not defined" — a truncation reported as a missing function.

    So: whole lines until the braces and parentheses balance and the statement
    is terminated. Crude, and it does not need to be more than that; every
    name passed here is a top-level helper written in this project's style.
    """
    idx = js.find(name)
    if idx == -1:
        raise AssertionError(f"{name!r} not found in dashboard.html")
    if name.startswith(("const", "let")):
        end = idx
        while True:
            end = js.index("\n", end) + 1
            chunk = js[idx:end]
            if (chunk.count("{") == chunk.count("}")
                    and chunk.count("(") == chunk.count(")")
                    and chunk.rstrip().endswith(";")):
                return chunk
    # A function declaration: through the matching closing brace at column 0.
    return js[idx:js.index("\n}", idx) + 2] + "\n"


#: byNight() no longer hardcodes 15 and 1 — it asks the served risk window —
#: so its helpers come with it. Order matters only in that everything must be
#: declared before it is used, and these are all `const`/`let` in one scope.
NIGHT_HELPERS = ("const pad2", "const localDateKey",
                 "let RISK_START", "const riskWraps", "const inRiskWindow",
                 "const nightOf")


def run_js(snippet, tz, helpers=("const H_MS", "function chooseStep",
                                 "function generateTicks")):
    """Run a snippet with the dashboard's date helpers in scope, under TZ."""
    js = extract_js()

    # Take only the standalone helpers we need. The rest of the file touches
    # the DOM and Chart.js, neither of which exist here.
    wanted = [_declaration(js, name) for name in helpers]

    prog = "\n".join(wanted) + "\n" + snippet
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(prog)
        path = f.name
    try:
        # Inherit the environment and override TZ. Replacing it outright drops
        # SystemRoot/ComSpec on Windows, where node then exits non-zero with an
        # empty stderr -- which looks exactly like a DST bug and is not one.
        env = dict(os.environ)
        env["TZ"] = tz
        out = subprocess.run([NODE, path], capture_output=True, text=True,
                             env=env, timeout=30)
        if out.returncode != 0:
            raise AssertionError(
                f"node failed under {tz} (exit {out.returncode}): "
                f"{out.stderr or '<no stderr>'}")
        return json.loads(out.stdout)
    finally:
        Path(path).unlink(missing_ok=True)


def tz_is_honoured(tz):
    """Does this platform's node actually apply TZ?

    Not universal -- some Windows/node combinations ignore it. A test that
    silently runs in the wrong zone proves nothing, so check rather than
    assume, and skip loudly if it does not take.
    """
    try:
        got = run_js(
            "console.log(JSON.stringify("
            "Intl.DateTimeFormat().resolvedOptions().timeZone));", tz)
    except Exception:
        return False
    return got == tz


@unittest.skipIf(NODE is None, "node not installed")
class TestTickGeneration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not tz_is_honoured("Australia/Sydney"):
            raise unittest.SkipTest(
                "this platform's node ignores TZ; DST behaviour cannot be tested here")

    # Local midnights either side of a DST transition.
    CASES = [
        # tz, a date range spanning that zone's spring-forward
        ("Australia/Sydney", "2026-09-28", "2026-10-12"),
        ("America/New_York", "2026-03-01", "2026-03-15"),
        ("Australia/Brisbane", "2026-09-28", "2026-10-12"),  # control: no DST
    ]

    def _ticks(self, tz, start, end, step_days):
        snippet = f"""
        const a = new Date('{start}T00:00:00');
        const b = new Date('{end}T23:59:59');
        const ticks = generateTicks(a.getTime(), b.getTime(), {step_days} * D_MS);
        console.log(JSON.stringify(ticks.map(t => {{
          const d = new Date(t.value);
          return {{ h: d.getHours(), m: d.getMinutes(),
                   day: d.getDate(), month: d.getMonth() + 1 }};
        }})));
        """
        return run_js(snippet, tz)

    def test_daily_ticks_stay_on_local_midnight_across_dst(self):
        """The bug: fixed-ms stepping lands ticks at 23:00 or 01:00."""
        for tz, start, end in self.CASES:
            with self.subTest(tz=tz):
                ticks = self._ticks(tz, start, end, 1)
                self.assertTrue(ticks, f"no ticks generated for {tz}")
                offenders = [t for t in ticks if t["h"] != 0 or t["m"] != 0]
                self.assertEqual(
                    offenders, [],
                    f"{tz}: {len(offenders)} tick(s) drifted off midnight, "
                    f"first at {offenders[0] if offenders else None}")

    def test_multi_day_ticks_also_stay_on_midnight(self):
        for tz, start, end in self.CASES:
            for step in (3, 7):
                with self.subTest(tz=tz, step=step):
                    ticks = self._ticks(tz, start, end, step)
                    self.assertTrue(all(t["h"] == 0 and t["m"] == 0 for t in ticks),
                                    f"{tz} step={step} drifted off midnight")

    def test_daily_ticks_advance_one_calendar_day_each(self):
        """Across spring-forward the gap is 23 hours, not 24 — but the tick
        must still land on the next calendar day."""
        for tz, start, end in self.CASES:
            with self.subTest(tz=tz):
                ticks = self._ticks(tz, start, end, 1)
                days = [t["day"] for t in ticks]
                self.assertEqual(len(days), len(set(days)),
                                 f"{tz}: a calendar day was repeated or skipped")

    def test_ticks_are_within_the_requested_range(self):
        for tz, start, end in self.CASES:
            with self.subTest(tz=tz):
                snippet = f"""
                const a = new Date('{start}T00:00:00').getTime();
                const b = new Date('{end}T23:59:59').getTime();
                const ticks = generateTicks(a, b, D_MS);
                console.log(JSON.stringify({{
                  ok: ticks.every(t => t.value >= a && t.value <= b),
                  n: ticks.length
                }}));
                """
                r = run_js(snippet, tz)
                self.assertTrue(r["ok"], f"{tz}: tick outside range")
                self.assertGreater(r["n"], 0)


@unittest.skipIf(NODE is None, "node not installed")
class TestSubDayTicks(unittest.TestCase):
    """Hour-sized steps are genuine durations and stay fixed arithmetic."""

    def test_hourly_ticks_are_evenly_spaced(self):
        snippet = """
        const a = new Date('2026-07-01T00:00:00').getTime();
        const b = new Date('2026-07-01T12:00:00').getTime();
        const ticks = generateTicks(a, b, H_MS);
        const gaps = ticks.slice(1).map((t,i) => t.value - ticks[i].value);
        console.log(JSON.stringify({ n: ticks.length, gaps: [...new Set(gaps)] }));
        """
        r = run_js(snippet, "Australia/Brisbane")
        self.assertEqual(r["gaps"], [3600000])
        self.assertGreater(r["n"], 5)

    def test_no_ticks_when_range_is_inverted(self):
        snippet = """
        const t = generateTicks(2000, 1000, H_MS);
        console.log(JSON.stringify({ n: t.length }));
        """
        self.assertEqual(run_js(snippet, "UTC")["n"], 0)

    def test_tick_count_is_bounded(self):
        """A huge range must not generate an unbounded array."""
        snippet = """
        const a = new Date('1990-01-01T00:00:00').getTime();
        const b = new Date('2030-01-01T00:00:00').getTime();
        console.log(JSON.stringify({ n: generateTicks(a, b, D_MS).length }));
        """
        self.assertLessEqual(run_js(snippet, "UTC")["n"], 400)


@unittest.skipIf(NODE is None, "node not installed")
class TestStepChoice(unittest.TestCase):
    def test_step_grows_with_span(self):
        snippet = """
        const spans = [3*H_MS, 12*H_MS, 2*D_MS, 8*D_MS, 15*D_MS, 60*D_MS, 300*D_MS];
        console.log(JSON.stringify(spans.map(chooseStep)));
        """
        steps = run_js(snippet, "UTC")
        self.assertEqual(steps, sorted(steps), "step choice is not monotonic")




def run_night_js(snippet, tz="Australia/Brisbane"):
    """Run a snippet with byNight() and its date helpers in scope.

    Same extraction approach as run_js: take only the standalone helpers, so
    nothing here needs the DOM or the chart renderer.
    """
    js = extract_js()
    wanted = [_declaration(js, name)
              for name in NIGHT_HELPERS + ("function byNight",)]

    prog = "let readings = [];\n" + "\n".join(wanted) + "\n" + snippet
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(prog)
        path = f.name
    try:
        env = dict(os.environ)
        env["TZ"] = tz
        out = subprocess.run([NODE, path], capture_output=True, text=True,
                             env=env, timeout=30)
        if out.returncode != 0:
            raise AssertionError(f"node failed: {out.stderr or '<no stderr>'}")
        return json.loads(out.stdout)
    finally:
        Path(path).unlink(missing_ok=True)


@unittest.skipUnless(NODE, "node not available")
class TestEveningCoverageIsResolutionIndependent(unittest.TestCase):
    """The evening-premium rule counted SAMPLES, which means different things
    per provider. The dashboard required 12 -- "2 hours at 10-min resolution"
    -- but the evening window (3pm-1am) is only 10 hours long, so an hourly
    government feed could never reach 12 samples inside it. Every user on a
    keyless government source, which is the default a new user is steered to,
    saw "--" for evening premium and every worst-nights ratio, permanently,
    with a full database behind it.
    """

    def test_an_hourly_feed_produces_a_ratio(self):
        got = run_night_js("""
          for(let h = 0; h < 24; h++)
            readings.push({t: +new Date(2026, 6, 15, h, 0, 0),
                           v: (h >= 15 || h < 1) ? 20 : 10});
          const nights = byNight().filter(n => n.complete);
          console.log(JSON.stringify({complete: nights.length}));
        """)
        self.assertGreaterEqual(got["complete"], 1,
                                "an hourly feed produced no complete night")

    def test_a_ten_minute_feed_still_produces_a_ratio(self):
        got = run_night_js("""
          for(let h = 0; h < 24; h++)
            for(let m = 0; m < 60; m += 10)
              readings.push({t: +new Date(2026, 6, 15, h, m, 0),
                             v: (h >= 15 || h < 1) ? 20 : 10});
          console.log(JSON.stringify({
            complete: byNight().filter(n => n.complete).length}));
        """)
        self.assertGreaterEqual(got["complete"], 1)

    def test_a_barely_logged_night_is_still_rejected(self):
        """The rule must still exclude a partly-logged day -- that is what it
        was there for. Two hours either side is not a night."""
        got = run_night_js("""
          [15, 16].forEach(h => readings.push(
            {t: +new Date(2026, 6, 15, h, 0, 0), v: 20}));
          [9, 10].forEach(h => readings.push(
            {t: +new Date(2026, 6, 15, h, 0, 0), v: 10}));
          console.log(JSON.stringify({
            complete: byNight().filter(n => n.complete).length}));
        """)
        self.assertEqual(got["complete"], 0,
                         "a two-hour sample was accepted as a complete night")


class TestEveryRenderStepIsGuarded(unittest.TestCase):
    """ARCHITECTURE §3.3: one failing render step must not blank the others.

    The guard had been applied to a *list* of steps rather than to the act of
    running one, so renderHeader() -- which calls renderFooter(), the surface
    carrying attribution and the health disclaimer -- was invoked bare. A throw
    in either took out every panel after it, which is exactly the bug §3.3
    exists to record.
    """

    def dashboard(self):
        return (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_render_calls_nothing_outside_the_guarded_loop(self):
        html = self.dashboard()
        body = html[html.index("function render(){"):]
        body = body[:body.index("}", body.index("for(const"))]
        # The only call in render() is the one inside the try.
        calls = re.findall(r"(\w+)\(", body)
        ignore = {"render", "for", "of", "try", "catch", "console", "error"}
        self.assertEqual({"fn"}, {c for c in calls if c not in ignore},
                         f"render() calls something outside its guard: {calls}")

    def test_every_render_function_is_listed_as_a_step(self):
        """A function that draws but is not in the table is either dead or
        unguarded, and both are worth failing over."""
        html = self.dashboard()
        defined = set(re.findall(r"function ((?:render|draw)\w+)\s*\(", html))
        listed = set(re.findall(r"=> (\w+)\(\)", html))

        # A helper is fine unguarded *if* something guarded calls it --
        # renderFooter is called by renderHeader, drawDays by dayTable. What
        # must never happen is a drawing function nothing guarded can reach,
        # because that one is being called from somewhere with no guard at all.
        def body_of(name):
            """The function's own body, brace-matched.

            A fixed-size window instead let the scan run past the closing
            brace into the *next* declaration, so a function appeared to call
            the one defined after it -- and every step looked reachable
            whether or not anything reached it.
            """
            start = html.find(f"function {name}(")
            if start < 0:
                return ""
            open_brace = html.find("{", start)
            depth, i = 0, open_brace
            while i < len(html):
                if html[i] == "{":
                    depth += 1
                elif html[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return html[open_brace + 1:i]
                i += 1
            return ""

        reachable = set(listed)
        for _ in range(4):                      # a few levels is plenty here
            for fn in list(defined | {"dayTable", "todayStats", "updateStatus"}):
                if fn not in reachable:
                    continue
                reachable |= set(re.findall(r"((?:render|draw)\w+)\s*\(",
                                            body_of(fn)))

        missing = defined - reachable
        self.assertEqual(set(), missing,
                         f"drawing functions nothing guarded reaches: {missing}")

    def test_the_dashboard_does_not_recompute_the_trend(self):
        """compute_trend() in poller.py says in its own docstring that it lives
        there so the menu bar, the tray and the dashboard cannot disagree. The
        page had its own copy, thresholds and all."""
        html = self.dashboard()
        i = html.index("function renderTrend(){")
        body = html[i:html.index("function renderRecordStats", i)]
        self.assertIn("latest.trend", body)
        for literal in ("d>=5", "d >= 5", "d>=2", "d >= 2"):
            self.assertNotIn(literal, body,
                             "the dashboard re-derives the trend thresholds")


class TestTheDashboardDoesNotSecondGuessQuality(unittest.TestCase):
    """§3.5: the plausibility judgement is made once, in Python, on the
    concentration. The page renders it."""

    def dashboard(self):
        return (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_the_page_has_no_threshold_of_its_own(self):
        html = self.dashboard()
        self.assertNotIn("IMPLAUSIBLE", html,
                         "the dashboard re-derives which readings are suspect")

    def test_the_page_renders_the_stored_verdict(self):
        html = self.dashboard()
        self.assertIn("seriesData.suspect", html,
                      "the page does not render the flagged readings at all")

    def test_the_page_does_not_convert_the_index_back_to_micrograms(self):
        """Dividing the index by four is the Australian scale written out as
        arithmetic, and wrong on any other. The concentration is served.

        Anchored on `seriesData.suspect` — the API field, which is a contract —
        rather than on the name of the local that receives it. The previous
        version indexed on the literal `const bad = ((seriesData`, so renaming
        that variable broke the test against a page that was still correct.
        A check pinned to a name tests the name.
        """
        html = self.dashboard()
        i = html.index("seriesData.suspect")
        block = html[i:i + 1600]
        self.assertNotIn("/4", block)
        self.assertIn("pm25", block)

    def test_the_page_tells_extreme_air_apart_from_a_broken_sensor(self):
        """Two verdicts arrive now, and calling the wrong one a fault is the
        worst mistake available here: telling somebody their sensor is broken
        when what is actually happening is that the air is dangerous."""
        html = self.dashboard()
        self.assertIn("'extreme'", html,
                      "the page does not distinguish the two verdicts")
        i = html.index("seriesData.suspect")
        block = html[i:i + 2600]
        self.assertIn("=== 'suspect'", block)
        self.assertIn("=== 'extreme'", block)


DAY_HELPERS = ("const pad2", "const localDateKey")


@unittest.skipIf(NODE is None, "node not installed")
class TestDayBucketing(unittest.TestCase):
    """ARCHITECTURE §3.2. `toISOString()` converts to UTC, so in UTC+10 a 9am
    reading lands on the *previous* date and a 9pm reading does not. Grouping
    by `toISOString().slice(0,10)` therefore scrambles any evening-versus-
    daytime comparison, which is the one this tool exists to make. It produced
    an evening-premium chart wrong by up to 10× before it was caught.

    The existing check greps the source for `const localDateKey`. That catches
    the helper being deleted and nothing else — a rename, a rewrite, or a
    caller going back to toISOString all pass. These run the real code.
    """

    @classmethod
    def setUpClass(cls):
        for tz in ("Australia/Brisbane", "America/New_York"):
            if not tz_is_honoured(tz):
                raise unittest.SkipTest(f"node here ignores TZ={tz}")

    def keys_for(self, iso_times, tz):
        snippet = ("const out = %s.map(s => localDateKey(new Date(s)));"
                   "console.log(JSON.stringify(out));" % json.dumps(iso_times))
        return run_js(snippet, tz, helpers=DAY_HELPERS)

    def test_a_morning_reading_keeps_its_own_local_date(self):
        """9am in Brisbane is 23:00 the previous day in UTC. Bucketed by the
        UTC date it moves back a day; bucketed locally it does not."""
        keys = self.keys_for(["2026-07-30T23:00:00Z"], "Australia/Brisbane")
        self.assertEqual(["2026-07-31"], keys,
                         "a 9am local reading was filed under the day before")

    def test_an_evening_reading_keeps_its_own_local_date(self):
        """9pm in Brisbane is 11:00 the same day in UTC, so this one is right
        either way. Both are needed: the bug moves *some* readings and not
        others, and a test using only the unaffected case passes against the
        broken code."""
        keys = self.keys_for(["2026-07-31T11:00:00Z"], "Australia/Brisbane")
        self.assertEqual(["2026-07-31"], keys)

    def test_the_same_instant_files_differently_in_different_zones(self):
        """The proof that the key really is local. If it were UTC-derived,
        both would answer the same and every test above would be vacuous."""
        instant = ["2026-07-30T23:00:00Z"]
        brisbane = self.keys_for(instant, "Australia/Brisbane")   # 09:00 Jul 31
        newyork = self.keys_for(instant, "America/New_York")      # 19:00 Jul 30
        self.assertEqual(["2026-07-31"], brisbane)
        self.assertEqual(["2026-07-30"], newyork)

    def test_a_date_key_is_zero_padded(self):
        """"2026-7-5" sorts after "2026-11-01" as a string, which is how a
        chart ends up with its days in the wrong order."""
        keys = self.keys_for(["2026-07-05T02:00:00Z"], "Australia/Brisbane")
        self.assertEqual(["2026-07-05"], keys)

    def test_a_key_never_comes_from_an_iso_string(self):
        """The mechanical guard. localDateKey could be correct and a caller
        still reach for toISOString somewhere else in the file.

        Comments are stripped rather than matched line by line: the warning
        that exists to forbid this names it, and wraps across lines, so a
        prefix check flags the documentation of the rule.
        """
        js = re.sub(r"/\*.*?\*/", "", extract_js(), flags=re.S)
        js = re.sub(r"//[^\n]*", "", js)
        self.assertNotIn("toISOString", js,
                         "toISOString is used in live code; it converts to "
                         "UTC and scrambles day bucketing")


@unittest.skipIf(NODE is None, "node not installed")
class TestEveningsAreBucketedByNight(unittest.TestCase):
    """`byNight()` decides which evening a reading belongs to, and midnight is
    the hard part: 00:30 belongs to the evening that started the day before."""

    @classmethod
    def setUpClass(cls):
        if not tz_is_honoured("Australia/Brisbane"):
            raise unittest.SkipTest("node here ignores TZ")

    def nights(self, readings, tz="Australia/Brisbane"):
        js = extract_js()
        start = js.index("function byNight(){")
        end = js.index("\n}", start) + 2
        by_night = js[start:end]
        snippet = (
            "const readings = %s.map(r => ({t: new Date(r.t), v: r.v}));\n"
            % json.dumps(readings)
            + by_night
            + "\nconsole.log(JSON.stringify(byNight().map("
              "n => ({date:n.date, eve:n.eve, day:n.day}))));")
        return run_js(snippet, tz, helpers=NIGHT_HELPERS)

    def test_after_midnight_belongs_to_the_evening_that_began_yesterday(self):
        """A reading at 00:30 is part of the night of the 30th, not the start
        of the 31st. Filed under the 31st it would split one episode across
        two rows and halve the peak of both."""
        out = self.nights([
            # 18:00 and 20:00 and 23:00 local on the 30th, then 00:30 on the 31st
            {"t": "2026-07-30T08:00:00Z", "v": 40},
            {"t": "2026-07-30T10:00:00Z", "v": 50},
            {"t": "2026-07-30T13:00:00Z", "v": 60},
            {"t": "2026-07-30T14:30:00Z", "v": 70},
        ])
        self.assertEqual(1, len(out), f"the night was split: {out}")
        self.assertEqual("2026-07-30", out[0]["date"])

    def test_daytime_and_evening_are_separated(self):
        out = self.nights([
            {"t": "2026-07-30T00:00:00Z", "v": 10},   # 10:00 local — daytime
            {"t": "2026-07-30T02:00:00Z", "v": 12},   # 12:00 local — daytime
            {"t": "2026-07-30T09:00:00Z", "v": 40},   # 19:00 local — evening
            {"t": "2026-07-30T11:00:00Z", "v": 50},   # 21:00 local — evening
        ])
        self.assertEqual(1, len(out))
        self.assertGreater(out[0]["eve"], out[0]["day"],
                           "the evening premium came out backwards")


class TestTheDashboardCanReachTheSettings(unittest.TestCase):
    """Everything is configurable from a page, and for a while nothing on
    screen said where. A user looking at the dashboard had no route to the
    settings at all, and the panel that mentions adding a network told them to
    open a terminal for something the page can do."""

    def dashboard(self):
        return (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_there_is_a_link_to_the_settings_page(self):
        self.assertIn('href="/settings"', self.dashboard(),
                      "the dashboard is a dead end")

    def test_no_panel_tells_the_user_to_run_setup_in_a_terminal(self):
        """The terminal path still exists and is still mentioned — as the
        alternative, not the instruction."""
        html = self.dashboard()
        self.assertNotIn("Then run <code>python3 setup.py", html)


@unittest.skipIf(NODE is None, "node not installed")
class TestOneBadReadingCannotDestroyTheChart(unittest.TestCase):
    """The y-axis used to be the maximum of what was plotted.

    That was only ever safe because the worst readings never arrived: anything
    over 350 µg/m³ was filed as a sensor fault and filtered out upstream. Now
    that extreme air reaches the chart -- which is the point, since it is the
    air most worth seeing -- the maximum is the wrong ceiling. One spike of
    900 µg/m³ against an ordinary week stretches the axis twentyfold and
    flattens everything else into a line along the bottom.

    A high percentile separates the two cases without a threshold to tune: a
    real event is *sustained* and drags the percentile up with it, while a
    one-off is a handful of points among thousands and does not. These run the
    real function out of the page, not a copy of it.
    """

    def ceiling(self, values):
        # run_js already parses stdout as JSON; `Infinity` is embedded as a
        # JavaScript literal rather than JSON, which is why it is written into
        # the program text rather than passed as data.
        return run_js(
            f"console.log(JSON.stringify(axisCeiling({json.dumps(values)})));",
            "Australia/Brisbane",
            helpers=("const AXIS_PERCENTILE", "function axisCeiling"))

    def test_a_single_spike_does_not_set_the_ceiling(self):
        ordinary = [8.0] * 999
        self.assertEqual(8.0, self.ceiling(ordinary + [900.0]),
                         "one reading stretched the axis over the whole week")

    def test_sustained_bad_air_does_set_the_ceiling(self):
        """The other half, and the half that makes this honest. A fire is not
        an outlier -- it is most of the window -- and the axis must follow it
        rather than pinning a genuine emergency to the top edge for days."""
        smoke = [400.0] * 500 + [8.0] * 500
        self.assertEqual(400.0, self.ceiling(smoke),
                         "the axis ignored air that was bad for half the window")

    def test_a_handful_of_readings_shows_everything_it_has(self):
        """Below ~50 points the top 2% is one reading, so a percentile is just
        the maximum wearing a hat. A short window is not the place to hide the
        worst value in it."""
        self.assertEqual(900.0, self.ceiling([8.0] * 20 + [900.0]))

    def test_an_empty_window_does_not_blow_up(self):
        self.assertEqual(0, self.ceiling([]))

    def test_an_infinity_does_not_take_the_axis_with_it(self):
        """One unusable value must not carry off the whole chart -- the same
        rule as the series endpoint, where a single bad timestamp used to
        blank every graph on the page.

        Deliberately a *short* window. A percentile shrugs off two bad values
        among a thousand whether or not they were filtered, so a long window
        cannot tell a working filter from a missing one: this test passed with
        the filter deleted. Under fifty points the maximum is used directly,
        and there Infinity is the whole axis.
        """
        self.assertEqual(8.0, self.ceiling([8.0] * 20 + [float("inf")]))
        self.assertEqual(8.0, self.ceiling([8.0] * 20 + [None]))

    def test_the_chart_actually_uses_this_ceiling(self):
        """The function being right is not the same as it being called.

        Reverting the call site to Math.max(...ys) left every test in this
        class green while the chart went back to being destroyed by a single
        spike -- the function was correct, and dead. A unit test of a helper
        proves nothing about the caller.
        """
        js = extract_js()
        i = js.index("function drawChart(){")
        block = js[i:i + 900]
        self.assertIn("axisCeiling(", block,
                      "drawChart no longer asks for a robust ceiling")
        self.assertNotIn("Math.max(...ys", block,
                         "the axis is back to the maximum of the data")


@unittest.skipIf(NODE is None, "node not installed")
class TestAReadingOffTheScaleIsStillVisible(unittest.TestCase):
    """Clamping alone would be a lie: a spike drawn at the ceiling looks
    identical to a reading that happens to sit there. The renderer marks the
    ones it pinned, and keeps the true value for the tooltip."""

    def test_the_renderer_pins_and_marks_rather_than_dropping(self):
        js = extract_js()
        i = js.index("_drawLine(){")
        block = js[i:i + 2500]
        self.assertIn("p.y > y.max", block, "nothing detects an off-scale value")
        self.assertIn("over", block)
        # The point kept for the tooltip must be the untouched one, or the
        # page would report the ceiling as the measurement.
        self.assertIn("raw: p", block)

    def test_the_true_value_is_never_overwritten_by_the_clamp(self):
        js = extract_js()
        i = js.index("_drawLine(){")
        block = js[i:i + 2500]
        self.assertNotIn("p.y = ", block,
                         "the clamp wrote over the measurement it was drawing")


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
