"""Brand-new-install tests.

The project has to work for someone who is not the author: a clean clone, an
empty home directory, no API keys, no prior config. Every one of these was
verified by hand against a real fresh clone; they exist so the path cannot
silently rot, because the person who would notice is a stranger who never
reports it — they just leave.

No network calls: providers are stubbed at the boundary.
"""

import ast
import getpass
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fusion   # noqa: E402
import poller  # noqa: E402
# Importable both ways: `discover -s tests` puts this directory on the path,
# `-m unittest tests.test_x` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from netguard import block_outbound  # noqa: E402
import setup   # noqa: E402
import store    # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



class FakeProvider(poller.Provider):
    """A keyless provider, so a first run needs no account at all."""

    slug = "fake"
    label = "Fake network"
    tier = "reference"
    accuracy_note = "test double"
    resolution_minutes = 60
    needs_key = False
    attribution = "Fake data"
    licence = "CC0"

    def current(self, src, key):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return ({"headline": 7.0, "now": 7.0},
                {"site_id": src.get("site_id"), "site_name": "Fake site",
                 "last_seen_utc": now.isoformat(timespec="seconds"),
                 "temperature_unit": "C"})

    def history(self, src, key, start, end):
        out, t = [], start
        while t < end:
            out.append({"utc": t, "pm25": 6.0})
            t += timedelta(hours=1)
        return out

    def discover(self, latitude, longitude, radius_km, key):
        return [{"site_id": "fake-1", "site_name": "Fake site",
                 "latitude": latitude, "longitude": longitude,
                 "distance_km": 0.5}]


class FreshInstallCase(unittest.TestCase):
    """Isolate HOME, the data directory and the config, as a clone would be."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.data = base / "data"

        self._env = os.environ.get("HOME")
        self._env_profile = os.environ.get("USERPROFILE")
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

        self._saved = (poller.DATA, poller.LATEST_PATH, poller.LOG_PATH,
                       poller.CONFIG_PATH, poller.CSV_PATH,
                       poller.ALERT_STATE_PATH)
        poller.DATA = self.data
        poller.LATEST_PATH = self.data / "latest.json"
        poller.LOG_PATH = self.data / "poller.log"
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.CSV_PATH = self.data / "readings.csv"
        poller.ALERT_STATE_PATH = self.data / "alert_state.json"
        poller.FORECAST_PENDING_PATH = self.data / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = self.data / "forecast_skill.json"

        poller.PROVIDERS["fake"] = FakeProvider()

        # A first run captures weather, which goes out over urllib rather than
        # through poller.http_get -- so stubbing the provider was not enough
        # and this suite was quietly calling Open-Meteo eight times a run.
        self.outbound = block_outbound(self)

        # The poller narrates to stdout, which buries a real failure in a wall
        # of routine progress lines. Capture it instead of printing it.
        self.logged = []
        self._log = poller.log
        poller.log = self.logged.append

    def tearDown(self):
        (poller.DATA, poller.LATEST_PATH, poller.LOG_PATH,
         poller.CONFIG_PATH, poller.CSV_PATH,
         poller.ALERT_STATE_PATH) = self._saved
        poller.log = self._log
        poller.PROVIDERS.pop("fake", None)
        if self._env is not None:
            os.environ["HOME"] = self._env
        if self._env_profile is not None:
            os.environ["USERPROFILE"] = self._env_profile
        else:
            os.environ.pop("USERPROFILE", None)
        self.tmp.cleanup()

    def write_config(self, cfg):
        poller.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        poller.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")


class TestNothingConfigured(FreshInstallCase):
    """What a clone looks like before setup has been run."""

    def test_config_loads_with_no_file_at_all(self):
        cfg = poller.load_config()
        self.assertEqual(poller.enabled_sources(cfg), [])
        self.assertEqual(cfg["location"]["name"], "")

    def test_defaults_carry_nobody_elses_location(self):
        """Shipping a real default location is how the first version put one
        person's suburb into every install."""
        cfg = poller.load_config()
        self.assertIsNone(cfg["location"]["latitude"])
        self.assertIsNone(cfg["location"]["longitude"])
        self.assertEqual(cfg["sources"], [])

    def test_network_status_still_answers(self):
        """The 'what could I add' list must work before anything is set up --
        it is how a new user discovers there is anything to add."""
        nets = poller.network_status(poller.load_config())
        self.assertTrue(nets)
        self.assertTrue(all(not n["in_use"] for n in nets))

    def test_at_least_one_network_needs_no_account(self):
        """Someone who will not sign up for anything must still be able to
        use this. If that ever stops being true, the barrier to a first run
        goes from zero to a signup form."""
        keyless = [n for n in poller.network_status(poller.load_config())
                   if not n["needs_key"]]
        self.assertTrue(keyless, "no network works without an account")

    def test_every_keyed_network_says_where_to_sign_up(self):
        for n in poller.network_status(poller.load_config()):
            if n["needs_key"]:
                self.assertTrue(n["signup_url"],
                                f"{n['provider']} needs a key but offers no link")


class TestFirstRun(FreshInstallCase):
    """A complete first poll with no API key anywhere."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "location": {"name": "Somewhere", "latitude": -33.5, "longitude": 151.0},
            "sources": [{"provider": "fake", "site_id": "fake-1",
                         "site_name": "Fake site", "latitude": -33.5,
                         "longitude": 151.0, "enabled": True}],
            "aqi_scale": "au",
            "backfill_days_on_first_run": 1,
            "alerts": {"enabled": False},
        })

    def test_first_poll_succeeds_without_any_key(self):
        cfg = poller.load_config()
        self.assertEqual(poller.get_api_key(cfg["sources"][0]), "")
        latest = poller.do_poll(cfg)
        self.assertIsNotNone(latest)
        self.assertIsNotNone(latest["aqi"])

    def test_first_run_seeds_history_so_charts_are_not_empty(self):
        poller.do_poll(poller.load_config())
        conn = store.connect(poller.db_path())
        try:
            rows = sum(c["rows"] for c in store.counts(conn))
        finally:
            conn.close()
        self.assertGreater(rows, 1, "first run should backfill, not just poll once")

    def test_latest_json_is_written_and_complete(self):
        poller.do_poll(poller.load_config())
        d = json.loads(poller.LATEST_PATH.read_text(encoding="utf-8"))
        for key in ("aqi", "band", "provenance", "sources", "networks",
                    "attributions", "scale_label"):
            self.assertIn(key, d, f"latest.json missing {key}")

    def test_data_directory_is_created_rather_than_assumed(self):
        self.assertFalse(self.data.exists())
        poller.do_poll(poller.load_config())
        self.assertTrue(poller.db_path().exists())

    def test_single_source_is_not_reported_as_corroborated(self):
        """With one instrument there is nothing to cross-check against, and
        claiming agreement would be a lie."""
        poller.do_poll(poller.load_config())
        d = json.loads(poller.LATEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(d["sources"][0]["corroboration"], "single_source")

    def test_first_run_is_announced_clearly(self):
        """A first run seeds history, which takes visibly longer than a poll.
        Saying so is the difference between 'working' and 'hung'."""
        poller.do_poll(poller.load_config())
        joined = " ".join(self.logged)
        self.assertIn("first run", joined.lower())

    def test_polling_twice_does_not_duplicate(self):
        poller.do_poll(poller.load_config())
        conn = store.connect(poller.db_path())
        try:
            before = sum(c["rows"] for c in store.counts(conn))
        finally:
            conn.close()
        poller.do_poll(poller.load_config())
        conn = store.connect(poller.db_path())
        try:
            after = sum(c["rows"] for c in store.counts(conn))
        finally:
            conn.close()
        self.assertLessEqual(after - before, 1)


class TestPortCollisions(unittest.TestCase):
    """A busy port has two very different causes and needs two answers.

    Another Airo squatting there serves old data and looks like a dead agent,
    so the user must be told. Something unrelated holding the port is merely
    in the way, and refusing to start a dashboard over it is a bad trade.
    """

    def test_finds_a_free_port_above_a_busy_one(self):
        """The probe must not hand back a port somebody is listening on.

        SO_REUSEADDR means different things per platform: on Windows it allows
        binding a port another socket is actively using, so a probe that sets
        it reported the busy port as free.
        """
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        try:
            free = poller._free_port(busy)
            self.assertIsNotNone(free, "no free port found at all")
            self.assertNotEqual(free, busy,
                                "probe returned a port that is in use")
            self.assertGreater(free, busy)

            # And the returned port must actually be bindable.
            with socket.socket() as check:
                check.bind(("127.0.0.1", free))
        finally:
            sock.close()

    def test_a_non_airo_listener_is_not_mistaken_for_airo(self):
        import socket
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            self.assertIsNone(poller._serving_this_project(port, timeout=0.5))
        finally:
            sock.close()

    def test_nothing_listening_reports_nothing(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertIsNone(poller._serving_this_project(port, timeout=0.5))


class TestCredentialProtection(unittest.TestCase):
    """secure_path must actually restrict, and must report honestly when it
    cannot -- a key that merely looks protected is worse than one known not
    to be."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_restricts_a_file(self):
        f = self.dir / "a.key"
        f.write_text("secret", encoding="utf-8")
        self.assertTrue(poller.secure_path(f))
        self.assertIs(poller.path_is_restricted(f), True)

    def test_restricts_a_directory(self):
        d = self.dir / "sub"
        d.mkdir()
        self.assertTrue(poller.secure_path(d, is_dir=True))
        self.assertIs(poller.path_is_restricted(d), True)

    def test_reports_none_for_a_path_that_does_not_exist(self):
        self.assertIsNone(poller.path_is_restricted(self.dir / "nope"))

    def test_a_permissive_file_is_reported_as_such(self):
        """The check must be capable of returning False, or it proves nothing."""
        if os.name == "nt":
            self.skipTest("world-readable is expressed via ACLs on Windows")
        f = self.dir / "open.txt"
        f.write_text("x", encoding="utf-8")
        os.chmod(f, 0o644)
        self.assertIs(poller.path_is_restricted(f), False)


class TestNoStalePathAssumptions(unittest.TestCase):
    """Nothing may assume the author's machine."""

    #: The directory every account's home sits under, on each platform we
    #: ship to. No name in here belongs to anybody: they are the shapes an
    #: absolute home path takes, so a path baked in on a macOS machine is
    #: still caught when the suite runs on Linux, and the reverse.
    HOME_ROOTS = ("/Users/", "/home/", "\\Users\\", "C:/Users/")

    def machine_markers(self):
        """Path fragments that exist only on the machine running the tests.

        Derived rather than written down. A literal username or checkout
        directory in this file would ship exactly the thing the check exists
        to keep out — the test would become the leak. Everything here is
        path-shaped (a full absolute path, or a name with separators either
        side) so a directory that merely shares a word with something in the
        source cannot raise a false alarm on a contributor's machine.
        """
        try:
            user = getpass.getuser()
        except Exception:                      # no passwd entry: minimal CI
            user = ""
        home = Path.home().resolve()
        markers = {str(home), home.as_posix()}
        markers |= {str(ROOT), ROOT.as_posix()}
        names = [user, home.name, ROOT.name] + [p.name for p in ROOT.parents]
        for name in names:
            # Short or generic names ("src", "airo", "code") appear in URLs
            # and module paths for perfectly good reasons; only a distinctive
            # directory name is evidence of somebody's own machine.
            if len(name) < 5 or name.startswith("."):
                continue
            markers |= {f"/{name}/", f"\\{name}\\"}
        return sorted(m for m in markers if len(m) > 2)

    def test_no_absolute_home_paths_in_source(self):
        markers = tuple(self.HOME_ROOTS) + tuple(self.machine_markers())
        offenders = []
        for name in ("poller.py", "store.py", "fusion.py", "scheduler.py",
                     "analyse.py", "setup.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for marker in markers:
                if marker in text:
                    offenders.append(f"{name}: {marker}")
        self.assertEqual(offenders, [], f"hardcoded paths: {offenders}")

    def test_no_text_file_is_read_without_an_explicit_encoding(self):
        """Path.read_text(encoding="utf-8") and open() use the LOCALE encoding, which is
        cp1252 on Windows. Any file containing a micro sign or an em dash then
        fails to decode -- so reading our own config, latest.json or a key file
        crashed there while working perfectly everywhere else."""
        import re as _re
        offenders = []
        for py in sorted(ROOT.glob("*.py")) + sorted((ROOT / "tests").glob("*.py")):
            text = py.read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if _re.search(r"\.read_text\(\s*\)", line):
                    offenders.append(f"{py.name}:{n} read_text() with no encoding")
                if _re.search(r"\.write_text\([^)]*\)", line) and "encoding=" not in line:
                    # Multi-line calls are checked by compilation, not here.
                    if line.rstrip().endswith(")"):
                        offenders.append(f"{py.name}:{n} write_text() with no encoding")
                if _re.search(r'\.open\(\s*newline="\s*"\s*\)', line):
                    offenders.append(f"{py.name}:{n} open() with no encoding")
        self.assertEqual(offenders, [],
                         "unencoded text I/O will break on Windows: "
                         + "; ".join(offenders))

    def test_config_resolves_outside_the_repo_by_default(self):
        """A config inside the working tree can be committed by accident."""
        env = os.environ.pop("AIRO_CONFIG", None)
        try:
            self.assertNotIn(str(ROOT), str(poller._resolve_config_path().parent),
                             "config should default outside the project")
        finally:
            if env is not None:
                os.environ["AIRO_CONFIG"] = env

    def test_example_config_ships_and_is_valid(self):
        example = ROOT / "config.example.json"
        self.assertTrue(example.exists(), "new users have nothing to copy")
        cfg = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(cfg["sources"], [], "example must not ship real sources")
        self.assertFalse(cfg["location"]["name"], "example must not ship a location")




class TestSetupCannotTrapAUser(unittest.TestCase):
    """An interactive prompt that can only be left by answering correctly is a
    dead end for the one person who cannot: the new user who does not yet know
    what it wants. The location prompt reprinted the same one-line error
    forever when answered blank, with no hint that 'coords' or Ctrl-C existed.
    """

    def _loops(self, src):
        import ast
        tree = ast.parse(src)
        return [n for n in ast.walk(tree)
                if isinstance(n, ast.While) and isinstance(n.test, ast.Constant)
                and n.test.value is True]

    def test_every_prompt_loop_has_a_way_out(self):
        for name in ("setup.py", "backup.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            for node in self._loops(src):
                body = ast.get_source_segment(src, node) or ""
                if "ask(" not in body and "ask_yes(" not in body:
                    continue
                self.assertTrue(
                    any(k in body for k in ("quit", "SystemExit", "break")),
                    f"{name}:{node.lineno} prompts in a loop with no escape")

    def test_the_location_prompt_offers_an_exit_and_escalates(self):
        src = (ROOT / "setup.py").read_text(encoding="utf-8")
        i = src.index("def choose_location")
        body = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("'quit'", body, "the prompt does not mention how to leave")
        self.assertIn("attempts", body,
                      "repeated failures must escalate, not repeat")
        self.assertIn("locate_by_ip", body.split("while True")[1],
                      "a stuck user is never offered the automatic fallback")


class TestLegacyKeyMigration(unittest.TestCase):
    """Pre-v0.4 kept the key at ~/.airo/apikey, because there was only one
    network. get_api_key() still reads it, but only for PurpleAir -- so a user
    on the old layout who adds a second network has an inconsistency they
    cannot see. --doctor tidies it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".airo").mkdir()
        self._home = poller.Path.home
        poller.Path.home = staticmethod(lambda: self.home)
        self.addCleanup(lambda: setattr(poller.Path, "home", self._home))

    def test_a_legacy_key_is_copied_to_the_current_name(self):
        (self.home / ".airo" / "apikey").write_text("ABC123\n", encoding="utf-8")
        self.assertTrue(poller.migrate_legacy_key())
        self.assertEqual(
            (self.home / ".airo" / "purpleair.key").read_text(encoding="utf-8").strip(),
            "ABC123")

    def test_the_original_is_left_in_place(self):
        """An older Airo, or a restored backup, must keep working."""
        legacy = self.home / ".airo" / "apikey"
        legacy.write_text("ABC123\n", encoding="utf-8")
        poller.migrate_legacy_key()
        self.assertTrue(legacy.exists(), "the legacy key was destroyed")

    def test_an_existing_modern_key_is_never_overwritten(self):
        (self.home / ".airo" / "apikey").write_text("OLD\n", encoding="utf-8")
        modern = self.home / ".airo" / "purpleair.key"
        modern.write_text("CURRENT\n", encoding="utf-8")
        self.assertFalse(poller.migrate_legacy_key())
        self.assertEqual(modern.read_text(encoding="utf-8").strip(), "CURRENT")

    def test_an_empty_legacy_file_is_not_migrated(self):
        (self.home / ".airo" / "apikey").write_text("\n", encoding="utf-8")
        self.assertFalse(poller.migrate_legacy_key())
        self.assertFalse((self.home / ".airo" / "purpleair.key").exists())

    def test_nothing_happens_when_there_is_no_legacy_key(self):
        self.assertFalse(poller.migrate_legacy_key())

    def test_the_migrated_key_is_not_world_readable(self):
        (self.home / ".airo" / "apikey").write_text("ABC123\n", encoding="utf-8")
        poller.migrate_legacy_key()
        self.assertTrue(poller.path_is_restricted(
            self.home / ".airo" / "purpleair.key"))


class TestSetupNeverSuggestsADeadStation(unittest.TestCase):
    """Distance alone chose a broken install. Setup suggested Midvale --
    the nearest station to a test location -- and the brand-new
    user's first poll returned "every source failed / No data". Several of
    the nearest stations publish no PM2.5 at all."""

    def sites(self):
        return [
            {"provider": "qld", "site_id": "mid", "site_name": "Midvale",
             "distance_km": 0.7, "reporting": False},
            {"provider": "qld", "site_id": "eas", "site_name": "Eastvale",
             "distance_km": 5.2, "reporting": False},
            {"provider": "qld", "site_id": "wbk", "site_name": "Westbrook",
             "distance_km": 6.4, "reporting": True},
        ]

    def test_the_nearest_reporting_station_wins_over_the_nearest_one(self):
        picks = setup.recommend(self.sites())
        ids = [p["site_id"] for p in picks]
        self.assertIn("wbk", ids)
        self.assertNotIn("mid", ids, "suggested a station that reports nothing")

    def test_an_unprobed_station_is_still_eligible(self):
        """Absence of a probe is not evidence of a fault, and suggesting
        nothing is worse than suggesting a site we could not check."""
        sites = [{"provider": "qld", "site_id": "aaa", "site_name": "A",
                  "distance_km": 1.0}]              # no 'reporting' key
        self.assertEqual([p["site_id"] for p in setup.recommend(sites)], ["aaa"])

    def test_a_probe_failure_does_not_condemn_a_station(self):
        sites = [{"provider": "qld", "site_id": "aaa", "site_name": "A",
                  "distance_km": 1.0, "reporting": None}]
        self.assertEqual([p["site_id"] for p in setup.recommend(sites)], ["aaa"])

    def test_something_is_still_suggested_when_every_station_is_dead(self):
        """A list with no suggestion at all reads as 'nothing here works' and
        leaves the user with no default to accept."""
        dead = [dict(s, reporting=False) for s in self.sites()]
        self.assertTrue(setup.recommend(dead),
                        "no suggestion offered when all probes failed")

    def test_a_sentinel_only_station_counts_as_not_reporting(self):
        """Southmoor publishes a real 24-hour average while its live
        channel returns -9999: history exists, but every poll would display
        "No data"."""
        class Fake:
            slug = "qld"
            needs_key = False
            def current(self, src, key):
                return {"headline": -9999.0, "now": -9999.0, "24hr": 3.6}, {}
        saved = poller.PROVIDERS.get("qld")
        poller.PROVIDERS["qld"] = Fake()
        try:
            self.assertIs(setup.probe_reporting(
                {"provider": "qld", "site_id": "sou"}), False)
        finally:
            poller.PROVIDERS["qld"] = saved

    def test_a_working_station_probes_true(self):
        class Fake:
            slug = "qld"
            needs_key = False
            def current(self, src, key):
                return {"headline": 3.9, "now": 3.9, "24hr": 6.1}, {}
        saved = poller.PROVIDERS.get("qld")
        poller.PROVIDERS["qld"] = Fake()
        try:
            self.assertIs(setup.probe_reporting(
                {"provider": "qld", "site_id": "wbk"}), True)
        finally:
            poller.PROVIDERS["qld"] = saved

    def test_a_network_error_probes_unknown_not_dead(self):
        class Fake:
            slug = "qld"
            needs_key = False
            def current(self, src, key):
                raise OSError("connection reset")
        saved = poller.PROVIDERS.get("qld")
        poller.PROVIDERS["qld"] = Fake()
        try:
            self.assertIsNone(setup.probe_reporting(
                {"provider": "qld", "site_id": "wbk"}))
        finally:
            poller.PROVIDERS["qld"] = saved

    def test_probing_is_capped_so_setup_cannot_hammer_an_api(self):
        self.assertLessEqual(setup.PROBE_LIMIT, 20)


class TestPortCollisionsAreHandledByCause(FreshInstallCase):
    """Two different collisions, two different right answers, and neither
    branch was exercised.

    Another Airo on the port means refusing: two servers make "which one am I
    looking at?" unanswerable. An unrelated program means moving, because
    refusing to open a dashboard over a port clash is a bad trade.
    """

    def test_another_airo_is_refused_rather_than_duplicated(self):
        real = poller._serving_this_project
        poller._serving_this_project = lambda port, timeout=1.5: "/some/other/airo"
        import socket
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            with self.assertRaises(SystemExit) as caught:
                poller.serve_forever(port)
        finally:
            poller._serving_this_project = real
            held.close()
        self.assertIn("already serving", str(caught.exception))
        self.assertIn("/some/other/airo", str(caught.exception),
                      "the user is not told which install has the port")

    def test_an_unrelated_program_makes_it_move_rather_than_refuse(self):
        real_probe = poller._serving_this_project
        real_free = poller._free_port
        poller._serving_this_project = lambda port, timeout=1.5: None
        poller._free_port = lambda start, tries=20: None      # none available
        import socket
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            with self.assertRaises(SystemExit) as caught:
                poller.serve_forever(port)
        finally:
            poller._serving_this_project = real_probe
            poller._free_port = real_free
            held.close()
        # Having tried to move and found nowhere, it says so and names the
        # setting -- rather than reporting the other-Airo message, which would
        # send the user hunting for a second copy that does not exist.
        said = str(caught.exception)
        self.assertIn("in use by something else", said)
        self.assertNotIn("already serving", said)


class TestKeyResolutionOrder(FreshInstallCase):
    """get_api_key decides which of three places a key comes from. Neither
    early return was exercised."""

    def test_a_keyless_provider_needs_no_key_at_all(self):
        """Returning "" rather than None matters: http_get must not send
        X-API-Key: None, which urllib rejects outright and which would break
        every keyless provider at once."""
        key = poller.get_api_key({"provider": "fake"})
        self.assertEqual("", key)
        self.assertIsNotNone(key)

    def test_a_keyless_provider_sends_no_key_even_if_a_file_exists(self):
        """A stray fake.key -- left by an experiment, or by a provider that
        used to need one -- must not start being sent to a network that never
        asked for it."""
        keydir = self.home / ".airo"
        keydir.mkdir(parents=True, exist_ok=True)
        (keydir / "fake.key").write_text("a-key-nobody-asked-for", encoding="utf-8")
        self.assertEqual("", poller.get_api_key({"provider": "fake"}))

    def test_the_environment_wins_over_the_file(self):
        """So a key can be supplied to a scheduled run without writing it to
        disk at all."""
        keydir = self.home / ".airo"
        keydir.mkdir(parents=True, exist_ok=True)
        (keydir / "purpleair.key").write_text("from-the-file", encoding="utf-8")
        os.environ["PURPLEAIR_API_KEY"] = "from-the-environment"
        try:
            self.assertEqual("from-the-environment",
                             poller.get_api_key({"provider": "purpleair"}))
        finally:
            os.environ.pop("PURPLEAIR_API_KEY", None)

    def test_the_file_is_used_when_the_environment_is_empty(self):
        keydir = self.home / ".airo"
        keydir.mkdir(parents=True, exist_ok=True)
        (keydir / "purpleair.key").write_text("from-the-file", encoding="utf-8")
        os.environ.pop("PURPLEAIR_API_KEY", None)
        self.assertEqual("from-the-file",
                         poller.get_api_key({"provider": "purpleair"}))


class TestProbingASiteBeforeSuggestingIt(FreshInstallCase):
    """probe_reporting answers True / False / None, and the difference
    matters: None means the probe failed and must not be held against the
    station, while False means the station really publishes nothing."""

    def test_an_unknown_provider_cannot_be_probed(self):
        self.assertIsNone(poller.probe_reporting({"provider": "nonexistent"}))

    def test_a_provider_answering_with_nonsense_is_not_the_stations_fault(self):
        class Weird(poller.Provider):
            slug, needs_key, resolution_minutes = "weird", False, 60
            label, tier, accuracy_note = "Weird", "reference", ""
            attribution, licence = "Weird data", "CC0"
            def current(self, src, key):
                return "not a dict", {}
        poller.PROVIDERS["weird"] = Weird()
        try:
            self.assertIsNone(poller.probe_reporting({"provider": "weird",
                                                      "site_id": "1"}))
        finally:
            poller.PROVIDERS.pop("weird", None)

    def test_a_search_of_a_network_that_does_not_exist_is_reported(self):
        """Named as unknown, not as whatever exception came out of using None
        as a provider. "AttributeError: NoneType has no attribute discover" is
        a true statement about our code and says nothing to the user."""
        found, failures = poller.discover_sites(
            {"latitude": -33.5, "longitude": 151.0}, 25, ["nonexistent"])
        self.assertEqual([], found)
        self.assertEqual("unknown network", failures["nonexistent"])


class TestLegacyCsvImport(FreshInstallCase):
    """The pre-v0.5 readings.csv path. Both refusals were unexercised."""

    def test_no_csv_means_nothing_to_import(self):
        cfg = {"sources": [{"provider": "fake", "site_id": "1", "enabled": True}]}
        self.assertFalse(poller.migrate_legacy_csv(cfg))

    def test_a_csv_with_no_configured_source_is_not_guessed_at(self):
        """Importing a single-source CSV requires knowing which source it
        belongs to. Attaching it to the wrong one would be worse than not
        importing it."""
        poller.CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        poller.CSV_PATH.write_text("utc,pm25_10min\n2026-07-01T00:00:00+00:00,5.0\n",
                                   encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            poller.migrate_legacy_csv({"sources": []})
        self.assertIn("does not record which", str(caught.exception),
                      "refused without explaining what to do about it")


class TestWhereTheConfigAndDataAreFound(unittest.TestCase):
    """Resolution order, which decides whether an upgrade keeps reading the
    database it already has or quietly starts a blank one beside it.

    Not a FreshInstallCase: these functions are what *produce* the paths the
    harness overrides, so they have to be called with the environment set and
    the module constants left alone.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        (self.home / ".airo").mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in
                     ("HOME", "USERPROFILE", "AIRO_CONFIG", "AIRO_DATA")}
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        os.environ.pop("AIRO_CONFIG", None)
        os.environ.pop("AIRO_DATA", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_the_environment_overrides_everything(self):
        """So a test run, or a second profile, can be pointed elsewhere
        without editing anyone's real configuration."""
        chosen = self.base / "elsewhere" / "config.json"
        os.environ["AIRO_CONFIG"] = str(chosen)
        self.assertEqual(chosen, poller._resolve_config_path())

        data = self.base / "elsewhere" / "data"
        os.environ["AIRO_DATA"] = str(data)
        self.assertEqual(data, poller._resolve_data_dir())

    def test_the_home_config_is_preferred_over_one_in_the_checkout(self):
        """A config inside a git working tree holds a location and a chosen
        sensor. It is supported for development and must never win over the
        user's own.

        A checkout-local config has to actually exist for this to distinguish
        anything — without one the fallback returns the home path regardless,
        and the test passes with the preference removed. `HERE` is pointed at
        a temp directory rather than writing config.json into the repository,
        which is gitignored, hook-blocked and CI-blocked for good reason.
        """
        (self.home / ".airo" / "config.json").write_text("{}", encoding="utf-8")
        fake_checkout = self.base / "checkout"
        fake_checkout.mkdir()
        (fake_checkout / "config.json").write_text("{}", encoding="utf-8")

        saved = poller.HERE
        poller.HERE = fake_checkout
        try:
            self.assertEqual(self.home / ".airo" / "config.json",
                             poller._resolve_config_path(),
                             "a config in the checkout won over the user's own")
        finally:
            poller.HERE = saved

    def test_a_checkout_config_is_used_when_there_is_no_home_one(self):
        """The development path, which must still work."""
        fake_checkout = self.base / "checkout"
        fake_checkout.mkdir()
        (fake_checkout / "config.json").write_text("{}", encoding="utf-8")
        saved = poller.HERE
        poller.HERE = fake_checkout
        try:
            self.assertEqual(fake_checkout / "config.json",
                             poller._resolve_config_path())
        finally:
            poller.HERE = saved

    def test_a_checkout_config_is_used_only_when_there_is_no_other(self):
        local = poller.HERE / "config.json"
        if local.exists():
            self.skipTest("this checkout has a real config.json")
        self.assertEqual(self.home / ".airo" / "config.json",
                         poller._resolve_config_path(),
                         "a first run must resolve to where setup will write")

    def test_an_existing_checkout_database_is_preferred_over_an_empty_home_one(self):
        """The upgrade path. Choosing the empty ~/.airo/data over a full
        ./data would start a blank database beside years of readings and look
        like everything was fine."""
        legacy = poller.HERE / "data"
        if (legacy / "airo.db").exists():
            self.assertEqual(legacy, poller._resolve_data_dir())
        else:
            self.assertEqual(self.home / ".airo" / "data",
                             poller._resolve_data_dir())

    def test_a_configured_data_dir_beats_the_defaults(self):
        target = self.base / "on-a-volume"
        (self.home / ".airo" / "config.json").write_text(
            json.dumps({"data_dir": str(target)}), encoding="utf-8")
        saved = poller.CONFIG_PATH
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        try:
            self.assertEqual(target, poller._resolve_data_dir())
        finally:
            poller.CONFIG_PATH = saved


class TestReportingFilePermissionsHonestly(unittest.TestCase):
    """path_is_restricted must be able to answer "I do not know". A function
    that returns True when it cannot check is worse than one that has no
    opinion, because the user is told their key is safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_path_that_does_not_exist_is_unknown_not_unsafe(self):
        self.assertIsNone(poller.path_is_restricted(self.dir / "absent"))

    def test_a_check_that_cannot_run_is_unknown_not_safe(self):
        """On Windows the answer comes from icacls. If icacls fails, the
        honest answer is None -- returning True would tell someone their
        credential is protected on the evidence of nothing."""
        if os.name != "nt":
            self.skipTest("the icacls path only runs on Windows")
        f = self.dir / "a.key"
        f.write_text("x", encoding="utf-8")
        real = poller.subprocess.run
        poller.subprocess.run = lambda *a, **k: type(
            "R", (), {"returncode": 1, "stdout": ""})()
        try:
            self.assertIsNone(poller.path_is_restricted(f))
        finally:
            poller.subprocess.run = real


class TestPollingWithNothingConfigured(FreshInstallCase):

    def test_a_poll_with_no_sources_says_what_to_run(self):
        """Returning None rather than raising: the poller runs unattended on a
        schedule, and a traceback in a log nobody reads is not a message."""
        self.write_config({"sources": []})
        self.assertIsNone(poller.do_poll(poller.load_config()))
        self.assertTrue(any("setup.py" in line for line in self.logged),
                        "the user is told nothing happened and not why")


class TestNothingEscapesTheIsolationTheHarnessProvides(unittest.TestCase):
    """A path captured at import does not follow CONFIG_PATH when a test
    repoints it, so the test still reads -- and writes -- the real user's
    files.

    This is not hypothetical. `DATA_MARKER` was a module constant, so a test
    that isolated the config still read the developer's own
    ~/.airo/data-location. `--migrate-data` with no explicit source asks
    other_databases() where to migrate *from*, that answered with the real
    data directory, and the test suite moved it aside. Repeatedly.
    """

    def test_the_data_marker_follows_the_config_path(self):
        saved = poller.CONFIG_PATH
        poller.CONFIG_PATH = Path("/tmp/somewhere-else/config.json")
        try:
            self.assertEqual(Path("/tmp/somewhere-else/data-location"),
                             poller.data_marker_path())
        finally:
            poller.CONFIG_PATH = saved

    # Paths derived from DATA at import. They are real files under the user's
    # home, and a harness that repoints DATA without repointing these writes
    # to the user's own install. Every harness currently sets all four -- this
    # is what stops the next one forgetting.
    #: Grew from four to six when Phase C added its two ledgers, and this
    #: guard is what noticed — which is the point of it. The name says four
    #: for the shape of the rule, not the count.
    DERIVED_FROM_DATA = ["LATEST_PATH", "LOG_PATH", "CSV_PATH",
                         "ALERT_STATE_PATH", "FORECAST_PENDING_PATH",
                         "FORECAST_SKILL_PATH"]

    def test_a_harness_that_moves_the_data_dir_moves_everything_under_it(self):
        """The failure this prevents: a test isolates DATA, believes it is
        sandboxed, and writes latest.json or the log into the real ~/.airo.

        DATA_MARKER was exactly this and was missed because it was derived
        from CONFIG_PATH rather than DATA, so it did not look like one of the
        four. It is a function now; these four are still constants because
        converting them touches every call site, and that refactor is not
        something to do in the same change as the incident it would prevent.
        """
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            src = path.read_text(encoding="utf-8")
            if "poller.DATA =" not in src:
                continue
            for name in self.DERIVED_FROM_DATA:
                # An *assignment*, not a mention. Checking for the name alone
                # passed on a harness that only referenced it while restoring
                # it in tearDown -- which is the half that does not isolate
                # anything.
                if f"poller.{name} =" in src:
                    continue
                self.fail(f"{path.name} repoints poller.DATA without "
                          f"repointing poller.{name}, so anything writing "
                          f"there lands in the real ~/.airo")

    def test_the_four_derived_paths_are_still_the_only_ones(self):
        """If a fifth appears, the rule above has to learn about it — silently
        gaining one is how the marker escaped in the first place."""
        import ast
        tree = ast.parse((ROOT / "poller.py").read_text(encoding="utf-8"))
        derived = []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not names or not names[0].isupper():
                continue
            text = ast.unparse(node)
            if "DATA /" in text or "CONFIG_PATH." in text:
                derived.append(names[0])
        self.assertEqual(sorted(self.DERIVED_FROM_DATA), sorted(derived),
                         "a module constant derived from a user path at "
                         "import appeared or disappeared")


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main(verbosity=2)
