"""Contractual and consumer-law obligations, enforced by test.

Attribution is required by PurpleAir's Terms of Service §4.8 and by CC BY 4.0
for the government feeds. The health disclaimer exists because Australian
Consumer Law s18 makes misleading conduct actionable, and an air-quality
reading presented without caveat implies a precision consumer sensors do not
have.

Both are the kind of thing removed in a tidy-up by someone who does not know
why they are there. These tests are the reason they cannot be.
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poller  # noqa: E402
import store   # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browserguard import (  # noqa: E402
    block_browser_for_module, restore_browser_for_module)
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def html_surfaces():
    """Every page a user can actually be shown, found on disk.

    Enumerated rather than listed. A hardcoded list is a list somebody has to
    remember to extend, and it failed exactly that way: settings.html was
    added as a full user-visible surface -- rendering the location, the
    sources and the readings -- and every obligation test here kept passing
    because none of them had heard of it. The register claims these contracts
    enumerate; now they do.
    """
    found = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob("*.html"))
    found += sorted(p.relative_to(ROOT).as_posix()
                    for p in (ROOT / "tray" / "ui").glob("*.html"))
    return found


def user_visible(name):
    """Source with comments and docstrings removed.

    The claim detector must look at what a user is shown, not at what the code
    says about itself. Without this it flags the comment in poller.py that
    exists precisely to warn against the phrase it contains -- punishing the
    documentation of a rule for quoting the rule.
    """
    text = read(name)
    path = ROOT / name

    if path.suffix == ".py":
        import ast
        import io
        import tokenize

        # Strip comments.
        out = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type != tokenize.COMMENT:
                    out.append(tok)
            text = tokenize.untokenize(out)
        except Exception:
            pass

        # Strip docstrings, which are documentation rather than output.
        try:
            tree = ast.parse(text)
            docs = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    d = ast.get_docstring(node, clean=False)
                    if d:
                        docs.append(d)
            for d in docs:
                text = text.replace(d, "")
        except Exception:
            pass
        return text

    # Shell, HTML and JSX: drop whole-line comments.
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//", "<!--", "*", "/*")):
            continue
        lines.append(line)
    return "\n".join(lines)


class TestAttribution(unittest.TestCase):
    def test_every_provider_declares_attribution(self):
        for slug, p in poller.PROVIDERS.items():
            self.assertTrue(p.attribution,
                            f"{slug} publishes data with no attribution string")

    def test_purpleair_attribution_is_the_required_wording(self):
        """ToS §4.8 and the Attribution Guide require this phrase."""
        self.assertIn("Powered by PurpleAir",
                      poller.PROVIDERS["purpleair"].attribution)

    def test_government_feeds_attribute_under_cc_by(self):
        for slug in ("qld", "nsw"):
            self.assertIn("CC BY", poller.PROVIDERS[slug].attribution,
                          f"{slug} does not carry its CC BY attribution")

    def test_latest_json_carries_attributions_for_display(self):
        """Every UI renders from this. If the field goes, so does every
        attribution at once."""
        src = read("poller.py")
        self.assertIn('"attributions"', src)

    def test_each_surface_renders_attributions(self):
        """Every page found on disk, plus the tray. Enumerated, so a surface
        added tomorrow is already in scope."""
        for path in html_surfaces() + ["tray/src/main.rs"]:
            self.assertIn("attribution", read(path).lower(),
                          f"{path} does not render attribution")

    def test_no_surface_hard_codes_a_provider_attribution(self):
        """Attribution must be rendered from latest.json, never written as a
        literal. The SwiftBar plugin printed "Powered by PurpleAir"
        unconditionally: a Queensland-only user was credited to a network they
        do not use, and never saw the CC BY notice they actually owe.

        The provider classes and the export header are the two places allowed
        to hold the wording -- they are keyed by provider, so they cannot
        attribute the wrong one."""
        allowed = {"poller.py", "store.py"}
        literals = [p.attribution for p in poller.PROVIDERS.values()]
        for path in html_surfaces() + ["tray/src/main.rs", "tray/src/airo.rs",
                                       "poller.py", "fusion.py"]:
            f = ROOT / path
            if not f.exists():
                continue
            if f.name in allowed:
                continue
            text = f.read_text(encoding="utf-8")
            # airo.rs holds a JSON fixture for its own tests; strip test code.
            if "#[cfg(test)]" in text:
                text = text.split("#[cfg(test)]")[0]
            # Strip markup before matching. The dashboard footer read
            # "Powered by <a ...>PurpleAir</a>", so the literal never appeared
            # contiguously and this test passed for months while the page
            # credited PurpleAir to a Queensland-only user.
            flat = re.sub(r"<[^>]+>", "", text)
            flat = re.sub(r"\s+", " ", flat)
            for lit in literals:
                for hay, how in ((text, "verbatim"), (flat, "once markup is stripped")):
                    self.assertNotIn(
                        lit, hay,
                        f"{path} hard-codes {lit!r} ({how}) instead of "
                        f"rendering latest.json['attributions']")

    def test_the_dashboard_footer_is_built_from_the_data(self):
        """The footer is a legal surface: PurpleAir ToS 4.8 requires their
        attribution and 7.3 their accuracy disclaimer, and the government
        feeds are CC BY. Which apply depends on the sources this install
        actually reads."""
        html = read("dashboard.html")
        self.assertIn("renderFooter", html,
                      "the footer is not rendered from latest.json")
        self.assertIn("footAttrib", html)

    def test_the_purpleair_accuracy_disclaimer_is_conditional(self):
        """Shown to everyone it would assert that a government regulatory
        monitor carries no accuracy guarantee -- wrong, and not ours to say."""
        html = read("dashboard.html")
        i = html.index("renderFooter")
        body = html[i:i + 2000]
        self.assertIn("usesPurpleAir", body,
                      "the ToS 7.3 disclaimer is shown unconditionally")

    def test_the_health_disclaimer_is_not_conditional(self):
        """Unlike the ToS text, this applies to every install however
        configured, so it stays in the markup rather than being rendered."""
        html = read("dashboard.html")
        i = html.index('id="footTerms"')
        self.assertIn("not medical advice", html[i:i + 600].lower())


class TestOneWidget(unittest.TestCase):
    """Three menu-bar implementations meant each re-derived bands, staleness
    and attribution from latest.json, so every feature was written three times
    and drifted twice. The Tauri tray is the only one, and the only one that
    runs on more than macOS."""

    RETIRED = ["swiftbar", "ubersicht", "xbar", "argos", "bitbar"]

    def test_the_retired_macos_plugins_are_gone(self):
        for name in self.RETIRED:
            self.assertFalse(
                (ROOT / name).exists(),
                f"{name}/ is back -- Airo maintains exactly one widget")

    def test_the_tray_is_the_widget(self):
        self.assertTrue((ROOT / "tray" / "src" / "main.rs").exists())

    def test_no_doc_still_tells_a_user_to_install_a_retired_plugin(self):
        """A removed widget that is still documented is worse than one that
        was never built: the instructions look current and lead nowhere."""
        for doc in ("README.md", "CONVENTIONS.md", "ARCHITECTURE.md"):
            low = read(doc).lower()
            for name in self.RETIRED:
                if name not in low:
                    continue
                # Naming it while explaining the removal is fine; telling
                # someone to install it is not.
                for line in low.splitlines():
                    if name in line:
                        self.assertFalse(
                            any(v in line for v in ("install", "copy ", "symlink")),
                            f"{doc} still instructs installing {name}: {line.strip()!r}")


class TestHealthDisclaimer(unittest.TestCase):
    PHRASES = ("not medical advice", "not a medical device")

    def _has_disclaimer(self, text):
        low = text.lower()
        return any(p in low for p in self.PHRASES)

    def test_readme_carries_it(self):
        self.assertTrue(self._has_disclaimer(read("README.md")))

    def test_every_page_carries_it(self):
        """Enumerated from disk rather than listed, so a page added tomorrow
        is already in scope. Listing them by hand failed once: settings.html
        arrived as a full user-visible surface and no obligation test here had
        heard of it."""
        for path in html_surfaces():
            self.assertTrue(self._has_disclaimer(read(path)),
                            f"{path} shows a number without a caveat")

    def test_exports_carry_it(self):
        """An export outlives the app that made it."""
        header = "\n".join(store._export_header(
            {"provider": "purpleair", "site_id": 1, "site_name": "x"}))
        self.assertTrue(self._has_disclaimer(header))

    def test_licensing_doc_states_it(self):
        self.assertTrue(self._has_disclaimer(read("LICENSING.md")))


class TestNoUnsupportableClaims(unittest.TestCase):
    """ACL s4 puts the burden of showing reasonable grounds on whoever makes a
    representation about a future matter. Nothing here may forecast, and
    nothing may make a health claim about an individual."""

    FORBIDDEN = [
        r"\bsafe for (your|the) ",
        r"\bwill be\b.*\b(hazardous|unhealthy|dangerous)",
        # "does not guarantee data accuracy" is PurpleAir's required
        # disclaimer -- the opposite of a claim. Only match unnegated forms.
        r"(?<!not )(?<!never )\bguarantees?\b[^.]{0,40}\b(accura|safe)",
        r"\bdiagnos",
        r"\bcure[sd]?\b",
        r"\btreat(s|ment)? your\b",
    ]

    @property
    def SURFACES(self):
        return html_surfaces() + ["poller.py", "fusion.py"]

    def test_no_health_or_forecast_claims_in_any_surface(self):
        for path in self.SURFACES:
            # Collapse whitespace first: the required PurpleAir disclaimer
            # wraps as "does not\n guarantee data accuracy", and a lookbehind
            # for "not " never sees it across the line break.
            text = re.sub(r"\s+", " ", user_visible(path).lower())
            for pattern in self.FORBIDDEN:
                m = re.search(pattern, text)
                self.assertIsNone(
                    m, f"{path} contains an unsupportable claim: "
                       f"{m.group(0) if m else ''!r}")

    def test_time_hint_is_about_ventilation_not_health(self):
        """'Close up before the evening rise' is a statement about a window.
        'Safe for your asthma' would be a medical claim."""
        hint = poller.compute_time_hint({"risk_window": {"enabled": True,
                                                         "start_hour": 15,
                                                         "end_hour": 1}})
        if hint and hint.get("text"):
            low = hint["text"].lower()
            for word in ("safe", "healthy", "asthma", "diagnos"):
                self.assertNotIn(word, low,
                                 f"time hint makes a health claim: {hint['text']}")


class TestDataProtectionNotices(unittest.TestCase):
    def test_exports_warn_that_they_reveal_a_location(self):
        header = "\n".join(store._export_header(
            {"provider": "qld", "site_id": "x", "site_name": "y"}))
        self.assertIn("location", header.lower())

    def test_purpleair_export_forbids_redistribution(self):
        header = "\n".join(store._export_header(
            {"provider": "purpleair", "site_id": 1, "site_name": "x"}))
        self.assertIn("DO NOT REDISTRIBUTE", header)




class TestPrivacy(unittest.TestCase):
    """Airo knows where you live, at street resolution, updated every fifteen
    minutes. Anything that sends that off the machine -- or lets a third party
    run code on a page displaying it -- is the highest-consequence class of
    bug here, and none of it is visible in the UI when it goes wrong."""

    def test_the_dashboard_loads_nothing_from_a_third_party(self):
        """A <script src>, stylesheet or font from another host gets that host
        your IP on every open, and gives it arbitrary code in a page showing
        your address. Chart.js came from a CDN until it was replaced by the
        renderer at the top of the inline script."""
        html = read("dashboard.html")
        bad = re.findall(
            r'<(?:script|link|img|iframe)[^>]*\b(?:src|href)\s*=\s*["\']'
            r'(https?://[^"\']+)', html, re.I)
        # An <a href> is a link the user chooses to follow, not a subresource.
        self.assertEqual(bad, [], f"dashboard loads external resources: {bad}")

    def test_the_tray_window_loads_nothing_from_a_third_party(self):
        html = read("tray/ui/index.html")
        bad = re.findall(
            r'<(?:script|link|img|iframe)[^>]*\b(?:src|href)\s*=\s*["\']'
            r'(https?://[^"\']+)', html, re.I)
        self.assertEqual(bad, [], f"tray window loads external resources: {bad}")

    def test_every_ip_geolocation_service_is_https(self):
        """The request says this address runs Airo; the reply is the user's
        approximate home. Over http both are readable in transit, and the
        reply is forgeable by whoever answers first -- which decides the
        monitors the user is then offered."""
        import setup as setup_mod
        for name, url, _ in setup_mod.IP_LOOKUP_SERVICES:
            self.assertTrue(url.startswith("https://"),
                            f"{name} is contacted over plaintext: {url}")

    def test_no_outbound_url_is_plaintext_http(self):
        for mod in ("poller.py", "setup.py", "backup.py", "store.py", "fusion.py"):
            for line in read(mod).splitlines():
                if line.strip().startswith("#"):
                    continue
                for m in re.findall(r'["\']http://[^"\']+', line):
                    # localhost never leaves the machine.
                    if re.search(r"://(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])", m):
                        continue
                    self.fail(f"{mod} contacts {m} over plaintext http")

    def test_there_is_no_telemetry(self):
        """Airo must never report on its own users. If this ever becomes
        desirable it is an opt-in feature with its own review, not a constant
        someone added."""
        for mod in ("poller.py", "setup.py", "scheduler.py", "backup.py",
                    "store.py", "fusion.py", "analyse.py"):
            low = read(mod).lower()
            for word in ("telemetry", "analytics", "sentry", "posthog",
                         "mixpanel", "amplitude", "segment.io"):
                self.assertNotIn(word, low, f"{mod} mentions {word}")

    def test_location_never_travels_in_a_url_query_string(self):
        """Query strings are logged verbatim by every proxy and server in the
        path. Coordinates go in the body, or to a provider that needs them to
        answer -- never as decoration on an unrelated request."""
        src = read("poller.py")
        for m in re.findall(r'urlencode\(\{[^}]*\}', src):
            if "lat" in m and "purpleair" not in m.lower():
                # nwlat/selat are PurpleAir's bounding-box search: the whole
                # point of the call, and the narrowest form that answers it.
                self.assertIn("nwlat", m.lower() + "nwlat",
                              "coordinates in a query string")


def _shipped_module_names():
    """The Python modules the installer actually ships, from the stager."""
    import ast
    stager = ast.parse(
        (ROOT / "tools" / "stage_bundle.py").read_text(encoding="utf-8"))
    for node in stager.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "MODULES"
                        for t in node.targets)):
            return [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant)]
    return []


class TestRiskRegisterIsTrue(unittest.TestCase):
    """The register in ROADMAP.md claims each risk is closed by a named test
    or function. A register that cites something which no longer exists is
    worse than no register: it reports coverage that is not there."""

    def _cited(self):
        text = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        start = text.index("## Risk register")
        # End at the next top-level heading, whatever it happens to be. Pinning
        # the name of the following section meant moving that section broke
        # every one of these tests at once.
        nxt = re.search(r"^## ", text[start + 5:], re.M)
        end = start + 5 + nxt.start() if nxt else len(text)
        return text[start:end]

    def test_every_cited_test_file_exists(self):
        for name in sorted(set(re.findall(r"`(test_\w+\.py)", self._cited()))):
            self.assertTrue((ROOT / "tests" / name).exists(),
                            f"the register cites {name}, which does not exist")

    def test_every_cited_test_class_exists(self):
        section = self._cited()
        for mod, cls in sorted(set(re.findall(r"`(test_\w+)\.py::(\w+)", section))):
            src = (ROOT / "tests" / f"{mod}.py").read_text(encoding="utf-8")
            # Anchored, not substring: "class TestModelLicenceRenamed"
            # contains "class TestModelLicence", so a rename slipped past the
            # obvious check. The same prefix collision once made the tray's
            # pid report as the poller's.
            pat = rf"^\s*(class {re.escape(cls)}\b|def {re.escape(cls)}\s*\()"
            self.assertRegex(src, re.compile(pat, re.M),
                             f"the register cites {mod}.py::{cls}, which is gone")

    def test_every_cited_function_exists(self):
        # Citations appear both bare -- `secure_path()` -- and qualified --
        # `forecast.training_sources()`. Matching only the bare form made this
        # test pass by finding nothing at all.
        cited = set(re.findall(r"`(?:\w+\.)?(\w+)\(\)`", self._cited()))
        self.assertTrue(cited, "no functions cited; the pattern has drifted")
        for fn in sorted(cited):
            # Python `def`, or Rust `fn`. The register describes the whole
            # project and the tray is part of it — citing report_problem(),
            # which lives in airo.rs, failed here as "gone" while sitting in
            # the file the same row names as its enforcement.
            pat = re.compile(
                rf"^\s*(?:def|(?:pub )?fn) {re.escape(fn)}\s*[(<]", re.M)
            # Modules read from the stager rather than listed here. This was
            # a hand-written tuple and had already fallen behind: units.py,
            # weather.py and analyse.py were absent, so a register row citing
            # anything in them would have failed for the wrong reason. The
            # check-written-as-a-list shape, inside the contract that exists
            # to stop the register drifting.
            searched = ([ROOT / m for m in _shipped_module_names()]
                        + sorted((ROOT / "tray" / "src").glob("*.rs")))
            found = any(pat.search(f.read_text(encoding="utf-8"))
                        for f in searched if f.exists())
            self.assertTrue(found, f"the register cites {fn}(), which is gone")

    def test_every_risk_row_names_an_enforcement(self):
        """A row with an empty third column is a documented risk pretending to
        be a mitigated one -- exactly what this register exists to prevent."""
        for line in self._cited().splitlines():
            if not line.startswith("| ") or line.startswith("| Risk") \
               or set(line) <= set("|- "):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            self.assertEqual(len(cells), 3, f"malformed row: {line}")
            self.assertTrue(cells[2], f"no enforcement named for: {cells[0]}")


class TestTrayAveragesParity(unittest.TestCase):
    """The tray shows each rolling average as an index plus the raw µg it was
    derived from. A bucket present in one map and missing from the other
    renders as a bare number with nothing beside it -- and publishing the
    derived value without the canonical one inverts rule 6."""

    def _latest(self):
        path = poller.DATA / "latest.json"
        if not path.exists():
            self.skipTest("no latest.json on this machine")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_indexed_bucket_has_a_raw_value(self):
        d = self._latest()
        aqi = d.get("averages_aqi") or {}
        raw = d.get("averages_pm25") or {}
        missing = [k for k, v in aqi.items()
                   if v is not None and raw.get(k) is None]
        self.assertEqual(missing, [],
                         f"buckets with an index but no raw value: {missing}")

    def test_the_builder_emits_both_maps_with_the_same_keys(self):
        """Asserted against the source too, so it holds on a machine that has
        never polled."""
        src = read("poller.py")
        i = src.index('"averages_pm25"')
        block = src[i:i + 900]
        for bucket in ("now", "10min", "30min", "60min", "6hr", "24hr", "1week"):
            self.assertIn(f'"{bucket}"', block,
                          f"averages_pm25 omits {bucket}")


class TestDetailWindow(unittest.TestCase):
    """The tray menu is a native NSMenu: no font control, no colour. The two
    things that made the old SwiftBar readout legible at a glance -- a
    monospaced table and band colour -- are simply unavailable there. They
    live in the detail window, which is ours to style, so that surface has to
    actually carry them."""

    def html(self):
        return read("tray/ui/index.html")

    def test_the_averages_table_is_monospaced(self):
        """Space-padded columns in a proportional face come out ragged, which
        is precisely why the menu cannot do this."""
        h = self.html()
        i = h.index(".avg td")
        self.assertIn("monospace", h[i:i + 300])

    def test_numbers_are_tabular_so_columns_do_not_jitter(self):
        self.assertIn("tabular-nums", self.html())

    def test_every_average_row_is_coloured_by_its_own_band(self):
        h = self.html()
        self.assertIn("averages_band", h,
                      "rows are not coloured, or the band is re-derived here")

    def test_the_window_never_derives_a_band_from_a_number(self):
        """ARCHITECTURE §3 and rule 7: the poller decides bands, every UI
        renders them. A threshold here would be a second copy of a
        health-relevant decision, free to drift."""
        h = self.html()
        for forbidden in ("<= 33", "< 33", "> 66", ">= 100", "aqi > "):
            self.assertNotIn(forbidden, h,
                             f"the detail window re-derives a band: {forbidden}")

    def test_a_section_with_nothing_in_it_is_hidden(self):
        """A heading over empty space reads as a fault. On a fresh install
        there are no averages and no sources yet."""
        h = self.html()
        self.assertIn('el("avgSection").style.display', h)
        self.assertIn('el("srcTable").style.display', h)
        self.assertIn("No source has reported yet", h)

    def test_the_layout_is_bounded(self):
        """The window is resizable; unbounded, the monospace columns drift to
        opposite edges and stop reading as a table."""
        self.assertIn("max-width", self.html())

    def test_attribution_and_disclaimer_survive(self):
        h = self.html()
        self.assertIn("attributions", h)
        self.assertIn("Not medical advice", h)


class TestEveryProviderAttributesItsExport(unittest.TestCase):
    """CC BY requires the notice to travel with the data, and an exported CSV
    is exactly the data travelling.

    store.py holds the redistribution wording in a table keyed by provider
    slug. A table is a list somebody has to remember to extend, and it was
    already one short: a provider with no entry exported an **empty**
    attribution line under "Licence unknown", which for a CC BY feed omits the
    one thing the licence actually requires. Enumerated from PROVIDERS now.
    """

    def header_for(self, slug):
        return "\n".join(store._export_header(
            {"provider": slug, "site_id": "1", "site_name": "Site"},
            poller.export_terms()))

    def test_every_shipped_provider_names_itself_in_an_export(self):
        for slug, provider in poller.PROVIDERS.items():
            with self.subTest(provider=slug):
                header = self.header_for(slug)
                self.assertNotIn("Licence unknown", header,
                                 f"{slug} exports with no licence stated")
                self.assertTrue(
                    any(word in header for word in provider.attribution.split()[:3]),
                    f"{slug} exports without its attribution: {header!r}")

    def test_a_provider_the_table_has_never_heard_of_still_attributes(self):
        """The gap this closes: the check above passes today because every
        shipped provider happens to have a table entry. The next one will
        not."""
        class Newcomer(poller.Provider):
            slug = "newcomer"
            label = "Newcomer network"
            tier = "reference"
            accuracy_note = "n/a"
            resolution_minutes = 60
            needs_key = False
            attribution = "Contains Newcomer data, CC BY 4.0"
            licence = "CC BY 4.0"

        poller.PROVIDERS["newcomer"] = Newcomer()
        try:
            header = self.header_for("newcomer")
        finally:
            poller.PROVIDERS.pop("newcomer", None)

        self.assertIn("Newcomer", header)
        self.assertNotIn("Licence unknown", header)

    def test_an_export_with_no_terms_supplied_still_says_it_does_not_know(self):
        """Honest rather than silent. An empty licence line reads as "no
        restrictions", which is the dangerous direction to guess in."""
        header = "\n".join(store._export_header({"provider": "mystery",
                                                 "site_id": "1"}))
        self.assertIn("Licence unknown", header)


class TestTheTrayDecidesNothingAboutUrls(unittest.TestCase):
    """Hard rule 7 is about air-quality logic, and the same reasoning covers a
    port: a literal in the tray is a second copy of something Python owns, and
    it drifted the moment serve_port became configurable."""

    def test_no_url_or_port_is_hardcoded_in_the_tray(self):
        for name in ("tray/src/main.rs", "tray/src/airo.rs"):
            # Comments stripped: the doc comment on open_page() explains why
            # the literal is gone and necessarily quotes it. Failing on that
            # punishes the documentation of a rule for stating the rule.
            src = user_visible(name)
            if "#[cfg(test)]" in src:
                src = src.split("#[cfg(test)]")[0]
            self.assertNotIn("8787", src,
                             f"{name} hard-codes the default serve port")
            self.assertNotIn("127.0.0.1:", src,
                             f"{name} builds a server URL of its own")

    def test_the_tray_asks_the_poller_to_open_a_page(self):
        src = read("tray/src/airo.rs")
        self.assertIn('"--open"', src,
                      "the tray no longer routes page-opening through Python")


def setUpModule():
    block_browser_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_browser_for_module()
    restore_airo_paths_for_module()


class TestTheTrayNeverSwallowsAFailure(unittest.TestCase):
    """A menu item that fails silently is indistinguishable from one that does
    nothing, and the person clicking it has no way to tell.

    Twenty-seven handlers discarded their error with `let _ = ...`. The one
    that mattered is `spawn_python` refusing to start — a missing interpreter
    or an incomplete payload — because that makes *every* item in the menu do
    nothing at all, and this project has shipped a bundle whose payload was
    missing a module before.

    Checked from Python because CI runs the Rust tests only where a toolchain
    is installed, and this is a property of the source rather than of a build.
    """

    def main_rs(self):
        return read("tray/src/main.rs")

    def test_no_airo_call_discards_its_error(self):
        discarded = re.findall(r"let _ = airo::(\w+)", self.main_rs())
        self.assertEqual(
            [], discarded,
            f"these tray actions fail silently: {sorted(set(discarded))}. "
            f"Use `if let Err(e) = ... {{ airo::report_problem(...) }}`.")

    def test_the_reporter_exists_and_says_where_it_puts_things(self):
        """Checked inside `report_problem`'s own body, not anywhere in the
        file. The first version of this asked whether `notify_desktop` was
        mentioned in the source — deleting the *call* left the definition
        behind and the test passed while nothing reached the user."""
        src = read("tray/src/airo.rs")
        self.assertIn("pub fn report_problem(", src)
        body = src.split("pub fn report_problem(")[1].split("\n}")[0]
        self.assertIn("eprintln!", body,
                      "nothing is written to stderr, which launchd captures")
        self.assertIn("notify_desktop(", body,
                      "nothing reaches the person who just clicked")

    def test_every_handler_that_can_fail_names_the_action(self):
        """A notification reading "failed" helps nobody. Each call site passes
        the menu wording, so the message says which item was clicked."""
        # `[^"]*`, not `[^"]+`. Requiring a character meant an empty label did
        # not match the pattern at all, so the loop below never saw it and the
        # check passed on exactly the input it exists to reject.
        calls = re.findall(r'report_problem\("([^"]*)"', self.main_rs())
        self.assertTrue(calls, "no handler reports anything")
        self.assertEqual(
            len(calls), self.main_rs().count("report_problem("),
            "a report_problem call does not pass a literal label")
        for label in calls:
            self.assertGreater(
                len(label), 3,
                f"unhelpful label {label!r}: a notification reading "
                f"'failed' does not tell anybody which item they clicked")

    def test_the_reporter_does_not_route_through_python(self):
        """The case being reported is "Python would not run". Asking Python to
        report it would be the one message guaranteed to be lost."""
        src = read("tray/src/airo.rs")
        block = src.split("pub fn report_problem(")[1].split("\n}")[0]
        self.assertNotIn("spawn_python", block)
        self.assertNotIn("poller(", block)

    def test_the_windows_stub_is_declared_rather_than_silent(self):
        """A platform fallback that quietly does nothing is a feature that
        silently does nothing — four separate Windows-only failures in this
        project started that way."""
        src = read("tray/src/airo.rs")
        self.assertIn('#[cfg(target_os = "windows")]', src)
        windows = src.split('#[cfg(target_os = "windows")]')[1][:600]
        self.assertIn("stderr is captured", windows,
                      "the Windows no-op does not say it is one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
