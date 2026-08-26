# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contracts that enumerate rather than list.

The risk register is only worth what its enforcement covers, and enforcement
written as a literal list stops covering the moment someone adds a provider, a
surface or a module. Every check here discovers its own subjects from the
codebase, so a new PROVIDERS entry or a new HTML file is in scope the moment
it exists rather than the moment someone remembers to add it.

Where a check genuinely cannot be automatic, it names the exemption and why,
so the gap is visible instead of implied.
"""

import ast
import hashlib
import inspect
import os
import json
import re
import subprocess
import sys
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fusion   # noqa: E402
import poller   # noqa: E402
import weather  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    GUARDED, ORIGINALS, real_airo_home,
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



def git_tracked(root=None):
    """Every path git tracks, repo-relative. The one way this file asks.

    `safe.directory` is passed for the call rather than assumed. This suite
    redirects HOME so that nothing writes into the developer's own install,
    and a redirected HOME means git cannot read a global config — so on a
    checkout owned by another user (a container copying the tree in as root,
    which is exactly how this project's Linux baseline runs) git refuses the
    repository, writes to stderr and prints nothing.

    Nothing was reading that stderr. An empty list is indistinguishable from a
    clean tree, so every check built on this reported success while looking at
    no files at all — a whole class of guard switched off by an environment,
    silently, in the one run that exists to catch what the developer's machine
    cannot. It raises now.
    """
    root = ROOT if root is None else root
    proc = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "ls-files"],
        capture_output=True, text=True, cwd=root)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git could not list the tracked tree, so nothing here is being "
            f"checked: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def python_modules():
    """Every shipped Python module. Tests and tooling excluded."""
    return sorted(p for p in ROOT.glob("*.py") if p.name != "setup_test.py")


def user_surfaces():
    """Every file that renders something to a user.

    Discovered, not listed: dashboard.html and the tray window were both
    checked by name, so a third UI would have been silently exempt from the
    privacy and attribution rules.
    """
    out = [ROOT / "dashboard.html"]
    out += sorted((ROOT / "tray" / "ui").glob("*.html"))
    out += sorted((ROOT / "tray" / "src").glob("*.rs"))
    return [p for p in out if p.exists()]


def strip_rust_tests(text):
    return text.split("#[cfg(test)]")[0] if "#[cfg(test)]" in text else text


class TestEverySurfaceIsPrivate(unittest.TestCase):
    """Airo renders a home address. Anything a surface loads from a third
    party gets that third party the user's IP, and arbitrary code in a page
    displaying their coordinates."""

    def test_no_surface_loads_a_third_party_subresource(self):
        pattern = re.compile(
            r'<(?:script|link|img|iframe|source|video|audio)[^>]*'
            r'\b(?:src|href)\s*=\s*["\'](https?://[^"\']+)', re.I)
        for path in user_surfaces():
            if path.suffix != ".html":
                continue
            found = pattern.findall(path.read_text(encoding="utf-8"))
            self.assertEqual(found, [],
                             f"{path.relative_to(ROOT)} loads {found}")

    def test_no_surface_hard_codes_any_provider_attribution(self):
        """Enumerated from PROVIDERS, so a network added tomorrow is covered.
        Markup is stripped first: the footer read
        'Powered by <a ...>PurpleAir</a>', which no substring search found."""
        literals = [p.attribution for p in poller.PROVIDERS.values()]
        allowed = {"poller.py", "store.py"}      # keyed by provider, cannot misattribute
        for path in user_surfaces():
            if path.name in allowed:
                continue
            text = strip_rust_tests(path.read_text(encoding="utf-8"))
            flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text))
            for lit in literals:
                self.assertNotIn(lit, text, f"{path.relative_to(ROOT)} hard-codes {lit!r}")
                self.assertNotIn(lit, flat, f"{path.relative_to(ROOT)} hard-codes {lit!r}")


class TestEveryModuleIsQuiet(unittest.TestCase):
    """Discovered from disk, so a new module cannot opt itself out."""

    LOOPBACK = re.compile(r"://(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])")

    def test_nothing_talks_plaintext_http(self):
        for path in python_modules():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                for m in re.findall(r'["\']http://[^"\']+', line):
                    if self.LOOPBACK.search(m):
                        continue
                    self.fail(f"{path.name}:{n} contacts {m} over plaintext")

    def test_no_module_names_a_telemetry_vendor(self):
        vendors = ("telemetry", "analytics", "sentry", "posthog", "mixpanel",
                   "amplitude", "segment.io", "datadog", "bugsnag")
        for path in python_modules():
            low = path.read_text(encoding="utf-8").lower()
            for v in vendors:
                self.assertNotIn(v, low, f"{path.name} mentions {v}")


class TestEveryProviderMeetsTheContract(unittest.TestCase):
    """Enumerated from PROVIDERS. Adding a network is meant to be one
    subclass; these are the obligations that come with it."""

    def providers(self):
        return sorted(poller.PROVIDERS.items())

    def test_each_declares_the_interface(self):
        for slug, p in self.providers():
            for attr in ("slug", "tier", "attribution", "licence", "needs_key",
                         "resolution_minutes", "coverage_box", "covers",
                         "current", "history", "discover"):
                self.assertTrue(hasattr(p, attr), f"{slug} has no {attr}")

    def test_each_carries_a_non_empty_attribution(self):
        for slug, p in self.providers():
            self.assertTrue(str(p.attribution).strip(),
                            f"{slug} would appear in a UI crediting nobody")

    def test_each_states_a_licence(self):
        for slug, p in self.providers():
            self.assertTrue(str(p.licence).strip(),
                            f"{slug} exports would carry no terms")

    def test_a_keyed_provider_says_where_to_get_one(self):
        for slug, p in self.providers():
            if p.needs_key:
                self.assertTrue(getattr(p, "key_url", ""),
                                f"{slug} needs a key and offers no way to get one")
                self.assertTrue(getattr(p, "key_env", ""),
                                f"{slug} has no environment variable")

    def test_each_declares_a_tier_the_ui_understands(self):
        for slug, p in self.providers():
            self.assertIn(p.tier, ("reference", "indicative", "consumer"), slug)

    def test_each_has_a_plausible_cadence(self):
        for slug, p in self.providers():
            self.assertGreater(p.resolution_minutes, 0, slug)
            self.assertLessEqual(p.resolution_minutes, 24 * 60, slug)

    def test_gap_thresholds_scale_with_each_provider(self):
        """A fixed threshold is right for a 10-minute feed and fires on every
        poll against an hourly one."""
        for slug, p in self.providers():
            threshold = poller.gap_threshold_for(p, {})
            minutes = threshold.total_seconds() / 60
            self.assertGreater(minutes, p.resolution_minutes,
                               f"{slug} would report a gap between normal polls")

    def test_no_provider_can_bypass_the_sentinel_guard(self):
        """clean_measures() is applied once at the boundary, so a provider
        added later inherits it without knowing it exists."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index("def poll_source")
        head = src[i:i + 700]
        self.assertIn("clean_measures(", head,
                      "the boundary guard is gone; every provider is now "
                      "individually responsible for rejecting sentinels")


class TestDestructiveCommandsArePreviewable(unittest.TestCase):
    """Anything that removes data must be inspectable before it runs."""

    DESTRUCTIVE = ("--prune", "--repair")

    def test_each_destructive_flag_accepts_dry_run(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = src.index('"--dry-run"')
        line_start = src.rfind("\n", 0, i)
        self.assertIn("ap.add_argument", src[line_start:i],
                      "--dry-run is inside the mutually exclusive group, so it "
                      "cannot combine with the modes it modifies")
        for flag in self.DESTRUCTIVE:
            self.assertIn(f'"{flag}"', src, f"{flag} no longer exists")

    def test_every_delete_in_the_store_is_reachable_only_deliberately(self):
        """Enumerated from the source: a new DELETE has to justify itself."""
        src = (ROOT / "store.py").read_text(encoding="utf-8")
        deletes = re.findall(r"DELETE FROM (\w+)", src)
        self.assertEqual(sorted(set(deletes)), ["readings", "sources"],
                         f"an unreviewed DELETE appeared: {set(deletes)}")

    def test_pruning_is_a_no_op_without_a_finite_window(self):
        import store
        src = (ROOT / "store.py").read_text(encoding="utf-8")
        i = src.index("def prune(")
        self.assertIn("keep_days <= 0", src[i:i + 700],
                      "prune no longer refuses an unset retention")


class TestTheRegisterCoversWhatTheCodeDoes(unittest.TestCase):
    """The register is a claim about coverage. These keep the claim honest."""

    def section(self):
        text = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        start = text.index("## Risk register")
        # End at the next top-level heading, whatever it happens to be. Pinning
        # the name of the following section meant moving that section broke
        # every one of these tests at once.
        nxt = re.search(r"^## ", text[start + 5:], re.M)
        end = start + 5 + nxt.start() if nxt else len(text)
        return text[start:end]

    def test_all_seven_categories_are_present(self):
        s = self.section().lower()
        for cat in ("legal", "privacy", "data loss", "provider dependency",
                    "key handling", "sustainability"):
            self.assertIn(f"### {cat}", s, f"no {cat} section")
        self.assertIn("forecast", s, "forecast liability is not covered")

    def test_every_row_names_an_enforcement(self):
        for line in self.section().splitlines():
            if not line.startswith("| ") or line.startswith("| Risk") \
               or set(line) <= set("|- "):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            self.assertEqual(len(cells), 3, f"malformed row: {line[:60]}")
            self.assertTrue(cells[2], f"no enforcement for: {cells[0][:60]}")

    def test_every_test_file_that_exists_is_cited_or_deliberate(self):
        """A test file nobody cites is either dead or an uncovered risk."""
        cited = set(re.findall(r"(test_\w+\.py)", self.section()))
        actual = {p.name for p in (ROOT / "tests").glob("test_*.py")}
        uncited = actual - cited
        # These are cited by class rather than by file, or cover mechanics the
        # register does not claim (date maths, scales, the fresh-install flow).
        allowed = {"test_dates.py", "test_scheduler.py", "test_contracts.py",
                   "test_sustainability.py", "test_forecast.py",
                   "test_fresh_install.py", "test_backup.py", "test_fusion.py",
                   # Cover mechanics rather than a register risk: platform
                   # permission plumbing, and two third-party parsers whose
                   # rows are cited by behaviour elsewhere.
                   "test_key_permissions.py", "test_geocode_and_backfill.py"}
        self.assertEqual(uncited - allowed, set(),
                         f"test files no register row points at: {uncited - allowed}")


class TestEveryFlagTheParserOffersIsRead(unittest.TestCase):
    """A flag nobody reads is a promise nobody keeps.

    `--daemon` was declared with help text and never read back from `args`. It
    happened to do the right thing, because every other mode returns before the
    polling loop and so the loop is the fall-through — but only by accident. A
    new mode added above it would have claimed the fall-through and `--daemon`
    would have silently done something else, with its help text still promising
    the old behaviour.

    Enumerated from the parser's own `add_argument` calls, so a flag added
    tomorrow is covered without anybody remembering to add it here.
    """

    #: Flags that name the behaviour you get anyway. Reading these would mean
    #: branching on a condition that is already true, which is a no-op dressed
    #: up as logic. Listed, so "not read" stays a decision rather than a hole.
    SYNONYMS_FOR_THE_DEFAULT = {"daemon"}

    def declared(self, module):
        """Every flag the module's parser offers, from its syntax tree."""
        tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
        out = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value.startswith("--")):
                    out[arg.value[2:].replace("-", "_")] = arg.value
        return out

    def test_poller_reads_back_every_flag_it_offers(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        unread = []
        for dest, flag in self.declared("poller.py").items():
            if dest in self.SYNONYMS_FOR_THE_DEFAULT:
                continue
            if re.search(rf"\bargs\.{dest}\b", src):
                continue
            if re.search(rf"getattr\(\s*args\s*,\s*[\"']{dest}[\"']", src):
                continue
            unread.append(flag)

        self.assertEqual(
            [], sorted(unread),
            "these flags are offered with help text and never read, so they "
            "promise behaviour nothing implements. Either read the value, or "
            "add it to SYNONYMS_FOR_THE_DEFAULT with the reason.")

    def test_the_synonym_list_does_not_name_a_flag_that_is_gone(self):
        """A allowance for a flag nobody offers any more is an allowance that
        would hide the next one to go quiet."""
        declared = set(self.declared("poller.py"))
        stale = self.SYNONYMS_FOR_THE_DEFAULT - declared

        self.assertEqual(set(), stale,
                         "the allowance names flags the parser no longer has")

    def test_the_check_can_fail(self):
        """The filter shown a known-bad sample. Once every real flag is read
        or listed, "found nothing" and "the filter is broken" look identical —
        which is exactly how an inverted filter went unnoticed in this file
        before."""
        flags = self.declared("poller.py")

        self.assertIn("daemon", flags,
                      "the parser walk found no --daemon, so it is not "
                      "reading what it thinks it is")




class TestInternalPlanningStaysInternal(unittest.TestCase):
    """The legal analysis is a candid reading of a third party's terms,
    including where our position may be weak — useful written down, an
    admission against interest published. The commercial plan has no reader
    who benefits from it being open.

    The obligations arising from both ARE public and enforced; only the
    reasoning is withheld."""

    INTERNAL = ("INTERNAL.md", "COMMERCIAL.md", "PLANNING.md")

    def test_no_internal_document_is_tracked_by_git(self):
        tracked = git_tracked()
        # An empty listing passes every assertion below while checking
        # nothing, and git returns one whenever it declines to answer.
        self.assertTrue(tracked, "git listed no tracked files at all")
        for name in self.INTERNAL:
            self.assertNotIn(name, tracked, f"{name} is committed")
        for path in tracked:
            self.assertFalse(path.endswith(".internal.md"), f"{path} is committed")

    def test_the_gitignore_matches_by_shape(self):
        """A rule naming one file is the failure that leaked 16,995 rows."""
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for name in self.INTERNAL:
            self.assertIn(name, ignored)
        self.assertIn("*.internal.md", ignored)

    def test_the_public_roadmap_keeps_the_risk_register(self):
        """Published deliberately: every row is mitigated and enforced, so it
        reads as rigour rather than as a list of ways in."""
        s = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertIn("## Risk register", s)

    def test_the_public_roadmap_drops_the_commercial_plan(self):
        s = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertNotIn("## The hosted service", s)
        self.assertNotIn("## Legal — PurpleAir terms", s)

    def test_the_public_roadmap_says_what_is_withheld_and_why(self):
        """Silently removing sections would read as a gap; saying so does not."""
        s = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertIn("Planning that is not published", s)
        self.assertIn("LICENSING.md", s)

    def test_the_obligations_themselves_remain_public(self):
        lic = (ROOT / "LICENSING.md").read_text(encoding="utf-8").lower()
        self.assertIn("purpleair", lic)
        self.assertTrue(
            any(w in lic for w in ("cc by", "attribution")),
            "the public licensing document lost the terms users must follow")


class TestTheRoadmapCitesWhereWorkLanded(unittest.TestCase):
    """A finished item may not point at `Unreleased`, because `Unreleased`
    moves out from under it.

    Five rows of the finished table said "CHANGELOG Unreleased" — true on the
    day each was written, and false the moment 0.6.0 was cut, because
    promotion renames the heading and the citations do not follow. The rows
    then named a section holding somebody else's work, and the reader with the
    best reason to check where a feature shipped got the wrong answer.

    Promotion is the one moment this drift enters, and RELEASING §1.2 is a
    list of steps a person performs. This project's own position is that a
    documented rule is an unenforced one: prose is not executable and nobody
    re-reads it. So the rule is here instead, where cutting a release runs it.
    """

    HEADING = "## Where the finished items went"

    def finished_table(self):
        text = read("ROADMAP.md")
        self.assertIn(
            self.HEADING, text,
            "the finished table has been renamed or removed, so this check is "
            "reading nothing")
        after = text.split(self.HEADING, 1)[1]
        table = after.split("\n## ", 1)[0]
        self.assertGreater(
            table.count("|"), 20,
            "the finished table looks empty, which is how a guard goes quiet "
            "without failing")
        return table

    @staticmethod
    def rows_citing_a_moving_section(table):
        """The predicate, in one place, so the can-fail test below exercises
        the same code the real check runs rather than a copy of it."""
        return [line.strip() for line in table.splitlines()
                if "CHANGELOG Unreleased" in line]

    def test_no_finished_item_cites_a_section_that_moves(self):
        offenders = self.rows_citing_a_moving_section(self.finished_table())
        self.assertEqual(
            [], offenders,
            "these rows say a finished item shipped in CHANGELOG's Unreleased "
            "section. Once that section is promoted the citation names "
            "whatever landed next — cite the version it actually shipped "
            "in:\n  " + "\n  ".join(offenders))

    def test_every_version_cited_is_a_section_that_exists(self):
        """The other half. A row may not name a release the CHANGELOG has
        never had — a typo in a version number reads exactly like a fact."""
        changelog = read("CHANGELOG.md")
        headings = set(re.findall(r"^## \[?([0-9]+\.[0-9]+\.[0-9]+)\]?",
                                  changelog, re.M))
        self.assertTrue(headings, "no released section found in CHANGELOG.md")
        cited = set(re.findall(r"CHANGELOG ([0-9]+\.[0-9]+\.[0-9]+)",
                               self.finished_table()))
        self.assertEqual(
            set(), cited - headings,
            f"the finished table cites CHANGELOG sections that do not exist: "
            f"{sorted(cited - headings)}")

    def test_the_scan_can_fail(self):
        """Guards the walk, the way every other enumeration here does. A
        contract over an empty set passes and means nothing."""
        table = ("| #1 | A thing | CHANGELOG Unreleased |\n"
                 "| #2 | Another thing | CHANGELOG 0.6.0 |\n")
        self.assertEqual(["| #1 | A thing | CHANGELOG Unreleased |"],
                         self.rows_citing_a_moving_section(table))


class TestLatestJsonMatchesWhatTheTrayExpects(unittest.TestCase):
    """latest.json is a contract with a program in another language.

    The tray types some fields as sequences. serde's `default` covers a field
    that is *absent*; it does nothing for one that is present and null, and
    serde then fails the whole document. So a single unexpected null anywhere
    makes the tray report "no reading yet" beside a full database.

    That happened. `quiet_hours` was null for every install that had never set
    quiet hours — the default — and the tray went blind for all of them. The
    same shape took it out once before, when a band ceiling of infinity made
    the file unparseable at all.

    The tray now tolerates nulls too. Both halves are kept because either
    alone is one edit from the same outcome, and this is the half that runs on
    every commit.

    The field list is read out of the Rust rather than written here, so a new
    Vec in the tray is covered the moment it exists.
    """

    def sequence_fields(self):
        rust = (ROOT / "tray" / "src" / "airo.rs").read_text(encoding="utf-8")
        rust = strip_rust_tests(rust)
        found = set(re.findall(r"pub\s+(\w+)\s*:\s*Vec<", rust))
        self.assertTrue(found, "no sequence fields found — has the tray moved?")
        return found

    def latest(self):
        """A latest.json built the way the poller builds one."""
        import sqlite3
        import tempfile
        import store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "airo.db")
        self.addCleanup(conn.close)
        sid = store.upsert_source(conn, "qld", "demo", "Demo site",
                                  latitude=-33.5, longitude=151.0)
        store.insert_readings(conn, sid, [
            {"observed_utc": "2026-01-01T00:00:00+00:00", "pm25": 7.0}])
        conn.commit()

        # Deliberately the barest config: no quiet hours, no thresholds. That
        # is the shape that produced the nulls, and the shape most installs
        # actually have.
        cfg = {"location": {"name": "Demo", "latitude": -33.5, "longitude": 151.0},
               "sources": [{"provider": "qld", "site_id": "demo"}],
               "alerts": {"enabled": True}}
        return poller.build_latest(conn, cfg)

    def test_no_sequence_field_is_ever_null(self):
        latest = self.latest()
        blocks = [("", latest)] + [(f"{k}.", v) for k, v in latest.items()
                                   if isinstance(v, dict)]
        for name in sorted(self.sequence_fields()):
            for prefix, block in blocks:
                if name not in block:
                    continue
                self.assertIsNotNone(
                    block[name],
                    f"latest.json sets {prefix}{name} to null, and the tray "
                    f"types it as a sequence — serde fails the whole document "
                    f"and the tray shows 'no reading yet'")

    def test_the_file_is_valid_json_for_other_languages(self):
        """json.dumps writes Infinity and NaN as bare literals by default and
        reads them back, so nothing looks wrong from inside Python. Every
        other parser rejects the whole file."""
        with self.assertRaises(ValueError):
            json.dumps({"x": float("inf")}, allow_nan=False)
        json.dumps(self.latest(), allow_nan=False)      # must not raise


class TestNoTestIsSilentlyShadowed(unittest.TestCase):
    """Two methods of the same name in one class: Python keeps the last.

    This has now happened twice, from the same cause both times — rewriting a
    block by slicing the file and reinserting a new body without removing the
    old one. It is invisible: duplicate methods are valid Python, the file
    parses, the collector reports no error, and the suite count barely moves.

    The first time cost three CI rounds. Every fix landed in the earlier copy
    and never ran, so failures were being read from code that could not have
    changed, and two confident diagnoses were made from that evidence.

    The shadowed body may also be the *better* one — here the later copy was
    identical, but a rewritten test being shadowed by its predecessor means the
    improvement silently does nothing.

    Cheap to check, so it is checked, rather than relying on noticing.
    """

    def test_no_class_defines_the_same_method_twice(self):
        import collections
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                names = [n.name for n in node.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                for name, count in collections.Counter(names).items():
                    self.assertEqual(
                        1, count,
                        f"{path.name}: {node.name}.{name} is defined {count} "
                        f"times. Python keeps the last, so the others never "
                        f"run — including any fix made to them.")

    def test_the_shipped_modules_are_checked_too(self):
        """Same trap, same silence, outside the tests."""
        import collections
        files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "tools").glob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for scope in [tree] + [n for n in ast.walk(tree)
                                   if isinstance(n, ast.ClassDef)]:
                names = [n.name for n in scope.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                for name, count in collections.Counter(names).items():
                    self.assertEqual(
                        1, count,
                        f"{path.name}: {name} is defined {count} times; only "
                        f"the last one exists at runtime")


class TestEveryUserAgentNamesTheSameVersion(unittest.TestCase):
    """Four User-Agents went out to third parties, carrying three versions.

    `airo-poller/1.0`, `airo/0.5` for Nominatim, `airo-setup/0.5`, and
    `airo/0.5` from weather.py — while `poller.VERSION` said `0.5.0` and its
    own comment declares itself canonical. An identifying version is the thing
    these APIs' rate-limit policies actually ask for (Nominatim's requires
    one), so a stale string is not cosmetic: it identifies the client as
    something that has not existed for two releases.

    Three of the four derive from VERSION now. weather.py cannot: it imports
    nothing of Airo's by design — "reads nothing of Airo's", per its own
    docstring — and poller imports *it*, so reaching the other way would be a
    cycle. Its copy is pinned here instead, which is the cheapest thing that
    fails loudly when VERSION moves.
    """

    def test_pollers_own_agents_carry_the_canonical_version(self):
        self.assertIn(poller.VERSION, poller.USER_AGENT)
        src = inspect.getsource(poller.geocode)
        self.assertNotIn('"airo/0.5', src,
                         "the Nominatim User-Agent is a literal again")

    def test_setups_agent_carries_the_canonical_version(self):
        import setup as setup_module
        src = inspect.getsource(setup_module)
        self.assertNotIn('"airo-setup/0.5"', src,
                         "setup's User-Agent is a hardcoded version again")
        self.assertIn("airo-setup/{poller.VERSION}", src)

    def test_weathers_pinned_copy_still_matches(self):
        self.assertIn(
            poller.VERSION, weather.USER_AGENT,
            f"weather.USER_AGENT is {weather.USER_AGENT!r} but poller.VERSION "
            f"is {poller.VERSION!r}. weather.py is a deliberate leaf and "
            f"cannot import poller — update the string by hand, in step.")

    def test_every_agent_identifies_the_project(self):
        for name, agent in (("poller", poller.USER_AGENT),
                            ("weather", weather.USER_AGENT)):
            self.assertTrue(agent.startswith("airo"),
                            f"{name}'s User-Agent does not identify Airo")


class TestStalenessIsFusionsDecisionOnly(unittest.TestCase):
    """`gap_threshold_for()` wrote fusion's tolerance out as `* 2 + 5`.

    Two copies of one staleness rule drift, and either direction is a bug that
    contradicts itself on screen: a source the poller reports a gap for while
    fusion still treats it as current, or the reverse.
    """

    def test_the_threshold_is_built_from_fusions_constants(self):
        src = inspect.getsource(poller.gap_threshold_for)
        self.assertIn("fusion.STALE_INTERVALS", src)
        self.assertIn("fusion.STALE_GRACE_MINUTES", src)

        # The docstring names the old expression, deliberately — it is the
        # history. Check the code, not the prose about the code.
        tree = ast.parse(textwrap.dedent(src))
        body = tree.body[0].body
        if (isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]
        code = "\n".join(ast.unparse(n) for n in body)
        self.assertNotIn("* 2 + 5", code,
                         "the tolerance is written out again in the code")

    def test_it_still_computes_what_it_used_to(self):
        """Behaviour-identical at today's values — this change must not have
        moved a threshold while tidying it."""
        class Hourly:
            resolution_minutes = 60

        class Fast:
            resolution_minutes = 10

        self.assertEqual(
            timedelta(minutes=60 * 2 + 5),
            poller.gap_threshold_for(Hourly(), {"poll_minutes": 15}))
        self.assertEqual(
            timedelta(minutes=15 * 2 + 5),
            poller.gap_threshold_for(Fast(), {"poll_minutes": 15}))

    def test_moving_fusions_constant_moves_the_threshold(self):
        """The point of referencing them: one edit, both places."""
        class Hourly:
            resolution_minutes = 60

        saved = fusion.STALE_INTERVALS
        fusion.STALE_INTERVALS = 3
        try:
            self.assertEqual(
                timedelta(minutes=60 * 3 + fusion.STALE_GRACE_MINUTES),
                poller.gap_threshold_for(Hourly(), {"poll_minutes": 15}))
        finally:
            fusion.STALE_INTERVALS = saved


class TestTheZeroDependencyRuleHolds(unittest.TestCase):
    """Hard rule 1, enforced in the suite and not only in CI.

    CI has always checked this, but only there — so a stray dependency was
    something you found out about after pushing, and only for the seven files
    that were named in a literal list. `forecast.py`, `weather.py` and both
    tools were never scanned at all.

    Both lists are read off disk here for the same reason CI now does: a
    literal list of "the shipped modules" stops being true the moment somebody
    adds one, and the failure is silent — the check keeps passing while
    covering less.

    This matters beyond tidiness. The installer ships a bare CPython with no
    package manager and no site-packages; one `import requests` and the
    installed app fails on first launch, on the user's machine, in a way that
    never reproduces here.
    """

    #: Development tools may use a development dependency, named here with the
    #: reason. They run on a maintainer's machine or in CI and never reach a
    #: user's, so they are outside what rule 1 protects.
    DEV_ONLY = {
        "coverage": "tools/check.py measures the coverage floor; CI installs it",
    }

    def shipped(self):
        """Every module at the repository root.

        Found on disk, not read from `stage_bundle.MODULES`. Scanning the
        bundler's list would tie this check to that list being correct — and
        when it was not, weather.py went unscanned by *this* test too, so a
        dependency added there would have passed. One list being wrong should
        not silently narrow an unrelated check.

        Whether a module ships is a different question, asked by
        test_runtime.py against the payload it actually builds.
        """
        out = sorted(ROOT.glob("*.py"))
        self.assertGreaterEqual(len(out), 8, "the module list looks wrong")
        return out

    def tooling(self):
        return sorted((ROOT / "tools").glob("*.py"))

    def test_nothing_imports_outside_the_standard_library(self):
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("sys.stdlib_module_names needs Python 3.10+")
        stdlib = set(sys.stdlib_module_names)
        files = self.shipped()
        # First-party means "a module of this project", which is any *.py at
        # the root — NOT "a module that ships". Conflating them made this test
        # fail on poller.py importing weather while weather.py was missing
        # from the payload: a real bug, but a different one, and it belongs to
        # the payload check in test_runtime.py which names it precisely.
        # A test that fires for two unrelated reasons teaches people to read
        # past it.
        local = {q.stem for q in ROOT.glob("*.py")}

        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif (isinstance(node, ast.ImportFrom)
                      and node.level == 0 and node.module):
                    names = [node.module.split(".")[0]]
                for name in names:
                    self.assertTrue(
                        name in stdlib or name in local,
                        f"{path.name}:{node.lineno} imports {name!r}, which is "
                        f"neither standard library nor part of this project. "
                        f"The installer ships a bare interpreter — this would "
                        f"fail on first launch on a user's machine.")

    def test_tooling_uses_only_named_development_dependencies(self):
        """Build and dev scripts may reach outside the standard library, but
        only for something written down here with a reason.

        The distinction is what rule 1 actually protects: the installed app
        runs on a bare CPython with no package manager, so a *shipped* module
        importing a package fails on a user's machine. A tool that runs in CI
        does not. Left unstated, that distinction becomes "anything in tools/
        is fine", which is how a dependency arrives without a decision.
        """
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("sys.stdlib_module_names needs Python 3.10+")
        stdlib = set(sys.stdlib_module_names)
        local = {p.stem for p in ROOT.glob("*.py")} | {p.stem for p in self.tooling()}

        for path in self.tooling():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif (isinstance(node, ast.ImportFrom)
                      and node.level == 0 and node.module):
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in stdlib or name in local:
                        continue
                    self.assertIn(
                        name, self.DEV_ONLY,
                        f"{path.name}:{node.lineno} imports {name!r}. If that "
                        f"is a deliberate development dependency, add it to "
                        f"DEV_ONLY with the reason; if it is not, it does not "
                        f"belong here.")

    def test_no_dependency_manifest_exists(self):
        """The other half of the rule. A manifest is how a dependency arrives
        without anyone deciding to add one."""
        for name in ("requirements.txt", "Pipfile", "pyproject.toml",
                     "setup.cfg", "poetry.lock", "environment.yml"):
            self.assertFalse((ROOT / name).exists(),
                             f"{name} exists — the Python side takes no "
                             f"runtime dependencies (rule 1)")




class TestEverySourceFileCarriesItsLicence(unittest.TestCase):
    """The licence has to travel with the file, not only with the repository.

    LICENSE quotes the FSF's own guidance -- "attach them to the start of each
    source file" -- and for a long while only the root LICENSE carried the
    notice. A module pasted into a gist, or lifted into somebody else's tree,
    then arrives stating nothing about what it is licensed under and offering
    no way to find out. That is the single case a per-file notice exists for,
    and it is exactly the case a root LICENSE cannot reach.

    Two SPDX lines, so a machine reads them as readily as a person does.

    The strings live here and nowhere else. `tray/Cargo.toml` and
    `tauri.conf.json` each already stated the same licence in their own
    wording, and a legal fact held in three independent copies is the shape
    this project keeps paying for -- so both are checked against the constants
    below rather than trusted to still agree with them.
    """

    HOLDER = "Donnish Pty Ltd"
    YEAR = "2026"
    SPDX = "AGPL-3.0-or-later"

    #: How far into a file the notice may sit. Deep enough for a shebang and
    #: the blank line after it, shallow enough that "at the start of the file"
    #: still means something -- a notice buried under a docstring is not what
    #: the FSF guidance asks for and is not what a person skimming a pasted
    #: snippet will see.
    HEAD_LINES = 6

    def copyright_line(self):
        return f"SPDX-FileCopyrightText: {self.YEAR} {self.HOLDER}"

    def licence_line(self):
        return f"SPDX-License-Identifier: {self.SPDX}"

    def sources(self):
        """Every tracked Python and Rust file, read off `git ls-files`.

        By extension over the tracked tree rather than from a curated list.
        A list of "the files that need a header" stops being true the moment
        somebody adds one, and it fails silently: the check keeps passing
        while covering less. `git_tracked()` is the one way this file asks
        what is in the tree, and it raises rather than returning an empty
        list when git cannot answer.
        """
        out = [ROOT / f for f in git_tracked()
               if f.endswith((".py", ".rs")) and (ROOT / f).is_file()]
        self.assertGreater(
            len(out), 40,
            "the source walk found almost nothing to check, which is how a "
            "guard goes quiet without failing")
        return out

    def test_every_tracked_source_file_carries_both_lines(self):
        missing = []
        for path in self.sources():
            head = "\n".join(
                path.read_text(encoding="utf-8").splitlines()[:self.HEAD_LINES])
            if (self.copyright_line() not in head
                    or self.licence_line() not in head):
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            [], missing,
            f"these source files carry no licence notice in their first "
            f"{self.HEAD_LINES} lines, so a copy of one states nothing about "
            f"its licence:\n  " + "\n  ".join(missing))

    def test_no_file_states_a_different_licence(self):
        """A stale header is worse than none: it is a confident wrong answer
        about what somebody may do with the file."""
        wrong = []
        for path in self.sources():
            for line in path.read_text(encoding="utf-8").splitlines():
                # Comment lines only. The scan reads its own source too, and
                # the expression that builds the expected line is not a
                # licence declaration -- reading it as one made this test
                # fail on the file that defines it.
                if not line.lstrip().startswith(("#", "//")):
                    continue
                if "SPDX-License-Identifier:" not in line:
                    continue
                if line.split("SPDX-License-Identifier:")[1].strip() != self.SPDX:
                    wrong.append(f"{path.relative_to(ROOT)}: {line.strip()}")
        self.assertEqual([], wrong,
                         f"the project is {self.SPDX}: {wrong}")

    def test_the_crate_manifest_names_the_same_licence(self):
        manifest = (ROOT / "tray" / "Cargo.toml").read_text(encoding="utf-8")
        self.assertIn(
            f'license = "{self.SPDX}"', manifest,
            "tray/Cargo.toml declares a licence the source headers do not")

    def test_the_bundle_copyright_names_the_same_licence_and_holder(self):
        conf = json.loads(
            (ROOT / "tray" / "tauri.conf.json").read_text(encoding="utf-8"))
        stated = conf["bundle"]["copyright"]
        for token in (self.YEAR, self.HOLDER, self.SPDX):
            self.assertIn(
                token, stated,
                f"tauri.conf.json's copyright is {stated!r}, which does not "
                f"name {token!r}. It is what a user reads in the installer.")

    def test_the_scan_can_fail(self):
        """Guards the walk. A contract over an empty set passes and means
        nothing, which is how three checks here stayed green after the thing
        they enumerated moved."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bare = Path(tmp) / "bare.py"
            bare.write_text("print('no notice here')\n", encoding="utf-8")
            head = "\n".join(
                bare.read_text(encoding="utf-8").splitlines()[:self.HEAD_LINES])
            self.assertNotIn(self.licence_line(), head)


class TestNoScriptIsBuiltOutOfAVariable(unittest.TestCase):
    """A script body assembled by interpolation is an injection waiting to be
    reached.

    The folder picker built an AppleScript with an f-string, so a `"` in the
    prompt closed the string literal and the rest became AppleScript —
    `do shell script "..."` in a request body ran as the user. The PowerShell
    branch had the same shape with `'`. It sat behind the /api/choose-folder
    token, which is one guard rather than none, but a secret was the only
    thing between a request field and a shell.

    The project already had the safe pattern and the newer code did not reach
    for it: argv is a list that no shell parses. The fix passes the prompt as
    data — argv on macOS, an environment variable on Windows — because
    escaping is a rule somebody has to remember and data cannot be forgotten.

    There was an escaper here too, `_as_str()`, and it is gone: nothing called
    it. Keeping a quoting helper nobody uses is worse than not having one,
    because a test asserting it still escapes reads as proof that the values
    reaching AppleScript are escaped — and they are not escaped, they are not
    interpolated at all, which is the stronger property.

    Matched by shape rather than by the one known site, per rule 3's lesson.
    An f-string is flagged only when its literal parts look like a script; an
    f-string containing a path or a message is not this problem.
    """

    #: Constructs that only appear in a script meant for another interpreter.
    SCRIPTY = (
        "do shell script", "display notification", "tell application",
        "choose folder", "choose file", "osascript",
        "New-Object", "Write-Output", "Add-Type", "ShowDialog",
        "Start-Process", "Invoke-Expression",
    )

    def sources(self):
        return sorted(p for p in ROOT.glob("*.py")) + \
               sorted((ROOT / "tools").glob("*.py"))

    def test_no_interpolated_string_looks_like_a_script(self):
        for path in self.sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                # Only the literal halves; the placeholders are the danger,
                # not the evidence.
                literal = "".join(v.value for v in node.values
                                  if isinstance(v, ast.Constant)
                                  and isinstance(v.value, str))
                placeholders = [v for v in node.values
                                if isinstance(v, ast.FormattedValue)]
                if not placeholders:
                    continue
                for marker in self.SCRIPTY:
                    self.assertNotIn(
                        marker, literal,
                        f"{path.name}:{node.lineno} builds a script by "
                        f"interpolation ({marker!r}). Pass the value as data "
                        f"— argv on macOS, an environment variable on Windows")

    def test_the_notification_script_is_a_constant_and_the_text_is_argv(self):
        """The real protection for a notification is not escaping — it is that
        the script body is a constant and the text travels as argv.

        `message` carries a `site_name` that arrived in a provider's JSON, so
        it is third-party text heading for a shell. This asserts the two halves
        that make that safe, because the previous test asserted neither: that
        the AppleScript reads `on run argv`, and that a title containing every
        character an escaper would have had to handle appears in the command as
        its own untouched argv item rather than anywhere inside the script.
        """
        self.assertIn("on run argv", poller._OSA_NOTIFY)

        hostile = 'a"b\\c" & do shell script "id'
        (argv, env, stdin), = poller.notification_commands(
            hostile, "sub", "msg", "Ping", os_name="posix", platform="darwin")

        self.assertEqual("osascript", argv[0])
        self.assertEqual(poller._OSA_NOTIFY, argv[2],
                         "the script body must be the constant, verbatim")
        # Present as its own argument, unquoted and unescaped...
        self.assertEqual([hostile, "sub", "msg", "Ping"], argv[3:])
        # ...and absent from the script, which is what makes escaping moot.
        self.assertNotIn(hostile, argv[2])
        self.assertEqual({}, env)
        self.assertIsNone(stdin)


# --- Shapes rule 2b is enforced by -------------------------------------------
#
# One helper per shape, at module level, because the check and the sample that
# proves the check can fail must run the same code. A self-test that restates
# the pattern goes on passing while the real one rots, which is the failure
# this file exists to prevent.

#: Extensions that hold no readable text. The locality scan reads *every*
#: other tracked file rather than an allowlist of extensions: a leak in a file
#: type nobody thought of -- LICENSE, Cargo.toml, a fixture .txt -- is exactly
#: the gap that an allowlist leaves open.
NOT_TEXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".pdf", ".zip",
            ".gz", ".woff", ".woff2", ".ttf", ".db", ".sqlite")


def tracked_files():
    """Every text file git tracks, repo-relative.

    The tree, never the history: a check that scanned commits would report
    leaks that a release-time history cut is going to remove anyway, and would
    fail permanently in the meantime.
    """
    return [f for f in git_tracked()
            if not f.lower().endswith(NOT_TEXT) and (ROOT / f).is_file()]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


#: SHA-256 of lowercased one- and two-word tokens the tracked tree must not
#: carry, padded with decoys until the list itself says nothing.
#:
#: Hashes rather than the words. A plaintext denylist of strings that must
#: never be committed *is* those strings, committed -- readable by anyone who
#: clones the repository and rather easier to find than the leak it guards
#: against. A digest cannot be read backwards and compares just as well,
#: because the scan hashes what it finds and looks the result up.
#:
#: That argument is sound and it was not sufficient. A digest cannot be read
#: backwards, but a short list of digests can be read sideways: the keyspace
#: here is place names, which is a published list a few thousand entries long,
#: so anyone can hash a gazetteer and see which entries match. The handful of
#: digests this file used to hold were recovered exactly that way, in a couple
#: of passes, by a review of this file -- at which point the guard was no
#: longer a denylist. It was the answer, and the check written to prevent a
#: disclosure had become one.
#:
#: So the list is padded until reversing it returns nothing that was not
#: already public. Every digest below is a locality name from a public
#: Australian gazetteer, and the set is filled by rule rather than by choice:
#: every Queensland and New South Wales locality within about fifty kilometres
#: of the two state capitals, less any token the tracked tree already uses
#: legitimately -- those would fail this file's own scan on the next commit --
#: and less names under six letters, where the false positives cost more than
#: the check buys. Nothing is judged name by name, because a judgement about
#: which entries look plausible leaves its own fingerprint on what survives it.
#:
#: What that buys: reverse the whole set and an Australian gazetteer comes
#: back. Which of these tokens this project has an actual reason to fear is
#: not recoverable from here, is not marked by the ordering (sorted), is not
#: marked by the count, and is written down nowhere in the tree.
#:
#: The corpus itself is not stored beside the digests, for the reason at the
#: top: a file listing a thousand place names is a file listing a thousand
#: place names, and the scan below reads this file too.
#:
#: The cost, stated here rather than discovered later. Most of these names are
#: innocent and a few are ordinary English words, so this check can fail on a
#: line that meant nothing by it. When it does, rewrite the line the failure
#: names. Do not delete the digest it reports: from here nobody can tell an
#: innocent entry from one that matters -- that is the entire point of the
#: padding -- so the entry deleted for being inconvenient is exactly the entry
#: that was doing the work.
_LOCALITY_DIGESTS = """
0075b60664f5aa5db86583a93b76c5ab5c161567ea64f29eb8e1f374b50560f9
00dc54c96ca7275472a2ed3b4e1222e0f19b9d4c5269a76fc4dada8b601d680a
0173bb85cf60d6fbc82ac01add7c4301a90aa7952244694f7b8bdae7ff57d993
0184cb2ffd943adfaa74fef09cf8e8b17ea3541123718c84a7a8cb40405a3554
01bd25d8541bd3fcffd6b6eb392992c84a90dd870702dcb17ba89cd830ca6df6
01e1aa870a888fd59099b73dfe1068ce14d68b974665dda0f4fc9a3f671d40dd
01f9ade8b258586b6e8fa4cd9b0f1ecd157120c0e1f370f4863f2db79d8748aa
024e5367642cbd0b4787f8f581e84f16a76bc3d95dbbe6aa7db90015511a0d9f
0254e91c53e4883bf8af88c12ec85783915ecda65f528292236b53de0d9a452b
02686323028c37bb4826211974a1280e0d3992a8ec0bcdfb76c8c8dc4a684a77
027a1a4e9c9174d4f923b3796b3d868e4f365ebf05f050495854c9e975dbd17c
027d40feff466efbaa036d0afb285d110b3525d8f3100f9f452bbf89b8a8113c
02a6d24e51dc866f5fa5dfce85922ef5e0da44f41bca2a6c486e4ccd0f893075
02d8e67368c5ce415c9fbb2b9488d9a36f64e832a0d0f62ccd7291acfdcdcbd2
031f8418f57a36b9c29c00c9fbdab3ed74ab49a4f3ce5a0e63f1832f3f85ef88
03462f6940ad34a43f10ade1eed49c0d7e5d839e003a4a20a1da2a35fd6c973f
0346a8ef8a3135d6a9d5ef8eeeac3c73fa95b062383351c1c8865f02ee2a74f8
03b02ada2e468cec545af89098891abd72fb7c84ac4ad037fe26bbf09095efba
03c1b4ab2748288d1b9f7571d9a1d6bbb4a1545de7bba1deb7f52be34b556a26
03f2b4e624fafe8e7a564b479277940a6438e84f177c93a45f1918b77fa78a08
040b0ab081e3bbb6d5a805af543fa460fe25589a04fbe5a093a4c073b5558c3a
0458a117c7395f5f1439ab0158a09e388a360e14e930046eb229e06729d8045e
04747ed2447ae9ea34ebc34e96906fcc6271b64f786cda2b021c7733ea4705c1
04965a94de10008943ff46c59b30e92310b02ba74e28573470561ba5d770d3ad
04b83922a79c7b8718d261f149f29c9e3dcab57446633a6e49747d7c64cfb576
04cc921bc6c2ec53b3bf747b2cb75c00338403c390365caf35ab48f745a4c569
050db4c910f032b2161588d66bfb985cc65a5cc6eb30056aa92624d41206d104
051f289111f81c299c6fc2a212dee9b2a958f31055e8ff89ecfb5d3ce3a64615
0538de39e2b5ba5943cbd37a44ede9d9c2e8c062178c687bc624a825ed40b814
0545008346a8b2ccca064d2e582c276c1deb56300e5a35259a004d0e425d3cf4
0546d76879bb7850231d1f30d6a7cf5bf5ea7947d17cacc78185f1c9a5cf09fb
05cedcd3863aa8f61a609ac51b6a0e8d23ca112089c949c7bb7e2035e79aa323
0606c1f7d6efdfab7a0740566718a2391182a056da130cd7bd6add4954c59aca
06374281cdd8e0e03b90474741bb537d5cee075eb60e0df4df47d4bafb1e89ae
069d983841671123eb7c27f43c46f3c3e4c5df893ab5b858a79c69d635f05785
06d307c7ae1d4cc543023bd1540b7530a09306492f6c54e9865b4381e26be44b
071074f79e20d718ba7881725ec11908f0fa2d748486520c816d35b607e4642f
07148f7fabe2512e8bad6f9a1044d764ab4f532a6037275bc13e33d3d52fd654
078f79640b1d1ac03bd0538925d6480a07ab2c74552190e9f80975fb8fe99fbe
07bdf52d40f63fde1f997f254fd0cf35ac778d6704a3b6c3df1725c449d625b0
07c77855d94344e7ce4911589107e6c3770963e4b15b61ce946af5dd03102f9c
086212f15daf8244bd1ff576e21978e8e51e8421dc3d8409cb0d96f4afe355da
08cfd3662142719be62045f32f702b2dd164748b61249aa5286ed49f7b016347
091d29ac0b09248a6e71774911c4842c6f127355f06e596c9e533670ea37bd7b
0962329b2e78cdb7aaf574c8ecd0b75614663903c75d911ed4ef7ef38d6fa385
09f5b2a1a5497c412a9393b90b24d67bd1bb1525bba3bd89ff84aaa1741645d1
0a173aaa3c2c69b33f25c1ba3a43e968084b0ebd8ab40ca761315c0545a658f4
0a378203703cac543e85190c7da87fa5f0392896a884cd7bc899bdc79ac6c556
0a4992ea442b53e3dca861deac09a8d4987004a8483079b12861080ea4aa1b52
0a64dda62dd7ded3ddfec7629b6b8f97aa7627fd98e4e3d67404e2a281a2a0fa
0a6766b4298614f712f9b26c869c7ed8eda6f036b2ff66a794a608fad5cfa48d
0aa029c5364f8e75e51e0206a81eb5951970135360e316da1c484fba7fff8749
0ac0e0bbed2512c0d56010910db515b6446637940963886de9a301eeeb27b351
0ac9687c705b70761d3343abd89dfd92914a9fecb2d6a7734d16bb705216d578
0ace7322e1caf17f512b0121ca6eaa5655285258e4a62fbe41240f9ac2ecb7a6
0ae3df4dccde47a1b09d39207b9f30a2597ac47bb957d8fd5af4b90455158f20
0ae9690fcfb4776b5a637b32faab01bfe7ff14f4dc86074e8082a177719f3862
0b0f178a7d65b654f77bc0fed1938328b6b71fbef41a924af73a49c8df6ac921
0b211d8e8aa871db8d4e34b9380f9ca9378f101941fe6d6e157b3d01b12b604c
0b3cb38c7e067d9d037cade4c7913baf559f460631e383cc4a5c3be37e60af3b
0b4554854fa7a30bffdff2918a4442245be0d1aacb97f2fae6b82cd51660f001
0b54232dc6edf74f68a47c20828d44043b4bc4162d2521b12271c9fa54963f6f
0b58495f47520cbed2dd4aec8af8e0484cf381b7606544d4409c36cf12c65ac1
0b5946adf8b4fd0339d084a28005066cd77959432328cdbff7fc2a288d59a854
0b59a4dd5f6b516df4d6e95bfaea8aceb3c4a72e0ea2750049ce5d9cc5dd8d82
0b9fde8d06961580eb062c715f8da3792ae9710d21740048a81339baa517cd7d
0bb1fd02b4afe7fcdb24a937f2182e8c749faf60ab7379d81ac3beff8d4baaee
0bbf58a356addf61a439ff12bfdab98c9e18d5ad04c9ca730b5f2051fb034b0f
0befe4cb82a9de497c806d5d5f59344895da24ed87e83fbbfc2af46346f32db5
0bfc8708c0d6696e49b2247060f1ca3fe670fbdb663a38584d981825367c11d9
0c48a9609baa7e6ea456104fb04ac47b932e43069fb574fc3b7eb5c25bd37078
0c6b03130b74627bcc9d8e8ea9dee8c38a905641eddf2ac50882024122a3764b
0c7779c62e2e848c366a982f9b72fecd4b8c59e3a384c1b13823553a73005b46
0cccb6fec059851c722abfbf12204081921a8df6174754ba780ef233aac6bf69
0d754b3bc53d185d4516bc207cfd9de3845425f8f1049ed107e7d11a1b3ed528
0db2c02c48d880aa0147ca5e6c3e3a705a5523a5cd634934bd3f9daf12ccdab5
0e17476bdad9fffac3415c41a9dd8a62707025e6c16cb5c9daa3bf24ec829955
0e68b34aad8378d38e15f2f462829e1608bb0a4a12ae2ac3af4a58226f32ae71
0e94bb7cea47d64d56ed4113bbf7085b63ed572d79e82778173c128ab6109d43
0eb4d0825fe29095f85158003675cd8349df3a5d63d3462e5ea0e48cff61daf0
0eba6c58d0fbbff16d976e91897d030dfe273534648759dd14c326497fff8297
0f2a65e2f9d03d7f63f3bc19b8ae2a6bb1d23ded8e2dd091114eefdb4fa188d3
0f55b36662984355d9eac6e17f8e82bb3c22b50f9e575dd7f943f034a29ae916
0fbe6f2d952a74b67f811ac2ebf0846b4c96884c6b17380e3be633c05cf8cceb
0fd706309f7a986f741fe70a78c0fe45ed511d2200ae5dd2bb6e3e4e245af118
0fe9609285cc152fddade7dc1ed34f89896100fd2323ed5b0d3be66855f90b26
10571689779f5c6f9db5e23a6a0830de888703a779b9ce2f8d17ff0e241dbbfd
10978b8b077673d4b3c5019048fdcda91cd0ab79e45ae5c49dab43f1c64acad3
109ad66d7b7da7e384f892db4022ea03b3c6e0c681d130429b68e4076f57682b
10b2bb538bf3a3cdb11f6b750071ccb24ea673ce1321b4ca4366510fa77b3cf3
10caf1ab5ec78ed0043c414ce9bb64d0c9b4de8c5106750e06e4bba3f43ca562
11285229bab264ba9502d93a691295f9dfe40700c41a24d422698d4f30348e16
117e563e4c987b56dd058328b7e77ece63797def5d5b1c4a6bdb82acd9d3d5d4
11a696e2db48136db03349ea13c812166f4b9ce239096d925001a6135553d374
120d144e73dbfcb606bfcabf64ac7e822e2ca9999daa683ef8307f654eb2f6c3
1257e34f415feaa1c1305ca7bcb656ad95fd22afcc10e7125c47e58f72b1cf87
126642d4dbe31e8d30759df590478d293496a001fbb5d77c185086001198797c
1272e28b51858059aa32ec294d39bb6865ae0b9d933f69f2130ed044f48b9f86
12b4304552258a1f53817cc85384a7bafbef0ddeea7c027aaa0d43b0ee0ef1b4
130e4057e900827368748191a583e0113e6fb40f5d906d2c948a78e5bb0015a5
131c08fb36b2e23147b61e2f3ad6001340317842f1d8804e405d08ff2615a0e4
134651a758b3b6765d1b7815a0729de673c7ace592d525fcaf10aa032b82ecec
13544190d606692870766fabfcb632b6486742a7918af00f51cce27c3c83afa3
1387511d9d12dad6c0f507f5f476c3693291b4542da49f24c6cc3a8b8c18e0cf
13a12854045da97c1ccb9d10dea573071b8d83be7ff4f33abd16436c1941aa78
13aacb007b5bae7029b7d318bf6a8c4ee636862116ac0557f30333d8f7424917
1423ee1096a513902b0815bcb9f01b89f46c6a756478cc678dca93de192d3610
1453101a5d0a77f37842fe224764c767811aa00f7f6e4f267b1d9f4fa36d0da1
147edf01094c4f8f129d1db5056a9a9e5c4a1fcf3330c9ed271cf732750c7448
14a0a5f9ccfa945a2a484409666d32f75891ec6b2501f8b0facfe8d14827129b
14a481c7f088484e44a857cd1f6336b7071cf66524cfe90c17f0219c7600804a
14b8f7ebdcd6f3ce3dc908d7733a76f459d5863a2af5b57e3ab1d0a54b7ab7ae
14ba855fe6afbed915570579de7311d05e4c12f879f6d1846a846b1967b5d18f
14d2dd7c576c8c9606bc1cc25b5a9e742d0fc69182f757debd399103a3936049
14f7f63b711420ba795f444ccaf089f74b229400834a4aa5aeb9259a97f3b73b
153f887143b58cd91a456953521340b0667ab4a52199da80a7911170182faa54
159838cef09ab1d822f2e83b2c004f4c1bfc2551946c4a99fcaa63eaf8586af5
167d6aea9a069ea15a6656ac0701364aa346e57a2a4cdb8803e2b3a84707d052
167d963baccade952f1bcadfa8bfba66f451e724537055012ddb8ae7fa7b90c2
16aeb551e029fc2a9572fdc1349430bd9d5d81e2f9f802c586562fa3dae60e94
16b2d99302b0b60edfb7eaed82f9d4d2700b28c08774fba1a695ffb9276ad7d5
16b42e0e016202e22fe5fb0468d463a21737d9142703ee5fafc452629d863ab9
1705bd832d26d85ca17573cdc1a79273d9e3e5eb4d1de3dcad9d57b48d44df79
17aa92894c3b118707182f2aab15d528ada15f01a88a0ec3813a31544fb6d33e
1811f7efc4f0e7efcca7da1eeb3b5c7f56d81bf93d583da8ebe06a8a4266463c
1821a5a49432fcaa10709147758532d0522d2f9ace520fdab42c767f63567d20
18242d06c4ac810b0bd27ef83ab47c5e325d92a6e68167ba1f0d452ff30cffe8
1865cd77edcdc3f771da07e2cc2242a93ddb4f1d534ec163a502fa40f374447c
18c33592ef0c5fc32a61502e438a19dc47e883609ccecc145a97095b88a94f59
1917e95bed71e6d8fe55277f2287fe66240c2802ec57cc95060501d5dd1c014c
194b64989fdbeae81987632055ed4ee280cb72afc6e1fce13899eb65013761b9
19629833767cc7660da2d1838578c4c0ecb3e4cc235654506bdee90cd85a78d2
19cc11932fd540b88087817f759082b7e6ee7a313ce82a7b97f75b28708ef734
19e1157efa24961acdecab46bbf72879afd75a4ed7cd8d21da2a32619d849059
19e33414eac4c9cec62292213c40518de94f59974e12e6be36264683bfce15e9
19f1a7a1b3ff38a344bb2c78369113bd74dcdad9a946d8c566659e1e9413fba3
1a0062055ac73432e6e0239a16271c939082b7cd72e5ccbf76d82df4618ab42d
1a0e39bef970b49e8da9dbee91739beed7123e1abfad500dcb2143e31373eddf
1a248cf29847e1744afcb487994a0c462703b09ba7d318919ccd70633fcff662
1a7b617205b0fe04e6a23e4664cd516d6cd86474dc31cc4c56806a258de05711
1b5fbbb7f97c0e5d15b20bed2776f173f4b7d756df3b0dc0b96e355744134bd2
1b6548536dd69908f4a007ecbbe8c10bb64145903e8a12999591b8e78a6299d3
1bda208ca702142414daa7363f6310ff1ca910b4c64536c764a60e6b951179fb
1bdc911e75963d7a7804cd28f69a9a91754550b8169215541f15a92656cbe4a6
1be86a4381864c9c17a4efbf6b3c18b107ddbd07f65a2680bda817c0fca443e9
1c042cfcf466df027d65b58732e13036192ec23ac1507d0a2a0836634be7e059
1c319422ec54348121d78bc53d3444119fdd9daf866ca5bb94b2986e929c8d33
1c4c7284b512dd721bbc4621ece09e2e09c655880ac81d89b975446483a3cd8b
1c7e77193223410aa6f653b1b996a898fe04fea78818b3faa0b98291d8dec62f
1cd0cf3338c699576c794997fae3d997f6397d342e4c571c2a7c6f907c020ded
1cee2bfbb00b1173133fd90e80782493b4f9058d028076bfb1320398b800d431
1cf15c996f33d79efb96874911cdf28de38c356a6b4e1fa727487fb62ac3e318
1d169565ee55e09a8a69e7d888a02392d107dc3f7514d746aca7a86a9e2baff6
1d3e7fde89dffcd5c7799b5276ae3f822367a24390717b491272d70c79bf9871
1d646fd3b53c702bd64b3213d0064a5ed6fc733841c3aa9c64f4121901a7d332
1d886b73ec8eb4aef28dc2844a661f0548f1f60117fbf70b7e1fcf87ea035af2
1e9bbc55cf5e4b8559d9e73f40913791c20093ef6c5d1381ac1c1eb1ba128938
1edbc2b562a8f4d3a6181c1b8fbf50a1f9768a3516177ad3299cb79c77b15f12
1edcec0114ea5e9f9c87bcaa756f011a0f20113cdd294fe34da7a5cd5d48a0df
1efd7daa40af24fbacf7cb50d9252049c80f032ad02b9550c210faa691f7309e
1f297f83a620a0258a4dae9177ddab4cd47a139339508a72410fe8f78bb15f74
1f67995485521d611d1defcf85e903d4e2db410fa2320cdb3ce7078ced8d7e89
1f75aae1fdeea77e8dc09029f5551b7c6ef4c05eca2421372cfa4edadfea172d
1fa593d50ff2463a868466683139da28775f7c463db7eb0ad59f2c9ceada1957
1fb49f7bc1814b0b5d849f9b25ee031160f6f0b6ed074e91e7c26c52cb32b66a
20061845ecb476935778e076c5f066f491937f6d870805b801ec828e68ba0769
205556f14222038ad8ac7aa357ab7f709676345a41aa253dee087a0b7d3d4bba
206fb3374f4a2cf631f4debc0f9b55e142a713ad96cc9c0389d271e4020bea67
209e78a2b5f8f20bdb569de7b31844d4753117da9465d6343d9751e98b804396
209fe2aa3a2f2ea0a820fb20b3b685d488ef4d4098e846f881931092334cfd6c
20b8338345c724c0d991683c360964293b95deac7fc5d5186582eaca92d9605e
20f78dbb8d4c5f2501c24caa0b5ae61d3425a232299f5f925307553042fa9b7a
215e9d40d0bb9cab0266a829b9da337e7ac8f20e99841062db847649d3d88a5a
217d8b20005c0c8048e47c7f18f13791845911420a03dd6574996b364a23d014
218bd384486f1950d97b1d00691e0fb26f741584ee7019a60b11a6021da2c106
21ba559230c01a3eb9d330780fd78084ba32ecad2f65f5fd6cb793e45acd1411
21bc35d77b158a9d2a373cacc0f9a72a48916a96dff9f2c89e8adfa246b8030a
2292fa0c11f952ef45f4b5fdae77f99acb5925c243e2a616140dc9f0e116cd7d
22994f80163ecdecac35aed4225fdfa11d58c44975f518c76e994372f2355cfb
2315a60155db14110b2ca3333a0322c716e35179d280ddc13a4562b50e1df803
235ad8d5bd960b17ceda56ad0266ce0a86c3c28dd2dc1ddf2fdc9ca230a86280
235c279335a5f67f0e14b5c1ba8e098bfd896e2f334bb843d81478fb7914e141
23948b01e861437826ddde016a3742aa4346cfa07770db466f486623b652c15f
23c3edd525d274c1018f20ca88f2fe9101e1f997421500db28dbff5b0b0ea7e2
2438e96b46b2a4f7ca3966344fa14898537dd7a36a05293000914778df86b9e6
246c178c09b5cac44fcda8981489ae6f0a80a7906a839d4f823200d0e81e82ff
24f280b6f353d92037cf0bd0d24fd1d0f20e0fc42346fe99bf09b52103584c2e
25358cb9f5cb3184564c6becfb4f5985843c9c25a5c8915ca65175784cc403e7
2559db35856b0f4149b8f8f9a7021378201d75b2412a8af340703668b914d68d
25b130747e0c809831b3d2ef26ce2fe1e89eeb48e644d8b1a563b8f786fe5861
25d695bb5d0e087731edde5c70b725a989b073a3e0ad9c32e669bd291fa81f19
2643b1b74d4dacebe7cf159aa31b82af491cccbbb60a9f236926b16d29d944d8
266fb3e4bd5401f0b0c55e2bb0555513c15b680d835214bbcda7e4e4ac5b7300
267b46176ef39ed4c72aca2774ccceb9e378c183fb3fdce99f74d67fc2ec8cfe
267cf0516cbe4646ce542bcf11c8aa7c88421f7e23273b12710030b679615b4e
268b4962d8817fd36d4bd35df6cb2ae90e37152997abbb3dca991d15fbbe43fc
2690f2f846c094179061b25e63a662621f77e8f46602232b210ea96aa16f2caa
26a0a5af0356907db3eae343a0456342d2483891749d0cea48970b937b81a073
26a473701d087aff40af181776c2c5d3ff26d9bb2da76922f15e7923502ed5c6
26b43d817434de448943705aa806d44d4f469ff37d34fb926e2df2d9e1de56ff
26c4d57be2aaff14d0a55ace2271e69e6c1795e62bd33ac28a8224809da16e7b
26f739e4aa57a17d62d8115db35d9633b3d18047bc50711f60bfa780e98ce602
2718873d2e6943a3cb8d01b93bc53c2d788e18b2bf242bb9a69cb152e6375cfc
273067decdf8919785bebf2a66ae0c4e05f042bf5c5919b125225cabf68f9276
27de9e7259b3f66355996d4d2401f7e228e0305e0ab0b519afdff57c5d4b108d
281a1db004dfca5552c8db658b961cbf2d66f1b3af8fd3d3880e48142234e1c9
284627b670f3719fe06de5ce70504e8406f5e98caee963d864caa2adb2918733
28622847da719610158e08567076a5d5ba5aa2f9e5b60966f20b3d64e8562e24
28812f61a0e85c8aba2afe971746fd9a94960fe2c344e74e59588399ca3bfce7
28a24038757c711f47619903ccf36185386ee38b9849fe1065b1a93cb0145b32
28d3751d2693303314e477e89806f47dc5e00468899e2c2415c19c16d8863123
28e3b1bbb881158451b7ec57df563fa392ffa0efa7d797a2766ce869afdd84e5
28e55d94c18f5a68aef3f7edf68870333015ac9551ac3246dbf8945d9c87bcd8
29009aa8eab48629f75f6b1a7efdf5ec86b00dc9f0f73e6f22b43ee9ceed354c
292f7e98a60cc431bf2d1bcd2b3d70e27d1053e430601849e000961ef95380a5
294805f422fa14aa09f07ab8fa25774017bd6b9e630e8f108f22a3731a9b9384
294f9aca5bbb060249b7ce18936e370aecdbe09b3c34037ff63250a9741f9d4e
298949fb20dc9faed86d1ca228b7a813d13d1335230759071bc13376d54eb9f6
29ab4cb568451765f2c41d2720672b33a5ede62384dc53392ac0646531d1e63b
29af02fb97604b3dde34705a4f740847cda4990cb6b6874d878b6a4745554f25
29c9f17bce7a2a003897170432194c5f17bb62083d99a29e36be23bde2543dca
29d529c6b6f09803de66f85825f87d028b89451f49f31d65d03b79931433c6ee
29f3e76214681290cc200c53c744ee316d747fd26b73102a1d9966dee8aec128
2a325d1d3f8869ec21c2f08edf05d04eb408dc26ba099e548afdd15efda31828
2a673f42fbe19be3d82457736190c1e9dcd3d4eac231369eed6c1fc8054330df
2acc711ed80f323b5ee29d398c0f606c744c37ab73d5fdb95b21ea608f6f5ba8
2b133cc69d4689e8363a728ac4b8f0a650a7f667fadb4349aa581e3bbbf22f5c
2b190db9c387ca560ad487a0c9c7361e18a0764a821a036327c928d9879d885b
2b19cbb821af07af530a33bf35fb80442f0ee4094d5d43bac7da7055340d19c6
2b2659f40538cd6a9d63a9e0a94c8b3f0517188ea0b8e5ced089f69b6295c2f8
2b3d3dcbfd856ebbdf630b6102fc9271d78df66bb541d43d0b16407158961758
2b98733c93250bffac36e51d485149d88d89f603b1035840d7067f4ed1614ccb
2bdab981c2162cd102f3fc7c300d0ee715295408a73acce6a52f4d7ccda0448c
2c713bf7c92912530260608d7a69139c9a5093c2c527f2a4edb043dc9e116925
2c73919457eee277531804f16a612bb9fcde1717840de8b7b9b5f93eb44102a2
2c93d6f53d69f47f6df19a7d5ddf535e0bfa01cd2caf2ffd3ca0d2baf808ebcb
2d301fa11e8ac58eedc3026b1472052c66ea7bdea6290c840494888c66db8f66
2d4e1a6ea6dd1c6ecba644ba35077cde3c184f66870c5adab1d6ff828ee8bfa5
2d6598cd90d972a82e1c02c25404452c0054bdb02a839dfe0822f8b0471173f9
2d9a8ca9036d0d357febe5f72307d8626311f760eea3d77be1b42d6d4a627107
2db47eb71f5d17824f7ca33a66db15f1dbcca059854bd381149865737166f72f
2dca07b63e534d3de2694a835684abc029fd08b0ea484e72bf2a7c79e04e26e4
2de6c1599f1e554d89467f79ffa8d15976d7402e75eef136fe27f64a5557c792
2deaca506cc493445f846d222d6999cb88b5f92ea69972145ec1075e4fb50425
2df31a04ff770f670e621bfaff444c6ce40197c28dcf757a7a15e40af171a4ca
2e2d853f45baafc9537d2a197d8093e3c8af01d7f6a72b270bd16c69974eb36e
2e3f5c5e67d7b13319b14dc4794c0ad5206f1bb8d3d0b716ffc27d048b7d1723
2e88030f377f760bcf63e5d5237f3e9d73a881f275aef9c8082688b044e3fe4f
2f4a7faf70084b3dd8ee3918144848e85f3e3ca98c4d0af03e2539c7b4b81fe5
2f555b454c5a527301fac3aef1b1d77fea08683345aa9d029dc47a891a979695
2f5832846744fad694d8ed3c9941ca96799086ea2858a82f52e048ece54cc21b
2f6cdbc7d86b719cfa8906c79128a015eadcf761e3be5d74159193ad72e3430a
2f7513d6fecfa27fe6f617614a42767f25813fdb238655223acecb65656dd2b5
2f81ce1411ecc79b19e79b161360a7334b59ddabf8ec9a21032c1cf2cd5c44e6
2f85eae379ce3a66d7da5ab8575e7946563e1298bd3ded0152611e1ba2b52eb3
2ff88f33c120dddc652181287a9d8c080cdc6656d28f547ccd06fff72801b75d
306055e293ab8cce7d43127c1677f0b5011435200e265f027dba17da0ae0eb19
30b5b42277500aebeeebe45e95c216441c071eb6f66d3e95796c59c06162c295
30de5f69e1efe45e88414e8de6c2a4ad07c5538155bcfda48e3fd1460c329acd
3100970b83b562c2eb69ec0294bebed56f662d392f43746108654a61a61596e9
311f1ea41e4295dbefcb0ed8f21950b1c49fa6ff3153c7c9fae3952d0a589367
3130a94ae087f99f0e070046c32574dcf6de9212f0071e428bdb80bffac75c25
313a666c29fdd9be554b0485de4208c669b4596437e17456dd359bde588a6314
31bae99b833d151407885ed28e745590cf5e97d1fc1b950ea12d67b11d8199de
31d0f386f79ae766ff7ec953f8bc51fe5e08b1e7f292026a328b8b452a2a22b8
31f48c094c71001244d5aa0ed63bb96d1ef13fb930fa067c1da9a3c8d7d78f46
31fa1802b3ce7020570cec2be78869bcc01b815526f918f008635457d61b0b16
31fec3813d59705150560806b1c8455c3d8ec62752bac7733973e470478dec39
32022478da2eac4d668a7ab3e8bd98b8225346e08a48b259c88b358c8a406949
3259138318d8d440dc78dd1fda6e1d67e418a87d1cd4720cb9b3b50c7b8c28ae
327925970a524df3f9f4f54c1babc20e6a348779474eb1db085ab217dca4addc
3285a40132592afcea4faf385cf0ec7936c984be099b18a715f173de394fb70f
32df6510c27f585c077536f2f6cac2a9d5979bf7cb6bb2ca6efe056fdd1b46ed
32eb1e5e33fa3062452daa3b9ab0a39691dfa99f6f5ddee7b40025b16f2a3a76
330b2faaad625743f919f4d9486d4babe27db9129a8724d702e0cf52db95edd9
342d3db81c999ba57b9315e3289e5bb58811860f7d81d67f5ed3be060fedffdd
34363cea9ee8d6eac45fec05478ea74dbaf3ba7d644d0697c6d99f1fa2d8850e
344ea32c0c379d28bb412dc51c684ac43459a0ca6e99d29a679ed4da4043cc1c
3468f769cadd12bdfd65bc53e26e0128720d6f1c704a5dc5bd6055671c0942d8
3486662f2365a76ef8a23c25e6ea8f1ad91f9126b315c4b49ef8c6fd21537af5
349cf7d0e4282cbd8dd483e47c4cd9372f1b2fc3ab7c7a5420d994f9e3c4f01d
34fe40a6b7af2aadbe0df47ee0c49fe0f45b5ea22664a9a433cd3f516a03c88d
3509b853ec3628f4490e6033c0deb117c88a2e869f8dfed27eb338cc9393ce28
351af1ce2559516884957889e651eb422a30364c527c9187a9a6eeb3ae3a0722
35481c903bcff9f4e429f1568f73ad7a9038da6573876984558b424dbb23a2b5
354a00a6cb985058fd8f4e8748b7166a7b07fd45b9353d4fb5789a075a1a6f9d
359e07f35d7076955da03d50ca1b97128a1f86b738d87f73b6e35b3a62a1281d
360730a38c9b3f8287d379518c65044be64f287e0d9792cd12a67e8a258cb0e2
361b5285631f69367c462dc2e3f23f48797c659b86b029ff3f5c71f0dff7f3b4
362082717a1449bbf5167eb531f8d64bbe656137266ddd240ae94bd8f8908763
362a64a919a7ccf669f20437e4f4e0bed91e46a048b29252a399eeb4e5343cc4
36428776e75e6ab26a33dd7a6fddbf2fd5bfcbab40cb681162fc572bcf3accce
364d655f3a730a20b53236590c4db703298eb27d8a1582bca4d6bf82b9056b52
36a50b200d0c770d9cb8605a7543f7933aad80cc6c54ee2408ff284ea037cd09
36b6042c527c1204acd58bf7feb6fd57e8cd095a91a86d524a80524658ccc2bd
3707f2e023cffc7af5055532cff231a1192f7fc91a5843427020fe6508642488
37194a4d79ab4bcaab83e265286b6098ccde5fc4cddb92a943486d16548eb99a
37418818fbfde86513f3ec55fe9087907789a4f3e24a425c8c80a775e3b6995b
3765020da84919a4e4b44c9bf9f1fffec07cadf271489559cecddd058a467994
37c893986bba8c703ff8e36c388a86897e8c8054978da7cc2faffbaa767b53f2
37e9a6b31c796311133fed2b58e0fc5d5b890a758876a5d1ad72ed5d8e2de38f
3808a484bb08a5df7b48479dcd2491fb1d0393049c00a2e8f48feb6e13221cd4
38bc63cd5253413662084be9822df494d2dbce64fa247fbd1a1ff34f385dcaf4
38c9f3d058668e78280a46f293ebde6a987f4fdee387d7f40cd862b8a6f74ae1
38cea63157c9bbfb7a6c01ab50478c111c7181752421f29ca6b2aa846fb32a45
38e9305270f524cc87764e5957479ba5068fe094fd6dc9ce37090e358fc3d591
38fefd0d2dad12d0b15f5988f837973cd04455f26e6da93e9fe59e0f9b1f4ad4
39180412800646f5aab01e22563f80d834137fccaba3b465ee1674b5dd3cc975
397fff705c0709e8edc23dbed1e5de970ec932c267b5525f61e915fe6aa118ff
39b2ceb631c3ec3a2be317bb2a724926b401dcbceab83e8dbc13c2252b2d8fd6
39cbbd743199b30e70709bd94f44164d3ba0ca4f61ba8d1e1ab0901f013fd3ed
39d1dbf70c99cbd025da3cb1a34b4253b9aa3cc71f4b82865d7240988e85efb8
3a27f5c6410a877c3e0e693da02d720fa4ba13122cbcde11e2506bb27261c809
3b04e409684a700614122afddaf3d9348e5f63b39104b57f6ed749c7f8fac489
3b583100cc4f8b8e249731140dd5d0f1c7e5dc4792362d8f52c06e64ec722c53
3b95854719fc87e2cbacd2797ef8a90fb1d1f63dd6737fab255a24c8a8cda700
3b994591bb57b104d75c7943adbd838f27f7f11d9c010c4dfa1e27824a2f2c98
3bc533c5cc266853fb054e937ab1ffd767460304c8f68f12b785e0276a124a16
3c1a2268ff77dc5b0c4a65fee3d87c3892c404d73daa8db6632cb349788969a7
3c225866c7204b235bfbb997fe1050374d92361e885613e3c789f7e6af1e46a8
3c2b30bad9ad3d549b767e4ca31fa2ecc0046fb68df087cfb714530bf486b654
3c31db72d401cde65818cbf83d5fcb26ebe91c12562654c8036cf1ef2a604aa8
3c61e07b59101823085650bb689f9c5d85cc3a38730dec49b7068ac073140cf8
3c851f68834b71afc77ced49e8f1bda97dfd450bc7b3893d9561d70738e621bb
3cb562c897155ca6e376e1df492d7bd0c09a09a7fd20a632b6ea1092837d1952
3ce222318459d96f04f78586333e89df21be785693ed5ac3581a915baefd39fa
3ce5e0ec06eebc3abe3569b69c02ca921131b00cd0b3ea03cfffe4dd5b7169aa
3cf5409c38913c0b836a0530b6de790c5d8065a5793f4728d5070e9bb80c5e3c
3d01eb5a209615050c01798903f6a54393ac914a77c4077f48ca8c3b4c374a6d
3d0a8fd4296ea160fd3a2b6c0e723babb06eab9d91dc19fb3c7f3e27c660a081
3d564cdbd75aea90aab83f6ecc791a52a36a8b85ac7edca88e3aa9b4e2c481f5
3e048c538ec9846fefc668f717579e64f7855e3508bd25cb9171d79de437fe5e
3e082cec7404faf00427adfa26ea3989928a5c658e818ddd2598847d656228dc
3e41fd4e68281db57e64c77b4463b1a6788293ce1c6e1f351f348a3649dc0e53
3f11b5ee0bd83d767897d4ee522e91c62224f9d006bcdb563745c31c243c73a9
3f3227465faf4599bbe8590c1d6139154d97914b97b52ba2d2985375197dbe63
3f96a6cefd1e7e20bec2c5398eb982a9d75bc7fb229a2a62e88801a2da729e88
3fc10fc46dd5aa19687ffd491b3c5a0112b28c09c6bcb2e14875f1e9711b90ee
40052872b997f53913eac63e0139b9703d440846f0409ba68c192e345914e81b
4030a726af1145b39fe631c46385d454fa3556f9f77d7ed236837aab99aa3a7e
40336ac508e76e92a8288e3c457c60d0906c18a7ef60fedf8d30384f5e905f4a
404ece60efc0fe6aff5a53ed1aa3f17a734e6d34083864254dff23f0649eeabf
40542f7e6b9ec1650870123e1481805167519c1ccab767d0a7d4eba76eab509b
41358b38aa0d220b186858f357039db43003d889ced960be3fc021c72cb49843
41393566611867f753f5137c1be7e7761e34fea6396d670b5cc43d4139fdf4e3
41983275fd420da46c1e731d6fab69307793682d2ad5c19800e1ff4ea5a1c76d
419fcb45e6bc88e9ce08b2e2eb4b12e55e84fa3ac834a419042e79ab709d6eb7
41aca73db0fa43af732e8dde269c5fc3f4fe5715eb3af152b4e638c0222fe9ff
41c1a3705885c830cc09e51b2d1c2d019b041c1d2b871ac1f475269fa7b058f4
41cd1703927bdd9bbad60420e03d66f3143cc7ba4ebeeec610d19110a06aedce
41ddc887cd5cbaa4f43d5b3e8e088d4038bfec138065ec44b3933761cdb6e96b
420902996fd24fe476532e505c5ebf2c8fed6d257c7ac36c367e83afdb899a23
42269f06fc9ea7dc6048658300d20695d29f8285c5cf8b767868f83718fb5f81
426743ebfd68ceb4569411c059a050e8385f104e2080a699c1f57b9253244b7f
4296e0299eccd1b76ed64d63de1566f4008b63a0b2d4423f248879a62cdf3122
42cda2eefe0ea28a9df2fb43dd7378890ecd1c81a3cdc04a227f8eb17b5b54d5
4319ac156c08c965194ad40b73941e81b015cfd33c28312c8a61257c95c26d27
432d80a0b5013d108c811a3e754003c9af0410e496542a737100e7dcc45f9589
4367166e2878394913de9bf965ae2f2215feb0b98a655c4c75507bf259a0fb2c
437d882f55bef521b03cba876658165938c48dd2805d81f01334cdfbc5de2040
43875c2e3d7ae81b5bcc94df8b1faeaca3dbe3ebe36aa08c3f0cf7cdbca487af
43c1209bb43c7a77924a8b63648ffd7d54361cda91106ecd4ec8facc5ed02b46
43e67ea876ad08d90a99180ace04f093bdfe20510cab77ae4d592f7c49cae5fe
4412d2c74904453658b3a6efcb8391495434a8ce8b5a891d1a56a9c0695aba86
4422464949a982ee8093d11fbe1f05fb5059219e6fe12a145a37b2c5ec66cde5
445a07d36d3ffef825904acc1a6fc63a2cd1b1dd4c892f6653f111bffd20ad35
4462bce6b4c03feff3629564b14d0638944ae23e1272450df1179a8db58a4904
447cb4adb4f20acebff96f501a7bcdb09ccb1345d31c39358743626a656e9c4d
44d694d6e1565728d28f20a6ebdbf848ca781bd14bbd9a52c8aef9aedfd84f18
44e8faa8136f1ce037cbc273a199ed9fff8996276ee8f0c29c71d598c279e04f
45244db2e27845e90beddedb1ec102fcca2c128a42cb87de37330613f6c1d4f3
452d8f9698e637fcf7b78fd527ede2a624f70d1de61cab46d03f9c11ba3b99b9
45d7c42cb65265ae97c298d439c18e5293d6615a66627b382d04491113afd794
45e554ebeedb105fda84bbbe99e31ebdeaf8bbb6dd9666ee68829ba2797d8537
45fb4b192aab0cac804ee19c4c1dd588e1d5f5f5e714057875c4dad4eb7def08
46169a1a2bfa27e1ad682d42f2d8f64e35c47c3d6964d1fb0ed5e1b54791ab1d
46497eba130c2f1b626942d5e45257bdb042d8871bb4785ae93b5155e9c0a137
46a008302e7825c0dc3b06fed7dcd68e5acc9be74e10554f3a3cb78ce896c796
46ace6e2849e960d93802da4a8e31189b76978abe4f6bc8b21b73072e59c713e
46af78a5983293773992639ea18c9e7afa3d429944936b74dfff21ce4e4c5abf
46bacedc7760e06b03838366f2fd22912f5c195c4843c1c4b8043f6608f78d7d
46da0040a71dc5d382df1a5bbf26c30a30c40deb741a9f8158f301ebab22c0ca
472a73bc6336bdfb9e40adc7d325e903fccd5fcb1bbce58d2c8d59798173b0c5
47813acb5f58a9b8a9860e783297f3523921cd0f0e65fb46b17c3d4d2657064a
47839611815e5c6a42ee3d865e76ba41db7abf777fe3109448512c17beff1876
4800a166cf4e369e4c956293c94662595e1dacc05ac3229d0476dbb42198b0fd
484e82fe8cb054da94beb1588a6cffbe0b63b883299abcf075ed45d9260e1190
48b885dbbf24975dbc8ebf43a8e893ddfaf85cff4252f7b6facb32778d4d57ee
48f676b4e8c078490e32ff8637fe3aea5d23441a99853a26ab280c228a395644
48fdccf4e130d2124c0192f38ea04f5227f511747263ca1b46ac4105b9704c51
49231d550cb19d61985b77d5d27efc3090cc0877b1b5a9f511e002f8d32ca34c
494e4ec5a5ace446b5ff7a027af1f295f3cb744886cf279d4ad90859d7fb699f
49b90f352b1c14b062ad186107ac07cc1ee5c764d331b5216c38c1beda15861a
4a1908ad8ca9180606ebd873de955de2c4d90f641a018096e5a6bb292e6675b3
4a1b98ee87b0e72ad01af514595388c5c6320f16a4563ad0edd25db8bb3197e2
4a2639fed5d8b459eb4b45e9a8b80db9a998cf08f426626dad420973177b778a
4a6885bd903c3ca17dd1b377e6acae42002e77d529c60400d86fbc183b1b609e
4ace35ac53fbcf6983cfa57d745c0f1cd614bf5688104322dc52b4c9cf094a0b
4aef74c9e05b705911b76fd30545537d2875292ac89b72a7747fdeca7c5b1cca
4b167e4b2a70004c659337c35a62e9fab322a86bbb590fa8c28938196fc30157
4b2d7145a954f25270a4f7434c0090677d75116496e9679f9c594bf033b617d3
4b9f6a12da220aa93a2ca678ac673b130f7fb2d6cfb3199630f49e9cd3c3fcff
4bedc01ede592e8be8e26fc29085f0f47d74c9157159efaab4a19f56c04078b6
4bef05e7ae3bae88cb473f769b1f397ca9e99cdbe4983ffcb266075020f8427f
4c0dd14929a1c8d37aea8776df85bd4e6e64409039cf9419052d42b79a861c68
4c21592a51a44a15135308a1d6428184e28bb0bb31fc8c0d17f51cf2c7284cf8
4c24a54ebdcfac4eb04bb76d16963ef4ed9d75f983bcaa53ffc4c593b4593dca
4c2c09b2f31c2ecd78142ec0ba10d3fec8f66b7384832464bc1b27bdac5965eb
4c5464de1dcb5e9a4391a42d327ee08f84cb1fde0a1fea8922e23842219dc7a5
4c84d21d50b5c612203cf8e8bcf8d5340a86bc964038b3db019bb4543edc0a84
4ca7c234aefeb0b246d42ee35f79ef85c5d01c163c5c1760f5a67f8423ee357b
4cc52462b5f03754077371c7478cc3bfd935d9c6860625dd585fcc296f00dbab
4cd862a3b93a395128099cb18dcd097b20b12800f4b0819efa7d656f000ba7aa
4ce006207441ee6978e76a47875a518f00e187ef6fd6e3794c372df22513ba75
4d3c46336d1d985bfd94d8c2423a0537ff7262050169a50ee1c9b1aeb4bc62eb
4d436ad06f585830a451ad8517e751569689cf0ceed1184ce609dc4d393e1a1a
4d62a253220fd4f660778259550cbb11121a9ba2e53cbe756f644780cf1dd735
4d7f4539400e685737406b83766df1acd67d0e4b41d035382f73157c456520f6
4d9cb7a511c6f91c59281da6b82487e928c90b9f812f688b78ed7d6139719bdc
4de0fdafc4ed400ac16182bfb84980f8a6ad8539a28800af35eb13cf8defe33f
4e0a7f620e97f09125ef1353fc750f4dc9291037392c9bc7691efe072539c8ab
4e0eb4b0da40e156b9b414b0d125ff0e446f908f53aeaf8e2be9884b910b21fe
4e2b8ac667adebbd6884accbba4a627ddf652b7ba552209b4d695e04a67fe21a
4e773278a43fc5d789395c289821d0f369010f44182eb037668022f278fd9727
4ec575dc42d3161284afb02358221c404dc8ad72e01f279d7cc17766973c96bf
4ee91f622baafbffac00f425362d330789a00ac56cd5f04ff4f905da60461fde
4ef880832df61e704d5989d807af8591980c560b93549841337f3af2e6fcb8bb
4f12579b6ff864a50b9b770ccf2307c5a11277a70aa58a27c7dc2746f364a033
4f72c2c9d9ed83d52340511064a998793335cf90002a4765354e9f6a45bea1df
4f7464d9ce301263e5594957c1c41e3e94457375bd6fb6b603484deffb1eb7f5
4f8c4975772b45796f9409ade8d6329a64cb810749f3bbcab848a1ed1c6bf5b9
4fb4734e13afede3caa83c5849bae91527673ca643143662acc32ce5b764f329
4fbd65f30752e7d43839dd4cf2248b20bac060845d25aaaa0237defff58432b2
4fc295eb74ee1ef33b00d08b1742120ea04e2746aa22a9df12753112e5cc4624
4fc6229dc7abde56b779248bc4770246697b3b2e8518fa14c5f6eeb18c94798e
4fdd91fcdf7953f7a634e5a077c5a64c6764886ad36cccd5b556c0f0ee845a31
4ff7f78b705a0c23e5729044982ac7ebbffbfb948aed8043c864ba380859117c
4ffdebc4a06e09ca6b5721310102d62ec451867b6d4c8c481f94edceebdc106e
50279190d022bb3402ad821cb65fabb05c21c139f65ecf67ea0305ff90102ccd
504b5a52bbb19ac7a6e462bae1aa07c372467a7b9efee61f63377bd2ce89f0ef
505a2dd37fbec6485dfee2b9c9e540d5b27cb903f7a0644cbfa396a7ce16c4a9
5061cba65e2f73e46043f527ba94db7ff51de0e83c4e31e30646f0f9e8051ec6
509cc8dcf7f94e6befddeebe3568bc5b7155be1be29639078c315024fe57ab7b
50dea4a8e60bcc71cb9d2771701e65151860bb588a9b2c3eb1f98dfb765e63b5
50fee7f8aaef047989aec4fa1a8a4bf76c00429fef992c729f218492aa5f0295
51138f15cb3c2a2aad3e87d5d6033ae58d75b2eb26f2318be4dbd2689ffecb64
5148dda86f9beaac35e2e5a7022985aaacedd3df818c41881f0eb35da51f3a49
5161d640503e4604ee95c5f4301802854986cbfa77410964609ec53d775e9ba4
516b7bef9ce4d8bfef6c0749bb97b3d78de88f6fe1fd8c10f3ba3bddb08f8591
516d79eb7a4f70890de24ab3860c3f275b7e5978edbc003062f6ddd712e71692
5179e629173adf551436b2589166a9f29d4a6cebc0ffa0cf651fe35d62a14133
518c6b122e397375c7e0ac5863a58bdd5a23c4f8b0d7b61ae715a3243b0f75ce
51c862d4c3e752b181a4dd0dbda3586a149112a61227298fc9715c0df2877368
51cf69b308fc2003b8f7a01c08d0b7c724a87339d94460c4b1127ee0d4685c00
5234b55d5ead2c3de0430c5a980a8897a2b62b7acd6139ff74f93f8c000aa5fe
524d0daa5a1c58805427acb6609de121c9c242b63b284292decdd39521fb8e16
528874df41dc5406c2a13a2dd4cd4056735b8fc039d6aad472e7f6da0b03cba0
52a2d2024f82c8a5f3ff465f36392100b548d2d0e8a4d1b4d7e641bef7b33cf6
52c8f9c886c2f791afb36d28af28183de185de09431d772359b4bbb73e8360ad
52d40d4233e74209ea8873e37192225cf2fc301941ef0e542ec9d111797f25c0
52dde705319dd2e8db0f6f19344480d2fb49d8a37bb3dfcdf8cae6fa3ecdcb7f
52e79ee29931bbbd2fc2610b03705384503c769456befd5140ab2e8cdd91e82f
5310e6297d8707853d12fc2846aa5b094808ee5802daa82c811e132f2f7a4ed8
5312a7b55d4df5819f37b901e69be95c36fb8ed802e339786bf4dda032fcf6dd
534e96f548f1b6cb7d29fc018cce7bb071cda74d098b3ac1b387d182bf1854d4
5359c1edfcff44672261e10149c56bf8df903ffe5b9326ef96f422807d4fc37f
5388a5a5ac0868b9e3b94ffeebe2b0b6b2173805cbe40b3199a2a7d6102f30a3
53b0ba5720e4b2fdc6b73402d137133d657e445d5cebef370873a4e42351c73b
53b11728b62ea09edfa3f32a44096ea243a25c62562ddaf634314445b9be6900
53e51045c7587d2f9b0ae0bc578dbc3a69172f3759db57dab9d09699e58dcb00
53e5e33078b4badd1271e0bef532938dd22ceb2c3dbc584dcdbce4b5360b5e8d
541ffaa030d89151eb2bd707bd7c2a8ead3e099559f12bc6b7771405a39be083
54410489a4971bbff160b8781fa83cf5dfe3781c0d83734c2f5b74bbb7910661
54c4b528abebcd166acd5348dc52902151a551fa74e33a16d80a976a3a8dba3d
54d4f69336d51bc723845642492c9a835196fbbb7c67cf9a23106e3808c5055b
54ea17f734de44cb32d1dab80d4da227138e82c4eea7284843205a596c5422fe
54f298f4a615e811399409273b4c95f6cfe616a5fea8509601c49f87ec76c5e4
54f327765bc6a9736b9d06825b03d6830b2846209e45859de019486decf6e8cb
554f19d00cdbe4ed559aaea8cc33fbea77027d0e9935265610298c380bf3d03b
5580be3b98d3edbe2c2b5c2527326d29132054b65559da412fea4a2ac19eb613
56179446a73983f42e066397f0ca305edd3ae63f790de8abbab3ba0fd4da2ab1
563176059f02bf42014d2d9e992cf2c0a51073d9da3c4de0164454479f7e181b
563eedbad39fa8cb8b690ba345d8566c59b8824566f198a422b930ea2a6a8747
564d2ca4e1e3363675d9df7e118bb00677345f905fc68a59f26bf49c50c976ce
56b9fbe6dffd39e62b45534486ac625d25a8acdf1ead74bd467a74cafdfc0d20
5722579fa07900e53bbd57a11aea609030fea840d23eb801255dff708f3a372c
579650a0d154aab91b611949c2ab618c68fe499141be5fdeebcaf7ec0c771abb
57bc0aad677492da7a00731e3e411055b9828c6439f502fa5abd8fddb7a8a260
57ed5977900e1143628807aca377e1e8c7a5d98c84c5a8ab04d5e88c548ce19d
5809386d704c42e6eb7331ae1ac98c388a1a872396250bcc13f09bccb8dbdbfa
5817b5ce7a9274075c72459601346f53feb9ee5c07045e711055033fb62f3f0c
58435233e3b17e930edeb5b05ec9ab591c5fb0bf93eb5f0bb9e24453c5fc6741
587a3231d7565d22077b86ddc7e02412ce062d1b39e57d5442b9eb756cb47468
58d593c672bf18805bb58fa1f4fa2df5a10ce56f279fa31bcfe5911c9d10fad3
59094a6fa913773387449cef2a121085c65b61404d4054d351d7b6308b2a09fd
590c8aa2e52a3d13dbfcc1fbb2c088f40d4f79922b3f8e8dc3efe77281c2fc85
594d6fdd6a4de05c1f4bc3ec2abdea0a6a3a1cb0f7a491dd1afdd6eac65c6192
5988667a99534e8f5064c7b8cd0ba0c72633e83bd6ee19aaf410b4256dd8e51a
59991e869d178b832d8ad22d85c3378dbc60450937accd751b5a120bc49dd39a
59fc785d16b8f8bb96d5192c611eb9d0a48eabf66d8227325d152213f5706d2a
5a2352a25387ccf860bf322c4ee4e081c04da0a9435ba3e9ed5913eff759941d
5a4dd241d887c2662c54c2346e304f550cc99c928b4bce4ee352c48b77e04ac1
5a78cbf561df683f19d7bb3fe29a76ecc2e268138f3772b8a400e0f432b3ed0d
5a85d88d332992ac29c78d6e3fa38e63fee3a9a9e3d6bdc17ee9d44ff07032ec
5a8feacb8d8d007be0fd75508c9fa31aa1c4ed3a18b66100d0eddf2e26a131a8
5aa9aac2cd69ef8ca3cc6f62c3ac9e4f2181aa2df42973630865c32438fa14c2
5b91520cfd91246ef4c1278a08239d866d0bfc27fab0c8c69c35909542787ce4
5ba2f673a589da2f475be5c7adea5ce11dce48e34d1dcdfd872d84a0850d6763
5be7b01474e75504048d5cfaf02887b2571837f48b7f3ae44138ee7d9b704784
5be8d6f4f1fcac3f1bf757b45b84eb54edd8a29700ab1c021d2e99527faed7f6
5bfee44763652e4ef8cb5a6c3990868bdd39c7a094f585a9720cc2e83a942533
5c291eb94b389999dca801c5140a8c4348f11f6f544a6275a2f29e95c21b653e
5c6d833d6e638e2509241468124347e1d317f24084a3c0866c3d1bd53fd6b0d2
5ce3cdf2ecaaa4b19cae31ea3eb6b769bc5c11dd4494f345924f3a329cf87eda
5d0fa92e33f81686b0c32c4fae6af8410573cfa69088fb05a3da04428890f8d6
5d177d62544de367f14d7c56c10c18d93d984e1d08bfa1857fe31180ea79d2cd
5d207afcf05d1494d2a61182cd14d5f4f15f0ede9784a32b0d8bc4be444443f8
5d47dee0e2b5a8a9dfc13f4a529232e002e0c0e30fd50b88a1e76b8fa191adf3
5d6b051294efc2ebbce61968e7882da0066275eee83e4934a9442644ef91c63f
5ded74097c2b0b601a1bb0d110ed481e60f2117847e805375e7785212a92b3b5
5e016816520d937560e51e3d6ca57c57f37487549b5062380ca847dfb2b35e97
5e122d1b8d5048f6473c603419bb26a9d9ebaae2e54597b407127d1ba931e03c
5eee4cad5e174b5c590de613dbabe754b5ffa4ab00b091a3eec938d1c2c2a2f8
5efdf03c974252721698183dd1ef74dc6b46a692ad582c37e22cf8e6c17ff0ee
5f0c779448809610692098841595741f967749549d696e7ebdcbdeddb5a632fa
5f1633ec35fde2e2b65d658215e6d4ecf3476bebb6ac7eb5f8639fc0bf60d1fe
5f265b88e1912e4bfa0c0949179d7f5d6dc0c22fcdcb790729b756b212cb693b
5f480129f6f7cdd66bcf57c7d9c68805425e8ec1aa0fcfc234d93b1465c40e25
5f4d9c6758e42c28f0982022eddab588a726fe9fedad59d3be33623443ff5867
5f777a45fda4bf4ce493c3c830e4e4fa6b080a014474a2a6a8b86a15a398a151
5fa5ade1f344d2b2f36c8e5fa7018cc3ed827250b91f33a9da958776322a2028
5fc0ade7021dafe544beaccdb795c2abf11281e906967456e14f5c5e90678bc2
5fdba9d5897a8b2b2866fee291485e03072aa9de70a841c9804854983bc6e45d
5ff59d8d21fa4b37934e1bf42c6a69bdcfd69f77b43892c8ddadb9029376c4c8
601a6c8ea3ae0ee522b310d13d8341ea08fc22a08e3cda3d2609444c0e1188a8
6087514a4d82bca85b0b2c43a77ccef9d85467703519b55d13ab834dfda7696f
60aa4f2b8fe9f8c6bdf22e4404ab0c093976e8ba7385a231d8a5221ae0524ed2
61485e5ada50ea5dda0aaf960e8a18aaea8791d415ee5fdf66edc388c814614d
615a4063b7d8e0179aa778b4f7aa3e9085e5c02f265a66dd0f6f2c77ef7e6f34
6173478917848f88ee7350783451ea5b86ef90c761c5cd59107d02569a4ccdd7
6175eedda819b013d8a4f7443729b668bf9a5b1a1d53d5fb9d6d5540d2a7d2ef
618e07edb352ae6a20c117b936e53a9557ec928d429aabd089e6da5b8536130f
6193fb66dc45fe74bdcfbc9a809e0aac39eb7a9becdf0e26f269092dfee43ff3
61e529c88c05ec01dfd1f0775f7005463afb7c4c1031cff2c56fd0270783f716
62115bc4e4ee79564c53d79b624e495041e41117facbc19c0f3fc7ab119eb31e
621e894c803baf65caf4d5f95983d5cffbce7d5473a38c719026c02cdd066b55
6240762f81f96b9e460e349ee28a4a440d076e37ff53230bf73e239527e12f2b
624dd3af1609f1586f585724c01fc10867e92a2fe1f9a311e3a6dba0984a29d9
6258a7edc1b8ab45906c64fb8a7244df29363277ee9a1a22ca822c8ec81c72c3
627c2fd41d70a8082fa82b9b9a2cb065d0f584d89e2c221e118f48317981921f
627e5fd1efdec07c7d879f12535d1b1ce935cab649a98d9c2bfb448ed77fb6ea
62fc5aa49911df94580cbab2ba855b706183084389861c5dd07c0bd87859dd5e
6300e10dcfe891ba4a67322c074de32ac5402280ffb582dbadc4b71b5f81f5a0
63200936d9f2f972f43d078939cc620151bfac3009a2cca583cd4cd1ca00206d
63d8dbd8aa0fb22cd7972373b4f36dba5dff48e78bc3acc5fd33ff42096e95d4
63df55de811bfa1425ccd9d45722427fa6d752678f5421358c69b88565fb02f0
649d4dba5aa2b38493f753cb78d63411b17a70cb067e4efd9df43926561dc35b
64aa5b058bdf6c6007343c7f35648686a5cc0a810ca419384cab25f8a0dc7358
651bdfeb9ab1626c43318a2029d114ed150c7dd02c3dde5d619606706560693e
654ef6fdbddc12fdf9c3e93ee5d40287d26e7c81ffd9cc3161bb0c911863bb23
657cd4d68c6a6e51740f32894b11446e720096f954c4faa6b7bdb708e4b8e215
657fcfcf2c9486b4e17e79898fad133272dc935506b10598c5b5d28c4689cb91
65c0770a74911a91ef40e91f0b21c12c455f7f9e259d717dfb12669dc3e9ecdb
662afdadbe919fd315e6f8b6d9bf58fc1c5da6e37e102aa0892b072b0f6ef24a
66764ae1ee8d81a64330be1bcb4fbde4a914221300153b9ea6ebb55dfb48d921
66be8809e3295c87f791f84822562bd658edc616ea2402c2578d1ea34ca1d2b6
66f4e79210c3d45a5936b56728e1e0e8ac564ef80aa378c0ca85a5a40c3cd7f0
674809bfecd3e97384c13839f6c314f88b258b598f4f2d2c4a1616d17039e3fe
67eaae82b626e9457011f40e569b251abf77dbf97ce8d2a45ca7755627148bf0
684371afe2327ef5db7edf3646f07bf4b06cc07e5247fa20b01aee30013d84e0
689b05fcda2209feedbd412cf9a7d53c23a123829ceb7117e9297a0c9c1dd232
68b138d5381a4e859efdb686a3b57f337e9bdc7887c69355816067b1f56071ab
68b82cddb5bf7440da803d0049ccf34c9c3c47eb43eb6a2626d71dbc260d0a40
68f33307b68ab28be13a2399b0207989c6d474c6781fd4cae4969707be2bfb33
6914c0d4cd20ff55491a0ee398c01b8586f83b9ede0d120bf800b0d1e1cbee78
696114777e1b71e8fe6d7c0c34a120ca91c2f76e0f6c502e5e4061ea6cc83fac
69a54d88d186dc48e36e9b440ebc84c963628dcd87293f088a5e781bc60a6ab0
69c6afb61a83a0453b8cbd27d52b38f33637b041fc716aadf7f0ed174d6bda73
6a1e23b8e5a87c654553f5572b6c6f92d1257fb52535ef8ab276902ab95e6e5f
6a3c5c5a52ffe530fbe299b21f2f450cc894057c755133675e5536288ad8703b
6a48172d566ad4d6d094d848485db064dfcf961b3804d66d6a79f9f19b3533b2
6aade1652e395f5bf6bea1fe71433edef454ccf7626cfe30fc3c2f7dcec33861
6aadee1a2b887e1b977b61631590aff68577c5aee59cd9a886fe65082bd4b826
6b10c1fe41b5ce623670a344c3997c8fecaba5a53849c27a3a4aadcce54ce595
6b26fb64cdaf118be20f5b6145c920c892af811c3be32a7c46f98ad24fb8533d
6b5f7306f738e978f39e8a9d84d021986a1310b0903d6bc62b6e533821ef9fec
6b68f365efdfba193d312ca07ea8180b005d2b47c49288b6e84bbca162a3984c
6b799285302eff5e63a8dae867b10037a58ecdf3c087ade0ed1423bc3688c83a
6b8990da20ed50ce50b888058f9069f60ed9a6eaf9a2067c4e522add9aa25d0e
6c63c925562bfd442f77249eb1272cf4c74d6fdc135984e69a3b99410958625d
6c738efde95df4b59818431d469da8b59a7ddfe9929ec149fe5f45b4daba1aa0
6d5260f67e63fa05835c1dfafa1dc17c6da84c229bd2f10c49ef382c57c38cdd
6d9611b3ec18af457b767889d0dabd691df4c08984245e6ce1a6402aade3b79f
6dbf912a6b9f7c7a9f5a99ec4e932ee9d1ccb95fa3d1d7b531cae1d7fba6ef3c
6e2e94eeb33dab8bf1f90b93bda251d33fb5f9a74761998296ceff2fac7a888f
6e4bf9a532eca15c368df881d4a635a813debba7b63b2ca196447ef7acce7417
6e4fcadbf6a8759607c05c490225f2f0f1c3df428ddc391ae9c20add005dd23f
6e8d39bbf487e16e2bf3a61f8cc6eab3c2c08804a3ba15fe19285272c5085b04
6e9263f3eec7bc935ad5f2289c9e5d716ee71c6ad8788da55189ff26f11a5996
6e991d55f5c5e5439776bc43ff33748c6aaf2421e306e6490a7ea62f303d1056
6ecc1f1c9b309c70405a0b0ce6a6e5bac6a70fe602093ef0c255ee4e107cbb6e
6f1ce777304853774f74074553937d5b787c4fa822715c16eb3a8084039c859c
6f2021690ac90458faf81434c25389dc5f62229e5dcc9c0354aa53f1186e929e
6f39faea9b8c0a11ff7c40a8562ad1b7ae53b7c2bd992a09f5a1411dd9de8fa4
6f73d675473de6ea8767f1ba0df8c3ececa16ac2fcf56db3a705f0a93b992b7d
6fab6352925380eb1ce2c36f4e6bafe6322b778105eb1bafc04120aab747be49
6fc9b2cbddc8148ad838dce6ca9989ecead7e058be929c2ba4a91b755cae74c3
6fe00af5ce91b501543ed22edaf2798ddf6e40d866eb03f0dd79a95b45893294
70031c31f086603b99f865b17570dbac74726685607e0b3a3b64552cf32d05d9
7067de5a552df0af40aa42d363d7c09c8be5d517819ce44fb2680b699f0d53ef
707bfd90d6896aa063f9d4b10479d5e76c5bb29ae1894d4bccb1e25c79edf89e
7082be3ee50b12c836ed06038977526e7bb0148df35fbd52660d30b1082ba7bc
7085d0f364bad25c3eeac149530d67440a0e63439f9d41fe7546be9c8851f63f
70a98412bdaf732d354960897816524dede3c4c3ae50e8e18e53b2a367dc4791
715135e7e8d00fc9d00ea2612d4eca72e817f7dbb665912c31359e6afb5b572c
715ce765a96465232d9b45f26838775cf63ec3318c39b88687aabd8505d11714
71884c527af2204fce6bb711a49f996d0b1db336aec3f05e3d9a38220cfedce3
71c328babd4b3a81a8b0642165aec19238b6f35c41f1489c51c49f27c8e7a776
71c3fbbfbd8b2b23915a0445cbf87bf266a222f7352807026404e8ec1f80abea
720440c92f0df79d72ae0329ab9219509c7927de08f5f734794695cfebbeac23
72431642952c88571782befef8650ca9ac3aeaee83a77b026a524f4fe7752aec
724c7ac35ba053a7a0f130200e7ce854d73f955152d6858f99259b9282461412
72a62e467eecffe14925718a91abf98d077d47cec725626778e765bfe73ed538
733c0de9bb34380988d22095b3d0faa61075e16c3c4796e24d57fa0fd63e45e7
736189d6b69b9a26484837924da7d770d8c48cea209b7c0bc53b0d3fce7b963e
737c8711851a9068066987cf85c8662226fdf16db7d5d8e052ea6e9ec2775cac
73cbfb16f8e1bdcc0c8b716ea7281b0f9035229390adea88fff9de98bb79ca0d
73d3b98929c7ed0c0333aaa93fdd1f425b4d6f817fa20e92fa23b6be7a17ce23
74194bae898bd621a9eb8beadf5e08b3d1df187059c6139e19d14d255581e960
7433a9212adb1531d3c2408e57dbc1eb6d7406d8ff38d2604adba3ddb15d59b0
74366af05a83733146a488720785301c07da94ff7c5417269832e248f2ef0115
743f07115fb19525f6d1081eed8289fbf92bc32d29d749cdde309e62881a860c
744b058002ae4148ccd7da663f62a3caf59017c840d95eb411e43a88cdba4661
7464c4422d42134e1d58a78411eb26908c4b57350b0d703c374cfd728a473ea3
746fbf680b6855be73905bf04c9695fc627302cd8b8d49e17db3ea9d2a2b6a7c
748e4e806c84f4afa5470602e7578001f00d0d7a5f5cb53041c35b9d81818f55
74c8f920a7a158cb3f6d4e33cf783a073d4675b64e3172b2b93bdfe660b97b29
74df454e9f60df58541764f559cc4e85b0a564418677b4f86c6e8dc9d5b64903
7505b34219912d50c333ea0dbee8a3915ff2f24e08545805e57242ffa0d3b150
754b5e7da743219e6c7ba19ab28d6f7e85291fc7db9369c9b841c5be02ecbccc
755b5f54073f64c02f29b2b3079d821b0606347df37e8acad6f22d5348875445
75c3dc1af5e60c30dd481af7d5e0a3976ae8732f3ca8c09784bb345f58d85b54
75e5d4f1c49af1566ef37f68dc9236da7ef8b3eb48632ca3eeb4f8faacaeafa4
7615f30e3cb5665002d9b0cd52975da934cf196d12046f0042bbf39e03c215de
7655ccda365652a4f39a9c7e4b018bf0b65320ac0a83315cec7542fe6a67b030
76e4aa1b6d2ca68b43d031ae7b5da9daac32daf2c0dc13c7075f9c0d0cc5e62e
771f2e865dce129b2c866f8ece834fc57299131a9ea8127c58628e93aefdb41e
777e78eb36ca02ff21469f52e3a54aa3d27e51938bb40967ee22380e9deef7eb
77b0c51e19f8dae77aa58443e664c4de4cb6fe00789d140b2526208a4689e908
77c682fc9c3e1f0b299d27cdbd0a606a95dcd8b64c83d82639ae3d8246afcddd
78180a5d648b640471b8964b641d758f0f3f22147b24548d3fa0d28df9ad2137
78947234190c98c63a99873726ea149f1256823bf1572a0b2fba67f57fe29671
78afae539381433e2805e3012120a552a6f6fa1fcc8dc756ba7f18c7c41d5c20
78b02fd471a2441cc38836a1bfadcaaf78a81127d961ea08d4e02b0e566a8807
78c722bfccd68057dfc88595d25f95d98836f7f2ca6e54d0b7b4ba931612db7e
79747b95961bdf8d9efee2e99023d9867591c8b223dbedf841dd050be7748084
7974e9eb5d1bbea87ef884d801699aa4e4ae4ce984704d30f18efa80a4844cb1
797da211143d2ce2ed620afcdd23ebadf73f17f90e2d8724dcd99d114303a1b0
79be76056c31aad7e079041cca0c546da39d50bc2e6a795ce7dbf2542fe0f6db
7a039460d488c46aab1a70265bf960d045684b20ff94c89e65a284f6a788a069
7ad12b0c2420981d1bcf80c8463d84459a545640ba3f62bcb6b8a182ae20ec4c
7af1dfdb936fbe4c2781a1fb70219fa50e1a2685a1044863f8732400cf604886
7b5816af21153306bb3b0d19fa8f1b0f5aefbd9243d74a31602385ef7e290631
7b7e18308212a8b903a586962440dda2064aa6cad6047bc9f3330510d0b976c0
7b8ad5d2420b1c7d41b678313a7460f7d3ec06890fbea4ded708c2cf09e0ef3c
7ba84b57db28dcd3757ae13ee1c5ad77a194ae45575e72491bdcd8118babe0f4
7c35073df8063157b7abe8a6db8856bac6ceca86fcf02b3fd529536fdf2d9fec
7c442f3fa7d8f102918cdee395eb4e7b2e3e069ddd10b41c891246b92d1bed80
7c60939f1ca7243e6ba4c1a33c28e1ffc0671844e555ba533cbbee47e1f53404
7d0e5ba4185b397585629af8a22b2a14867b22b5dca46decd7bb2401b42496ea
7db7fe8dd4f97b899d7b2bb97927aaaaee032d0b6b5fd6d26c2d6696d6355951
7dd7b5d5a34aaa99113ee471330d9dbab4d2555c8559ae3348c3ccbeead7267e
7df09a095f85d094b639de3c81e334bb980779cb9fe3226bdf55793b04e7ae11
7e0e714e6daabd236eee4cf1670f4b992c10d4fd557d577eb99c36340534286a
7e20cece610a787fe9eeda992f98caca95bed0f50e763f3aa7573edb7f94ba75
7e47cb0407c1a9b99e132c457c83b3b8ab6f2a8e724054f11f5d08eb8d6fa138
7e7a575fac0d691feb7039c21c4befac145e898c05da2736ae4670b45ee85a5d
7efc9abed4cdbcd0408db46c954b6e02f9cfe82875d6cf70e22945ef92ab40a6
7f22fdeaba20f957c36779500de1d21343b48e2b5140dae17e00e136b9c96042
7f4e042d89220efa3474821be2858243aeb116a8b3cf7eb47fc381217b7677a6
7fd7d8a4e4e9adfc529a535a4ba752a1dd086cfabe72ff4a6fe31216087376b8
7fdbcbd8e86685abc0ed579c4552ff4532c41a88c59d155806994e215188e349
7fdc845107382b3a0bb5a1bba86d36a5b2b290701ced9090212646ac145b5292
8066cf1dfefd44fa5b9443b54a56c0fdf54570c33b500aa024967f94882a91cc
806843b01b068bd4f74016fac29103647579dadba09e42cdf3aa3ad7c945c16d
8073b2e5a71f046f732a9392a4f13a616c12bf6763d4d103645a7b1846597f80
808eb72ac539eb12f1f5f48f03952d065b5e6c25e0520146189653c38c92e854
809aa7591de0941830e981b8c9c710bd1a282e6004b86c491cd02da78d8f31e2
80d0ef4aa763e32f9896b42993cccfc06ccf81db03b3293873acd8d6bad989e0
8123776f7e45da928474fc8c3e198c514768904b30b742275e91c2930da7d5fe
8168b12f3c5cdae76ae24d2ffd38346a339f5875ed7452bc615458f3f4f0dc8c
8185b33db18dfbd29d353b984eccb8297e5efcf4f0f5ff6b3a8f073bc0ff9e91
81952d224a521c30bcabc3ab4c9ace9622dd1d68729999e38bfec973d51df251
81a7c90557884060ded4abe310507b9c6f8b913d4397c1e42a392f2d88fba6e9
81a9c355cc8d339c2bfd7738ef9b314f32a33a4ddbf44fc08791704858038dd7
82498bb8d0ee08ba98e156fe82513c511962eb49843bb37c370867aff2aee6c0
824ff43ba8f54ee96580b0e4019a79544224e2903840fa94381ee5e5a8066049
82d9222e43e51eab5218f9110e80b3b3eb9ea5bbf3f8e460d8cdde7f6e8525be
82feaf7820f17fc21fbe3d93b6f6385a617e2e7a650a901293caac5aacb0ee1a
8328a877856b647847173826176f7fd2703abd7c11ea5ba2f7e30ea876ac67b3
836b0057bb2c42e2ba07dda3d56ce8c00c597c36c002f706cbe5c124589fa9a6
837bb8f668a4e839b85980bdf2696dff1bbacb9e61dc8600c5e1f62406f9652e
839371bbbedd7d23eba98118a0dc4ce5fcbbae7b51a12a561f0242f8709e7ee7
83f7eed71280b50a679cb69517a90b78a263fc1f758a84b116daf335f647ad9a
83fca3bd55dc63eb13d0fd8d47beab878361e1fd70f738c6c3505194f959d7f1
83fe15fc96102ac0c21e9d55a6f0c36014bcbde317d9ab2f0daedecb02cd19bc
8454883906ea11c4ecfdec7a5de8ed9d3e7f0d7fe8781aca6cf0e2cc2c386975
84808c4dd49ac177a0512f6b4f70bb4c1d87aadffe8cca35ebc7eec02e77c400
84894b3cff2ccb7a6049fb68e6a52fb4644a7f7997a6bddc24010e0806768a45
84e72b368d30e5ecb64e4889fb6c6d0339aef17ea4299c7a7e803e2e47b5ace0
84f9e6f8e4c8a8375392c97ba8356724951c58e18380ae45e5f2a49843ff475a
850042e2a55b7639b344d988dc632f43e35142ecc082ded8df62f248cb64d119
851ed5f7c3043061f046183f022a34d8d0d6ea6260b6df636de5d1efe6f7bf2d
85585163ff60d8af27ea18dd4a9168b10a7b8ae466e4f06370065cdf3a098dd1
856204d2bc090c3bf91a4961d1ea9ace7bf2f6d84c7ac6ee9a65995987dd8af8
85e230d7e55e7d0d7491447de6b0fc1e3dfe08e71569c3f1d0623c4907f80fca
85e86d336cebf861212e945b4703ec7e8095f2eccd55abcfaa42b5cb8632ea7e
86595cc38398d617a752b664d22f7db4b2259614ec71718ec9cd7b56b503787d
866f00322aa693b8646d52ce1da69ebd4dafec4d937c606f9f5aa32e6314a564
86abe026f6f12acccdad5b58d850850ff5ce87680b36e2182a892351944eb7ef
86b8a650872d67c3ca98180417d40234161d964ddcf958191f140c659d7aeb75
86be81057a9418aabe24452e1f9aa7702daac04d6e132670eb9c9a5640609156
87a4f4e1cd6c26a7724692c45eb3598c4606c9d03a8797102144b8bc1bb2c978
882472fe165058893691d24224eb1d64313d310085c8552256f508dede00427b
88871300f3b9311b56ef88bf5caab599a308e0c20f429ed6a6fac9a4be220815
88ac5c9cc087eeb6b736b5afa9abb103d2405a975f8c5a633e81fec27664f0c7
88ee2af9386c29aa016a04818bc97c6473c02ec490638b64e9d6345d70c566c9
88f215d508af881d7017262a8032ae4a0b35847544677522ec7c84d8ee406190
88fb4b5e09d4b9b9f90606e88e90c300c12842d4480f82b0d2ba39d9e6d1a2c9
890f14f33ca7c2e664a0e794294563050a1bafe9a3a7aa723d587b1a66432044
890f588c65dc24cf365e704e910de0703450d3b2d21d2a47088cfbff1dbb89e1
891842f6c892cf5992a81698d443403e668a2ff87534378e48cfff803ac32bc4
89194224bc6241b4bea073d9c5be250c178e2a502627f677c6555f9c1f8adf8a
89d58467e23b16f34bf180d72296d50104f9e2d3391179883dd664c1b23776e2
89db82296f96335aa922eee76dcdeb6d4b43659f869a95b2e0883a54ca8cd750
8a250f7d86b8b496c7d455f7e0a486eccc6f614e671c0580823d4c347b1e6f9c
8aa302982705b6d1dc4f05fb2dd5d7925dcb0a310d6de0c5c46f31a2f64a9ccc
8ac19d785f33f6bdd1c57463d28d10b7c682f6ecd513ac4ad44c0866408d51c1
8ac1e36c05d18109239cad11aebebe1fd84c533f02831c705274c5d7bfe7d710
8b0bb11383853deb90fe48b0345af39f3264fca242ae36685395e313de9fff0d
8b2725257f99fd732fb9abdece238b2a0dd940299978e4fc816c7f76091bdcbd
8b586eb3abed3104c46127bb62abfc45e00f7f340f4c2b85165813fb1c831bf4
8bd5aca8c81cd8058db50958cde6c6e5efcebcb88f0440c99f70b1cdbb2b0c4d
8bdbe6bbdc36275325857c7fab57b9e7a1bfeb1435931eee721d6a21ba606ef6
8c4429bc830963dc3b6a535de37d49b6eae3dbcce0a3f76d88e462098d92211d
8c5abc51d62bf13c7f3c0cd748e45c73e1b6981824655fdf220646491d3687ce
8c76fbd80ec291bd81b57da0eadca9d03e3ddc11549a09584e74fae3f4195599
8c7fa5e33278e7cf23f3a1c2e129b1c91e2e9bb4d3c5c2d1908f46305260ebf0
8ca2101047362ac367ca21fcad6e438bc3bb0cc870b6edbecdd8e50068dc3808
8cb285b04392467e3f4679ebdf73865ef3caf67424e480b46fff36e0854fee7a
8d47a91464490b13305d009e789b4f97eda17d5bd0523f631d469dc8ca85705c
8d5ac1254c45cb5521b8b3c677a0ee54546e1dc0b09f6246052d68151ca5427f
8d96e321eeb6f9b750dbf69a3b811e2d0fbb36a10aa61d53cd708cf7c1ce6004
8dfeae038c80d64371d1878ef845d7e9dda31fb9304a7ee74c45a899d18dad23
8e74897eb45dab759ddc4a88489d00afcf10707f3bdd4a99f3b4af617e4a2e60
8ede3a9b17999cca0b3fa2622c0c3287ef09ff89437e6051b37c1944435c4839
8efd215467da9074b253478ab8a821eba638de13450bd8a402495e9be8cdff43
8f0718945dcb670f806e1115cdacd7e0cd64201de2ec68fd19909597db9e88b8
8f14adc9b0547432d7327e69abd1f046010dc5bb3786f8e36245c1a812b77543
8f4620522fb17a618ba36bc575bccdae4032f7a57d8f866159306510e4a50889
8f5441be8b5b023121a2b4f2f22404d0046dceb9caaa6bc9376f0b94b77fab61
8fc86f1adb4b60f7fcc0f70d1356bc3d79d7275bf40fd67c6a039a37b679fe32
900e58ed57766fe852ea0e5f2635fa90413dcc36e97d3849dfb408b32a2e54c9
90e7e41b160c65da0ba073e2fa56e482e7823c0e29b7952f1785cc053599b533
90e81c745a29854b039d84d7f27b57a76b8926a2b332a34408feff32fa13befb
90e8f615ad0cf4bca521d6cd44baa2395f2d3c3029a046d9eaf3c6d19b4a412b
910d36aa22a92df9a17c5d6d788335f5ee1128634146c268a1618d8000e389fc
910f52857e16172c63ea8cb26f8885436635eda1b993c2fe20d93d1e46ac6bb3
916c999229f62ae839aafd28785e5f08f0fd473599f145ac7265d7fc251dca98
916d17f0f1a11719a9d265e8ebd53c70dffd149f2645fd4f94ab49eba66875ef
91c4d5c8988228ccb93ef7e54fc3d19cc8fca7eb3ecd7cd369d65c8ee26e02b4
91cf485b70a0d166587d2678786680eb85fc1b7c4ff80c1023dff4d538493685
91df8de4dc172693d31139772e79b9af054d37a2f99ee49b4d08f7b1278eb328
927cf99a20480d63d6bbdedd07d8daac48174f79d069919860863888d14c303a
93709763b6c017db8cbd04ba89d6b591c29c21e8f2e24aad2ffbe6125b396d9b
93cb042bb34b4bcacb0fc602247ca7dd215a4d88985327ba084bf84d32d6afbc
93e334cbea75b8ff7c57daebd13ea2f9ce65f45d23c4daeddba1b435bed09350
93f2c34812f928bd07d04703eb79d40ec7dfa7ec04b7d834a8c9e2f0c5b705e8
941b6309e95a11a007117bf10d23c364e958f3d98d3903574f64b29179f6c23e
94a74df38921ae265be96e7a01758b9769f6267ed1d6e688dd6e5f07282c77b7
94b8cb884d096d646ad09537522d98cdec6b04130cd21996a0066c7adef4b66e
951edc7b57c345f21ccf34173ab140f566d307b0749c6d173af8abb1fe74c361
9552bbceb4953d23046f3607b4f81d832a52d4426fb21a12eed268dde2756e09
956baf0240928f5ac7576481bfb24926e8784b2e85ba337e1569a6238c915e16
9591bbabeeaaa7dd278af7bebf6a7f6adf0dc999bb7b67f83b0547396667cb85
95fe14a879ab45ce295005992c355a6ae24e43b64900dd97031c8deb1dd67ae2
9612feea76cfa5571afaa3b8727b2716664daf3845aa1b6edc165c5bf66c8123
9656c9210cc8d7911ca9e0b165ae68264d91161463aabed2400a97270e82801f
965fa50e018ab6f4abb72880b218a30fa177e5df47b15a476d22e3c7aacf2729
9696992249a5a798452bc894cb260a0c4118250a4777a76af6be715873fa7088
96a5c9dad320617425fba026e3570eaaa9c9fc11aa48a9b1155d35fbdbfdc316
96a9cf937e451026514b5ea974bbb444575c69bb6f0c41d0c2a4f4c86a0d2a22
970bc39d3ed77b74319e1339f8d09f2dd021e083fdb64c8f24ef2264355140d0
97357f2192884ee815f7a00aa4c0bec63f98507633bc905e23e742783976a169
97b076f87442cca6d589ad2d76fd124b1ec6206905b2d272f629521b39b87f8b
97ed95a681b40a7ce0124c43e5d544626e1ed4e1216429e5ce0014eacf729067
97fc173f4de0369a5b97129b9d9ff5e2732d0acc9eec20790dd81570bfa3443d
9825a152972fcfd25fd9aaa71a27c705f7fcedf29c7ac374c705c56ee7d77eb8
988b7e5ad702156d05781ce8f0efbf2638862f41c80d2422638c996c7f7bb900
988caa47fda4b8de3b5ee97644531e3d30f5db3f7f2808a3f260ce20c11dcee7
98e2e7c61d7e664342e936d3ce02908988494fd8ea5365b5dfc21457657a6e6a
98f7b0d7b22d5336d0065689fa2bdaf21deb0d0d8c567472e2d4ea7c142f22b1
993183b897cbf1b632f029cb914c8b8f825f04949fbf77b886730664d7be9c52
999a8cf63e071817b46053e2e76d69c22d37534f842bc7fa64ff68280ad06a56
99e0fc74f1a58d59bb9e8e16e5eb15e9a6948e7a887209b7b484e7c81edb7d5f
9a3f86487e85889e819e9a6d06f56b721ee94b3dcbf345be609a64215ad1942f
9a6f7d6e73e3907bc99f6c863e08da9a79a9e4e79fe994c3263a3b2a491eee1d
9aae167b28d59bdbb6826015c456ac9c91e4e49f3d3fe09652560daf4ab3e280
9acc96a21fd60ebb63a2ac8a1911145083ee7d36afd7db2dd40c3c0573dc0c2c
9ad378e6dab4ce5c3b5d073df4b2bd0aedf4c36745248e97bdff40a89e339975
9ad712113e32b3db2bff31f9206a3fadeaed1e34fabd0514191cd077c1714bc0
9adcea6bd29275945a2f1fb7ef840d0f39eda998ca03f1026b476cd4b30b7967
9aec02510851f2bbb753649453cacb8a25ee824245a79c5960a9757bfdb3bec9
9b085b49955c82cc7509e9d789aba8c691f0afd2cf36f14b0ac0c62286db31a5
9b360030653c9d1263ab6b485e331f2d92ce7d08709eb62a9f42d0647d9af14f
9b6891f00f58d608803f00e49585320532846c02a750ca63fbf66e4828baef02
9b80553e87a93f7a9993db00f05e96f0d8c730492a01e8b3c2920b774aa159b8
9c75223f4aa417de679aa1c8162a6883648299fcc97c82efbb2cfd70935dcf2a
9cd6193d793c46e269cff061677ab4edb52f444180b569133714c874531a3398
9cec3adc48adf09de57969ad25553394c5f4bc5c3bac97b8f22a7613d1db8dd2
9cffd5f32bed9fd230fa82d8be2aa2850887012b129544d2bd4e0ec45b5c5f63
9d01d0d24ed2a7ad71bc2510c282273f2accf94d2764e9b4a38a2bf640599eeb
9d5fe0a696eeb4d0ed1197392f8fb0c77099d773be886c84319660400c7b0d28
9d79117002e289445ce3d652d50d0c8b522a8a2d502a8e701fa597b9738052f1
9d9e0f5c848679aae663bf64d8f17203e9af836edf426c4ac2748a9b55744cb1
9e0edbbb860216014cf99d4533a6a2d13475e445d357685a823bea89a5bbfe0c
9e741153bc09765f06f80e1e98244da02335c3fce37844282d062da705d3c73f
9e861941ad8bf5bcb649e5fde92d712528200a216018c2437371498e6ab7683d
9ea6afe6a59f119462f689ddd12cd018c6c34a122f39ad9d4b4a831490f58706
9ea9f84decfbf8ae57960c1ff4be4e1481193476c6553548dea9587e5cb86820
9eebe1e1e63ffb141d8cac9171aa294ecbd2d95bcf6f86700c4855ddcffa939c
9f1fa72f11bd1b328d71f7f07a7db6ba1b63ab6a2400dfb8f74ff5c951305015
9f5b1e4361791decf1a60b16ffcd64000f617acaf21034e1d011287bc36527c0
9f7134a31a4304c77bd3ccd0a76e264cefb3bc262bc29a8c80e3a9e461504bfc
9f7d2767d8ef7e631509eda5702e2d1ee1b07f124496228a09b891ce1c313b01
a0257efad09dca81490ef494c92215a76d29974650a84cef7a2fbb76abb16e57
a045331ba4eaf6979457b9a87cea23871b2ceb9aadc221812f733e4e44ceb670
a04710435d1f8b6bf2e0fc4fdbfe9412125da32dd6af3bcaaeabde01dd8c385e
a067d08632653775252f64f2b719fdc25644b386077172c8018f5c786598d031
a0761138c8abc33f4091d466debe23170cb43a6620595636cf0172c850f15aac
a08bb641c0baac9b700d726ccf3e0ec15ccf5999d7b978df51857c8a247b3480
a08e6170b409d302e304311f6a74a22c1819771e9302dd843d5407dbf64743f1
a0a0096a142a60b6d88c03270d2cafa00fc940ab00d619fdff25c279a98d8520
a0a1c653153088fa6834e3bf8acf0d27ff054286360d84be77bafb7098fc8a0a
a0a85a6c6bc6e28062a6e69b841251981df14a4a069073882d60f810c45f1887
a0c02ff75b4d1833fb108aeae1f0017e6799ce7c011bb6d5b001f7b15e2e074d
a14cc586fabab50058e9d4ec6fe8110a4fb662fe27461f0d4b16e1f7de30ad55
a157ccfef854550172d8813cb76b7822f2ddbcc35f34123547a615052f8cf253
a21804409ca875c1e8ff4113b913e5f3cdab668c47b45b76e99bd9dd33fbfd02
a222a7b28c3acf9453d249a1d528f1fd1dbe5652c4a82b9506cab9974f3fade5
a236272b59e64aae7e0102d3769c1fe58816db890a426b83eb4daa215e56545c
a26120a8faab205f5293860d775b61625756c1bce54e9789d873a41b106a2f20
a27d1ae2c9cefef7f9f2ec57799ef07055a856f0a7160af659318b45c3684db4
a2e0fd5d3616a564569b0972c93556fc9a61ab341273c3ac0dabae060104b5ba
a2f6b6cdfde65b22958098ff1b9fa5a30388c9192a5c23196597c7967295baf3
a31888ea7690544a149d8559397f6dd31516d00bda85fd03848eec342c22794c
a3401923d6f7ef6c2684af1af0f9e9e35d04c8d69ddd8d455aa672e33c43c4cc
a34e72b7993db3c68fd99736ae4a753710b832718373c4a67681050d1eb1aea0
a35414311f67e2cc450d02eb5a3a71955e0a6b2561b7af64fc35d42e15272b60
a38d914a9c2a90dccf40eb7da65975c9991dc9fe8b73b33537e1ce28f546b67c
a3a4594d9d3dd4e403e6fd7125e98cabf6a0865002eae1839f2c4b6828414c12
a3c8bd385a7588ee242167606655a85a766f46bc1b3f84de5aebe59726b544f9
a40060ac707c133eeb8c508d944422f277f05f316316f06827e0d53f8806dd46
a45913ea60cb87e88300df2e0ea39f5dee7af778716013aa80df90e4057143d1
a468285f70fd4f3f31e3f6a6b29918072a56b29cc64cd27285dd482aeafa5bf6
a46b7cccab34ff74112bf4bd7dc160a57a5aef1ce5aa573dcae2debd70e50621
a4842afde30df2c8f73757ced2eeb5445590807218e7288278a917e8c54fc546
a4dbee2f3cc70c199bcb69c9cf3af095166f088aa8ebf47cfb5f2cb365a330cd
a52391e50eb3bc61615b631967d0427ab6975238aa79e8b594722326a43d5cc4
a536af737e6a73329e7c94178d8c4dc0dc6e53a6fae21d9affc7c7135423cafb
a537c6d6be9078c30038f24d3572a503f8adbdebac39751986eadde6b2789fa0
a55b16c642ad65f4d784ac15e3276c6df7e6af75210eb621a95409c505013b00
a5def2872003d9eb94099240f3df83d64e88794e4be43b063240b567626682fc
a6863a3c43ead06b0e7d83f6f3fdf5660470a3d39b9b8c9d6f79222f1b5c42c1
a6982c7f14bb4ff24540d84d9d8c9d096c4785ac0a0b8baaf42fc87326ce167d
a6a48ecd013c83ae57a1776b2d3f0f004cbd673c4b916ce523d3a9c3fc0d478f
a71eea3604bdc0f592b6008c08425b5b32abc120fcbcbda9658dfd4c3851662a
a7543571687d64da46ab1ac3535fd609d8029f1cd1b5364158acdaa938f6fcfc
a75ddca4a27755bd93d7bb400a8d69909ae5bf65661ca2194b81b03ee9ec050a
a75fb45f8af0255188485b3dd7bfd3ad979ed98d2fdb61eb7c1c33ac1d457a5f
a77295e682c4fbf965b62aca670afa134925733eac33c32f5ee6ccc3eb7be30f
a77318f34fe2684088b50b571d94d9e650ce66de8d1f3e229bee57fc49667d4b
a77fb81d946f679bf9c224e46567dc93b55e0c2b0f574f833378c1023cecc300
a798554e62b1358447440c428c05c6a5549625f374b18aeb411635766c272d33
a79b2ebabd93ed5dbfdc1da7757a0bf743b0f512053b58fbc8e5b7ee7123f8de
a7d6e235df9fe80c39df44cabf41a5100303d8bd3dbf49b3b35d9be4d1a1563e
a7ed1e4e74627cb7518d48f986ac78789cd755c9694658a77d73983d9db16efa
a815a8044bbdf7021f9cb39b964674737f44ecf2a419a5be16f0ce0918bf35e3
a83862abe874e0f5e3bf9844f436af89bffc3fd929fe4e1b462ff9682cec1383
a83cdf81e54de42355586486d99d51f3cdc5d823f9166cbcbb5312247266d95a
a84ad45683dc8d044a820ea1d1430f0e1d8847b784155b97b240ec940ef93e12
a8b64c07c6bd0b82b25fc061e812a886816f48fecc4d50961f9bd1f78982f655
a8c46c4be88ac6b00a78cb5ea9b225ea5ac2db8d309bfc766ae93a0e5596f107
a8c8436640a97ffde283c4b5043e79edd022f89710e12599ae55ad5281b6f847
a8d2cc25b929dbfd87d2da9009f8e0d524506a2ec9bd395ccba12e5b5cd6d035
a8ec86fd1d7d7cbac2e176bdec6a4e4c44124001dcf02639ac8941c6a9770030
a942c846892e4ee67f9d1222dae8bf6af9c701f2e3f553ab0079097e67958b8b
a95437492a58c72b034f5555a69acb6b9457c390d96adf274025ba1af3f8f261
a9ae433887918120d3b5d1479278799331ead42fc45fb4668239cc5937eed65b
a9b15ecbf00096e461f6e98ac59fb04461286fe40fb0e720be549c94896189a1
a9c9adfe3a692ebc5676f27e2f3b294880eeddeef1fda6406fb039998b3fc2d4
a9cd5b1d264dae850b3dbf5fe385a456c605fc70324001c88421ffbec5234049
a9f5fce074c56c624533c008c86e5ecb9b1a2cca2dcf37411c4efb5a0d1dbbfe
aa1eb9891f62481a750da9b3d96d87ce9e0935b9668e647ce82e8b675fb0395d
aa584be123c7fe1de865a2f93e602a9611b493dfccd105e33967223637e0c0ad
aa77b5ba4d79d0c9b07091dc3432a364d0d3d3691f60060e84c4eb08a9b97516
aa992155eaeab813fdfad5ad7edbaeb8ca1a65877222b45c25c75cfa8b6d5e94
aaa1cf272e6137461afcc0cf57aadf81cccfeca317dacc2acfb912fd16a3133a
aaf2359e32dd40abaed16d819042d0ebf60092866b991c938c05bf44a5a234a1
ab676465feaa3bfbca4a68edc268b579388d98ecd4c381965156f31c8a11d603
ab73d4bfb50d877ffdc71c3f855b2f77db27ec448fb73f5e16c77a6aa30b386c
abd67856995c4e21abd6eb503ff8a267d0726f7d747f46a2a84ee4dfe57a5a2e
abd95947eb183820607fbae552a3aaf3ec45b00659dd67550b2b4a8a73440867
ac0bb0cd65d6999cecf8425511b43c256bd9951296420696f70c2a87cb757ff6
ac13a8b534df4a28d0fbbbb64cff909b5b1e8faddb4e5ea57bb8a642ce998f66
ac1cc1ae04c271f06ecc08c3ee870a436dcc9e88547d46e9b48d06fcddd0289c
ac209bb75ecf5d4c84676f150474266bb3dbcf785991dfd4997e880984e0aec2
ac5878299e11b5eb3edfa463061729d174722759ed8b5fa4ae7df846301161d4
ac98aef9c93bb8c2a226970a13a0356708e7bfaf2b8b3691b854f2ad52c21f83
acab8e86333627e95041ee69bfba4a341cb175c9f0828a3018c243840a1df37f
acb14166489913bcfa1981c93e05e2d0f411cf0effa860455212f1e96b60d8e4
acc9489c9f19693c588ff868259a1533f645cfac20f6415ee5ac8a9ae07b5286
accd756f8637eb55fc6c3f7b09f5a9535ccbb2a95ea204e4228bc356b9d030e4
ad1d78b097c577b042412d979b58a3db9255a9ade74467e7870eb6e3f122b8fc
ad727429649952ad477b1f7522b903b28f0e08f02608e36d0cacf854da74efc4
ad73b9d479870115c7eb68fa4fe78d6cd9dd94cad8aa91a4613d9f69eb98ac65
ad88320707d93697afc9b09166deb7f0884bad49af787d557d2aa23403798cc2
adb7ddedfcd9bdaa38e7a6f438d0b81fa98bfff963e414773725845a2c164486
add628230ac59407480f7c527de3b92f481e40f452dee3b50e0adb035e50154e
add79f62e6df01b56d0cf870de0330da9bb073392eb5c4a1957624959776fd35
aecbcb4570809a27ae21349783b2ca67e76d570d16044ef27d1908a6598e4e60
af135ffdd73808ec58d9725bfb7ca0e3c941bee85f4c0bd9c476c46d8bc70374
afe66ffe762ef247e9042e92761f895646a9a609dfc4f5c73a2fac96d5e58f2d
b00417755af86a9bd3ff84f4c8cce66e3eb7750c7e0fd2bea877341cf98acebb
b00817892b64efb8c1136580056de858b7a572988ae1d78e7fc6c7f364ef19e4
b046fcf172aa69b3691a6db5e8a015825beb6b4b5dfd7a782243ad76eb360c6b
b055e3a6947a2cac29782c0a19cd3f4e3ffbefab5e104f4e2b2225d5ac4bfaf2
b0942022159de90c288e007306f4984b0882a32c55bcd98739acfe9de0e78099
b0b0ad38f9e74760434a7e54e03f59151c7a38120d54ec717888a22187b9f51e
b0beabf47762f6af49d7eb1f34b295fa87c5cf2e4b768da8dea78ff16c1255d5
b16c0ebb70dedca74a40c844e0cbd407aa8d7da798fbfc2b24d6ae94b6fd8202
b17151c26d7b12419973973bad2bd3fbd44b2c87102bd8e2cc92cf01a0e31246
b17bd50fd96386166337c22f65f10c5250e63aa2fd37b597e524f2ef670bbbb4
b17e0c2e2fdf8e7d41fcb794979e9cea1ba051b90f55246e70b296532cbc8bab
b18f7b16dac3ea61fd0c163edea62ef7b6c767b245d98438e8f21efa37c63505
b1c1165b9f569395de48b220a7aa24129bc651be330c6cc4037bd4e1c67b7350
b1ccbbe78f3242c57e0bd3cbe0e6f952cb86a75beb6bdb9abb98e73a18eb266f
b217feae40e8657a677e5044e632d736c05f6fa62c145bab17dc7b8bcd92e642
b21f8fb0cad538a1bc23c861f0aa260bfdcf1539b6323ab2d9ff3c7fc95478fb
b2681b09ef2ddbc8d2cd1f0750be8a85e4c589f2444cbde3cb2a660fab8e0ba8
b28c16cd354df721d443d1e2ceb5a01c4b818d7a652f65eb5c825ac1a721e44f
b28e7107f06b8a4b654516f4cbddd92438e3825362ba9a404cf3efe5ca145a03
b2b817b0f6fce25da089c2019a6e0f1f1cc9ac909e766dc7c533511bab9f7f7c
b2e63326a88bba530f3496e7c3771468666d75a7b694cc520602f2ec0d85b038
b37c956c1906df06dc0cf9b56cfa5f944c8e679b220eba62d78da706d4bc5816
b385bb84cdaedf5f07d7c74d68a8063a61a65406566c4afe98ded87951eba9ab
b3887a4cab88b216620c20830a346f3ef76f2639958bedded767cdfdffe194e9
b3ca758ad02bfce42f294ee252582e4729e7f4382481e9da51bbc3c6a4322946
b3fd3f0daa1b9780cba836b7c4245c5c183124cbb69efcf9ca7eba34f522b94b
b418a51a593b6a76184b7a0e83c27a1dc4a914e08fca0a8b574ba50034ebc100
b4204a4f8a59e31d62b1c1441ac6e2e375087f76dcb582d6d7e572b33a136f80
b42b5e76be8c4dd34fb7dd4ab59e4e230a170f999478d2de7b5d1ece7ba48b39
b4772e24c8adf894ba7ce45945616d420d9aa8d10766db1d18867c3c28b26120
b47eafc967b6c56d7e4c91bf01f2a6e65447e1e7087d6686f7622c41e537c88a
b4f20421d4c3c2e116a09c5704c1d419e32afcce2a0b31f6123c3df7e9db3bde
b504cafe579f274fec31efa9b1da69f5f054e445a11be1ac39717cf3c7eddd77
b5387bae6a469c9bd754decfc52f73cb18571fa24ac7c1bccf7e6942feb88fe6
b55e79035f441c4e7163436e3ef1ece41d0722b99af1d6483837fe0df2a4cbcb
b5abe9b049b548d7834b1f59454501a1bc644683222e235f72bb2bdc48e5a905
b5b6311e76b1bb073c8658b48846016c447db85092e782bb12c4d509f33bf685
b5da4db220c7e63bbb64c9a0fd5db5740e44f0e52513cd0c6352d71b2efa21d3
b5ec4a6db109164f6acc55025fb6218f9abad0bc01987918a020c0243bccd6e7
b6007d3dd67df15380d49a3de49db3c48ba80b675d3d1b8cbc181ce7bf23e9dd
b63f00a02e0528e6a97c68e959cf483eddbae7791ae011257c5a533e420b1fc5
b6b036e0f5c45b352cb9392d30d4fed19f20778cdf968a36836dbb0b5ae46b63
b7699ef12bb22b8687e7d270173a8ae0b2d8d36019a2a6851b66159d7e8e1c79
b83abfc61426622ee4b8a8accf5ccb83c933473d7f2edbbd7bd196cf91a3eb28
b8470795fdb829da51221542bfd92a02e0bf0e1e59fa896078c2654a1810c5dd
b87f4cd15f4a2ca68eb34a284ad97631d20fd802e50b0d96e6771954fb229588
b89422cb7374aac0c0377d8804f7a5dea6e0cc30a93c1c44bb33c52dec6ede61
b8a6a1fbf085fd41d6652a910e2dfdc80c5d36af38d3b426fb0f2b13f2cd6f31
b8d03d4feb16709e5e4e7d1fc663c565700b361008dde2973ee2f6fc417c67b7
b8d620466d4ac8a9913e42b1172b96428ed000712a628b68610c0803ee27155b
b9005865146304cddf24c4e2d85fc22a801d402130613761e50f96910bd1b589
b90d75e0724d06cbcdafed9b2cbd08c9765c2209f17fbe0652b491b7e4d66d67
b91cde838087891ad92037ab96d3641b5337e49fa9afb616b0b759d85089ea7c
b92df93e700644035cc71898c29b08bb3c78386d7bb56cc78fdf79882b6d814f
b95dad2816864ec53b2b0172f5e67ad7849e412feda6dfdb2f242b72f9841f02
b9ace04e11a4b967a22cb89379b30c131d13ef81bea88dcb1192fb95debb49e4
b9bfd32099465cb6f8a446e4ec1205fdc9143bb6f7d91d89fc903d9e2364cf28
b9eed1570c54cdd23efd8671d494dcd1fd36f7d95341f57b3ac708f0af7edaed
b9f8391299344dd39c2c373b06eebf4d6ad02dc521337701f0238ae19f0fb182
ba60ebaa9010edcacd7b919210e6da0ac96e4b033a7d62cee2390974df9a9682
babea3e1ff6f7b4a809c4c4b9e30985804624271545cce7d7277663f1cdd4500
bae6fb3a27713764faa41ebe4e18b6d28a9336dce0515b100d5d53b3a43ac19f
bb3ec40035649e8987ed78ec6650a6bc12e0c2e3b2c970b01ac353970f853c93
bb894e56541702707d7da40bd4ccea23c6c6bd828e44c636c8d5e05600571b1f
bc033c3cb6abd2f5568157bc1bd47ac1157bee5e2ccf3a4a69db082241177b59
bc329827e2939a6be97a228512e1e2c6d1934631e9cc65bbe418d4d311f18a8d
bc5988490d7ae5c2bd20b23a431830c674d926a92ac9fe62af72f3cd8f68d32b
bce52749a7136076c1bb9e2db529853be42608670cb7332c13302b9f633c1e1a
bceac12251c12c4320acc2127119e44df25121879c7dd8c024b79b913038ff59
bd0e6dbf1894fa02edafaadb568bdfe2207eb0361550056f09fd99eb092b6e18
bd4b3db66714c23ffb8f71eaac510c23de533fe5cab81531e10c59afc51f63fa
bd4f6d514739271453882ec3164fc8536cf59f1903138a28bf4206a84f44a420
bd70e77a8b9b0eed0982e8fb8cfc5f9eec9cc2bd82b144588222b71508d3dd68
bd9f686a9649a6bf0bfe540d24fb164e6377abe166c6feb37889f09ff2d628ac
bdb8aae937eeb8fcd3d99aa371b5fd8ed4c70d206b9d1951d42682f64b8a786b
bdba591f274b0ad8fa687e45e77b6e0911c4cf03384a3a1c064fc7106fde9eaf
bdee6afca1e6bb1bf54b755206ccfc7ec164f0636db12407ef34aafdaa6d4a56
be01497ab3ff09930187054421695cd19e63dbdb1fbf574a8bca07ae88a551f4
bec48bc12e14b1877a77db1a8d025d07540dcbeda6f95b67fadc07af42367b12
beff1c2026a0783ebb733f5ba5c764e6d2ddef45fa6fcf6ed3c6e2f03f6e8abd
bf14535fd3f365ad0d5fe7061332c064690c08ea8f1d5b34db0f068ffa452bc2
bf1e50c18ff0275c83ba2b99ebdec9f7f4fe37a4f8b4e2da33367db24c45eb2c
bf524e8f11301aeb2a70fcb3ec133a4ca677aa13acb515e94e6580ace8e92e1d
bf5aa4edb76305ba87878cd488da6ee4d48d482e3d4441a90d869038adce552f
bf7ddfb199af693e8bf375db010efafa609b8c3258cee0d9ac212e29a1641d1e
bfb1ea9a8c904fcd61c51d4bdaf55e288edc8d465f7fc8bc7a6178ad6750ae6d
c02c6b7b4afd8b39e052b02ef590892f47a397a67088e3cc8961fb2417acbd48
c04a6627893fbd69714c650e21abee651d1ca267994aca745a05bb2989ea53de
c0aebbec0844b645e99a57871d4f4698c50c4f0ca9ed67cc2b63a31bc77c5dd2
c11223dd894998fa86aa1784d068111765a79957bf4729070347ec1a6e4ca9b3
c12bfccaa7d01dbe58613bf68c554ba77f965d4fda46d6f1972663792faa08b8
c16187548e73f9037567c47ccf754c17cb4d1f50a59713d7a806b18420afd8b6
c1a0281fb02c3d6cf2f5126b6d9aff35b5ddecdca6393f57db442508d533ce63
c1ae3faf44155d6a96fef0651338ccf7a27595619e34772762ea8511ee69020a
c1cbce92685c3a3de5adefec9f1e5e1ce9ec4c1abc34d4d3e4f97c8952a6e4e2
c1d1af2be530ba792415e67944902a4aa95eacc0e6aa8e881f1150f3f34c1764
c1f7e63b58d0c45002bc4ffbc56d77bff64d9a9d1faf4431ee9b400675db175a
c20a781c79a883ce4f91ee24e5c9cc7b0a86591aace39df0959a323e10dcbb42
c20e213503e6cfe53911f4ffed19f43c3d779dd165f53a43daf3b5eae4cb634d
c2105518f5a3ddec7cafe1bde5d3b4426eefcb23a1c45c338eb9c8cf0d81964c
c23467f75be49c04a37e8f906198b879f4d07315713f839e25309c7a43ed7201
c252c99dc7f6d49d3faa4a43643cc55e3149534c2ddfcf12549d13284a7d2542
c287ee9da576680923b30e85a38115150de8152551c24537f9fefd92fa6072a7
c29471f70ccd86cd2fe48e77fd7a0997e99d7c5bfa0bcfa9fbbea867dbf88ee0
c2ef740dd1b8cf5926ddd7e1ec86a6ff30c7d5ea5a24b2feaf178ad31abd5f25
c302bcb9fdf9b552e36457f60a868e8ac760420ff06f24854c557c687785dee0
c3e70efaca55d6427457eb42ff498a9098557f18ed59d90fd557f92f2403e9be
c45e81843dd6e762308addc7f4fcee9fc93867ec8c464fbc60d65d3a613ede9d
c4b2badf36659b4ca9958b16163e395ce31f0fd4d6b095cabea14f1b157d81b0
c4e2e52e7a45a2e49c572bc9fe24a08570bcba49b32bb5ed0cbfd6030bd773d3
c500fdabff88296b17207c6751183ead3b2886e0b1073062bb480804da08ab84
c51911f06a5c9c1d1eb2db529ede3dacd0a507801486e7be3de88cc4f50f4f5f
c5233257ab728598726528d1d6a4512371cb4458ae9670f7c3f14afcadf5e48c
c5681eb55e4a78116bf224ab0b37fbe3a746b691b3cc473f20d1631f1c3f83a5
c5745750e775bb289d07817e345374acc1acf01c16bdc09b9f9eb1a0d6540b50
c6281a5300dac70f0db2c92e6b3a647d9ec19edfaee5ac09bb866b9d07c51fcc
c644c535fc449466dc475432775eed952b8c97ad94fb591467e5143442e5c955
c658e2c434c1647c3e48116cbff8327d467276c903cfa8e65f158fb377180bfb
c66182f1128ce5c510f9c08ddb1e3301e517266484b3243f63ad0cb5974c6bfb
c6d4b994ed3c05628d93fcea2eaaffd88f7c92cdd8e8c17169e9abd6515852b8
c6e895f0cb76a5eac7f24b4058f1e5b4862a45b3c7040a284391c6b2ffe6d730
c6fc8c0f80bcb49bf9e3683618c41aef12a1d8d2ac8b75bc188dce449055ab76
c7224039d148b7ff66b6f97bf62ad3dc1019dda1deeaef7c26d55426a939ff9e
c7658cfc89174811967c156eaf0214690cfe266dc827447e8c92b9291a372dd3
c7bfb20bf6b1da6010e91169d776bf6d6b16406734e072b86d77b19c46e2247b
c7e810aa5b0aeb79706f56790326183991bebc6d838edcd9cfed0d330cc54f1a
c817b688d728908243f43e283b56d0165d254a337c1f051c4856ef8fef8b0e83
c84088a56a68f369d6910044f628944dab8994c13cc25b368a7cdbe9052747ae
c8942eca5a1473b3b0d5c7a76be2b4af618e63d123cdd43edc49cfe4214ccf3b
c8be5304ecb2084c87859b2784c940391742f165e799c2c397735cfc037a316a
c8c2abdc4259eba4d78866e92549cb9314a32a2986ac26b80eb592a42c84f991
c8c88346e095f889bad5ee5bc1271cde07862d81fc2202fec469459f758108af
c912c49d7bed7aa4b764857bbad4dec75a8f4dd39348ac0c5c7e19b02860914c
c9382ecf6357e871defa5719bcd88658df134447a8296d640efd511ca3ceca88
c943c9b59058fd966aed5ece5ae5068717dd7d066b2c9e19c9846b41f7b46559
c9564c5bb75297ec0dcfcae08a68c2e9e7bb9aae9e8639b0b6d93e02666903f0
c98d7578e14cfa6dc646c70dd3f9d7301f6420aa73694ef45d3d50f28481a737
c9ada58a29e44bc0992d05d4afcad29c1ee5390ebba332643c088cf8c548b136
c9cb2264a07869394b240909a8b588dfba5fb3630b3812fc39daaa1fe31e56f6
c9d965aea7f61a5fee7188ce2b758585d1ea15554433b4fd0cb2cce58575c0a9
ca041a58df169a752aa84f217e199e25c9fdbe1a5de4f56a1f435611d4fa10ea
ca5d9fd18520796130cb06e45aec1ea772ecbf6a120bf0514b93b14691f4a0dd
ca8f3dd6ae9b62a971594e61ad747640a807a1cfa76de4851e208136486c296b
ca9b08de3b9ee98c78a94e6786ed8b23b81b2ef388d014698273ead7634f1855
caac2d14dbdacb1e74275d8ae6a775bc96392482c60adc4460f2f70c186846f9
cae90c3f48a7e9a5f9f6070a00ab1df5a8f739683afa9f3b2841a2931d97e9c0
cb01e63c0d3a0b913f546a9d48c58de03dcfd4ba0736df89b0b57550a185da3a
cb0468a2e28404347dbb184d34e7922cf0b194479d938a45ecc6fa63dc49a07c
cb32060264c7fffb3edf57206b666d65a18d29855981219d05a1451255af073d
cb7aa3499a60136be279806b8adfc468b2d405e99be58403b04c1b305abc058b
cb8c3032aaead80dcdc3a5c29029ac3a3b3fa9c08036f56f55bfd44040a3c680
cc49b1c3434eebc9355c9807b1596340d18a39f25ce0a6b79130af337d4cc8e7
cc63b5b1e3778cb66c9cbdd4edcf222e4f5e2f4a270a0d2b8244d0a54e662655
cc7acad893cc72d393d43352fee2f6294d74fecb2942136bd08ebad3a80c6aa9
cccef2b35acf45d268d48fe3d852fb6551dbf2687c48a35bd82a13df5ec0e3c6
ccd349669679ee287a989d6b612fff0cc48cb13ebbc10621800d893c2d97d389
ccdbcc753c172d20cb8804bfd18371e296ee616a1764c15c8a1466ad25fc4417
ccf68ae295739dad84c6121e573a6053a956187d3ddf483dfe079d5ddcef006a
cd043772eeadd7e40b4a52c65131945eb524d7b2be209710fca11225fc51525f
cd0e182ad3831bc4c16616f25911a503b9e269259d1e0a7534d08be90aa8b33f
cd301f0c1d58019031100d75de8f4e6863599e1cf38ba29682545352ea993d0a
cd4cfb5f44234939463c465e3ca97cdb19d06efcd2becbc6340c1335f3253c17
cdb832b4f5eb89c61a7a33d0a1d0878e844bbaba381c71b8eaebcff758adcdc7
cdc67acc9284b1fc37814198add39fce3893f08589d6b9ee9818c235733b3802
cdd939f682bc0a19378b0fc9d72e0ebc6698af7b9c7e776ed873d0a191c6bcb7
ce349b41f2a5935cc5aa0c4379d506465e4df458774765589e82d89c2fbda871
ce434d6c2e9273089a9d4961b28dfd753be8e6381af6022fe80be711e354b519
ce8b4a7432f71ce31f9ba6ec55557524aaa0aa135f1d6bd4596e38a7bfb4bd90
ceb978eee129c5089af0c3af97bae80313642ea3518c0f2faea08ac46866f86d
cebecc092c5960ab3a1935f0ec16b95366d1fb1e464ace3dc2bbbe9ba5fb00e9
cecb6ad8bf0d972204d9f42b7ac0649878c9a79091640aad51fbcf816456d8fe
cfe07fa70b1ff2a4256f7f615ce6213d98d09ec241eeac29bdbcf93ef2189256
cff6a8331cd5fab470f5e968b7fa6bcd7797b4804f3cb195c2364a91eccf3542
d01caeec65e00ecce13057d8cc1db70186d3d382621fa085252159d1a8e7a22f
d0460dd0336ebf5dcf17f51ea661b3914c11771be71cde4ef3490aa3772a2a5b
d0a875ab6f148a89c7050bde2ca44a64d193b23880ad8e87c49cd42d7f63ee21
d0bb068b745450eb281ffe161a5f7e75c2bd1204dafd3aeaf64ee1e6a1f45595
d0be1fdc9cce7c89f6b565bfd319542916cb69eb58e21c9c92d1a3ddf8c0e3cd
d0e70a11b708582d352a1313ab87c7380b5992039374e1d0da940a8139d1bd77
d10b831926d9d44bc69ec4ccaa4023765b5673265ac7c6d1204705463007e7fa
d11cb88157459cd944c9ddc0ce20c1667e95507474f031e27d46195709e65762
d1dad12af2a5daff36a63238d5c19710d0641f0b5b69fa5875caf3a836330077
d1e92794959653f78e6759feadc533b7b4253cf8297671d99a42494b707c0ba4
d1f31465b1484352b455cff25f5eca80145a039ac0f86eb84e2cfb4a84ee8885
d2641888ed6426afd3d3649066cf3614ec2eb63d3ec90ba2e3a54ba2dffa61ca
d2653ff7cbb2d8ff129ac27ef5781ce68b2558c41a74af1f2ddca635cbeef07d
d33979b6bd8deb716d4e75aad95ca64c006ac78f0ec839d0060885b505944247
d34d1c024f0c128cf80210f6d912958b577bb812506830aaef22cc3c4be2415d
d36ad9f9846d369d01fa03d672b5c3fdd1b436fe00b0a3d3432d15a9083b07ee
d37fc9a42db45d4817cf5d5e6e54de6847fb47d4c3166840b1ed25406c1b3c66
d3926be92d1ccebb3686554aa47bc7ed0d8fb4f905c6db1c8641b8573d21ee2f
d39ada030d41b271be7025a2beb5958ad7c2664798c9c55669da341361290d30
d3c605dce0444efdac47323a9c6615ba8cb1642b1fa31ebd9c7c434f8d63c037
d3d5f3d2e384cbac4efed72b9bf03d31689ced1210ea9c308123a9b7ce3c9fca
d3dd67d419f34e465b2eb20e64a965f9808bdc87e4965048cdf09770205888b9
d3f7b3a831f03d36576d0370d3e833acba587337f3b7cd2baaee2d5a185ac07f
d40f316dfc12485a88752bd9541cf08c3a08f730acef58bafd013dcb62910ca7
d415d1be324522709cae2d088b73f1eb3e8beaf7ce512965bfb91c19a5fc9ba1
d4d833b6a752741fcbf718ebc52732e667b0530b124ff69645795a163303e2c7
d4da1a6d72e291481cbc6ae5218d451bed2b10c32d0a68ae317bebb3dfba879c
d5641313b9aec1338d1ecad39f4e34c820bc6322dd5e3b0bb002396868e15198
d56a60d0e8a2b346342e964137d67e230f138cdfc9f5ee67ee2e98885b30af0d
d58a03f842ba58202f0682b0a90fd0f037b214164440727bb1602beedaf44b5f
d5cbf3f950f33e1acc27daaae00983198641d66f7d22ffb118f95a5361dce7a6
d5d3ab7a20650d5b27c6eeac9eb137a048997c81ac1a8c5e2bbd84681a6da305
d6629c10141d4e716552a894f8d42990909183eec6db5c27e233a9b497235ac7
d66c81ee8cd856a82440743069c6ed23f03f5816dabf6f7f04477f588bc89657
d6e6dc814738fc3f88b28538bb8bd2d12affebcc11ded0c44c17b16e26bc7503
d707186eda54355240489281e87d78abef16df79314c22f50a370f71ae782fc3
d711438a6989aa4526eefd83d6e2c1ea1df19b12416acdbd9722951f6ebe2ccc
d7117bce72e0e48b227351b0aac6d541f6834b316d29661ba993feb073baa918
d72951722ba6e9fe485962e99aae66976ddc28e652340c8d529998d85052564f
d74100584502fa418e8f548aa475b00ad7d7517a570db94a23db6d21ee106bce
d747bb4ee6cb2ed6615f811170ca1df285714101aa6480a9007f7280189a773d
d8299cc1dac00aeccbabc5985e8d2ce4e97cdd78acbfa16e974519717d112f46
d8330f9c7dd318030ca53b8d835c3deb1a5efcc545bcf7acf9b10166a6361c73
d8669de95814d36a7a2752bed45a0e84fffffa6511f0c8025cef31931baa69df
d87b85a66f9b90d562593b1ed375e9d95a19ff7d8daf0deb14680dbf36f63a32
d8c92791d2280abe5985ffaeaa07063cf0c8b9abf5338d688504e6074cf99093
d8e80f906d493721bc790d12094e62531f6b356410ff1b3e2da8ee54b4689842
d8e9432ccd728e07643738fe5c6e9a373bfb628af38ca5a522e15b143be8884a
d8eb7309c534fbe4d47d00603329ba55f989e30ea7c4bc96ae1ec9fef4d432d3
d91b02bc9160d086a153cc8ccb8bd4b2ca4d35215833defd274b370fe939a5c2
d91be3f1c7469d07f450ea10dc3329fa4672984643340e23ed9dc2499775e48b
d92be428257a0368f9b60f67b8262fe5d74b1f87c7603e56bd7182a0ec73b0a0
d92e7988b4e862332cfb496180ef21fa465c614b323f0d921ac928295d9adf36
d9e1945234b190acc3f1869b58c80049c4b29888eea5642c43f3aa7f2cdb7655
da02dc86389c60a0b450b7a74eb58651690bad4de0213b4ad7c06edab9f94668
da22f97851e4c229e7f65e9943113c793322359ec35d6dcad53fdec649c10d90
da62e0e098485c2d3b2c21c8a40349bd15be521e5e7181f116613879286ecc8e
da6fe3cb2825fb043285adde00401e4cb76a3c922fe4c3be8fef194f57ee4d9a
da9983aa810704051104effc6edaf7f24228d38c3feeeddcfdfd344cf5eee1af
daf7c2f52f94c599fd6c432c258280a36daf3d21738b678285e9ab3bb9b40b0c
db3de117c7a31ca61beaf3560c0e7e30031d1c36d4ac82dd1aaa552f01edc968
db424465ba5fab67a27e8509ffd35e989f2204fc78fcf40d6d3b72903150ab6d
db547a37f9398cf93d927b19d89895400ca3a79ed339c611eb4941f9627b6d64
db982bb5e250cee57c9a50ad91c08fa00300fc30059533155eadcb83458b43c1
dbecc8024b749b87d455a565f8305daa6926d1cfed9a0ef64d8a897829849df3
dc1a0f0aacf4aff8ae70242eb5c0f9105dea4b8886f1f1caaaefab2ad417750b
dc242413c7dad9dc0e88dc94742bf1125442492365a8a5fded4579b1f98167d7
dc397585e1709930928db9308e562ad0833442a88f5cd4f1374c0167cfc44d90
dc6b58c8eb0de3ad134b1711ec515665783c164646591ab9469e795712382292
dc86d1590419cbe8eb8ccc23061c7b5fe3d81e293e7094bd60e332359e070633
dce446c3a7887546a5336255a31fb5f03a3facb4415d5f51a65e3d840739a8f8
dd3794597093ea3630cc9dd81b1f304da6389c3e52f80f8f0b9002ccde36c154
dd408fce10ec1a9415a777a3e15283e0d4892280233a85f2bd35aa80c1c67603
dd77f3367179e84379df6bc59644cfbabb43a49c6e66baaa66f559ea74ddebb1
dde15985fea5eb94a8422bc637b40135a3cf097c93b3a6d5094e430dbdbea428
ddfcbf967ace7d9bcb5218d1fc9f5c1ac47d9b1573f50218e0f6171afb28148f
de23bcd0f2e97ddfd58ef80a7db52f7eeeaaf8cb95d119853f69703f46364055
de4402be9620dcbcd2d153b47cac0ecf7c2ea1b928b48763edc2314d7191ac9c
de69a6177ba35096853c67532fb54be174076693c22cef2207e2917c7d6622af
de75d63012243c1531250b3283e9b745300a8ccdce1769e392e7e87d255d7f58
de7cddc5ad6dba5b6ef96203bc59c5b670ba81c9f32bffaf5c485161a1efbe07
de8bce7986b225c70dee5a1172d7bd3bf8e26fc935b4df2a4b99827167584733
dedd45c4fbdd119a89f9eee8ee80f9dedb392cfb4e93aea18688ab1720f49405
defa35b0ef2aa7260918f61f926220b3fbc09f36284c01c5991592710517375f
df61a0d52be8a9da54b28f5b5b11d7ec4ecab65b6aa4dce25bd8620d82c32971
dfbd2deab2a3cb51c6f99245afbfff4ea52fc0ab2929e1cdfc63aaa560ac72b1
e008f6609ed7ce880dd87bb66e669d5ff52228870ef116ff8d16b90a1b46a365
e02876fca1c4adb7a77d13d568ac9ea5beafe0579cfee499b4d683c24801148c
e02c548a8c4f9101725a83252d351d51d348733456cdd3b0afbef8dd8a033dbe
e034fc2b3c282aacde57831a8860e00c39da02115478acf6bd8f8f1bb9efab5a
e0683936600c8229086841143621ce2507412ab2640609b5a28875c151b9f88b
e0f6998e5ef8245cdd9221f518a9c3c025fe9586b0972072e1fdb3640d2a7baa
e108f042ce5b748c94c5dd7b1a9e3cf184806c1f32437524c7514cc0763ba871
e13496525f1b993f1a294ca04b5225b5e5520adfdedef5a632c9da0b9cebe776
e18c09a67a158addb14d0b9ac4199866dbf4661740afb51128f08dc1070cb387
e1a15e3f82c7a0f74ac3e567483f519fc294ee8d46a8bb80115450ce45d4cdb7
e1a4f852f5eaf883a1c1944f510a864e70049d54994a355bc15835058c0672cc
e1be88aa4bfd45d0b86d1570966124b41959174e74d4064e5f6a406873ba78e9
e1df5ee2da9f0807f0f39b1fb923d5f721758232d1467db7106eb93ccc347b0c
e1ee91a144a4994b16ebc1b54428eaafcb554547b00673004700a4e8c5266acf
e214313f900fd3fe4c6f042f3a30b6b7d2598a44bfcb6e1509e69b3aee669eae
e25c47cf01659abcbad6ae9081563122464a439551f984ee6a66346c9e3c58c9
e25dab4e0894d9f5d8722509a55bb67e064882432632ca0068f5ab03d971e493
e292d853bf96f36c6b387d42dd0c2e991cfb333c3fd45cc16db5d5b5eb919f90
e2bafc5138a809227029b14df87514487904be3e83be288a69abd71d1a9e1507
e3542bdb74548974ba445317e1860ceb1f01305328f2e98c05ee70835db9d8c5
e36fba22c1bf21e88469c073640a93fb85489c88c0c7ef494ac02fc1b3165c83
e37223f52789441535f4f5d9448da0f03e3eeb8c8d606f357cdf8af59738e22b
e375ceb107fcb83600d4ac7f0fed972ff1568644a1493146503db29c68bec7b1
e3d20cfbf492621b2c07a96b652dd2adfff64f8adfe366b151a8bf595c13d208
e4093676c2debbceeb38398bc5e71581cb5806f390bebf2fd42b0f1d2fc8de25
e4dd7854727a3b0b7b0a7b3573bff7e9c117db4f72029b1cfabd159010a18474
e58c04213ce826412b2c95513ebb7e19dc815e67e1cb9c68ec73c9e9af28009a
e5969ad0c32443a7ec722d4b518fb94fe30f82cce7070cfa3dad20bb7c3f2272
e5be0c2287a193239a0050e1a6cd2bbb5fa28d44270da24df9959cbdd8cbf936
e5f4bb99129f27f1ea9c00c49f7404bc1f4733f81ec55f0036b47b6e6b06066e
e6257ea4b5dca8aef80cdd8697c9554b3cd8c21a510142caf0ef529868bf017b
e6b52f45ae83635bebe59bb002bb136e93ab00c2be150a7c24ab92bffa7919ec
e7422c2139494a485630f4cb6115ebbc1e0c87bf646579d887f84e8c0bcd9c43
e77aaa9afcfb49f284aa871441f3567b65f39827ba524bd3aacc01fc4d2eeb73
e782b7e82e15c52280ef491636371ccb63a4deca20c5205054512d7c602b2cbd
e7ad4db0a78a9e69ad91c5dcbc99226c3363bd8f566e79c9c00828c3dcf1bbe3
e7bf78c94f618255197e53b92692a68e8a36c861e78c7151fbbc9f515e16ef78
e82fa03c4adedf279bce6ff6ff6b784390a3a2b9d04372f78d88573605ea6792
e8422bc48257faa2972f01da03cffe736fdbd8a7681ef80080d667db92613fd4
e86a785dbcc3c880d1a4d5c68fd39b0695ab7e82e08ea434ee3fc32d9430f725
e8a2eedd56b46cdb32571488324461f362523a556a3761441ba0e9287b45bf98
e8bcbe3eac28ec912682c9f50562c9fbbbb153753c7794d7297412571e6b2350
e929f0d8002bb163c85b698c0795f6e73147817b379711639d1bf6ae4e031888
e9b93226336bc4d18401583698220889fa61868e021c5b57a4d5c4b798017785
e9c3dd15bb3a301ad651b04dd6ab45636b63ffdc717c4f336255e3d9a29f3f1a
ea3fc3b0508eacc08797e26d89f26334a5d1edef1503a239a6116feae8b7e1fd
ea472fead3561b5e058154628e54e64ffa8cbd12832e2dfad84196920f86ba1e
ea7c3e52c243e8039e2040daeafd42921a8ada7ed8bf1b19a455e130eb764e6b
eaa7716690f18d74e1c4f5643c9d39641f4a41a2899b6b8f6b0df719a8371556
eadd4fd8c4c1f454d1a3eb2bd85d8051013b75d654fc82a35f7945d4c42427bb
eae6d77e26d53f8b50aae22c0c31508293e4abe5056e6db46d13d681e9880f42
eb16f93fa25dec7e623929e8065d353cc38bb56c42f328f2ac105b2c57a62c07
eb2aade06cb0aff6e0fb091299368a98621e292712eae93f6c6ff2238d0a853a
eb9e82430d4fd000e039ffd03c0b42266ce0024edd8f3a6c99a6f41ec137e1ef
ebdad5263716ffb978b307d300cdb2ac8558069112844ca8a0b951e5093d0b3c
ec0e21d0045c0dfaca57acd6627eed1fa6b409fe4da57c797741a53d5fa2da28
ec5dff8a75c0077c6872a956a1ea816b30104841bac27d3f6a8aebe905583da8
ec678fa127efa72ed97a7b95fd61566b597ea069d6e366d95f94cef4a97208ce
ecbfb23a44dd829d9bbdc57a50e2ebdc0711b65810bb513b81a4def46b7a1380
ed0c7bd850c6c28f77d651cfb5a39e84613952ea7676ed5eb697c3cf6b518cc5
ed3a1480a8d2085469c8cf0d3e2c8fb86dfd1cf3e1be2f24cdac8027aae43e84
ed6d5542a921bd20bbdc07731b57c8022f817bf0e206f938afd4d3c05908b1a3
edc9de0a13e4a7608155cc797d305bf9f1c414ceac24f212743655470b44698c
ee882a78cf7199665d43443098a2da68a529644e9d41219ac8b8718ff492422e
eeb80b0b65ddb2584147d6acbe0aeb1ab61bcf16ad5cd032fb21a0881f23016f
eed2619b7989a06c88b08efb4ab6c8ddf8d4a8c751d9f1f2c66b3a2b510d3df7
eed62f9d30054097ce21dc2930041309823613f6ea5cec51b999edf71f2fed42
ef59278372ba3d79d3828713f736eae5e3b0c570def806d4ebabe2f2c3b16619
ef63bb65d7df04c3510aa7f4fa20d70220067cc1a3365fc07b9d253765b182f3
efbbd54a65ff9cf39df8f6c929a47df6553916186c1112c83b710087fc9749e1
f072f98c9729c2d35b81d51f84821c9ba8c3c6969f7b90751cb5397306c38066
f0be913bcfc22561804406628d5bbd88ba13d71e7901b71a0b25b15c9393c81e
f0d8fae6661f01256d9decbdb762f20a8176f9d9aad7be7cbbf4186b7ab9d090
f0da7cf69a74f344e114ecdc553aea62abd4d299468a96ae40ec8de321fe6bee
f10b69f5809a52d754e6ff18890a5a35d5988aac7a17ce51cbe5f36bb0a43108
f116ac7ddb5af688382445076012dac590f5719f0a95a4dacdbcfc032054547c
f126774e70dab12c79e71ba68e303db73e266529400c9d4bceb86c79a1c92197
f127c397ce580289c34ea9e37267c6d65c5325df1c00438283c8164543e5af17
f166fdbf164e9851e75f61f2ff5b07935b5861f2e6954840e38d130b487b9fa1
f1af0906d98c7f2fa89a652fd60529d204063a64f7143ca39b366ca6c867969e
f1af8d89075049c37085423dfbef31080ea24f0c6f509e4c5e7875e217f267af
f1d41ea061824378a00f020ce76fd6b1eccf5f9c4a4cf90be20e48cd99a317f3
f20409ebf71f211898947b6720dd6764c47f45ab1615381d74ad489e6dcd0a21
f232f887e952d996c23148cd7081fd276d61fa52545ade2d49aeb929d6dc46ec
f2625421565513cc2778bf7f5e2ff6a333b2eb13bff4e66151f0060e96c2c217
f272b9949bffe24433629e5ba8310c118c736f3e2a5119939a25994bbf66c864
f2bef211168601d15dd1143893028f9c7cb072a3fc28040147bc3f9712d8c719
f2d209db1bcaa4e056ec49d0b2306c910f48758a351b5e7e486734b25d3ef4f6
f2dce70ccd0a9852b6645463ec13c6a19d105262c1bd2e56a2926be77d74358d
f3021963c50f660c462aa9cafce0a311cf6ee7916d9c12a2d7b1ee95904adc7c
f36d1c0ab6c4599938b5f25cb74deaf92f47f1fb3cdf2a369277fe5fbbeaafec
f39b45f2fc93a9922110eead3a36828d8108b344527c367a137dc1b74e8f9621
f39efa8b1bfbacfc03655c32f945059e3d76754c5a421e407fa775a91e8397e6
f4378719b3cdbe4aa6d472d72c5bee62fc31efa1d6646bfd8e910f348060ab9e
f4521cf41941408881f7bfc5feab341734d96ff1362cc644e40c4818a3000097
f46a986c1e0afb5c086d85db0c35c108a9817e18cd77c21bfa14ab238d181021
f4a9239a515a9ce6c8c3980f81084ccfdb0ab4b6b4105eade92d2d8801def847
f5101d32aab05374f06ab7da6f79880d5d4c1ee87cd33aa96207e7fb62f7ae03
f58c9154ea5a251ae2ab897614747933fbcad8979e91f8d7aa2d23b9d3b583b1
f644b236e12dcf382c40fa10e90709e53a2f4dacf065a18591563ed09b2f9157
f649d740539606dbbd91bf5d97ecb8a3e6e72e8a8702250f024b02a353c2c6a9
f64cc83ece417aba20f43b1ee3b58a7528186cfbe463df2928952c34fcb9a8f9
f68814712218230c11c86f02edfdf3ce18943341056a28cebc21c8fcf718e967
f6cd3aa55fddd599575c80c178fb3b58c0c9e9191dd1ae0ad49322843702dba0
f70dfd88e2c3c97c593f3b78bec8d060d32a52332dd54c8369325698dd8b36e0
f76f245d9b798b52ff1d534ab29d5b58c6b9f4ab6d27bc3d3b9573ddf9027563
f77429263b14ecbf677b650f0a71080fb114cadc0f70bbf9047f6d98d9004c86
f79bc4a1c1c139deced822bb55d9028dff88b530e52d73a33fe242db631a56ee
f83277ffacd19710d31a7baee1681fc849bfc9f030a94247fd6515b0991c944f
f8397b91c6a113173e4ca1e6c68707f79600fe0dd3a05ae2bcdfa4875e934236
f84335ff834a8a32a76b1fc45ea3b1a0e6ee080d8d27134ece07304d88342983
f8609754e7024c851f39625ba518d9ba4b40ba93215f4c8571a1ca19c5e81c03
f86b461356b486fda63c054c8d9005598564a93ff4de8a4aeb7a798f8cce722a
f87c847565f36825c22f606a9167a86215d876ae17831c808a4eee9cfa83db7a
f8b476c2c6a512b8027d351cb984a52bf40dfb30d104bc2a424b1815dd9a010d
f8be2d15343b6a1f60c09d50a1a2a603cf6847c854e9b0b63f735145775bc8c5
f8ce85591c31f9f949a210e89e1337ba902745da3835682d114f55ba720995c9
f8ed751167048a65e499651eb40b0d97cfbab3d78490502df85fb49dcb9bc2a9
f8fbbe4f35905ce2e8f75d84f34aa5276ddc6a1fd49da69c77d6a78fac48a157
f91d27dd6def17842b03513398bfcf036ebbb8b8023a0cca26bb3f4c1f48d2c0
f95a914fdfe4b0f0d894331563f7eccbd3c5bbf91f7d1297d575d3761dbd3179
f968b6a56647cf5b791f3c6d77073a8c385202034a85692417f328493fbe1c72
f9e092889d305eb8d53815336dd9aa13d187affd62917aeef6810b6177491de0
fa03735fad0d5355a9e21df3df95af4afd0c40c0e7eaa1198cd07b2c61ecf84a
fa129113e25b120aceefdb2f91b539a9698bfcfb0dc7a876f2c7d94632937055
fa81abfbfc41acfe0d1e003d25584ecb3ff02c5f3bfe1311fb032c49bf805e76
fa91f81791ae5240dccc117e2a3a45b3f126f0162d7f05fc06b57cd851fa4d1e
fae5dd3e0e1bbc584cb5c2de46d58d353472746317a8640b2eda1f23e55a347a
faeee0209969e61a2fe519d3d2ae96a9a576d660fc8bcedb7aac062fa8d7cd32
fb0bcb31697bda4b498e74303d147c0484f349fc9add85954cbabafc4675236a
fb59f37d2160a7817e7285d195f3d99b42ac1d2c617d9aff788ca7bf0c6a0d18
fb7783ec40c73666f5bcff5fbd120656a9f176aa98dfbdacfd8cf897630bbaa6
fba6bc0d85893786752939d43fa26599295bd3cb2df653d334c650d35bede3a1
fbcc452ff10e963899b380cd0f69da054c64e4e0f2eeea6526f24b7705fb7d99
fbde46a3900bdd6e999d0d2c0fcc37cf38c6dfb194d98d740ac134e4d1a1e11e
fc2cd19d149f6193f14e89bf8b35cb8dafc9d81921877cc537098035b22ab1fd
fc51faad8db3c20edb316633958a93877f75496308e33f3ac0876bf4577ae313
fc634d9d4ae6e3ff1391396db120d97254b160f1deb814024bc06a40ee076181
fd12bfcff5c0a89e07dd50a4a586c3466c79742d7b8397e0337be081453544a5
fd48130c86f9d1b3f223f4c236fcdec04e3aaf969a8e9afaca258161a7153619
fd4d2bb26c73d8a3d5f37cf9a1d145fd876264c61a31b9652711ed77149bf8eb
fd520a3bbb5e7fe874297df00d444b6d7f17bd82ac10568dfd85377254d0bc48
fd601b13f7caf7a225497f89e23948ffed439e782237e15fa21e09bab2c0d530
fd817ebdb78f376bd8feb610a12e17810635a17c2de9969f084d7b6e1b40779a
fde6f5417fa35964e0fef2ea71d99c21835c3f7d552cdd9b0274ff56de9e5109
fdee64c65295bef92c48ce7e6b557ed5cffeae986a310f3f6b6c02813882afd1
fe11688c70c683dee6cf9fdf699f779cb84f35ab685742d04299d0ce3ae40faa
fe4f74e910a5d6f3386bbbbb3409f050b100c7778381d6ecf5c58b1efe9ee715
fea84b295b11dd0238a50e6cb6820e323b05e36bfaf8b0a1f9f6b9523001c88b
feeb827368b65d43e3b7d7757e031c54595fd5420a9c632083be899c02ad99de
ff4090f1281d7775ea41cc140820abec8dbca6f5b2541356af66cd0ecebcf80f
ff8c66e297829ce24a64a9841f34a979cf411385a139a3190ab1a35f50b1a370
ffa8c5edee6210fbaf03062d47b23bbaa1aa3483f07d77fbe46ceb47207204c4
ffbc7fbaecaf692015214e6755087b2200b7db90f9248bf332a376fd1d5e5b39
e086a9d90db0be8b2835ebdbff054f8bb6067f6b47379bcf1971923bfa5a551d
e68cc5a976a2d98b4f83d8f8f7fa7b71f6a1281a3131d8a87d03437660d97d8d
fadfbe22c148ad9a0dd4ddb651fd960444f3c4af5f9cfa3bf49f61a7cc6b06f9
6f9e38fcf39d8f8d0d5d21ed2134d7c51cd745dbc8fe2191d8f1cedc77fcb105
8c223e3cee44c3d13d9988b686de882a52ebe8799a4022e4b0a64f86b9a6eb53
9fe93417853739c1c18c2e8b051860d1a317824f1aa91304d16f3fe832486f7a
"""

BANNED_LOCALITY_HASHES = frozenset(_LOCALITY_DIGESTS.split())

def locality_shingles(line):
    """A line as normalised one- and two-word forms.

    Letters only and lowercased, so `Preston-Street`, `PRESTON_STREET`,
    `preston%20street` and `preston street` all reduce to the same two words --
    a token that only matched one spelling would be walked around by a URL.
    Two-word shingles because a locality is often two words, and hashing whole
    lines would only ever match a line that is exactly the token.
    """
    words = re.findall(r"[a-z]+", line.lower())
    pairs = [f"{a} {b}" for a, b in zip(words, words[1:])]
    return words + pairs


def locality_hits(text, banned=BANNED_LOCALITY_HASHES):
    """(line number, digest prefix) for each banned token in `text`.

    `banned` is an argument so the self-test can prove the machinery with
    digests of invented words. Testing it against the real set would need the
    real tokens, and writing them down is the thing this avoids.
    """
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for shingle in locality_shingles(line):
            digest = hashlib.sha256(shingle.encode()).hexdigest()
            if digest in banned:
                out.append((n, digest[:12]))
    return out


#: The shape a place-lookup site puts in its own URL: two or more hyphenated
#: words, a country or state code, and a numeric id. Citing one of those pages
#: discloses the place in the link itself, so the prose above it can be
#: perfectly careful and the footnote still gives the answer away. That is not
#: hypothetical -- a footnote of exactly this shape was in a tracked document.
LOCALITY_SLUG = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+){1,6}"
    r"-(?:au|australia|qld|nsw|vic|tas|sa|wa|nt|act)-\d{2,}"
    r"|[a-z0-9]+(?:-[a-z0-9]+){0,6}-latitude-longitude", re.I)

#: Hosts whose whole purpose is turning a name into a pin. A link to one is
#: worth flagging whatever its slug looks like. Assembled from their parts for
#: the same reason the samples below are: the scan reads this file too, and a
#: list of hosts written out here would be a list of hosts this check reports.
GEO_LOOKUP_HOSTS = tuple(f"{name}.{tld}" for name, tld in (
    ("elevationmap", "net"), ("mapcarta", "com"), ("latlong", "net"),
    ("distancesto", "com"), ("gps-coordinates", "net"), ("whereis", "com")))


def slug_hits(text):
    """(line number, matched slug) for geo-site URL shapes."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in LOCALITY_SLUG.finditer(line):
            out.append((n, m.group(0)))
        low = line.lower()
        for host in GEO_LOOKUP_HOSTS:
            if host in low:
                out.append((n, host))
    return out


#: A coordinate pair as everyone writes it. Two decimals, not three -- see the
#: test below for why that threshold moved.
COORD_PAIR = re.compile(r"(-?\d{1,2}\.\d{2,})\s*[,/]\s*(-?\d{1,3}\.\d{2,})")

#: The same fix in keyword form. `latitude=-33.5, longitude=151.0` is what an
#: API client, a dataclass and a JSON body all look like, and none of them are
#: a pair the regex above can see: there is no comma between the numbers, only
#: between the arguments. The pair check was the whole of rule 2b's coordinate
#: enforcement while this shape walked past it.
#:
#: The optional closing quote is not cosmetic. A JSON body spells the key
#: `"latitude": 58.89`, and the quote sits between the word and the colon --
#: so the pattern that was written for the Python spelling matched neither
#: JSON nor a JS object literal, which is most of the fixtures in this tree.
#: A real place at two decimals sat in the settings-API tests the whole time
#: this check reported the tree clean.
KEYWORD_LAT = re.compile(r"\blat(?:itude)?[\"']?\s*[:=]\s*(-?\d+\.\d{2,})", re.I)
KEYWORD_LON = re.compile(r"\blon(?:gitude)?[\"']?\s*[:=]\s*(-?\d+\.\d{2,})", re.I)


def plausible(la, lo):
    """Could this pair of numbers be somewhere on Earth?

    Ratios, percentiles and thresholds are written the same way and cluster
    near zero; loosening the decimal rule without this turns the check into
    noise, and a noisy check gets switched off.
    """
    if abs(la) <= 1 and abs(lo) <= 1:
        return False
    return -90 <= la <= 90 and -180 <= lo <= 180


def coordinate_pairs(text):
    """(line number, lat, lon) for every plausible coordinate pair.

    Matched over the whole text rather than line by line, because a pair a
    formatter has wrapped is still a pair, and the line number is counted from
    the offset so the failure can still say where.
    """
    out = []
    for m in COORD_PAIR.finditer(text):
        la, lo = float(m.group(1)), float(m.group(2))
        if plausible(la, lo):
            out.append((text.count("\n", 0, m.start()) + 1, la, lo))
    return out


def keyword_coordinates(text):
    """(line number, lat, lon) for `latitude=... longitude=...` pairs.

    Same line or the next one: a call long enough to hold both is a call a
    formatter has already wrapped, and requiring them on one line would miss
    every fixture that has been through black or a manual tidy.
    """
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = KEYWORD_LAT.search(line)
        if not m:
            continue
        window = line + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
        n = KEYWORD_LON.search(window)
        if n and plausible(float(m.group(1)), float(n.group(1))):
            out.append((i + 1, float(m.group(1)), float(n.group(1))))
    return out


#: Words that make a nearby number an identifier rather than a quantity.
PROVIDER_WORDS = re.compile(r"purpleair|openaq|sensor|site|station", re.I)

#: A site index is five to seven digits, quoted or not. Not preceded or
#: followed by a word character, a dot or a hyphen, which is what keeps the
#: timestamp in `data.migrated-20260802-150345`, a version, a float and the
#: middle of a checksum out of the results: those digits are part of a longer
#: token, and an index never is.
SENSOR_INDEX = re.compile(r"(?<![\w.-])(\d{5,7})(?![\w.-])")

#: The one way to say "this number is deliberate". Anything the shape catches
#: honestly -- a byte count, a row count, an equivalence whose subject is the
#: id's own type -- says so on its own line rather than being exempted by
#: value here. A list of exempt numbers is the narrow version of the rule
#: again: it covers the cases already seen and nothing else.
ID_MARKER = "numeric-id-is-the-point"


def excused(lines, i):
    """Is the number on `lines[i]` (0-based) marked as deliberate?

    Per line, not per file. The marker used to be looked for anywhere in the
    file, so one honest exemption in one docstring silenced every id in it --
    a whole file quietly out of scope on the strength of a sentence about a
    different line. The marker sits on the line or immediately above it, where
    a reviewer reads it against the number it excuses.
    """
    return ID_MARKER in lines[i] or (i > 0 and ID_MARKER in lines[i - 1])


def sensor_index_hits(text):
    """(line number, index) for site-index-shaped numbers near provider words.

    Two lines either side: a fixture writes the provider on the line above the
    id as often as beside it, and a JSON blob puts them on consecutive lines.
    """
    lines = text.splitlines()
    near = [bool(PROVIDER_WORDS.search(l)) for l in lines]
    out = []
    for i, line in enumerate(lines):
        if not any(near[max(0, i - 2):i + 3]):
            continue
        if excused(lines, i):
            continue
        for m in SENSOR_INDEX.finditer(line):
            out.append((i + 1, m.group(1)))
    return out


#: A home directory with a person's name in it. Backslashes are normalised
#: first, so the Windows spelling is the same check rather than a second one
#: somebody forgets to update.
HOME_PATH = re.compile(r"(?:/+Users/+|/+home/+|[A-Za-z]:/+Users/+)"
                       r"([A-Za-z0-9._$-]*)")

#: Account names this repository may contain: the generic roots with nothing
#: after them, the placeholder people in the scheduler fixtures, and the CI
#: runner. Named as a vocabulary of what is allowed rather than a denylist of
#: the maintainer's own login, for the same reason the site names are -- what
#: is allowed cannot go stale, and what is forbidden always does.
SYNTHETIC_USERS = {"", "me", "someone", "user", "youruser", "example", "test",
                   "runner"}


def home_path_hits(text):
    """(line number, root, account) for home paths naming a real account."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in HOME_PATH.finditer(line.replace("\\", "/")):
            if m.group(1).lower() in SYNTHETIC_USERS:
                continue
            out.append((n, m.group(0)[:len(m.group(0)) - len(m.group(1))],
                        m.group(1)))
    return out


class TestNoRealPlaceIsCommitted(unittest.TestCase):
    """Rule 2b: no real location, sensor id or coordinates, ever.

    The rule was already written down and the repository still drifted — an
    example in the wizard, a fixture in the store tests, a JSON blob in the
    tray tests, and a comment in the settings page all named the maintainer's
    own suburb, sensor or coordinates. Each arrived innocently, because a
    realistic example is easier to write than a synthetic one.

    Enforced rather than trusted, and by shape rather than by a list of known
    strings: a check naming one leaked value is the narrow version of this rule
    that already failed once with `data/` versus `data.migrated-*/`.

    Coordinates are the shape worth matching. A latitude in a source file is
    almost always somebody's real place, so tests use the deliberately round
    synthetic frame at tests/test_providers.py — anything ending in three or
    four significant decimals is a real GPS fix, not an invented one.

    Coordinates were also all this class checked for a long time, which left
    every other way the same fact travels: the keyword spelling of a pair, a
    site index, a place name in prose, a place name inside a URL, and a home
    directory with somebody's login in it. Each has its own shape and its own
    check below, each reads the tracked tree off `git ls-files`, and each has
    a sample proving it can still fail.
    """

    #: Files that legitimately carry real coordinates, each for a stated
    #: reason. The rule is about *the user's* place, not about any coordinate
    #: anywhere — so the exemptions are named rather than the rule weakened.
    #:
    #: test_scales.py  tests whether a *state* feed covers a real city —
    #:                Hobart, Brisbane, Sydney. Synthetic coordinates would
    #:                make the check meaningless: the bug it guards against is
    #:                a Queensland feed being offered to someone in Tasmania.
    #:
    #: RESEARCH.md, ARCHITECTURE.md and config.example.json were exempt too,
    #: from when they carried worked examples and citations with coordinates
    #: in them. They no longer do, and an exemption nobody needs is a hole
    #: nobody is watching — a document can drift back under cover of a reason
    #: that stopped applying. `test_no_exemption_outlives_its_reason` below
    #: fails if any of these stops carrying the coordinate it is excused for,
    #: so the list can only shrink by itself.
    ALLOWED = {"test_scales.py"}

    #: Only the coordinate exemptions above are scoped by file. Every other
    #: check in this class reads the whole tracked tree, this file included:
    #: the deliberate bad samples are assembled from parts rather than written
    #: out, so the guard is not exempt from the scan it defines.
    def files(self):
        keep = (".py", ".rs", ".html", ".json", ".yml", ".sh", ".md", ".toml")
        return [f for f in tracked_files()
                if f.endswith(keep) and Path(f).name not in self.ALLOWED]

    def frame(self):
        """The synthetic origin every fixture is built around."""
        text = (ROOT / "tests" / "test_providers.py").read_text(encoding="utf-8")
        m = re.search(r"HOME_LAT,\s*HOME_LON\s*=\s*(-?[\d.]+),\s*(-?[\d.]+)", text)
        if not m:
            self.fail("the synthetic frame has moved or been renamed")
        return float(m.group(1)), float(m.group(2))

    def test_no_file_carries_a_coordinate_outside_the_synthetic_frame(self):
        """Fixtures are built around one invented origin, so anything far from
        it came off a map.

        Proximity rather than precision, which was the first thing tried and
        does not work: offsets like -33.5500/151.0050 are deliberately three
        and four decimals deep, because the geometry being tested *is* small
        distances. Judging by decimal places flagged the project's own
        synthetic fixtures and would have taught everyone to ignore this test.
        """
        lat0, lon0 = self.frame()
        # Two decimals, not three. Three was the original threshold and left a
        # gap wide enough to walk through: a coordinate given to two decimals
        # is about 1.1 km, which names a suburb, and rule 2b says *no* real
        # coordinate, ever. Found by nearly committing one — and then again
        # immediately, because the first version of this comment used a real
        # pair as its example and this check caught itself.
        #
        # Accepted limitation, stated rather than quietly carried: a two-degree
        # window is about 220 km across, and the frame's window happens to
        # cover a major metropolitan area — so a genuine fix taken inside it
        # passes. The window is not tightened and the frame is not moved: the
        # frame is the origin every fixture's small-distance geometry is built
        # from, and the coverage-box tests assert against boxes drawn around
        # it, so moving it breaks the thing this check exists to protect. The
        # residual risk is one city's worth of coordinates, and it is covered
        # from the other side — by the place-name, slug and site-index checks
        # below, which do not care how far from the frame a leak sits.
        for name in self.files():
            for line, la, lo in coordinate_pairs(read(name)):
                if abs(la - lat0) <= 2.0 and abs(lo - lon0) <= 2.0:
                    continue
                self.fail(
                    f"{name}:{line} contains the coordinate pair {la},{lo}, "
                    f"which is nowhere near the synthetic frame at "
                    f"{lat0},{lon0} — that is a real place, and rule 2b says "
                    f"it cannot be committed")

    def test_no_file_carries_a_keyword_coordinate_outside_the_frame(self):
        """The same fix, spelled the way an API client spells it.

        `latitude=…, longitude=…` is a coordinate by any reading and the pair
        check cannot see it: the two numbers are separated by an argument
        name, not by the comma the pattern needs. Two fixtures in this
        repository were written in exactly that form, and rule 2b's coordinate
        clause was enforced for one spelling out of two.

        Same window and same exemptions as the pair check, so there is one
        policy about coordinates rather than two that can drift apart.
        """
        lat0, lon0 = self.frame()
        for name in self.files():
            for line, la, lo in keyword_coordinates(read(name)):
                if abs(la - lat0) <= 2.0 and abs(lo - lon0) <= 2.0:
                    continue
                self.fail(
                    f"{name}:{line} sets latitude={la} longitude={lo}, which "
                    f"is nowhere near the synthetic frame at {lat0},{lon0} — "
                    f"that is a real place, and rule 2b says it cannot be "
                    f"committed")

    def test_the_keyword_coordinate_pattern_can_fail(self):
        """The pattern shown a fix it must catch.

        Assembled from parts rather than written out: this file is inside the
        scan, and a sample written plainly would be a coordinate committed to
        prove that committing coordinates is caught. The numbers are a point
        in open ocean, chosen so the sample names nowhere at all.
        """
        # Named `north`/`east` rather than lat/lon, and on separate lines: the
        # two checks above read this file, and a sample assembled to dodge one
        # of them can still be caught by the other.
        north = 12.34
        east = 56.78
        sample = (f"    reading(pm25=5.0, lat" + f"itude={north},\n"
                  f"            long" + f"itude={east})\n")
        self.assertEqual([(1, north, east)], keyword_coordinates(sample),
                         "the keyword-coordinate pattern no longer matches a "
                         "coordinate split across two lines")
        json_form = ('    "lat' + f'itude": {north},\n'
                     '    "lon' + f'gitude": {east}\n')
        self.assertEqual([(1, north, east)], keyword_coordinates(json_form),
                         "the keyword-coordinate pattern no longer matches "
                         "the JSON spelling, which is how most fixtures in "
                         "this tree write a coordinate")
        near_zero = "lat" + "=0.25, lon" + "=0.50"
        self.assertEqual([], keyword_coordinates(near_zero),
                         "a ratio written as lat/lon reads as a coordinate; "
                         "the plausibility filter has stopped filtering")


    def test_the_synthetic_frame_is_still_obviously_synthetic(self):
        """If someone 'improves' the frame to look realistic, the check above
        stops being able to tell invented from real."""
        text = (ROOT / "tests" / "test_providers.py").read_text(encoding="utf-8")
        m = re.search(r"HOME_LAT,\s*HOME_LON\s*=\s*(-?[\d.]+),\s*(-?[\d.]+)", text)
        self.assertIsNotNone(m, "the synthetic frame has moved or been renamed")
        for value in m.groups():
            decimals = len(value.split(".")[1]) if "." in value else 0
            self.assertLessEqual(
                len(value.split(".")[1].rstrip("0")) if "." in value else 0, 1,
                f"the synthetic frame value {value} looks like a real fix; "
                f"keep it round so real coordinates stand out")

    def test_no_exemption_outlives_its_reason(self):
        """An exemption is a hole with a reason attached, and the reason can
        expire without anybody noticing.

        Three documents were exempt from the coordinate check for citations
        and worked examples they no longer carry. Nothing said so: the
        exemption simply kept holding a door open onto files that are read and
        edited constantly. A file that is excused for a coordinate it does not
        have any more is excused for the next one somebody adds.
        """
        for name in sorted(self.ALLOWED):
            paths = [f for f in tracked_files() if Path(f).name == name]
            self.assertTrue(
                paths, f"{name} is exempt from the coordinate check and is "
                       f"not tracked any more — delete the exemption")
            self.assertTrue(
                any(coordinate_pairs(read(f)) for f in paths),
                f"{name} is exempt from the coordinate check but carries no "
                f"coordinate any more. Delete the exemption rather than "
                f"leaving the file out of scope for the next one")

    #: A real PurpleAir or OpenAQ site is identified by a bare number, and that
    #: number resolves to a pin on a public map. So a *numeric* site id in a
    #: fixture is the maintainer's house waiting to be looked up, and a
    #: synthetic one cannot be: nothing real is called "pa-near".
    #:
    #: Shape, not a list of known values. The coordinate rule above is shaped
    #: for the same reason, and a list of leaked identifiers is a check that
    #: only ever catches the leak you already found.
    SITE_ID_CALLS = ("upsert_source", "site_id")

    def test_no_fixture_names_a_real_sensor_index(self):
        """Rule 2b's second clause, which was never enforced.

        The rule says "no real location, sensor id or coordinates". Only
        coordinates were checked, so three of the maintainer's sensor indices
        and two of their site names reached the repository through test
        fixtures — each written while looking at a real install, because a
        realistic example is easier to write than a synthetic one.

        A PurpleAir index is public on PurpleAir's own map. Index plus suburb
        resolves to an address, which is the whole of the risk.
        """
        offenders = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for n, line in enumerate(lines, 1):
                if not any(c in line for c in self.SITE_ID_CALLS):
                    continue
                for m in re.finditer(r"""["'](\d{4,})["']""", line):
                    # A test whose subject *is* the id's type needs a real
                    # number: "12345 and '12345' are one source, not two".
                    # Marked in the file, the way the clock-independence
                    # contract marks a deliberate truncation, so the exception
                    # is a sentence somebody wrote and not a silent hole.
                    #
                    # On the line, or the one above it. The marker used to be
                    # looked for anywhere in the file, which meant one such
                    # sentence exempted every id in that file — including the
                    # ones written after it, by someone who never read it.
                    if excused(lines, n - 1):
                        continue
                    offenders.append(f"{path.name}:{n}: site id {m.group(1)}")
        self.assertEqual(
            [], offenders,
            "a numeric site id in a fixture is a real sensor on a public map. "
            "Use a synthetic one -- 'pa-near', 'oaq-1' -- which cannot "
            "resolve to anybody's house:\n  " + "\n  ".join(offenders))

    def test_no_tracked_file_carries_a_site_index_shaped_number(self):
        """The same clause, widened to where the leak actually came from.

        The check above was narrow three ways at once: quoted digits only, on
        a line that also called `upsert_source` or set `site_id`, in
        `tests/*.py`. A bare integer passed it. So did a JSON fixture, a Rust
        test, a README example, a curl command in a document, and a comment
        recording "the sensor I read this off". None of those is a less public
        pin on a map than a Python fixture is.

        Any five- to seven-digit integer within two lines of a provider word,
        in any tracked file. Deliberate numbers say so on their own line — a
        marker a reviewer reads beside the number, rather than a list of
        exempt values that only ever covers the cases already seen.
        """
        offenders = []
        for name in tracked_files():
            for line, index in sensor_index_hits(read(name)):
                offenders.append(f"{name}:{line}: {index}")
        self.assertEqual(
            [], sorted(offenders),
            "a five to seven digit number beside a provider word has the shape "
            "of a site index, and an index resolves to a pin on a public map. "
            "Use a synthetic id, or mark the line "
            f"`{ID_MARKER}` if the number really is one:\n  "
            + "\n  ".join(sorted(offenders)))

    def test_the_widened_scan_still_sees_provider_lines(self):
        """A guard against the widened check passing by vacuum.

        It reads the tree through `git ls-files`; run somewhere git cannot
        answer — a source tarball, a container with no repository — that
        returns nothing, every file scan is empty and every check in this
        class reports success while checking nothing at all.
        """
        self.assertGreater(len(tracked_files()), 50,
                           "the tracked-file scan found almost nothing; the "
                           "checks in this class are passing by vacuum")
        lines = sum(bool(PROVIDER_WORDS.search(l))
                    for name in tracked_files()
                    for l in read(name).splitlines())
        self.assertGreater(lines, 100,
                           "no provider words anywhere in the tree, so the "
                           "site-index scan has nothing to look near")

    def test_a_refused_listing_is_loud_rather_than_empty(self):
        """The other half of the vacuum guard.

        git declines to answer for reasons that have nothing to do with this
        repository — an unowned checkout, a HOME it cannot read a config from
        — and it declines by printing nothing to stdout. Every check in this
        class then reads a clean tree. It has to raise, not return.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(RuntimeError):
                git_tracked(Path(outside))

    def test_a_marker_excuses_its_own_line_and_no_other(self):
        """The exemption, scoped.

        A marker anywhere in a file used to silence the whole file. That is
        the `data/` versus `data.migrated-*/` shape again: a rule that covers
        the case in front of it and quietly stops covering the next one.
        """
        # The samples below carry real markers of their own, because the scan
        # reads this file too — the check is made to dogfood its own exemption
        # rather than to exempt the file that defines it.
        marked = ["s = upsert(purpleair, 654321)  # " + ID_MARKER]  # numeric-id-is-the-point
        self.assertEqual([], sensor_index_hits("\n".join(marked)))

        above = ["# " + ID_MARKER + ": the equivalence is the property",
                 "s = upsert(purpleair, 654321)"]  # numeric-id-is-the-point
        self.assertEqual([], sensor_index_hits("\n".join(above)))

        distant = ["# " + ID_MARKER + ": the equivalence is the property",
                   "",
                   "s = upsert(purpleair, 654321)"]  # numeric-id-is-the-point
        self.assertEqual(  # numeric-id-is-the-point
            [(3, "654321")], sensor_index_hits("\n".join(distant)),
            "a marker two lines away still excuses the number below it; the "
            "exemption is file-scoped again")

    def test_the_sensor_check_can_fail(self):
        """The filter shown a known-bad sample. Once every fixture is
        synthetic, "found nothing" and "the pattern is broken" look
        identical -- which is how an inverted filter went unnoticed in this
        file before."""
        # 999999 rather than a real index: this sample has to survive a
        # history rewrite that replaces the leaked values, and a sample that
        # gets rewritten stops matching the pattern it exists to prove. The
        # marker on each line is the exemption doing its job — the widened
        # scan reads this file like any other.
        sample = '    store.upsert_source(conn, "purpleair", "999999", "X")'  # numeric-id-is-the-point
        found = re.findall(r"""["'](\d{4,})["']""", sample)
        self.assertEqual(["999999"], found,  # numeric-id-is-the-point
                         "the site-id pattern no longer matches a real index")
        # numeric-id-is-the-point
        self.assertEqual([(1, "999999")], sensor_index_hits(sample),
                         "the widened site-index pattern no longer matches an "
                         "index beside a provider word")
        bare = "    rows = [(purpleair, 654321, 5.0)]"  # numeric-id-is-the-point
        self.assertEqual([(1, "654321")], sensor_index_hits(bare),
                         "an unquoted index walks past the widened pattern")
        stamp = "    path = sensors / 'data.migrated-20260802-150345'"
        self.assertEqual([], sensor_index_hits(stamp),
                         "digits inside a timestamp read as a site index; the "
                         "check is about to become noise")

    #: The invented places this project is allowed to name.
    ALLOWED_SITE_NAMES = {
        "riverside", "northfield", "midvale", "eastvale", "westbrook",
        "southmoor", "fernway", "testville", "backyard", "kitchen",
        "e2e site", "site", "outside", "indoor", "regulatory station",
        "just added", "distinctive site name", "stub site", "sandbox site",
        "example station", "example sensor", "dead", "live", "private",
        "free site"}

    def site_name_files(self):
        """Every file that carries a site-name fixture.

        The tray's Rust tests hold JSON blobs of exactly the shape the Python
        fixtures do, and they were checked by nobody: two of the maintainer's
        real station names sat in them while this check read `tests/*.py` and
        reported that the vocabulary was clean. Fixtures live inside
        `#[cfg(test)]`, so the Rust is read whole rather than stripped.
        """
        return sorted((ROOT / "tests").glob("*.py")) + \
            sorted((ROOT / "tray" / "src").glob("*.rs"))

    #: `site_name="X"`, `site_name: "X"` and `"site_name":"X"` — the Python
    #: keyword, the JSON key and the Rust fixture's JSON key are one pattern,
    #: so the two halves of the app cannot hold two different policies.
    SITE_NAME = re.compile(r'site_name["\']?[:=]\s*["\']([^"\']{3,40})["\']')

    def site_names(self):
        found = []
        for path in self.site_name_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in self.SITE_NAME.finditer(line):
                    found.append((path.name, n, m.group(1)))
        return found

    def test_no_document_outside_the_exemptions_names_a_real_site(self):
        """Site names leak the same fact more readably than an index does.

        Checked against the *synthetic* vocabulary rather than a denylist of
        the maintainer's own places: naming what is allowed cannot go stale
        when they add a sensor, and naming what is forbidden always does.
        """
        offenders = []
        for where, n, raw in self.site_names():
            # "Northfield (OpenAQ)" is the synthetic place plus the provider
            # the row came from — the tray renders it that way. The place is
            # what the vocabulary is about, so the suffix comes off first.
            name = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip().lower()
            if name in self.ALLOWED_SITE_NAMES:
                continue
            if name.startswith(("test", "fake", "pa-", "oaq-")):
                continue
            offenders.append(f"{where}:{n}: site_name {raw!r}")
        self.assertEqual(
            [], sorted(set(offenders)),
            "a site name that is not in the synthetic vocabulary. Add it to "
            "`ALLOWED_SITE_NAMES` if it is invented, or replace it if it is "
            "somebody's real sensor:\n  " + "\n  ".join(sorted(set(offenders))))

    def test_the_site_name_scan_reaches_the_tray(self):
        """A guard against the extension above passing by vacuum.

        The Rust fixtures are the reason this check was widened; a pattern
        that stops matching their shape reports the same clean result as a
        tray with no fixtures in it at all.
        """
        from_tray = [f for f in self.site_names() if f[0].endswith(".rs")]
        self.assertTrue(
            from_tray,
            "no site_name fixture found in tray/src — either the tray's "
            "fixtures have gone or the pattern no longer reads Rust")

    def test_no_tracked_file_names_a_high_risk_locality(self):
        """Prose and URLs carry a place as plainly as a coordinate does.

        Every check above matches a machine-readable shape, so a sentence
        naming the suburb, or a footnote linking to a map of it, went through
        untouched — and both had. The rule is about the fact, not the syntax
        it arrives in.

        Compared by digest, so the check can name what is forbidden without
        the repository containing it. The failure prints the file, the line
        and the first twelve characters of the hash: enough to find the word
        by looking at the line, and no help at all to somebody who has only
        the source.
        """
        offenders = []
        for name in tracked_files():
            for line, prefix in locality_hits(read(name)):
                offenders.append(f"{name}:{line}: token sha256:{prefix}…")
        self.assertEqual(
            [], sorted(offenders),
            "a place name this project must not carry. Read the line, replace "
            "the name with a synthetic one, and do not write the word into "
            "the commit message or the pull request either:\n  "
            + "\n  ".join(sorted(offenders)))

    def test_no_tracked_url_embeds_a_locality(self):
        """A link can name the place the prose was careful not to.

        A footnote citing an elevation-lookup page put the suburb in the URL
        slug, where every reviewer's eye slides over it as punctuation. The
        shape is a giveaway on its own: hyphenated words, a state or country
        code, a numeric id — no other kind of page is addressed that way.
        """
        offenders = []
        for name in tracked_files():
            for line, slug in slug_hits(read(name)):
                offenders.append(f"{name}:{line}: {slug}")
        self.assertEqual(
            [], sorted(offenders),
            "a URL of the shape a place-lookup site uses. Cite the fact "
            "without the link, or link to something that is not a map of "
            "somebody's street:\n  " + "\n  ".join(sorted(offenders)))

    def test_the_locality_scan_can_fail(self):
        """The scanner shown tokens it must catch.

        The samples are invented words hashed here in the test, not the real
        list: proving the machinery works needs a token and a digest that
        agree, not the tokens the digests above stand for. Both spellings and
        both shingle lengths, because a URL writes a two-word place with a
        hyphen and prose writes it with a space.
        """
        one, two = "zarquon", "vogon crescent"
        banned = frozenset(hashlib.sha256(t.encode()).hexdigest()
                           for t in (one, two))
        self.assertEqual(1, len(locality_hits(f"a station in {one}, near", banned)))
        self.assertEqual(1, len(locality_hits(f"see {two.title()} today", banned)))
        self.assertEqual(
            1, len(locality_hits(f"/{two.replace(' ', '-')}-map/", banned)),
            "a two-word place written as a URL slug is no longer normalised "
            "to the words it is made of")
        self.assertEqual([], locality_hits("a station in the next street", banned),
                         "the scan matches text it was never given")

    def test_the_slug_pattern_matches_the_shape_it_is_for(self):
        """Assembled from parts, so the sample is not itself a slug in a
        tracked file — the scan reads this file too."""
        sample = "https://example.invalid/" + "some-place-brisbane" + "-au-1234"
        self.assertEqual([(1, sample.split("/")[-1])], slug_hits(sample),
                         "the slug pattern no longer matches the shape a "
                         "place-lookup site addresses its pages with")
        ordinary = "https://www.epa.gov/indoor-air-quality-iaq/what-merv-rating"
        self.assertEqual([], slug_hits(ordinary),
                         "an ordinary hyphenated URL now reads as a place "
                         "lookup; the pattern is about to become noise")

    def test_no_tracked_file_carries_a_home_directory(self):
        """Somebody's login is personal data and a path from their machine
        never runs on anyone else's.

        This was checked on the HTML surfaces only, because that is where it
        was found the first time. The username reaches a shipped artifact just
        as easily through a test fixture, a shell script, a workflow or a
        document quoting a terminal session, and those are read by more people
        than the dashboard's error banners are.

        The account name is not printed. The file and line are enough to find
        it, and a failure message is the last place to restate the fact the
        check exists to keep out.
        """
        offenders = []
        for name in tracked_files():
            if not name.endswith((".py", ".rs", ".sh", ".md", ".yml", ".yaml", ".json", ".toml")):
                continue
            for line, root, _account in home_path_hits(read(name)):
                offenders.append(f"{name}:{line}: a path under {root}"
                                 f"<account name withheld>")
        self.assertEqual(
            [], sorted(offenders),
            "a home directory belonging to a person. Derive it at runtime "
            "(`Path.home()`, `getpass.getuser()`), or use one of the "
            "placeholder accounts the fixtures already use:\n  "
            + "\n  ".join(sorted(offenders)))

    def test_the_home_path_scan_can_fail(self):
        """Assembled from parts for the same reason as the samples above."""
        sample = "    CHECKOUT = '/User" + "s/jbloggs/src/airo'"
        self.assertEqual([(1, "/Users/", "jbloggs")], home_path_hits(sample))
        windows = r"    CHECKOUT = 'C:\User" + r"s\jbloggs\src'"
        self.assertEqual([(1, "C:/Users/", "jbloggs")], home_path_hits(windows))
        generic = 'HOME_ROOTS = ("/User' + 's/", "/home/")'
        self.assertEqual([], home_path_hits(generic),
                         "the generic roots the fresh-install guard needs now "
                         "read as somebody's home directory")

class TestTheTrayNeverBuildsItsOwnUrl(unittest.TestCase):
    """Where a page lives is Python's answer, including for the app's window.

    serve_port is configurable AND the server moves itself when something else
    holds the port, so any address assembled in the tray is a guess that is
    wrong for some users and eventually wrong for everyone. It held
    `http://127.0.0.1:8787/dashboard.html` as a literal for the whole of v0.5
    and opened a dead page, or a stranger's page on 8787.

    This got sharper when settings moved into Airo's own window: a browser tab
    pointed at a dead URL is obviously broken, but an app window showing a
    connection error reads as the app itself being broken.
    """

    def tray_source(self):
        out = []
        for path in sorted((ROOT / "tray" / "src").glob("*.rs")):
            text = strip_rust_tests(path.read_text(encoding="utf-8"))
            # Comments explain the trap by quoting it; they are not code.
            body = "\n".join(l for l in text.splitlines()
                             if not l.strip().startswith("//"))
            out.append((path.name, body))
        return out

    def test_no_rust_file_constructs_a_loopback_address(self):
        for name, body in self.tray_source():
            for pattern in (r"127\.0\.0\.1", r"localhost", r"https?://"):
                self.assertIsNone(
                    re.search(pattern, body),
                    f"{name} builds its own URL ({pattern}); ask Python with "
                    f"`poller.py --url PAGE`, which knows the port")

    def test_python_offers_the_command_the_tray_needs(self):
        """The other half of the contract: if --url goes, the tray has no
        sanctioned way to find a page and someone will hardcode one again."""
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertIn('"--url"', src)
        self.assertIn("def page_url(", src)


class TestTheTrayKnowsEveryBandPythonCanEmit(unittest.TestCase):
    """The tray maps band *names* to glyphs, and nothing linked the two lists.

    Rule 7 puts the decision in Python and leaves the tray rendering it, which
    is right — but it couples them by string, across a language boundary, with
    no compiler or import to notice a mismatch. A name Python emits that the
    tray has never heard of falls through to the neutral glyph, which is the
    same one used for "no reading yet".

    That had already happened. The `raw` scale's fifth band is "Very high" and
    the tray did not list it, so air in the second-worst band on that scale
    rendered in the menu bar as though nothing had been measured. Quietly, and
    only for users of a non-default scale — the shape of failure CONVENTIONS
    warns about under platform fallbacks that return a constant.

    Enumerated from SCALES rather than listed, so a band added tomorrow is
    already in scope.
    """

    def band_names(self):
        names = set()
        for scale in poller.SCALES:
            for band in poller.scale_bands(scale):
                names.add(band["name"])
        return sorted(names)

    def test_every_band_name_appears_in_the_tray(self):
        rust = strip_rust_tests(
            (ROOT / "tray" / "src" / "airo.rs").read_text(encoding="utf-8"))
        for name in self.band_names():
            # assertTrue rather than assertIn: the haystack is the whole file,
            # and a failure that prints 1,300 lines of Rust buries the one
            # sentence that says what is wrong.
            self.assertTrue(
                f'Some("{name}")' in rust,
                f'the tray has no glyph for the band "{name}", so it would '
                f'render as "no reading" while that air is being breathed')

    def test_there_is_something_to_check(self):
        """A guard against the enumeration silently finding nothing, which
        would make the test above pass by vacuum."""
        self.assertGreaterEqual(len(self.band_names()), 12)


class TestNoSurfaceGivesInstructionsThatCannotWork(unittest.TestCase):
    """What a page tells a stuck user to do must still exist.

    The dashboard's "server isn't running" banner named ./dashboard.sh and
    ./check.sh, both deleted when the installer landed, and prefixed them with
    an absolute path from the author's own machine. Its "data is stale" banner
    named a log path that moved when data left the checkout, and a launchctl
    command that means nothing on Windows or Linux.

    Error text is the one surface read exclusively by someone already stuck, so
    it is the worst place for an instruction that fails. It is also the least
    exercised, which is why it rotted quietly through three releases.
    """

    def surfaces(self):
        return (sorted(ROOT.glob("*.html"))
                + sorted((ROOT / "tray" / "ui").glob("*.html")))

    def test_no_page_names_a_shell_script_that_is_gone(self):
        for path in self.surfaces():
            for script in set(re.findall(r"\./([a-zA-Z0-9_-]+\.sh)\b",
                                         path.read_text(encoding="utf-8"))):
                self.assertTrue(
                    (ROOT / script).exists(),
                    f"{path.name} tells the user to run ./{script}, "
                    f"which does not exist")

    def test_no_page_carries_a_path_from_somebody_machine(self):
        """A developer's home directory shipped in the product is both wrong
        for every user and a small disclosure of who built it."""
        for path in self.surfaces():
            text = path.read_text(encoding="utf-8")
            for m in re.findall(r"(?:~|/Users|/home)/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]*",
                                text):
                # ~/.airo is the documented location and is everyone's.
                if m.startswith("~/.airo"):
                    continue
                self.fail(f"{path.name} contains the path {m!r}")


class TestNoSurfaceConvertsAnIndexItself(unittest.TestCase):
    """The µg/m³ behind an index is the server's answer, not the browser's.

    Three sites in the dashboard multiplied by a hardcoded Australian 25
    regardless of the configured scale, so a US EPA install described air of
    30 µg/m³ as 22.5 and a `raw` install as 7.5. Same shape as the band
    boundaries that were centralised before it: a scale-dependent constant
    written into a page that does not know the scale.

    Matched on the constant rather than the arithmetic, because the arithmetic
    is unremarkable -- it is the 25 (or a 4, its reciprocal in disguise) that
    makes it Australian.
    """

    def surfaces(self):
        """Every page and the tray, found on disk rather than listed -- a
        surface added tomorrow is already in scope."""
        return (sorted(ROOT.glob("*.html"))
                + sorted((ROOT / "tray" / "ui").glob("*.html"))
                + sorted((ROOT / "tray" / "src").glob("*.rs")))

    def test_no_page_holds_the_australian_standard_as_a_conversion(self):
        for path in self.surfaces():
            text = path.read_text(encoding="utf-8")
            for pattern in (r"/\s*100\s*\*\s*25", r"\*\s*0\.25\b",
                            r"/\s*100\s*\*\s*AU_STANDARD", r"\bAU_STANDARD\b"):
                self.assertIsNone(
                    re.search(pattern, text),
                    f"{path.name} converts an index to µg/m³ with a hardcoded "
                    f"Australian factor ({pattern}); ask the server via "
                    f"ug_per_index()")

    def test_the_poller_publishes_the_factor(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertIn('"ug_per_index"', src,
                      "latest.json no longer carries the conversion factor, so "
                      "every surface has to guess it again")


class TestTheConventionsLiveInOneFile(unittest.TestCase):
    """The project's rules are in CONVENTIONS.md, and cited from there.

    They used to live in a file named for a particular editor's assistant.
    That is a statement about how the code was produced rather than about the
    code, and it was cited as the source of a numbered rule from test
    docstrings, from CI failure messages and from CONTRIBUTING -- so a reader
    tracing "why is this forbidden?" was sent to tooling.

    The name survives as a pointer file, because tooling looks for a fixed
    filename and there is no reason to break it. What must not come back is
    *content* in the pointer, or a citation aimed at it: two files claiming to
    hold the rules is how they end up disagreeing, and this project has been
    bitten by a second copy of a decision more than once.

    There are two pointers now. Most coding agents look for `AGENTS.md` rather
    than for a file named after one assistant, and a stranger's agent that
    finds no conventions at all writes to a project it has not read -- which
    costs a reviewer far more than a fourteen-line redirect does. The second
    pointer is held to every term of the first, in the same class, because the
    way this goes wrong is one pointer quietly becoming the real document
    while the other stays a stub: then there are two answers, and the reader
    has no way to tell which is current.
    """

    POINTERS = ("CLAUDE.md", "AGENTS.md")

    #: What a citation can live in. `.py` and `.md` are the obvious ones; the
    #: empty suffix is not decoration -- `tools/pre-commit` is a shell script
    #: with no extension, and it was the file that actually drifted. It told
    #: whoever it stopped to "see CLAUDE.md rule 3", which sends somebody who
    #: has just had a commit refused to a file that contains no rules at all.
    #: A scan over `tools/*.py` would have read straight past it.
    CITABLE = ("", ".py", ".sh", ".yml", ".yaml", ".json", ".md")

    def cited_by(self):
        """Everything that could plausibly cite a rule.

        This file is excluded from its own check: it has to name the pointer
        in order to forbid it, and punishing the test that enforces a rule for
        quoting the rule is the same trap test_obligations.user_visible()
        exists to avoid.

        `tools/` is in scope because the things in it *talk to people*. A hook
        that refuses a commit and a gate that fails a push are read at exactly
        the moment somebody wants to know which rule they broke, and a
        citation is worth nothing if it names a file that holds no rules.
        Walked rather than globbed by extension, for the reason above.
        """
        paths = [ROOT / "CONTRIBUTING.md", ROOT / "README.md",
                 ROOT / "ARCHITECTURE.md", ROOT / "SECURITY.md",
                 ROOT / ".github" / "workflows" / "ci.yml"]
        paths += sorted((ROOT / "tests").glob("test_*.py"))
        paths += sorted(ROOT.glob("*.py"))
        paths += sorted(p for p in (ROOT / "tools").rglob("*")
                        if p.is_file() and p.suffix in self.CITABLE
                        and "__pycache__" not in p.parts)
        here = Path(__file__).resolve()
        return [p for p in paths if p.exists() and p.resolve() != here]

    def test_the_scan_reaches_a_file_with_no_extension(self):
        """Guards the walk. The one file this class was extended for is a
        shell script called `pre-commit`, and an enumeration that quietly
        skipped it would pass while the citation it was extended to catch sat
        there unchanged."""
        scanned = {p.name for p in self.cited_by()}
        self.assertIn("pre-commit", scanned,
                      "the citation scan no longer reaches tools/pre-commit, "
                      "which is the file that drifted")

    def test_the_conventions_file_holds_the_rules(self):
        text = (ROOT / "CONVENTIONS.md").read_text(encoding="utf-8")
        self.assertIn("Hard rules", text)

    def test_a_pointer_holds_no_rules_of_its_own(self):
        """A pointer that grows content is a second copy waiting to drift."""
        for name in self.POINTERS:
            pointer = ROOT / name
            if not pointer.exists():
                continue    # removing one entirely is also a valid answer
            with self.subTest(pointer=name):
                text = pointer.read_text(encoding="utf-8")
                self.assertIn("CONVENTIONS.md", text,
                              f"{name} does not point anywhere")
                self.assertLess(
                    len(text.splitlines()), 20,
                    f"{name} has grown content; the rules live in "
                    f"CONVENTIONS.md and belong in exactly one file")

    def test_at_least_one_pointer_is_actually_there(self):
        """Guards the walk above, which passes over a pointer that does not
        exist -- so a class checking two absent files would be green while a
        stranger's agent found no conventions at all."""
        present = [n for n in self.POINTERS if (ROOT / n).exists()]
        self.assertTrue(present, "no pointer file exists, so nothing that "
                                 "looks for a fixed filename finds anything")

    def test_nothing_cites_a_pointer_as_the_source_of_a_rule(self):
        """The citation is the thing that mattered, not the filename."""
        for path in self.cited_by():
            text = path.read_text(encoding="utf-8")
            for name in self.POINTERS:
                with self.subTest(path=path.name, pointer=name):
                    self.assertNotIn(
                        name, text,
                        f"{path.relative_to(ROOT)} cites {name}; the rules "
                        f"are in CONVENTIONS.md and that is what should be "
                        f"named")


class TestTheCoverageFloorIsReal(unittest.TestCase):
    """A floor nobody can read is a floor nobody maintains.

    The number exists to ratchet: set from a real measurement, CI fails below
    it, and raising it is a deliberate commit rather than a side effect. These
    check the mechanism holds together — a floor file that drifts out of step
    with the modules, or a floor of zero, would pass every build while
    guaranteeing nothing.
    """

    def floors(self):
        path = ROOT / "tools" / "coverage-floor.json"
        self.assertTrue(path.exists(), "the coverage floor file is missing")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_shipped_module_has_a_floor(self):
        """Otherwise a new module starts at zero and nothing says so — which
        is exactly how analyse.py reached 0%."""
        floors = self.floors()["modules"]
        shipped = {p.name for p in ROOT.glob("*.py")} - {"setup.py"}
        missing = shipped - set(floors)
        self.assertEqual(
            set(), missing,
            f"no coverage floor for {sorted(missing)} — a module with no floor "
            f"can sit at zero indefinitely without failing anything")

    def test_the_floor_records_where_it_was_measured(self):
        """Coverage differs by platform — scheduler.py runs its macos_*
        branches on a Mac and its linux_* branches on Linux — so a floor with
        no platform recorded is a number nobody can reproduce or argue with.

        Found the hard way: a floor measured on macOS failed CI on Linux by
        0.1%, which reads as a regression and is not one.
        """
        where = self.floors().get("measured_on") or {}
        self.assertTrue(where.get("platform"),
                        "the floor does not say what it was measured on")
        self.assertTrue(where.get("why"),
                        "the floor does not say why the platform matters")

    def test_the_floor_file_names_nothing_that_has_gone(self):
        floors = self.floors()["modules"]
        shipped = {p.name for p in ROOT.glob("*.py")}
        stale = set(floors) - shipped
        self.assertEqual(set(), stale,
                         f"floors for modules that no longer exist: {sorted(stale)}")

    def test_the_total_is_not_a_token_number(self):
        """A floor of 0 would pass forever. This is the one number that has to
        mean something."""
        total = self.floors()["total"]
        self.assertGreater(total, 50, "the total floor is too low to be a gate")
        self.assertLessEqual(total, 100)

    def test_the_safety_critical_modules_hold_at_full_coverage(self):
        """fusion.py decides which number a user is shown; forecast.py decides
        what may be said about the future. Both are at 100% and neither has a
        good reason to fall."""
        floors = self.floors()["modules"]
        for name in ("fusion.py", "forecast.py"):
            self.assertGreaterEqual(
                floors.get(name, 0), 100,
                f"{name} is safety-critical and its floor has slipped below "
                f"100% — see ARCHITECTURE §2.5b and ROADMAP #9")


class TestDocsMatchTheCode(unittest.TestCase):
    """Documentation drifts silently: nothing fails when a README describes a
    flag that was renamed or a count that has doubled. These checks make the
    docs part of the build, so staleness is a red test rather than something a
    reader discovers.

    CHANGELOG entries under a released version are exempt throughout — they
    are a historical record and were correct when written.
    """

    LIVING = ("README.md", "ARCHITECTURE.md", "CONVENTIONS.md", "CONTRIBUTING.md",
              "SECURITY.md", "LICENSING.md", "ROADMAP.md", "RELEASING.md")

    def living_docs(self):
        return [(n, (ROOT / n).read_text(encoding="utf-8"))
                for n in self.LIVING if (ROOT / n).exists()]

    def real_flags(self):
        flags = set()
        for mod in ("poller.py", "scheduler.py", "backup.py", "setup.py",
                    "analyse.py"):
            src = (ROOT / mod).read_text(encoding="utf-8")
            flags |= set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))
        return flags

    def test_every_documented_flag_exists(self):
        """A doc promising `--foo` that argparse rejects is worse than no doc:
        the reader assumes they typed it wrong."""
        real = self.real_flags()
        for name, text in self.living_docs():
            for mod, rest in re.findall(r"python3 (\w+\.py)([^\n`]*)", text):
                for flag in re.findall(r"(--[a-z-]{2,})", rest):
                    self.assertIn(flag, real, f"{name} documents {mod} {flag}")

    def test_every_documented_module_exists(self):
        for name, text in self.living_docs():
            for mod in re.findall(r"python3 (\w+\.py)", text):
                self.assertTrue((ROOT / mod).exists(),
                                f"{name} references {mod}, which does not exist")

    def test_every_documented_shell_script_exists(self):
        for name, text in self.living_docs():
            for script in re.findall(r"`\./([a-z_]+\.sh)", text):
                self.assertTrue((ROOT / script).exists(),
                                f"{name} references ./{script}, which does not exist")

    def test_no_living_doc_still_describes_a_removed_widget(self):
        """SwiftBar and Übersicht were deleted. A doc that still tells someone
        to install one leads nowhere, and reads as current."""
        for name, text in self.living_docs():
            for line in text.lower().splitlines():
                if "swiftbar" not in line and "übersicht" not in line:
                    continue
                self.assertFalse(
                    any(v in line for v in ("install", "copy ", "symlink", "run ")),
                    f"{name} still instructs using a removed widget: {line.strip()[:70]}")

    def test_no_living_doc_claims_the_dashboard_loads_a_cdn(self):
        """SECURITY.md described the CDN as the one external asset in the
        Python path. It was removed; a security document describing a threat
        model that no longer exists is worse than one that says nothing."""
        for name, text in self.living_docs():
            low = text.lower()
            for phrase in ("chart.js from a cdn", "chart.js from cdn", "cdnjs"):
                if phrase not in low:
                    continue
                # Naming it while explaining the removal is fine.
                idx = low.index(phrase)
                window = low[max(0, idx - 220):idx + 220]
                self.assertTrue(
                    any(w in window for w in ("was ", "removed", "no longer",
                                              "replaced", "previous")),
                    f"{name} still presents the CDN as current")

    def count_tests(self):
        """Counted by parsing, not by running. Shelling out to `unittest
        discover` from inside the suite re-runs everything including this,
        which is recursive and takes minutes."""
        actual = 0
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    actual += sum(
                        1 for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name.startswith("test"))
        return actual

    def test_stated_test_counts_are_true(self):
        """A count nobody verifies is a count that is wrong. Asserted rather
        than removed, because "how big is the suite" is a real question a
        contributor asks before their first change.

        Counted by parsing, not by running: shelling out to `unittest
        discover` from inside the suite re-runs everything including this,
        which is recursive and takes minutes.
        """
        actual = self.count_tests()

        # A FLOOR, not an equality, and the reason is friction that was
        # costing more than the check was worth.
        #
        # Asserting equality meant every PR that added a test had to edit the
        # number in four documents, and any two concurrent PRs then conflicted
        # on those lines. Five rebases in one batch had that as their only
        # conflict. A check that makes routine work harder gets worked around,
        # and a number updated grudgingly in four places is a number that goes
        # wrong in one of them.
        #
        # A floor keeps what the check was for. The claim "N tests" stays true
        # as the suite grows, and it still fails loudly if tests are deleted --
        # which is the direction that actually matters, and the one nobody
        # would otherwise notice.
        #
        # Both phrasings, because matching only "N Python tests" left a blind
        # spot: ROADMAP's risk register said "391 Python and 29 Rust tests" and
        # sat 60 tests out of date while this test reported the count verified.
        for name, text in self.living_docs():
            for claimed in re.findall(r"(\d+) Python (?:tests|and\b)", text):
                self.assertLessEqual(
                    int(claimed), actual,
                    f"{name} claims {claimed} Python tests and there are only "
                    f"{actual}. Tests have been removed, or the claim was "
                    f"never true.")

    def test_no_stated_count_has_drifted_absurdly_far(self):
        """The floor above cannot be left forever without becoming useless.

        "At least 400" is technically true of a 4,000-test suite and tells a
        reader nothing. This fails once the real number is half again the
        claim, which is rare enough not to be friction and often enough that
        the figure stays worth reading.
        """
        actual = self.count_tests()
        for name, text in self.living_docs():
            # Thousands separators. The pattern used to be `(\\d+)`, which read
            # "1,760 Python tests" as a claim of 760 and failed a document that
            # was correct. Once the suite passed a thousand, the only way to
            # satisfy the check was to write the number in a way no editor
            # would -- so the check was quietly training the prose rather than
            # reading it.
            for claimed in re.findall(r"([\d,]+) Python (?:tests|and\b)", text):
                n = int(claimed.replace(",", ""))
                self.assertLess(
                    actual, n * 1.5,
                    f"{name} claims {claimed} Python tests and there are now "
                    f"{actual}. The floor has drifted far enough to be "
                    f"misleading — refresh it.")

    def test_the_architecture_layout_table_is_complete(self):
        """ARCHITECTURE.md's file table described two of nine modules. store.py
        and fusion.py — the schema and the safety-critical fusion decision —
        were both absent from the document that exists to explain them."""
        s = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"^\| `([a-z_]+\.py)`", s, re.M))
        actual = {p.name for p in ROOT.glob("*.py")}
        self.assertEqual(actual - listed, set(),
                         "shipped modules missing from the layout table")
        self.assertEqual(listed - actual, set(),
                         "layout table lists modules that no longer exist")

    def test_no_doc_points_at_the_pre_v06_data_path(self):
        """Readings moved out of the checkout in v0.6. CONVENTIONS.md rule 2a warns
        against hardcoding $PROJECT/data precisely because doing so once
        produced a stray empty database and a zero-row report — and the docs
        were still doing it."""
        for name, text in self.living_docs():
            for m in re.finditer(r"`data/(latest\.json|airo\.db|alert_state\.json)`", text):
                line_start = text.rfind("\n", 0, m.start())
                line = text[line_start:text.find("\n", m.end())].lower()
                # Naming it as the thing gitignored, or as the old location,
                # is correct; presenting it as where data lives is not.
                self.assertTrue(
                    any(w in line for w in ("gitignore", "pre-v0.6", "never",
                                            "do not commit", "excludes")),
                    f"{name} still gives {m.group(0)} as the live path")

    def test_licensing_covers_every_provider(self):
        """LICENSING.md is where a user learns what they may do with the data
        they hold. NSW was absent from its table — a keyless network, so one of
        only two a new user can read on their first poll, with a CC BY
        attribution obligation stated nowhere."""
        lic = (ROOT / "LICENSING.md").read_text(encoding="utf-8")
        # Only the data-licence table, and only each provider's DISTINCTIVE
        # name. Searching the whole document for any word of the label passed
        # NSW on the strength of "Government" appearing in Queensland's row.
        rows = [l for l in lic.splitlines()
                if l.startswith("| **") and "|" in l[3:]]
        self.assertTrue(rows, "the data-licence table is gone")
        table = "\n".join(rows).lower()
        for slug, prov in sorted(poller.PROVIDERS.items()):
            distinctive = str(prov.label).split()[0].lower()
            self.assertTrue(
                slug in table or distinctive in table,
                f"{slug} ({prov.label}) has no row in the LICENSING.md table")

    def test_no_public_doc_points_at_a_private_section(self):
        """Splitting the planning left public documents linking to sections
        that only exist in a gitignored file — a reader outside follows the
        reference and finds nothing."""
        private_markers = ("§Legal", "ROADMAP.md) §Legal", "§Legal below",
                           "§Legal L3")
        for name, text in self.living_docs():
            for marker in private_markers:
                self.assertNotIn(
                    marker, text,
                    f"{name} references {marker!r}, which moved to INTERNAL.md")

    def test_internal_sections_really_are_absent_from_the_public_roadmap(self):
        s = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        for heading in ("## Legal — PurpleAir terms", "## The hosted service"):
            self.assertNotIn(heading, s)

    def test_the_layout_table_lists_every_shipped_module(self):
        """CONVENTIONS.md's layout table is the map a new contributor reads
        first. A module missing from it is a module nobody knows exists --
        forecast.py sat unlisted after it was added."""
        conventions = (ROOT / "CONVENTIONS.md").read_text(encoding="utf-8")
        for mod in sorted(p.name for p in ROOT.glob("*.py")):
            self.assertIn(mod, conventions,
                          f"{mod} is not mentioned anywhere in CONVENTIONS.md")

    #: A heading that opens by naming one module -- `### `poller.py` internals`.
    #: Only tables under such a heading are read. Prose is deliberately out of
    #: scope: CONVENTIONS' trap list cites `field()`, a helper nested inside a
    #: provider method, and a check that has to parse English to tell a
    #: documented entry point from an aside is a check that gets turned off.
    INTERNALS_HEADING = re.compile(r"^#{2,4} +`([a-z_]+\.py)`[^\n]*$", re.M)

    def internals_tables(self):
        """(doc, module, table rows) for every module's own internals table."""
        out = []
        for name, text in self.living_docs():
            for m in self.INTERNALS_HEADING.finditer(text):
                rest = text[m.end():]
                nxt = re.search(r"^#{1,6} ", rest, re.M)
                block = rest[:nxt.start()] if nxt else rest
                rows = [l for l in block.splitlines() if l.startswith("|")]
                if rows:
                    out.append((name, m.group(1), rows))
        return out

    def test_every_function_an_internals_table_names_exists(self):
        """The other checks here read flags, modules and counts -- never a
        function name, which is how ARCHITECTURE §6's internals table came to
        describe five functions that do not exist. `poll_current()`,
        `backfill()` and `append_rows()` were the CSV-era shape, gone since
        v0.5; `au_aqi()` and `au_band()` survive only as deprecated JSON keys.
        A reader following that table goes looking for code that was deleted,
        and concludes they are reading the wrong file rather than a stale doc.
        """
        for doc, module, rows in self.internals_tables():
            path = ROOT / module
            self.assertTrue(path.exists(),
                            f"{doc} documents the internals of {module}, "
                            f"which does not exist")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            real = {n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))}
            for row in rows:
                for named in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)\(\)`", row):
                    self.assertIn(
                        named, real,
                        f"{doc} says {module} has {named}(), and it does not")

    def test_the_internals_scan_finds_a_table_to_check(self):
        """The check above passes vacuously if the heading shape ever changes.
        A rename of the section it reads would leave it green over nothing --
        which is the failure mode of every enumeration in this file."""
        tables = self.internals_tables()
        self.assertTrue(tables, "no internals table was found in any doc")
        named = sum(len(re.findall(r"`[A-Za-z_][A-Za-z0-9_]*\(\)`", r))
                    for _, _, rows in tables for r in rows)
        self.assertGreater(named, 5, "an internals table naming almost no "
                                     "functions is not being read properly")


class TestSecurityNamesEveryHostTheCodeCanReach(unittest.TestCase):
    """SECURITY.md's network table named three endpoints and closed with "No
    other network activity occurs". That was false: the code also reaches the
    NSW network, Open-Meteo's forecast *and* archive hosts, three
    IP-geolocation services and Nominatim. A threat model that under-states
    its own network surface is worse than none, because the reader stops
    looking.

    Enumerated from the source rather than listed, so adding a provider or a
    lookup puts it in scope on the commit that adds it. Hostnames that appear
    in the code without being fetched -- the key-signup pages, the CC BY
    attribution link, the User-Agent's own URL -- are in scope too: a security
    document that names them and says which are requests is the only way a
    reader can check the claim themselves.
    """

    HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
    #: The dashboard server talks to itself. It never leaves the machine, and
    #: SECURITY.md covers it under Network exposure rather than as an endpoint.
    LOOPBACK = ("127.0.0.1", "localhost", "0.0.0.0", "::1")

    def hosts(self):
        found = {}
        for path in python_modules():
            for m in self.HOST.finditer(path.read_text(encoding="utf-8")):
                host = m.group(1).rstrip(".").lower()
                if host not in self.LOOPBACK:
                    found.setdefault(host, set()).add(path.name)
        return found

    def test_security_names_every_host_the_source_can_name(self):
        doc = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for host, where in sorted(self.hosts().items()):
            # Matched on hostname boundaries, not with `in`. A substring test
            # passes `api.open-meteo.com` on the strength of the attribution
            # link to `open-meteo.com` -- a different host, reached for a
            # different reason, and the apex is never contacted at all.
            found = re.search(r"(?<![A-Za-z0-9.-])" + re.escape(host)
                              + r"(?![A-Za-z0-9-])", doc)
            self.assertTrue(
                found,
                f"SECURITY.md never names {host}, which appears in "
                f"{', '.join(sorted(where))}")

    def test_the_scan_finds_the_networks_it_is_for(self):
        """A regex that stops matching leaves this green over an empty set."""
        hosts = self.hosts()
        self.assertGreaterEqual(len(hosts), 8,
                                f"only {len(hosts)} hosts found in the source")
        for slug, prov in sorted(poller.PROVIDERS.items()):
            base = str(getattr(prov, "API_BASE", "") or "")
            host = self.HOST.match(base)
            self.assertTrue(host, f"{slug} has no readable API_BASE")
            self.assertIn(host.group(1).lower(), hosts,
                          f"{slug}'s own endpoint was not picked up")


class TestContributorLicence(unittest.TestCase):
    """Dual licensing is the whole reason this exists. A contribution offered
    only under the AGPL cannot go into a commercially licensed copy, and one
    such contribution anywhere in the tree removes the option for the entire
    project — so the grant has to be collected before code is merged, and a
    document nobody enforces collects nothing."""

    def cla(self):
        return (ROOT / "CLA.md").read_text(encoding="utf-8")

    def workflow(self):
        return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_the_agreement_exists(self):
        self.assertTrue((ROOT / "CLA.md").exists(),
                        "the sign-off refers to a document that does not exist")

    def test_it_grants_a_sublicensable_licence(self):
        """The specific word that makes dual licensing possible. Without it the
        grant is broad-sounding and commercially useless."""
        # In the GRANT clause specifically. The word also appears in the
        # paragraph explaining why it matters, so searching the whole document
        # passed even with the grant itself stripped.
        text = self.cla()
        i = text.index("**A copyright licence.**")
        clause = text[i:text.index("\n\n", i)].lower()
        self.assertIn("sublicense", clause,
                      "the copyright grant does not include the right to "
                      "sublicense, so it cannot support a commercial licence")
        for word in ("perpetual", "irrevocable", "worldwide"):
            self.assertIn(word, clause, f"the grant is not {word}")

    def test_it_is_a_licence_not_an_assignment(self):
        """Contributors keep their copyright. An assignment would deter people
        for no benefit the project actually needs."""
        low = self.cla().lower()
        self.assertIn("you keep your copyright", low)
        self.assertIn("not an assignment", low)

    def test_it_covers_patents_and_provenance(self):
        low = self.cla().lower()
        self.assertIn("patent", low)
        self.assertTrue("right to submit" in low or "yours to give" in low)

    def test_it_promises_the_open_version_survives(self):
        """A one-sided grant is a reason to refuse. What the project undertakes
        in return has to be written down too."""
        low = self.cla().lower()
        self.assertIn("agpl", low)
        self.assertTrue(
            any(p in low for p in ("stays", "never instead of", "remains available")),
            "nothing commits the project to keeping the open version")

    def test_it_says_the_text_is_not_lawyer_reviewed(self):
        """Claiming more certainty than we have would be the one genuinely
        dishonest thing this document could do."""
        low = self.cla().lower()
        self.assertTrue("not legal advice" in low or "not yet reviewed" in low)

    def test_it_offers_a_way_to_contribute_without_signing(self):
        low = self.cla().lower()
        self.assertIn("bug report", low)

    def test_ci_enforces_the_sign_off(self):
        w = self.workflow()
        self.assertIn("sign-off:", w, "no CI job checks contributions")
        self.assertIn("Signed-off-by", w)

    def test_the_check_runs_on_pull_requests(self):
        w = self.workflow()
        i = w.index("sign-off:")
        block = w[i:i + 900]
        self.assertIn("pull_request", block)

    def test_the_check_needs_full_history(self):
        """A shallow clone cannot see the commits it is meant to inspect, and
        would pass silently."""
        w = self.workflow()
        i = w.index("sign-off:")
        block = w[i:i + 900]
        self.assertIn("fetch-depth: 0", block)

    def test_the_failure_message_says_how_to_fix_it(self):
        """A red build with no remedy is a contributor lost."""
        w = self.workflow()
        i = w.index("sign-off:")
        block = w[i:w.index("\n  test:", i)]
        self.assertIn("rebase --signoff", block)
        self.assertIn("CLA.md", block)

    def test_the_check_is_skipped_on_pushes_and_that_is_stated(self):
        """It fires only on pull requests, so a push to main skips it entirely.
        That is correct — base.sha and head.sha only exist for a PR, and only
        the copyright holder can push to main — but a job that silently skips
        looks identical to one that passes, so the limit is written down."""
        w = self.workflow()
        i = w.index("sign-off:")
        block = w[i:i + 900]
        self.assertIn("github.event_name == 'pull_request'", block)
        self.assertIn("pull request", self.cla().lower())

    def test_the_check_rejects_a_sign_off_naming_someone_else(self):
        """Otherwise the record does not establish who granted anything."""
        w = self.workflow()
        i = w.index("sign-off:")
        block = w[i:w.index("\n  test:", i)]
        self.assertIn("MISMATCH", block)

    def test_every_document_that_should_point_at_the_cla_does(self):
        for name in ("CONTRIBUTING.md", "LICENSING.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("CLA.md", text, f"{name} does not reference the agreement")

    def test_the_maintainer_exemption_is_explained_not_assumed(self):
        """The project's own commits carry no sign-off, because a copyright
        holder cannot licence to itself something it already owns. That is a
        legal fact rather than a privilege — but an unexplained asymmetry in a
        contribution rule reads badly and should, so it has to be written down
        rather than left for someone to notice."""
        low = self.cla().lower()
        self.assertIn("who needs to sign", low,
                      "the CLA does not say who it applies to")
        self.assertIn("copyright holder", low)
        self.assertTrue(
            "pull request" in low,
            "the CLA does not say where the check runs, so the gap looks hidden")


class TestBothHalvesAgreeWhereDataLives(unittest.TestCase):
    """The poller writes and the tray reads. They have disagreed twice: when
    readings moved to ~/.airo/data, and when data_dir became configurable and
    only Python honoured it — the tray read the default location, found
    nothing, and said "no reading yet" while the poller wrote elsewhere. The
    symptom looks like a broken tray, not a misconfiguration."""

    def rust(self):
        return (ROOT / "tray" / "src" / "airo.rs").read_text(encoding="utf-8")

    def test_the_tray_reads_the_configured_directory(self):
        self.assertIn("configured_data_dir", self.rust(),
                      "the tray ignores data_dir, so a custom location "
                      "leaves it permanently blank")

    def test_both_halves_consult_the_same_sources_in_the_same_order(self):
        py = (ROOT / "poller.py").read_text(encoding="utf-8")
        i = py.index("def _resolve_data_dir")
        py_block = py[i:i + 1800]
        rs = self.rust()
        j = rs.index("pub fn data_dir")
        rs_block = rs[j:j + 1400]
        for token_py, token_rs, what in (
                ("AIRO_DATA", "AIRO_DATA", "the environment variable"),
                ("_configured_data_dir", "configured_data_dir", "the config setting"),
                ('".airo" / "data"', '".airo")', "the default location")):
            self.assertIn(token_py, py_block, f"poller lost {what}")
            self.assertIn(token_rs, rs_block, f"tray lost {what}")


class TestNoSurfaceInventsTheDataPath(unittest.TestCase):
    """The dashboard told users their readings were in `data/airo.db` — the
    pre-v0.6 location, and wrong for anyone who set data_dir. Telling someone
    their data is somewhere it is not is worse than saying nothing: they look
    there, find nothing, and conclude the tool is broken."""

    def test_the_poller_publishes_the_real_directory(self):
        src = (ROOT / "poller.py").read_text(encoding="utf-8")
        self.assertIn('"data_dir": str(DATA)', src,
                      "no surface can know where the database is")

    def test_the_dashboard_renders_it_rather_than_hardcoding_one(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("dbPath", html)
        self.assertIn("latest.data_dir", html)

    def test_no_surface_states_a_literal_database_path(self):
        for name in ("dashboard.html", "tray/ui/index.html"):
            path = ROOT / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("<code>data/airo.db</code>", text,
                             f"{name} hardcodes a database path")


class TestNoSuiteQuietlyUsesTheInternet(unittest.TestCase):
    """A test that calls a real API passes for the wrong reason.

    Found by an end-to-end journey showing 72 hours of weather in a database
    created seconds earlier. One suite run was making 25 requests to
    Open-Meteo, and nothing reported it: `capture_weather()` swallows every
    failure by design, so the calls succeeded quietly and would have failed
    just as quietly. The weather path was not being tested at all -- a third
    party was answering.

    Enumerated from disk rather than from a list of the two files that had the
    problem, because the next one will be written by somebody who never read
    this. A module that drives a poll must install the guard.
    """

    #: Anything that runs a poll reaches the weather capture, which goes out
    #: over urllib rather than through the provider boundary.
    POLLS = ("--once", "do_poll(", "capture_weather", "backfill_weather")

    def modules(self):
        return sorted((ROOT / "tests").glob("test_*.py"))

    def test_every_suite_that_polls_blocks_outbound_http(self):
        missing = []
        for path in self.modules():
            text = path.read_text(encoding="utf-8")
            if not any(marker in text for marker in self.POLLS):
                continue
            if "block_outbound" not in text:
                missing.append(path.name)
        self.assertEqual(
            [], missing,
            f"these suites drive a poll and do not block outbound HTTP, so "
            f"they will call Open-Meteo for real: {missing}. "
            f"Add `from netguard import block_outbound` and call it in setUp.")

    def guard_module(self):
        """Imported by path rather than by name: `discover -s tests` puts this
        directory on sys.path and `-m unittest tests.x` does not, and a
        contract that only holds under one of them is not a contract."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "netguard", ROOT / "tests" / "netguard.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_guard_itself_exists_and_allows_loopback(self):
        """The dashboard, the settings API and the port-collision tests all
        bind 127.0.0.1. Blocking that would break the product's own tests, so
        the guard has to tell the internet from this machine."""
        netguard = self.guard_module()
        self.assertTrue(any(h.startswith("http://127.0.0.1")
                            for h in netguard.LOOPBACK))

    def test_the_guard_is_catchable_by_the_code_it_interrupts(self):
        """Deliberately an Exception, not a BaseException.

        The opposite choice from the unexpected-request guard in
        test_providers.py, and for the opposite reason: this one *should* be
        caught by the same swallowing that hid the problem, so the code under
        test behaves exactly as it would with no network -- which is the
        condition being simulated. The attempt is recorded either way.
        """
        netguard = self.guard_module()
        self.assertTrue(issubclass(netguard.OutboundBlocked, Exception))
        self.assertNotIsInstance(netguard.OutboundBlocked("x"), SystemExit)


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


class TestNoSuiteTouchesTheDevelopersOwnInstall(unittest.TestCase):
    """CONVENTIONS: never mutate the developer's own `~/.airo` from tests.

    It was being broken, and the maintainer found it rather than a test did.
    A suite run appended two fixture strings to a live install's log, between
    two real polls:

        WARN weather unavailable: the service is down
        WARN weather failed: ValueError: something else entirely

    They read that tail and reasonably concluded their monitor had died. It
    had not -- nothing was lost, and readings were never touched -- but "it
    only reached the log this time" is luck. The same module-level paths point
    at the real database, the real config and the real alert state.

    Two distinct routes, and the second is why a single guard was not enough:

      in-process   `poller.LOG_PATH` and friends are resolved at import, so
                   any test reaching code that calls `log()` writes to the
                   real file unless they are redirected. Fixed by a
                   `setUpModule` hook in every suite.
      subprocess   a test that spawns `python -c "import poller; ..."` is
                   outside every in-process guard. It needs the environment
                   overrides poller already honours. One test was running
                   `--prune` against the developer's real install.
    """

    def modules(self):
        return sorted((ROOT / "tests").glob("test_*.py"))

    def test_every_suite_redirects_the_real_paths(self):
        """Checked as a *call* inside setUpModule, not as a string in the file.

        The first version searched for the name anywhere in the text, so
        deleting the call and leaving the import behind passed. A grep for an
        identifier proves the identifier is mentioned.
        """
        missing = []
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            hooks = [n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "setUpModule"]
            called = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "redirect_airo_paths_for_module"
                for h in hooks for c in ast.walk(h))
            if not called:
                missing.append(path.name)
        self.assertEqual(
            [], missing,
            f"these suites do not redirect poller's paths, so anything in "
            f"them that logs writes to a real install: {missing}. Add the "
            f"setUpModule/tearDownModule hook from tests/homeguard.py.")

    def test_a_subprocess_running_poller_is_given_its_own_home(self):
        """The route the in-process guard cannot cover.

        Found per call site by walking the AST, not by grepping the file. The
        regex version flagged a suite that only *stubs* `subprocess.Popen` and
        never spawns anything -- and a check that names innocent files is one
        nobody reads. Two textual proxies had already misled this audit; this
        one looks at what is actually called.
        """
        spawners = {"run", "Popen", "check_output", "check_call", "call"}
        offenders = []
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr in spawners):
                    continue
                # Does this call launch a Python interpreter?
                launched = ast.dump(node.args[0]) if node.args else ""
                if "executable" not in launched and "python" not in launched:
                    continue
                if not any(k.arg == "env" for k in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            [], sorted(offenders),
            f"these spawn an interpreter without isolating its environment, "
            f"so it reads and writes the developer's own install: "
            f"{sorted(offenders)}. Pass env= with AIRO_DATA, AIRO_CONFIG and "
            f"HOME pointed at a temporary directory.")

    def test_the_guard_covers_every_path_poller_resolves_at_import(self):
        """Enumerated against poller's own globals, not a list kept by hand.

        A path added later -- a second log, a cache, a lock file -- would
        otherwise sit outside the guard and be discovered the same way this
        was.
        """
        # Both are already imported at module level, and it has to be the
        # module instance the suites actually use. Loading homeguard with
        # spec_from_file_location built a *second* module object whose
        # ORIGINALS was empty, so the check fell back to the live (already
        # redirected) paths, found nothing under ~/.airo, and passed with
        # LOG_PATH removed from the guard entirely.
        # Against ~/.airo, not the whole home directory. The first version
        # compared against $HOME and flagged EXAMPLE_CONFIG_PATH, which lives
        # in the repository -- and the repository happens to sit under the
        # developer's home. A check that reports a file it does not mean is
        # how a check stops being read.
        protected = real_airo_home()
        # The values poller resolved at IMPORT, not the ones in force now:
        # this contract runs inside a process where the guard has already
        # redirected them, so reading them live reports every path as safe --
        # which it did, and the check passed with LOG_PATH removed from the
        # guard entirely.
        candidates = ORIGINALS or {
            n: getattr(poller, n) for n in dir(poller)
            if isinstance(getattr(poller, n, None), Path) and n.isupper()}
        unguarded = []
        for name, value in sorted(candidates.items()):
            if not isinstance(value, Path):
                continue
            if protected == value or protected in value.parents:
                if name not in GUARDED:
                    unguarded.append(name)
        self.assertEqual(
            [], unguarded,
            f"poller resolves these paths inside the developer's home and "
            f"homeguard does not redirect them: {unguarded}")


class TestTheHomeGuardItselfWorks(unittest.TestCase):
    """The guard is only worth having if it can be shown to do its job.

    Every check above reads *files* and would pass against a guard that
    redirects nothing. These run it.
    """

    def test_it_redirects_pollers_paths_away_from_the_real_install(self):
        real = real_airo_home()
        redirect_airo_paths_for_module()
        try:
            for name in GUARDED:
                value = Path(getattr(poller, name))
                self.assertFalse(real == value or real in value.parents,
                                 f"{name} still points into the real install")
        finally:
            restore_airo_paths_for_module()

    def test_it_redirects_the_home_directory_too(self):
        """The route a redirect of the module globals cannot cover: anything
        resolving the home directory at *runtime*. `run_doctor()` scans for
        orphaned databases that way and opened the real one; `get_api_key()`
        would read a real key by the same route.
        """
        before = Path.home()
        redirect_airo_paths_for_module()
        try:
            self.assertNotEqual(before, Path.home(),
                                "HOME was not redirected, so anything calling "
                                "it at runtime still reaches the real install")
        finally:
            restore_airo_paths_for_module()

    def test_it_puts_everything_back(self):
        """A guard that leaked its redirection would silently break whatever
        ran next, which is worse than not having one."""
        before = (Path.home(), {n: getattr(poller, n) for n in GUARDED})
        redirect_airo_paths_for_module()
        restore_airo_paths_for_module()
        self.assertEqual(before[0], Path.home())
        self.assertEqual(before[1], {n: getattr(poller, n) for n in GUARDED})


class TestTheTrayReadoutInTheReadmeIsReal(unittest.TestCase):
    """The README shows the tray's menu as text rather than a screenshot.

    That is better for a reader — searchable, screen-readable, and checkable —
    but it is also a literal pasted into a document, which is exactly the shape
    that drifts. A screenshot at least looks obviously old; a code block does
    not.

    So it is checked against what the program actually prints. Structure only:
    the headings and the labels. The numbers are demo data and the ages are
    relative to now, and pinning either would make this fail for reasons that
    have nothing to do with the tray.
    """

    #: The lines the tray emits that the README claims it emits.
    STRUCTURE = ("Sources", "Rolling averages")

    def readme_block(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        marker = "airo-tray --print-menu"
        self.assertIn(marker, text,
                      "the README no longer says where its tray readout "
                      "came from, so nobody can reproduce it")
        # The fenced block above the reproduction instructions.
        blocks = re.findall(r"```\n(.*?)```", text, re.S)
        trays = [b for b in blocks if "Rolling averages" in b]
        self.assertEqual(1, len(trays),
                         f"expected exactly one tray readout block, found "
                         f"{len(trays)}")
        return trays[0]

    def test_the_block_has_the_shape_the_tray_prints(self):
        block = self.readme_block()
        for line in self.STRUCTURE:
            self.assertIn(line, block, f"the readout is missing {line!r}")

    def test_it_shows_more_than_one_source(self):
        """The point being made in the surrounding prose is that the tray
        names every source and its distance rather than picking one. A readout
        with a single source would contradict the sentence next to it."""
        block = self.readme_block()
        after = block.split("Sources", 1)[1].split("---", 1)[0]
        rows = [l for l in after.splitlines() if "µg" in l]
        self.assertGreaterEqual(len(rows), 2,
                                f"the readout shows {len(rows)} source(s); "
                                f"the prose beside it says it shows several")

    def test_every_named_place_in_it_is_invented(self):
        """Rule 2b. The readout is generated from tools/demo.py, and the
        README says so — this makes it true rather than claimed.

        Every source row, not just the block as a whole: checking for "Demo"
        anywhere passed with a source renamed to a real-sounding one, because
        the location line still said Demo Valley. A published readout naming a
        real instrument is a published sensor id.
        """
        block = self.readme_block()
        rows = [l for l in block.splitlines() if "µg" in l and "km" in l]
        self.assertTrue(rows, f"no source rows in the readout:\n{block}")
        for row in rows:
            self.assertIn("Demo", row,
                          f"this source is not demo data: {row.strip()!r}")

    def test_the_binary_still_prints_that_shape(self):
        """Run against the real binary when it has been built.

        Skipped rather than failed when it has not: the Python suite must stay
        runnable without a Rust toolchain, and CI builds the tray in its own
        job. Stated as a skip reason so a permanently-skipped test is visible
        rather than silently absent.
        """
        binary = ROOT / "tray" / "target" / "release" / "airo-tray"
        if not binary.exists():
            self.skipTest("tray not built (cd tray && cargo build --release)")

        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            gen = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "demo.py"),
                 "--into", str(demo), "--days", "3"],
                capture_output=True, text=True, timeout=180,
                env=dict(os.environ, HOME=str(Path(tmp) / "home")))
            self.assertEqual(0, gen.returncode, gen.stderr[-400:])

            out = subprocess.run(
                [str(binary), "--print-menu"], capture_output=True, text=True,
                timeout=60,
                env=dict(os.environ,
                         AIRO_DATA=str(demo / "data"),
                         AIRO_CONFIG=str(demo / "config.json"),
                         HOME=str(Path(tmp) / "home")))
        printed = out.stdout
        for line in self.STRUCTURE:
            self.assertIn(line, printed,
                          f"the tray no longer prints {line!r}, so the "
                          f"README's readout is now fiction:\n{printed}")


class TestNoTestWritesIntoTheRealInstall(unittest.TestCase):
    """Four routes into the developer's own `~/.airo` have been found and
    closed. This asserts the property rather than the four fixes.

    Redirecting `HOME` covers anything that resolves a path at call time. It
    does *not* cover a module-level constant, which froze the real home at
    import — `backup.BACKUP_DIR` did exactly that, and the whole suite wrote
    archives into the developer's real backups directory and rotated their
    genuine ones away. Nor does it cover `launchctl`, which is keyed on the
    uid rather than on HOME.

    So the check is on the shape: no shipped module may compute a path under
    the user's home at import time, because import happens before any guard
    can be installed.
    """

    def shipped_modules(self):
        """From the stager's own list, so a module added tomorrow is in scope.

        `tools/` is not importable from here, so the list is read out of the
        source rather than imported — the alternative is a second copy of the
        module names in this file, which is the list-that-stops-covering shape
        this project keeps being bitten by.
        """
        import ast
        stager = ast.parse(
            (ROOT / "tools" / "stage_bundle.py").read_text(encoding="utf-8"))
        names = []
        for node in stager.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "MODULES"
                            for t in node.targets)):
                names = [e.value for e in node.value.elts
                         if isinstance(e, ast.Constant)]
        return [ROOT / n for n in names if (ROOT / n).exists()]

    def test_no_module_resolves_a_home_path_at_import_time(self):
        """`Path.home()` inside a function is fine — it answers when asked.
        At module level it answers once, before any test can redirect it, and
        every later caller gets the developer's real directory."""
        import ast
        offenders = []
        for path in self.shipped_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:            # module level only
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr in ("home", "expanduser")):
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            [], offenders,
            "a home-relative path is resolved at import, before any guard "
            f"can redirect it: {offenders}")

    def test_the_check_can_actually_fail(self):
        """Guards the walk. A contract over an empty set passes and means
        nothing, which is how three checks here stayed green after the thing
        they enumerated moved."""
        import ast
        tree = ast.parse('X = Path.home() / "thing"\n')
        found = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "home"]
        self.assertEqual(1, len(found))

    def test_the_enumeration_finds_the_modules(self):
        self.assertGreater(len(self.shipped_modules()), 5,
                           "the module walk found almost nothing to check")


class TestOnlyOnePlaceOpensABrowser(unittest.TestCase):
    """A browser is a side effect that leaves the process, and there must be
    exactly one way to cause it.

    `open_page()` used `webbrowser.open()`, which tests stubbed. Adding
    `/usr/bin/open` ahead of it went past every one of those stubs, and the
    suite opened a real tab on each run — roughly fifteen of them before
    anybody connected the two, because the tests that recorded "which URL was
    opened" simply saw nothing and failed on an empty list.

    One seam means `tests/browserguard.py` has one thing to block. Two seams
    means the guard is a suggestion.
    """

    def shipped(self):
        import ast
        stager = ast.parse(
            (ROOT / "tools" / "stage_bundle.py").read_text(encoding="utf-8"))
        names = []
        for node in stager.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "MODULES"
                            for t in node.targets)):
                names = [e.value for e in node.value.elts
                         if isinstance(e, ast.Constant)]
        return [ROOT / n for n in names if (ROOT / n).exists()]

    def test_no_module_reaches_a_browser_outside_the_one_function(self):
        import ast
        offenders = []
        for path in self.shipped():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name == "launch_browser":
                    continue
                body = ast.dump(node)
                if "'webbrowser'" in body or "/usr/bin/open" in body:
                    offenders.append(f"{path.name}:{node.name}")
        self.assertEqual(
            [], offenders,
            "a browser is opened outside launch_browser, so the test guard "
            f"cannot cover it: {offenders}")

    def test_the_enumeration_finds_the_one_that_is_allowed(self):
        """Guards the walk: if the detector stopped matching, the test above
        would pass over a file that opens ten browsers."""
        import ast
        found = False
        for path in self.shipped():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "launch_browser"):
                    dumped = ast.dump(node)
                    found = ("'webbrowser'" in dumped
                             or "/usr/bin/open" in dumped)
        self.assertTrue(found,
                        "launch_browser no longer opens a browser, so the "
                        "check above is looking for something that is gone")

    def test_every_suite_that_can_open_one_installs_the_guard(self):
        """Enumerated from disk by what the file actually drives, not from a
        list — the same reasoning as the netguard contract beside it."""
        needs, missing = [], []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            drives = ("open_page(" in text or "first_run(" in text
                      or '"--open"' in text)
            if not drives:
                continue
            needs.append(path.name)
            if "browserguard" not in text:
                missing.append(path.name)
        self.assertTrue(needs, "no suite drives a page-opening path")
        self.assertEqual(
            [], missing,
            f"these suites can open a real browser window: {missing}. "
            f"Install tests/browserguard.py.")


class TestNoSuitePutsANotificationOnScreen(unittest.TestCase):
    """The third guard of this shape, and the third time it was needed.

    `netguard` stops a test reaching the internet. `browserguard` stops one
    opening a window — added after the suite opened fifteen tabs. This one was
    added after a suite exercising the alerting path with a deliberate
    400 µg/m³ emergency fixture delivered "Air quality: Hazardous — AQI 1600"
    to the maintainer's desktop.

    Four suites already stubbed `poller.notify` by hand, which is exactly the
    arrangement that works until somebody writes a fifth. Enumerated from disk
    by what each file actually drives, so the fifth is caught by existing
    rather than by remembering.
    """

    def suites(self):
        return sorted((ROOT / "tests").glob("test_*.py"))

    def test_every_suite_that_can_notify_installs_the_guard(self):
        drives, missing = [], []
        for path in self.suites():
            text = path.read_text(encoding="utf-8")
            can_notify = ("maybe_alert(" in text or "--test-alert" in text
                          or "poller.notify" in text)
            if not can_notify:
                continue
            drives.append(path.name)
            # Three ways to stop a notification reaching a screen, and the
            # question is whether one of them is in place — not which. The
            # guard is the general answer; stubbing `poller.notify` is what
            # four suites did before it existed; and stubbing the subprocess
            # is what `test_notifications.py` must do, because it is testing
            # `notify()` itself and the guard would replace its subject.
            prevented = ("notifyguard" in text
                         or "poller.notify =" in text
                         or "notify=" in text
                         or "poller.subprocess.run =" in text)
            if not prevented:
                missing.append(path.name)
        self.assertTrue(drives, "no suite drives the alerting path")
        self.assertEqual(
            [], missing,
            f"these suites can deliver a real notification: {missing}. "
            f"Install tests/notifyguard.py.")

    def test_the_guard_records_rather_than_refuses(self):
        """A suppressed alert is a health warning nobody got, so the product
        treats a refused notification as normal. A guard that raised would
        have every test exercise a branch none of them mean to."""
        import notifyguard
        guard = notifyguard.Guard()
        self.assertTrue(guard("Airo", "sub", "message"))
        self.assertEqual(1, len(guard.sent))
        self.assertIn("message", guard.messages[0])

    def test_the_enumeration_finds_the_suites_that_matter(self):
        """Guards the walk: a pattern that matched nothing would pass this
        contract while every suite notified freely."""
        found = [p.name for p in self.suites()
                 if "maybe_alert(" in p.read_text(encoding="utf-8")]
        self.assertTrue(found, "the alerting pattern matches no suite at all")

    def test_the_suite_that_tests_the_notifier_stubs_the_subprocess(self):
        """Named specifically, because it is the one file that must *not*
        install the guard: it is testing `notify()`, and replacing the subject
        of a test with a double leaves the test asserting nothing.

        Checked rather than exempted. An exemption list is the shape that
        stops covering; this asserts the alternative protection is actually
        there."""
        text = (ROOT / "tests" / "test_notifications.py").read_text(
            encoding="utf-8")
        self.assertIn("poller.subprocess.run =", text,
                      "the notifier's own suite no longer stubs the "
                      "subprocess, so it shells out for real")
        self.assertNotIn("notifyguard", text,
                         "the notifier's suite installed the guard, which "
                         "replaces the function it exists to test")


class TestNoSuiteReachesTheLoggedInSession(unittest.TestCase):
    """The fourth guard of this shape, and the least visible of the four.

    `netguard` stops a test reaching the internet, `browserguard` one opening a
    window, `notifyguard` one putting a notification on screen. Each of those
    announces itself: a request, a tab, a banner. This one is silent.

    `launchctl` addresses agents as `gui/<uid>/<label>` — by the session, keyed
    on a uid and a fixed label, never by HOME. A test running under a
    redirected home therefore removes a plist that was never there and unloads
    the *real* agent of the login session, and `systemctl --user` and Task
    Scheduler behave the same way. That is how the maintainer's poller stopped:
    the plists sat untouched, `launchctl list` showed nothing, and the last log
    line was a clean successful poll.

    `scheduler.run()` refuses these in the shipped code, which is the right
    place for the product's own guard. This contract is about the routes it
    cannot see — a suite reaching `subprocess.run` directly, or one that
    restores the real HOME and then drives an install — and it enumerates from
    disk, so the next suite to import `scheduler` is in scope without anybody
    remembering.
    """

    def suites(self):
        return sorted((ROOT / "tests").glob("test_*.py"))

    def test_every_suite_that_can_reach_one_installs_the_guard(self):
        drives, missing = [], []
        for path in self.suites():
            text = path.read_text(encoding="utf-8")
            if "import scheduler" not in text:
                continue
            drives.append(path.name)
            if "schedguard" not in text:
                missing.append(path.name)
        self.assertTrue(drives, "no suite drives the scheduler at all")
        self.assertEqual(
            [], missing,
            f"these suites can reach the session manager this machine is "
            f"logged in to, and unloading the developer's own agent leaves no "
            f"trace anywhere: {missing}. Install tests/schedguard.py.")

    def test_the_enumeration_finds_the_suites_that_matter(self):
        """Guards the walk: a detector matching nothing would pass this
        contract while every suite talked to launchd freely."""
        self.assertIn("test_scheduler.py", [
            p.name for p in self.suites()
            if "import scheduler" in p.read_text(encoding="utf-8")],
            "the scheduler's own suite is not being detected, so the check "
            "above is looking at nothing")

    def guard_module(self):
        """By path, not by name: `discover -s tests` puts this directory on
        sys.path and `-m unittest tests.x` does not."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "schedguard", ROOT / "tests" / "schedguard.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_guard_covers_every_tool_that_addresses_a_session(self):
        """Named literally *and* reconciled with the shipped list. Enumerating
        alone protects against something being added and missed, not against
        the enumeration itself being cut — narrowing either list to launchctl
        would shrink the loop with it and nothing would go red."""
        schedguard = self.guard_module()
        import scheduler
        for required in ("launchctl", "systemctl", "schtasks"):
            self.assertIn(required, schedguard.session_managers(),
                          f"{required} addresses the session by uid and is no "
                          f"longer guarded in the tests")
            self.assertIn(required, scheduler.SESSION_MANAGERS,
                          f"{required} is no longer guarded in the product")

    def test_the_guard_matches_the_spellings_a_platform_actually_uses(self):
        """`schtasks.exe` on Windows, and an absolute path from a launchd
        agent, whose PATH is not a login shell's. A rule naming one spelling is
        the shape that leaked 16,995 rows."""
        schedguard = self.guard_module()
        for argv in (["launchctl", "list"], ["/bin/launchctl", "list"],
                     ["schtasks.exe", "/query"],
                     [r"C:\Windows\System32\schtasks.exe", "/query"]):
            with self.subTest(argv=argv):
                self.assertTrue(schedguard._is_a_session_manager(argv),
                                f"{argv[0]} would reach the real session")
        self.assertIsNone(schedguard._is_a_session_manager(["true"]),
                          "an ordinary command is being blocked, and a guard "
                          "that refuses everything gets switched off")

    def test_the_guard_refuses_rather_than_reporting_success(self):
        """The opposite choice from browserguard and notifyguard, and
        deliberately so: `install()` reports "your schedule is registered" on
        the strength of this return code, so a guard answering 0 would let a
        test assert a schedule exists when nothing was ever registered."""
        schedguard = self.guard_module()
        guard = schedguard.Guard()
        result = guard.refuse(["launchctl", "bootout", "gui/501/x"], "launchctl")
        self.assertEqual(1, result.returncode)
        self.assertIn("logged-in session", result.stderr)
        self.assertEqual(1, len(guard.attempts))
        self.assertIn("bootout", guard.commands[0])


class TestNoFixtureDependsOnWhereTheClockIs(unittest.TestCase):
    """A test whose result depends on the minute it runs at.

    Readings written at the top of the current hour are up to fifty-nine
    minutes old by the time fusion judges them, and fusion declines to
    headline a stale reading. Three tests passed at five past and failed at
    five to, and the failure named a placement bug that did not exist.

    The same shape made the coverage gate fail about one run in three, and
    `tools/coverage-floor.json` records it as the thing to look for: *a test
    whose behaviour depends on the wall clock, not a reason to add headroom.*

    Enumerated from disk. A fixture that truncates `now` to the hour and then
    stores it as a reading has to say why, because the honest version is
    almost always "use `now`".
    """

    #: Truncating is fine for a *bucket* key, an evening window, or a day
    #: boundary — the whole point there is the hour. It is the combination of
    #: truncation and storing the result as a current reading that rots.
    ALLOWED = "clock-independent:"

    def suspects_in(self, name, text):
        """The offending fixtures in one file's source.

        Takes the text rather than reading it, so the filter can be shown a
        known-bad sample. Without that, "found nothing" and "the filter is
        broken" are the same result once every real fixture is marked — which
        is exactly the state this file is in, and a fault inverting the filter
        went unnoticed because of it.
        """
        pattern = re.compile(
            r"datetime\.now\([^)]*\)\s*\.replace\(\s*\n?\s*minute=0")
        found = []
        for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                # Scoped to the enclosing function rather than a byte count.
                # A fixed look-back put the marker out of range as soon as the
                # docstring explaining it grew, which made the exemption
                # depend on how much reasoning somebody wrote down.
                start = text.rfind("\n    def ", 0, match.start())
                body = text[start if start != -1 else 0:match.end() + 700]
                if self.ALLOWED in body:
                    continue
                if "insert_readings" not in body:
                    continue          # not stored as a reading; not this shape
                found.append(f"{name}:{line}")
        return found

    def suspects(self):
        found = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            found.extend(self.suspects_in(
                path.name, path.read_text(encoding="utf-8")))
        return found

    def test_no_fixture_stores_a_truncated_now_as_a_current_reading(self):
        self.assertEqual(
            [], self.suspects(),
            "these fixtures write a reading at the top of the current hour, "
            "so how fresh it looks depends on the minute the suite runs at. "
            "Use `datetime.now(timezone.utc)` for the newest row, or write "
            f"`{self.ALLOWED} <why>` nearby if the truncation is deliberate.")

    def all_matches(self):
        """Every hour-truncated `now`, marked or not.

        The check above passes when nothing is found, and with every fixture
        marked that is indistinguishable from a walk that has stopped
        matching. This counts what the pattern reaches before the exemption is
        applied, so the enumeration itself is asserted to be alive.
        """
        pattern = re.compile(
            r"datetime\.now\([^)]*\)\s*\.replace\(\s*\n?\s*minute=0")
        found = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                start = text.rfind("\n    def ", 0, match.start())
                body = text[start if start != -1 else 0:match.end() + 700]
                if "insert_readings" in body:
                    found.append(f"{path.name}:"
                                 f"{text.count(chr(10), 0, match.start()) + 1}")
        return found

    def test_the_walk_still_reaches_real_fixtures(self):
        """Guards the enumeration, not the pattern. Skipping every candidate
        makes the check above pass while it examines nothing — which is what
        happened when the filter was inverted."""
        self.assertTrue(
            self.all_matches(),
            "the walk finds no hour-truncated fixture anywhere, so the check "
            "above is passing over an empty set")

    def test_the_filter_flags_a_fixture_that_actually_has_the_problem(self):
        """Shown a known-bad sample, because every real fixture is marked and
        an empty result therefore proves nothing about the filter."""
        offending = (
            "class T:\n"
            "    def test_it(self):\n"
            "        base = datetime.now(timezone.utc).replace(\n"
            "            minute=0, second=0, microsecond=0)\n"
            "        store.insert_readings(conn, sid, "
            "[{'observed_utc': base}])\n")
        self.assertTrue(
            self.suspects_in("sample.py", offending),
            "the filter no longer flags a reading written at the top of the "
            "current hour")

    def test_the_filter_lets_a_marked_fixture_through(self):
        """The other half. A filter that flagged everything would be turned
        off by the first person it inconvenienced."""
        marked = (
            "class T:\n"
            "    def test_it(self):\n"
            "        \"\"\"clock-independent: buckets by hour.\"\"\"\n"
            "        base = datetime.now(timezone.utc).replace(\n"
            "            minute=0, second=0, microsecond=0)\n"
            "        store.insert_readings(conn, sid, "
            "[{'observed_utc': base}])\n")
        self.assertEqual([], self.suspects_in("sample.py", marked))

    def test_the_pattern_would_catch_the_shape_it_is_for(self):
        """Guards the walk. A pattern that matched nothing would pass the
        check above while every fixture drifted."""
        pattern = re.compile(
            r"datetime\.now\([^)]*\)\s*\.replace\(\s*\n?\s*minute=0")
        sample = ("base = datetime.now(timezone.utc).replace(\n"
                  "    minute=0, second=0, microsecond=0)\n"
                  "store.insert_readings(conn, sid, [{'observed_utc': base}])")
        self.assertTrue(pattern.search(sample),
                        "the detector no longer matches the shape it is for")
        self.assertIn("insert_readings", sample)


if __name__ == "__main__":
    unittest.main(verbosity=2)
