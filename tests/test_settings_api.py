"""The settings API — what a settings UI is allowed to see.

Every configuration path in Airo used to end in a terminal. Replacing that with
a served page means the local HTTP server now describes the whole installation
to a browser, and the thing that makes that safe is not the UI: it is that the
payload is built field by field and scrubbed on the way out.

The tests that matter here are the negative ones. A settings page that renders
correctly and leaks a key is a worse outcome than no settings page.

No network calls: providers are stubbed at the boundary.
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fusion   # noqa: E402
import poller   # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)


SECRET = "sk-this-must-never-be-served-0123456789"


class KeylessProvider(poller.Provider):
    slug = "fakefree"
    label = "Fake keyless network"
    tier = "reference"
    accuracy_note = "test double"
    resolution_minutes = 60
    needs_key = False
    attribution = "Fake data"
    licence = "CC0"

    def current(self, src, key):
        return ({"headline": 7.0, "now": 7.0}, {"site_id": src.get("site_id")})

    def history(self, src, key, start, end):
        return [{"utc": start, "pm25": 7.0}]

    def discover(self, latitude, longitude, radius_km, key):
        return [{"site_id": "1", "site_name": "Free site", "distance_km": 1.2,
                 "latitude": latitude, "longitude": longitude}]


class KeyedProvider(KeylessProvider):
    slug = "fakepaid"
    label = "Fake keyed network"
    tier = "consumer"
    needs_key = True
    key_env = "FAKEPAID_API_KEY"
    key_url = "https://example.invalid/signup"


class SettingsCase(unittest.TestCase):
    """Isolate HOME, the data directory and the config, as a clone would be."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        (self.home / ".airo").mkdir(parents=True)
        self.data = base / "data"
        self.data.mkdir()

        self._env = (os.environ.get("HOME"), os.environ.get("USERPROFILE"))
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self._keyenv = os.environ.pop("FAKEPAID_API_KEY", None)

        self._saved = (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
                       poller.LOG_PATH, poller.CSV_PATH, poller.ALERT_STATE_PATH)
        poller.DATA = self.data
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        poller.LATEST_PATH = self.data / "latest.json"
        poller.LOG_PATH = self.data / "poller.log"
        poller.CSV_PATH = self.data / "readings.csv"
        poller.ALERT_STATE_PATH = self.data / "alert_state.json"
        poller.FORECAST_PENDING_PATH = self.data / "forecast_pending.json"
        poller.FORECAST_SKILL_PATH = self.data / "forecast_skill.json"

        poller.PROVIDERS["fakefree"] = KeylessProvider()
        poller.PROVIDERS["fakepaid"] = KeyedProvider()

        self.logged = []
        self._log = poller.log
        poller.log = self.logged.append

    def tearDown(self):
        (poller.DATA, poller.CONFIG_PATH, poller.LATEST_PATH,
         poller.LOG_PATH, poller.CSV_PATH, poller.ALERT_STATE_PATH) = self._saved
        poller.log = self._log
        poller.PROVIDERS.pop("fakefree", None)
        poller.PROVIDERS.pop("fakepaid", None)
        home, profile = self._env
        if home is not None:
            os.environ["HOME"] = home
        if profile is not None:
            os.environ["USERPROFILE"] = profile
        else:
            os.environ.pop("USERPROFILE", None)
        if self._keyenv is not None:
            os.environ["FAKEPAID_API_KEY"] = self._keyenv
        self.tmp.cleanup()

    def write_config(self, cfg):
        poller.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")

    def configured(self, **over):
        cfg = {
            "location": {"name": "Testville", "latitude": -27.0,
                         "longitude": 153.0, "timezone": "Australia/Brisbane"},
            "sources": [{"provider": "fakefree", "site_id": "1",
                         "site_name": "Free site", "latitude": -27.0,
                         "longitude": 153.0, "enabled": True}],
        }
        cfg.update(over)
        return cfg


class TestNoCredentialIsEverServed(SettingsCase):
    """The load-bearing tests. Hard rule 2: never log, print or serve a key."""

    def test_a_private_sensor_read_key_is_reported_as_presence_only(self):
        """`read_key` is the credential nobody expects, because unlike every
        other key it lives inside config.json rather than ~/.airo/<p>.key."""
        cfg = self.configured()
        cfg["sources"][0]["read_key"] = SECRET
        self.write_config(cfg)

        payload = poller.settings_payload(poller.load_config())

        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertTrue(payload["sources"][0]["has_read_key"])
        self.assertNotIn("read_key", payload["sources"][0])

    def test_a_source_without_a_read_key_says_so(self):
        """The flag has to be capable of being False, or it proves nothing."""
        self.write_config(self.configured())
        payload = poller.settings_payload(poller.load_config())
        self.assertFalse(payload["sources"][0]["has_read_key"])

    def test_a_key_on_disk_never_reaches_the_payload(self):
        (self.home / ".airo" / "fakepaid.key").write_text(SECRET, encoding="utf-8")
        cfg = self.configured()
        cfg["sources"].append({"provider": "fakepaid", "site_id": "9",
                               "enabled": True})
        self.write_config(cfg)

        payload = poller.settings_payload(poller.load_config())

        self.assertNotIn(SECRET, json.dumps(payload))
        paid = [s for s in payload["sources"] if s["provider"] == "fakepaid"][0]
        self.assertTrue(paid["has_key"], "presence must still be reported")

    def test_a_key_in_the_environment_never_reaches_the_payload(self):
        os.environ["FAKEPAID_API_KEY"] = SECRET
        cfg = self.configured()
        cfg["sources"].append({"provider": "fakepaid", "site_id": "9",
                               "enabled": True})
        self.write_config(cfg)
        try:
            payload = poller.settings_payload(poller.load_config())
        finally:
            os.environ.pop("FAKEPAID_API_KEY", None)
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_the_scrub_catches_a_credential_nobody_thought_about(self):
        """The backstop behind settings_payload().

        settings_payload() builds its output field by field, so it is safe by
        construction -- until someone adds a field in a year without reading
        why. This is the layer that catches that, so it is tested against a
        shape settings_payload() does not currently produce.
        """
        nested = {"a": {"b": [{"api_key": SECRET, "site": "x"}]},
                  "token": SECRET, "harmless": "visible"}

        out = poller.scrub_secrets(nested)

        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(out["harmless"], "visible")
        self.assertTrue(out["has_token"])
        self.assertTrue(out["a"]["b"][0]["has_api_key"])
        self.assertEqual(out["a"]["b"][0]["site"], "x")

    def test_the_scrub_does_not_modify_what_it_was_given(self):
        original = {"read_key": SECRET}
        poller.scrub_secrets(original)
        self.assertEqual(original["read_key"], SECRET,
                         "scrubbing a config in place would break the poller")

    def test_an_empty_credential_reads_as_absent_not_present(self):
        self.assertFalse(poller.scrub_secrets({"read_key": ""})["has_read_key"])


class TestThePayloadDescribesTheWholeInstall(SettingsCase):

    def test_it_covers_every_settable_area(self):
        self.write_config(self.configured())
        p = poller.settings_payload(poller.load_config())
        for area in ("location", "sources", "fusion", "aqi_scale", "alerts",
                     "data", "networks", "choices", "poll_minutes",
                     "retention_days", "auto_backup", "serve_port"):
            self.assertIn(area, p, f"a settings page cannot edit {area}")

    def test_a_source_carries_the_provider_facts_the_page_must_not_restate(self):
        self.write_config(self.configured())
        src = poller.settings_payload(poller.load_config())["sources"][0]
        self.assertEqual(src["label"], "Fake keyless network")
        self.assertEqual(src["tier"], "reference")
        self.assertEqual(src["resolution_minutes"], 60)
        self.assertFalse(src["needs_key"])

    def test_a_source_naming_an_unknown_provider_is_still_shown(self):
        """Surface, don't drop. A source the code no longer recognises is the
        one the user most needs to see in order to fix it."""
        cfg = self.configured()
        cfg["sources"].append({"provider": "retired", "site_id": "7",
                               "enabled": True})
        self.write_config(cfg)

        rows = poller.settings_payload(poller.load_config())["sources"]

        gone = [s for s in rows if s["provider"] == "retired"][0]
        self.assertFalse(gone["known_provider"])

    def test_the_data_panel_names_an_abandoned_database(self):
        """data_dir is configurable, which is a way to abandon a database.
        The settings page is exactly where that must not be silent."""
        self.write_config(self.configured())
        p = poller.settings_payload(poller.load_config())
        self.assertIn("other_databases", p["data"])
        self.assertEqual(str(self.data), p["data"]["data_dir"])


class TestChoicesComeFromPython(SettingsCase):
    """The page must never restate a list Python owns -- that is hard rule 7
    one level up. A fusion rule added in Python has to appear in the UI without
    anyone editing HTML, and a rule removed must not linger there."""

    def test_fusion_rules_are_whatever_fusion_says_they_are(self):
        self.write_config(self.configured())
        p = poller.settings_payload(poller.load_config())
        self.assertEqual(list(fusion.RULES), p["choices"]["fusion_rules"])

    def test_scales_are_whatever_the_scale_table_says_they_are(self):
        self.write_config(self.configured())
        p = poller.settings_payload(poller.load_config())
        self.assertEqual(sorted(poller.SCALES),
                         sorted(s["name"] for s in p["choices"]["aqi_scales"]))

    def test_a_new_rule_appears_without_touching_the_settings_code(self):
        self.write_config(self.configured())
        saved = fusion.RULES
        fusion.RULES = tuple(list(saved) + ["invented"])
        try:
            p = poller.settings_payload(poller.load_config())
        finally:
            fusion.RULES = saved
        self.assertIn("invented", p["choices"]["fusion_rules"])


class TestAlertsAreReportedAsTheyWillActuallyFire(SettingsCase):
    """A settings page showing a blank where alerting is using a default is
    worse than showing nothing: it invites the user to believe no threshold is
    set while one is firing."""

    def test_an_unset_threshold_reports_the_default_in_force(self):
        self.write_config(self.configured())
        alerts = poller.settings_payload(poller.load_config())["alerts"]
        self.assertEqual(poller.ALERT_DEFAULTS["threshold_aqi"],
                         alerts["threshold_aqi"])
        self.assertTrue(alerts["enabled"])

    def test_a_configured_value_wins(self):
        self.write_config(self.configured(alerts={"threshold_aqi": 40,
                                                  "enabled": False}))
        alerts = poller.settings_payload(poller.load_config())["alerts"]
        self.assertEqual(40, alerts["threshold_aqi"])
        self.assertFalse(alerts["enabled"])

    def test_a_setting_the_defaults_do_not_know_about_survives(self):
        """Filtering to known keys would silently discard a user's setting and
        change behaviour elsewhere."""
        self.write_config(self.configured(alerts={"experimental": 3}))
        alerts = poller.effective_alerts(poller.load_config())
        self.assertEqual(3, alerts["experimental"])

    def test_the_alerting_path_and_the_settings_api_read_the_same_defaults(self):
        """The regression this guards: two copies of a default mean the page
        displays one threshold while the alert fires on another."""
        import inspect
        src = inspect.getsource(poller.maybe_alert)
        self.assertIn("effective_alerts(cfg)", src)
        self.assertNotIn('a.get("threshold_aqi", 67)', src)


class TestTheEndpointServesIt(SettingsCase):
    """Through a real server on a real socket, because the handler's routing is
    the part a unit test of the payload cannot reach."""

    def setUp(self):
        super().setUp()
        cfg = self.configured()
        cfg["sources"][0]["read_key"] = SECRET
        self.write_config(cfg)
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def post(self, path, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        if token:
            req.add_header("X-Airo-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_the_settings_endpoint_answers(self):
        status, body = self.get("/api/settings")
        self.assertEqual(200, status)
        self.assertEqual("Testville", json.loads(body)["location"]["name"])

    def test_the_served_body_carries_no_credential(self):
        _, body = self.get("/api/settings")
        self.assertNotIn(SECRET, body)

    def test_an_unknown_api_path_is_still_a_404(self):
        try:
            status, _ = self.get("/api/nonsense")
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(404, status)

    def test_a_write_to_an_unknown_route_is_a_404(self):
        status, _ = self.post("/api/nonsense", token=poller.server_token())
        self.assertEqual(404, status)


class TestTheServerRefusesWhatABrowserCanBeMadeToSend(SettingsCase):
    """Binding to 127.0.0.1 keeps other machines out, not other pages.

    Every site the user visits can reach this server from inside their browser.
    Four checks guard it; each is tested alone, because a chain where one link
    is load-bearing and the rest are decoration looks identical to a chain that
    works — right up until the load-bearing one is refactored away.
    """

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, path, method="POST", headers=None, data=b"{}"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data if method == "POST" else None, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), dict(e.headers)

    def authorised(self, **over):
        h = {"Content-Type": "application/json",
             "X-Airo-Token": poller.server_token(),
             "Origin": f"http://127.0.0.1:{self.port}"}
        h.update(over)
        return h

    # -- the chain, one link at a time ---------------------------------

    def test_a_fully_authorised_write_reaches_routing(self):
        """The control. Without it, every test below could be passing because
        the whole chain is broken rather than because one check works."""
        status, _, _ = self.request("/api/settings", headers=self.authorised())
        self.assertEqual(200, status, "the guard chain refused a valid request")

    def test_a_rebound_hostname_is_refused(self):
        """DNS rebinding: a name the attacker owns, resolved to 127.0.0.1, is
        same-origin as far as the browser is concerned, so an Origin check
        never sees it."""
        status, body, _ = self.request(
            "/api/settings", headers=self.authorised(Host="airo.attacker.invalid"))
        self.assertEqual(403, status)
        self.assertIn("loopback", body)

    def test_another_site_is_refused(self):
        status, body, _ = self.request(
            "/api/settings", headers=self.authorised(Origin="https://evil.invalid"))
        self.assertEqual(403, status)
        self.assertIn("cross-origin", body)

    def test_a_form_encoded_write_is_refused(self):
        """Form and text/plain bodies are CORS "simple requests" — they reach a
        server with no preflight at all. Refusing them is what forces a
        cross-origin caller into a preflight this server never answers."""
        for ctype in ("application/x-www-form-urlencoded", "text/plain",
                      "multipart/form-data"):
            with self.subTest(ctype=ctype):
                status, body, _ = self.request(
                    "/api/settings", headers=self.authorised(**{"Content-Type": ctype}))
                self.assertEqual(415, status)
                self.assertIn("application/json", body)

    def test_a_write_without_the_token_is_refused(self):
        headers = self.authorised()
        headers.pop("X-Airo-Token")
        status, body, _ = self.request("/api/settings", headers=headers)
        self.assertEqual(403, status)
        self.assertIn("token", body)

    def test_a_write_with_the_wrong_token_is_refused(self):
        status, _, _ = self.request(
            "/api/settings", headers=self.authorised(**{"X-Airo-Token": "nope"}))
        self.assertEqual(403, status)

    # -- properties of the guards themselves ---------------------------

    def test_the_token_is_never_written_to_disk(self):
        """Nothing else needs it — the tray opens a URL and the server hands
        the page its token — so a file would be one more credential at rest."""
        token = poller.server_token()
        found = [p for p in Path(self.tmp.name).rglob("*")
                 if p.is_file() and token in p.read_text(errors="ignore")]
        self.assertEqual([], found, f"the server token was written to {found}")

    def test_the_token_is_not_guessable(self):
        self.assertGreaterEqual(len(poller.server_token()), 32)

    def test_no_response_ever_grants_cross_origin_access(self):
        """One permissive header would undo all four checks at once."""
        for path, method in (("/api/settings", "GET"), ("/dashboard.html", "GET")):
            with self.subTest(path=path):
                _, _, headers = self.request(path, method=method)
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_a_preflight_is_not_answered(self):
        status, _, headers = self.request("/api/settings", method="OPTIONS")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn(status, (400, 405, 501))

    def test_reads_are_guarded_too(self):
        """A rebound host reaching /api/settings hands over the user's location
        at street resolution — the highest-consequence category in the register,
        and one a read-only endpoint gives away just as completely."""
        status, _, _ = self.request(
            "/api/settings", method="GET", headers={"Host": "airo.attacker.invalid"})
        self.assertEqual(403, status)

    def test_an_absent_origin_is_allowed_when_the_token_is_right(self):
        """Browsers always attach an Origin cross-origin, so absence means this
        did not come from another site. A CLI client with the token is a
        legitimate caller with no Origin to send."""
        headers = self.authorised()
        headers.pop("Origin")
        status, _, _ = self.request("/api/settings", headers=headers)
        self.assertEqual(200, status, "a tokened non-browser client was refused")


class TestOneValidatorTwoCallers(SettingsCase):
    """setup.py asks these questions in a terminal and the settings page asks
    them in a browser. They are two views onto one file, so "what is a valid
    poll interval" must have exactly one answer."""

    def test_the_wizards_own_answers_pass_the_apis_validator(self):
        """The disagreement this catches: a config the wizard writes happily
        and the UI then refuses to edit."""
        produced = {
            "data_dir": "",
            "retention_days": 0,
            "fusion": {"rule": "nearest"},
            "poll_minutes": 15,
            "serve": True,
            "serve_port": 8787,
            "backfill_days_on_first_run": 7,
            "alerts": {"enabled": True, "threshold_pm25": 16.75,
                       "rising_delta": 12, "cooldown_minutes": 60,
                       "notify_when_clear": True, "quiet_hours": [1, 7],
                       "sound": "Ping"},
        }
        _, errors = poller.validate_settings(produced)
        self.assertEqual({}, errors)

    def test_setup_writes_through_the_shared_writer(self):
        import inspect
        import setup as setup_module
        src = inspect.getsource(setup_module.write_config)
        self.assertIn("poller.save_config", src)
        self.assertIn("validate_settings", src)

    def test_setup_refuses_to_write_settings_the_validator_rejects(self):
        """No path patching. `setup.config_path()` resolves under HOME when
        asked, and HOME is already redirected — this used to reach in and
        reassign a module constant, which was the workaround for that constant
        having frozen the developer's real home at import.
        """
        import setup as setup_module
        target = setup_module.config_path()
        self.assertTrue(str(target).startswith(str(self.home)),
                        f"setup writes to {target}, outside the test's home")

        # write_config narrates the refusal; capture it rather than burying a
        # real failure in the suite's output.
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                setup_module.write_config({"poll_minutes": 0})
        self.assertFalse(target.exists(),
                         "a refused config was written anyway")

    def test_both_writers_produce_the_same_file(self):
        import setup as setup_module
        cfg = self.configured(poll_minutes=20, aqi_scale="us_epa")
        wizard = setup_module.config_path()
        self.assertTrue(str(wizard).startswith(str(self.home)),
                        f"setup writes to {wizard}, outside the test's home")

        setup_module.write_config(cfg)
        poller.save_config(cfg, self.home / ".airo" / "api.json")

        self.assertEqual(wizard.read_text(encoding="utf-8"),
                         (self.home / ".airo" / "api.json").read_text(
                             encoding="utf-8"))


class TestSetupAndPollerAgreeOnWhereTheConfigIs(SettingsCase):
    """One resolver, asked twice, cannot disagree with itself.

    `setup.py` hardcoded `~/.airo/config.json` while `poller.py` resolved
    `$AIRO_CONFIG` first. With that variable set — which is how you run a
    second install, or a test fixture, or a config on an external disk — the
    wizard wrote a file the poller never read, and then printed the success
    line. Nothing failed; the settings simply had no effect, which is the
    worst shape a configuration bug can take.
    """

    def set_env(self, name, value):
        saved = os.environ.get(name)
        os.environ[name] = str(value)
        self.addCleanup(
            lambda: os.environ.__setitem__(name, saved) if saved is not None
            else os.environ.pop(name, None))

    def test_they_agree_when_nothing_special_is_set(self):
        import setup as setup_module
        self.assertEqual(poller.config_path(), setup_module.config_path())

    def test_setup_follows_AIRO_CONFIG_where_the_poller_reads(self):
        import setup as setup_module
        elsewhere = Path(self.tmp.name) / "elsewhere" / "airo.json"
        elsewhere.parent.mkdir(parents=True)
        self.set_env("AIRO_CONFIG", elsewhere)

        self.assertEqual(elsewhere, poller.config_path())
        self.assertEqual(elsewhere, setup_module.config_path(),
                         "setup ignored $AIRO_CONFIG and wrote where the "
                         "poller does not read")
        self.assertEqual(elsewhere.parent, setup_module.config_dir())

    def test_what_setup_writes_is_what_the_poller_loads(self):
        """The property underneath the path comparison: end to end, through
        both real code paths, with $AIRO_CONFIG in force."""
        import setup as setup_module
        elsewhere = Path(self.tmp.name) / "elsewhere" / "airo.json"
        elsewhere.parent.mkdir(parents=True)
        self.set_env("AIRO_CONFIG", elsewhere)
        # load_config() reads the module constant; $AIRO_CONFIG is what that
        # constant is resolved from, so point it at the same answer here.
        poller.CONFIG_PATH = poller.config_path()

        cfg = self.configured(poll_minutes=23)
        with contextlib.redirect_stdout(io.StringIO()):
            setup_module.write_config(cfg)

        self.assertTrue(elsewhere.exists(), "setup wrote nothing there")
        self.assertEqual(23, poller.load_config()["poll_minutes"])

    def test_the_resolution_order_is_the_documented_one(self):
        """$AIRO_CONFIG beats ~/.airo/config.json, which beats ./config.json."""
        user = self.home / ".airo" / "config.json"
        user.write_text("{}", encoding="utf-8")
        self.assertEqual(user, poller.config_path())

        elsewhere = Path(self.tmp.name) / "wins.json"
        self.set_env("AIRO_CONFIG", elsewhere)
        self.assertEqual(elsewhere, poller.config_path(),
                         "$AIRO_CONFIG did not win")

    def test_the_module_constant_still_answers_for_its_callers(self):
        """`CONFIG_PATH` is read at ~20 sites and monkeypatched by four test
        modules. Adding a call-time resolver must not retire it."""
        self.assertIsInstance(poller.CONFIG_PATH, Path)
        poller.CONFIG_PATH = self.home / ".airo" / "config.json"
        self.assertEqual(self.home / ".airo" / "config.json", poller.CONFIG_PATH)


class TestSetupAndPollerAgreeOnWhereAKeyGoes(SettingsCase):
    """`setup.py` had its own `key_path`/`save_key`, and they differed.

    Three ways, each of which loses a credential quietly rather than loudly:
    it did not lowercase the slug, so a key saved under a mixed-case provider
    name landed at a path `poller.key_path()` — which lowercases when reading
    — would never look at; a blank key wrote an empty file instead of removing
    the key, so there was no way to clear one from the wizard; and it reported
    the restriction from `secure_path()`'s return value rather than reading
    the mode back, which on Windows is the difference between a key file that
    looks protected and one that is.
    """

    def test_both_resolve_a_mixed_case_slug_to_the_same_file(self):
        import setup as setup_module
        setup_module.save_key("FakePaid", "secret-value")

        # The name the wizard would have used, lowercased or not, must be the
        # one the reader opens.
        self.assertEqual(
            "secret-value",
            poller.key_path("FakePaid").read_text(encoding="utf-8"))
        self.assertEqual(
            "secret-value",
            poller.key_path("fakepaid").read_text(encoding="utf-8"))
        self.assertEqual(poller.key_path("FakePaid"), poller.key_path("fakepaid"))

    def test_a_key_saved_by_setup_is_found_by_the_poller(self):
        import setup as setup_module
        setup_module.save_key("FakePaid", "  secret-value  ")
        self.assertEqual("secret-value",
                         poller.get_api_key({"provider": "fakepaid"}))

    def test_setup_can_clear_a_key_rather_than_blanking_the_file(self):
        import setup as setup_module
        setup_module.save_key("fakepaid", "secret-value")
        setup_module.save_key("fakepaid", "")
        self.assertFalse(poller.key_path("fakepaid").exists(),
                         "a blank key left an empty file behind")
        # get_api_key() answers "" for absent, never None.
        self.assertEqual("", poller.get_api_key({"provider": "fakepaid"}))

    def test_setup_no_longer_carries_its_own_copies(self):
        import setup as setup_module
        self.assertFalse(hasattr(setup_module, "key_path"),
                         "setup.key_path is back; call poller.key_path")
        self.assertIs(poller.save_key,
                      setup_module.poller.save_key)

    def test_a_key_stays_under_the_home_directory_when_AIRO_CONFIG_moves(self):
        """Keys do *not* follow $AIRO_CONFIG, and that is deliberate — stated
        here so it is a decision rather than an oversight.

        `key_path()` and `get_api_key()` both anchor on `~/.airo`, so they
        agree with each other, which is the property that matters: a key is
        read from where it was written. The config is relocatable because it
        is settings; a credential is tied to the account whose home it is in,
        and $AIRO_CONFIG pointing into a synced folder must not quietly start
        writing keys there. setup goes through `poller.save_key()` now, so it
        holds this line without having to know about it.
        """
        import setup as setup_module
        elsewhere = Path(self.tmp.name) / "elsewhere" / "airo.json"
        elsewhere.parent.mkdir(parents=True)
        saved = os.environ.get("AIRO_CONFIG")
        os.environ["AIRO_CONFIG"] = str(elsewhere)
        try:
            self.assertEqual(elsewhere, setup_module.config_path())
            setup_module.save_key("fakepaid", "secret-value")
            self.assertEqual(self.home / ".airo" / "fakepaid.key",
                             poller.key_path("fakepaid"))
            self.assertFalse((elsewhere.parent / "fakepaid.key").exists(),
                             "a credential followed $AIRO_CONFIG out of ~/.airo")
            self.assertEqual("secret-value",
                             poller.get_api_key({"provider": "fakepaid"}))
        finally:
            if saved is None:
                os.environ.pop("AIRO_CONFIG", None)
            else:
                os.environ["AIRO_CONFIG"] = saved


class TestWritingSettings(SettingsCase):

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())

    def test_a_valid_patch_is_stored(self):
        cfg, errors = poller.apply_settings({"poll_minutes": 30})
        self.assertEqual({}, errors)
        self.assertEqual(30, cfg["poll_minutes"])
        self.assertEqual(30, json.loads(poller.CONFIG_PATH.read_text(encoding="utf-8"))["poll_minutes"])

    def test_a_patch_changes_only_what_it_names(self):
        """A settings page saves one panel at a time. Asking it to send the
        whole config back to change a threshold is how a stale tab reverts
        everything else."""
        poller.apply_settings({"alerts": {"threshold_aqi": 40}})
        cfg = poller.load_config()
        self.assertEqual(40, cfg["alerts"]["threshold_aqi"])
        self.assertEqual("Testville", cfg["location"]["name"])
        self.assertEqual(1, len(cfg["sources"]))

    def test_nothing_is_written_when_anything_is_refused(self):
        """A half-applied save leaves a config the user did not ask for and no
        way to tell which half took."""
        before = poller.CONFIG_PATH.read_text(encoding="utf-8")
        cfg, errors = poller.apply_settings(
            {"poll_minutes": 30, "fusion": {"rule": "guesswork"}})
        self.assertIsNone(cfg)
        self.assertIn("fusion.rule", errors)
        self.assertEqual(before, poller.CONFIG_PATH.read_text(encoding="utf-8"))

    def test_every_bad_field_is_reported_not_just_the_first(self):
        """A form that reports one error per save is a form nobody finishes."""
        _, errors = poller.apply_settings(
            {"poll_minutes": 0, "aqi_scale": "martian", "serve": "yes"})
        self.assertEqual({"poll_minutes", "aqi_scale", "serve"}, set(errors))

    def test_an_unknown_setting_is_refused(self):
        """Silently storing a typo means the user believes they changed
        something that does nothing."""
        _, errors = poller.apply_settings({"poll_minutes_typo": 30})
        self.assertIn("poll_minutes_typo", errors)

    def test_a_key_cannot_be_written_through_the_settings_route(self):
        """Keys go to a mode-600 file outside the config. Accepting one here
        would put a credential into the object that gets echoed back on
        error."""
        _, errors = poller.apply_settings({"sources": [
            {"provider": "fakefree", "site_id": "1", "read_key": SECRET}]})
        self.assertIn("read_key", errors["sources"])
        self.assertNotIn(SECRET, json.dumps(errors))

    def test_a_removed_source_is_really_removed(self):
        poller.apply_settings({"sources": []})
        self.assertEqual([], poller.load_config()["sources"])

    def test_the_config_file_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows expresses this")
        poller.apply_settings({"poll_minutes": 30})
        self.assertIs(True, poller.path_is_restricted(poller.CONFIG_PATH))

    # -- the rules themselves ------------------------------------------

    def test_a_fusion_rule_must_be_one_fusion_knows(self):
        _, errors = poller.validate_settings({"fusion": {"rule": "guesswork"}})
        self.assertIn("nearest", errors["fusion.rule"])

    def test_a_poll_interval_below_the_floor_is_refused(self):
        _, errors = poller.validate_settings({"poll_minutes": 1})
        self.assertIn("at least 2", errors["poll_minutes"])

    def test_overnight_quiet_hours_are_accepted(self):
        """22:00 to 07:00 crosses midnight and is the window people actually
        want. Rejecting it as backwards would suppress the setting."""
        clean, errors = poller.validate_settings({"alerts": {"quiet_hours": [22, 7]}})
        self.assertEqual({}, errors)
        self.assertEqual([22, 7], clean["alerts"]["quiet_hours"])

    def test_an_impossible_hour_is_refused(self):
        _, errors = poller.validate_settings({"alerts": {"quiet_hours": [22, 25]}})
        self.assertIn("alerts.quiet_hours", errors)

    def test_coordinates_outside_the_world_are_refused(self):
        _, errors = poller.validate_settings({"location": {"latitude": 950}})
        self.assertIn("location.latitude", errors)

    def test_a_true_is_not_a_number(self):
        """bool is an int in Python: True == 1, so a naive check accepts it and
        stores 1.

        Tested against fields whose range *allows* 1, deliberately. Checking
        poll_minutes instead would pass whether or not the bool exclusion
        exists, because its floor of 2 rejects the 1 anyway — a test that is
        green for a reason other than the one it claims.
        """
        _, errors = poller.validate_settings({"retention_days": True})
        self.assertIn("retention_days", errors)
        self.assertIn("whole number", errors["retention_days"])

        _, errors = poller.validate_settings({"alerts": {"rising_delta": True}})
        self.assertIn("alerts.rising_delta", errors)

    def test_a_number_is_not_a_flag(self):
        _, errors = poller.validate_settings({"serve": 1})
        self.assertIn("true or false", errors["serve"])

    def test_an_unwritable_data_dir_is_refused_at_the_moment_it_is_chosen(self):
        """data_dir is configurable, which is a way to abandon a database. The
        probe happens here, not on the first poll."""
        blocked = Path(self.tmp.name) / "nope" / "deeper"
        blocked.parent.mkdir()
        blocked.parent.chmod(0o500) if os.name != "nt" else None
        if os.name == "nt":
            self.skipTest("directory permissions are not how Windows refuses this")
        try:
            _, errors = poller.validate_settings({"data_dir": str(blocked)})
        finally:
            blocked.parent.chmod(0o700)
        self.assertIn("data_dir", errors)

    def test_a_writable_data_dir_is_accepted(self):
        target = Path(self.tmp.name) / "elsewhere"
        clean, errors = poller.validate_settings({"data_dir": str(target)})
        self.assertEqual({}, errors)
        self.assertEqual(str(target), clean["data_dir"])
        self.assertFalse((target / ".airo-write-test").exists(),
                         "the probe left its own file behind")


class TestTheSettingsPage(SettingsCase):
    """The page itself: how it is served, and what it must not contain."""

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_the_page_is_served_with_a_real_token(self):
        status, body = self.get("/settings")
        self.assertEqual(200, status)
        self.assertNotIn(poller.SETTINGS_TOKEN_PLACEHOLDER, body,
                         "the placeholder survived; every save would fail")
        self.assertIn(poller.server_token(), body)

    def test_the_dot_html_spelling_is_substituted_too(self):
        """The static handler would otherwise serve the file with its
        placeholder intact, and every save would fail with a token error
        nobody could explain."""
        _, body = self.get("/settings.html")
        self.assertNotIn(poller.SETTINGS_TOKEN_PLACEHOLDER, body)
        self.assertIn(poller.server_token(), body)

    def test_the_token_is_not_available_as_an_endpoint(self):
        """A route handing out the token would undo the point of having one: a
        cross-origin page can issue the request even though it cannot read an
        ordinary response."""
        for path in ("/api/token", "/api/settings"):
            with self.subTest(path=path):
                try:
                    _, body = self.get(path)
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8")
                self.assertNotIn(poller.server_token(), body)

    def test_the_page_loads_nothing_from_a_third_party(self):
        """Same rule as the dashboard: this page displays the user's address."""
        html = (ROOT / "settings.html").read_text(encoding="utf-8")
        for pattern in ("src=\"http", "src='http", "href=\"http://", "@import",
                        "cdn.", "googleapis"):
            self.assertNotIn(pattern, html, f"external reference: {pattern}")

    def test_every_panel_is_drawn_inside_its_own_guard(self):
        """One panel throwing must not blank the page. A settings page that
        goes blank leaves the user with no way to fix the setting that broke
        it — and this project has already lost four panels to one failure."""
        html = (ROOT / "settings.html").read_text(encoding="utf-8")
        drawn = set(re.findall(r"panel\('([^']+)',", html))
        declared = set(re.findall(r"function (draw[A-Z]\w*)", html))
        self.assertEqual(len(declared), len(drawn),
                         f"{len(declared)} draw functions but {len(drawn)} guarded calls")

    def test_a_stale_token_tells_the_user_to_reload(self):
        """The token dies with the process, so a page left open across a
        restart gets a 403. Calling that 'forbidden' would send someone hunting
        for a permissions problem that does not exist."""
        html = (ROOT / "settings.html").read_text(encoding="utf-8")
        self.assertIn("403", html)
        self.assertIn("Reload the page", html)

    def test_the_page_carries_the_health_disclaimer(self):
        html = (ROOT / "settings.html").read_text(encoding="utf-8")
        self.assertIn("not medical advice", html)

    def test_the_page_never_hardcodes_an_attribution(self):
        """Attribution is rendered from the networks actually in use. A literal
        credits a network the user may not have."""
        html = (ROOT / "settings.html").read_text(encoding="utf-8")
        self.assertNotIn("Powered by PurpleAir", html)
        self.assertIn("n.attribution", html)

    def test_the_page_restates_no_list_python_owns(self):
        """A rule or scale spelled out in HTML is a second copy that drifts."""
        html = (ROOT / "settings.html").read_text(encoding="utf-8")
        for rule in fusion.RULES:
            self.assertNotIn(f'value="{rule}"', html,
                             f"fusion rule {rule} is hardcoded in the page")
        for scale in poller.SCALES:
            self.assertNotIn(f'value="{scale}"', html,
                             f"scale {scale} is hardcoded in the page")


class TestFindingMonitors(SettingsCase):
    """Discovery is shared with setup.py rather than reimplemented. Two front
    ends that each decide which station to suggest are free to suggest
    different ones."""

    def sites(self, **over):
        base = [
            {"provider": "fakefree", "site_id": "near-dead", "site_name": "Dead",
             "distance_km": 0.7, "latitude": -27.0, "longitude": 153.0},
            {"provider": "fakefree", "site_id": "far-live", "site_name": "Live",
             "distance_km": 8.0, "latitude": -27.1, "longitude": 153.1},
        ]
        for s in base:
            s.update(over)
        return base

    def test_a_station_reporting_nothing_is_never_suggested(self):
        """Distance alone once picked the nearest station, which publishes no
        PM2.5, and the first poll returned 'every source failed'."""
        found = self.sites()
        found[0]["reporting"] = False
        found[1]["reporting"] = True
        picks = poller.recommend(found)
        self.assertEqual(["far-live"], [p["site_id"] for p in picks])

    def test_an_unprobed_station_stays_eligible(self):
        """Absence of a probe is not evidence of a fault, and suggesting
        nothing is worse than suggesting a site we could not check."""
        found = self.sites()
        found[0]["reporting"] = None
        picks = poller.recommend(found)
        self.assertEqual("near-dead", picks[0]["site_id"])

    def test_a_network_that_fails_is_reported_not_swallowed(self):
        """One network being down is not a reason to show nothing from the
        others, but it is a reason to say so — an empty list otherwise reads
        as 'no monitors near you'."""
        class Broken(KeylessProvider):
            slug = "broken"
            def discover(self, lat, lon, radius, key):
                raise RuntimeError("upstream is down")
        poller.PROVIDERS["broken"] = Broken()
        try:
            found, failures = poller.discover_sites(
                {"latitude": -27.0, "longitude": 153.0}, 25, ["broken"])
        finally:
            poller.PROVIDERS.pop("broken", None)
        self.assertEqual([], found)
        self.assertIn("upstream is down", failures["broken"])

    def test_the_probe_cap_is_reported_rather_than_hidden(self):
        """A capped probe that looks exhaustive is how 'unchecked' reads as
        'fine'."""
        many = [{"provider": "fakefree", "site_id": str(i), "distance_km": i}
                for i in range(30)]
        _, probed, _ = poller.annotate_reporting(many, limit=3)
        self.assertEqual(3, probed)

    def test_setup_and_the_api_call_the_same_recommender(self):
        import setup as setup_module
        self.assertIs(setup_module.recommend, poller.recommend)
        self.assertIs(setup_module.probe_reporting, poller.probe_reporting)


class TestRemovingASourceKeepsItsReadings(SettingsCase):
    """The failure this guards against shipped once: remove_source() took
    delete_readings=True, and readings.source_id is ON DELETE CASCADE, so one
    argument erased every reading a source had produced."""

    def test_removing_a_source_from_the_settings_touches_no_reading(self):
        import store
        self.write_config(self.configured())
        conn = store.connect(self.data / "airo.db")
        try:
            sid = store.upsert_source(conn, "fakefree", "1", "Free site")
            store.insert_readings(conn, sid, [
                {"observed_utc": "2026-08-01T00:00:00+00:00", "pm25": 5.0}])
            before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()

        poller.apply_settings({"sources": []})

        conn = store.connect(self.data / "airo.db")
        try:
            after = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual([], poller.load_config()["sources"])
        self.assertEqual(before, after, "readings vanished with the source")
        self.assertGreater(after, 0)

    def test_the_settings_route_cannot_reach_the_purge(self):
        """forget_source() is the one operation that destroys readings on
        purpose. Nothing a web page can send may reach it."""
        import inspect
        src = inspect.getsource(poller.QuietHandler.do_POST)
        self.assertNotIn("forget_source", src)
        self.assertNotIn("remove_source", src)


class TestSettingKeys(SettingsCase):
    """The one route that accepts a credential. Hard rule 2 applies at every
    line of it: never log, print, or return a key."""

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Airo-Token": poller.server_token()})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_a_key_is_stored_and_never_returned(self):
        status, body = self.post("/api/keys", {"provider": "fakepaid", "key": SECRET})
        self.assertEqual(200, status)
        self.assertNotIn(SECRET, body)
        self.assertTrue(json.loads(body)["has_key"])
        self.assertEqual(SECRET, poller.key_path("fakepaid").read_text(encoding="utf-8"))

    def test_the_key_never_reaches_the_log(self):
        """The log is the surface that outlives the session and gets pasted
        into bug reports."""
        self.post("/api/keys", {"provider": "fakepaid", "key": SECRET})
        self.assertNotIn(SECRET, "\n".join(self.logged))
        self.assertTrue(any("api key set" in line for line in self.logged),
                        "setting a key was not recorded at all")

    def test_the_key_file_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are not how Windows expresses this")
        self.post("/api/keys", {"provider": "fakepaid", "key": SECRET})
        self.assertIs(True, poller.path_is_restricted(poller.key_path("fakepaid")))

    def test_the_protection_is_read_back_not_assumed(self):
        """A key that merely looks protected is worse than one known not to
        be. On Windows os.chmod only toggles read-only, which is how this
        became a real failure rather than a hypothetical one."""
        _, body = self.post("/api/keys", {"provider": "fakepaid", "key": SECRET})
        self.assertIn("restricted", json.loads(body))

    def test_clearing_a_key_removes_the_file(self):
        """Clearing a credential has to be as easy as setting one, or the only
        way out is a terminal — which is the thing this whole change is for."""
        self.post("/api/keys", {"provider": "fakepaid", "key": SECRET})
        status, body = self.post("/api/keys", {"provider": "fakepaid", "key": ""})
        self.assertEqual(200, status)
        self.assertFalse(poller.key_path("fakepaid").exists())
        self.assertFalse(json.loads(body)["has_key"])

    def test_an_unknown_network_is_refused_without_echoing_the_key(self):
        status, body = self.post("/api/keys", {"provider": "nope", "key": SECRET})
        self.assertEqual(400, status)
        self.assertNotIn(SECRET, body)

    def test_a_private_sensor_read_key_goes_to_the_config_not_a_key_file(self):
        """read_key is the odd one out: it belongs to a source, not a network,
        so it lives in config.json. It is written here rather than through
        /api/settings, which refuses credentials outright — one route handles
        credentials, and it is this one."""
        cfg = self.configured()
        cfg["sources"] = [{"provider": "purpleair", "site_id": "42",
                           "site_name": "Private", "enabled": True}]
        self.write_config(cfg)

        status, body = self.post("/api/keys", {"provider": "purpleair",
                                               "site_id": "42", "key": SECRET})

        self.assertEqual(200, status)
        self.assertNotIn(SECRET, body)
        stored = json.loads(poller.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(SECRET, stored["sources"][0]["read_key"])
        self.assertTrue(json.loads(body)["sources"][0]["has_read_key"])

    def test_clearing_a_read_key_removes_it_from_the_config(self):
        cfg = self.configured()
        cfg["sources"] = [{"provider": "purpleair", "site_id": "42",
                           "enabled": True, "read_key": SECRET}]
        self.write_config(cfg)
        self.post("/api/keys", {"provider": "purpleair", "site_id": "42", "key": ""})
        stored = json.loads(poller.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("read_key", stored["sources"][0])

    def test_a_read_key_for_a_source_that_is_not_configured_is_refused(self):
        status, body = self.post("/api/keys", {"provider": "purpleair",
                                               "site_id": "999", "key": SECRET})
        self.assertEqual(404, status)
        self.assertNotIn(SECRET, body)

    def test_the_keys_route_is_behind_the_same_guards(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/keys",
            data=json.dumps({"provider": "fakepaid", "key": SECRET}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(403, status, "a key could be set without the token")
        self.assertFalse(poller.key_path("fakepaid").exists())


class TestBackupRoundTrip(SettingsCase):
    """The user's actual requirement: get everything out to a place of my
    choosing, and get it back into a fresh install of a future version.

    The buttons are not the deliverable. This test is — an export nobody has
    proved they can import is a file, not a backup.
    """

    def setUp(self):
        super().setUp()
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Airo-Token": poller.server_token()})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def populate(self, rows=25):
        import store
        self.write_config(self.configured(poll_minutes=23, retention_days=90))
        conn = store.connect(self.data / "airo.db")
        try:
            sid = store.upsert_source(conn, "fakefree", "1", "Free site")
            store.insert_readings(conn, sid, [
                {"observed_utc": f"2026-07-{(i % 28) + 1:02d}T{i % 24:02d}:00:00+00:00",
                 "pm25": 5.0 + i}
                for i in range(rows)])
            return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()

    def read_everything(self):
        import store
        conn = store.connect(self.data / "airo.db")
        try:
            return sorted(tuple(r) for r in conn.execute(
                "SELECT observed_utc, pm25 FROM readings").fetchall())
        finally:
            conn.close()

    def test_export_then_import_into_an_empty_install_restores_everything(self):
        written = self.populate()
        original_rows = self.read_everything()
        original_cfg = json.loads(poller.CONFIG_PATH.read_text(encoding="utf-8"))
        elsewhere = Path(self.tmp.name) / "usb stick"   # a space, as real paths have

        status, archive = self.post("/api/backup/export",
                                    {"directory": str(elsewhere)})
        self.assertEqual(200, status, archive)
        self.assertTrue(Path(archive["path"]).exists())
        self.assertEqual(written, archive["readings"])
        self.assertTrue(archive["restorable"])

        # Wipe the install completely — the "new machine" case, not "same
        # machine with a stale config sitting next to it".
        poller.CONFIG_PATH.unlink()
        (self.data / "airo.db").unlink()
        self.assertEqual([], poller.load_config()["sources"])

        status, done = self.post("/api/backup/restore",
                                 {"path": archive["path"], "force": True})
        self.assertEqual(200, status, done)

        self.assertEqual(original_rows, self.read_everything(),
                         "readings did not survive the round trip")
        restored_cfg = json.loads(poller.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(original_cfg["sources"], restored_cfg["sources"])
        self.assertEqual(23, restored_cfg["poll_minutes"])
        self.assertEqual(90, restored_cfg["retention_days"])

    def test_keys_are_excluded_unless_asked_for(self):
        """A backup ends up on cloud drives and USB sticks in a way a settings
        file never does."""
        self.populate()
        (self.home / ".airo" / "fakepaid.key").write_text(SECRET, encoding="utf-8")
        out = Path(self.tmp.name) / "out"

        _, plain = self.post("/api/backup/export", {"directory": str(out)})
        self.assertFalse(plain["contains_keys"])
        self.assertNotIn(SECRET,
                         Path(plain["path"]).read_bytes().decode("latin-1"))

        _, withkeys = self.post("/api/backup/export",
                                {"directory": str(out), "include_keys": True})
        self.assertTrue(withkeys["contains_keys"])

    def test_an_archive_always_states_whether_it_carries_credentials(self):
        """Asked any way, answered the same way. Someone about to copy a file
        to a USB stick needs this, and it must not depend which command they
        used to look."""
        self.populate()
        out = Path(self.tmp.name) / "out"
        (self.home / ".airo" / "fakepaid.key").write_text(SECRET, encoding="utf-8")
        _, made = self.post("/api/backup/export",
                            {"directory": str(out), "include_keys": True})

        _, seen = self.post("/api/backup/inspect", {"path": made["path"]})
        self.assertTrue(seen["contains_keys"])
        self.assertIn("fakepaid.key", seen["keys"])

    def test_an_unwritable_destination_is_refused_before_anything_is_written(self):
        """An unmounted drive looks exactly like a typo, and finding out
        halfway through a tar is how you get a half-written backup you trust."""
        if os.name == "nt":
            self.skipTest("directory permissions are not how Windows refuses this")
        self.populate()
        blocked = Path(self.tmp.name) / "locked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            status, body = self.post("/api/backup/export",
                                     {"directory": str(blocked / "inside")})
        finally:
            blocked.chmod(0o700)
        self.assertEqual(400, status)
        self.assertIn("directory", body["errors"])

    def test_an_archive_failing_its_own_checksum_is_refused(self):
        """Restoring from an archive whose database does not match its
        manifest would replace a working install with a broken one. create()
        returning 0 only means it believed it succeeded — a truncated write or
        a full disk still leaves a plausible file behind.

        The damage has to be done *inside* the tar, leaving the archive
        readable and the manifest intact. Flipping bytes in the gzip instead
        makes the file unreadable, which trips an earlier check and passes
        this test without the checksum ever being consulted — which is exactly
        what the first version of it did.
        """
        import tarfile
        self.populate()
        out = Path(self.tmp.name) / "out"
        _, made = self.post("/api/backup/export", {"directory": str(out)})
        path = Path(made["path"])

        rebuilt = Path(self.tmp.name) / "tampered.tar.gz"
        staging = Path(self.tmp.name) / "staging"
        with tarfile.open(path, "r:gz") as tar:
            try:
                tar.extractall(staging, filter="data")
            except TypeError:
                tar.extractall(staging)   # filter= arrived in 3.12; CI runs 3.9 too
        db = staging / "airo.db"
        self.assertTrue(db.exists(), "the archive holds no database to tamper with")
        with db.open("ab") as f:
            f.write(b"\x00" * 4096)          # still a file; no longer that file
        with tarfile.open(rebuilt, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)

        seen = self.post("/api/backup/inspect", {"path": str(rebuilt)})[1]
        self.assertFalse(seen["restorable"],
                         "a database that does not match the manifest looked fine")

        before = self.read_everything()
        status, _ = self.post("/api/backup/restore",
                              {"path": str(rebuilt), "force": True})
        self.assertEqual(400, status)
        self.assertEqual(before, self.read_everything(),
                         "an unverified archive was allowed to touch the database")

    def test_a_file_that_is_not_a_backup_is_named_as_such(self):
        stray = Path(self.tmp.name) / "holiday.tar.gz"
        stray.write_bytes(b"not an archive at all")
        status, body = self.post("/api/backup/inspect", {"path": str(stray)})
        self.assertEqual(400, status)
        self.assertIn("error", body)

    def test_the_archive_is_verified_before_it_is_called_a_backup(self):
        import inspect as pyinspect
        src = pyinspect.getsource(poller.QuietHandler.do_POST)
        self.assertIn("backup.describe(target)", src,
                      "an export is reported as successful without reading it back")


class ServingCase(SettingsCase):
    """A real server on a real socket, for anything testing a route.

    Its own base class rather than something to inherit from a test class:
    subclassing TestTheApiSurfaceEndToEnd to borrow `call` re-runs all of its
    tests under the subclass's name too, which is a slower suite reporting
    numbers that mean nothing.
    """

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def call(self, path, method="GET", payload=None, headers=None, raw=None):
        body = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else None)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Airo-Token", poller.server_token())
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def readings(self, n=5):
        import store
        conn = store.connect(self.data / "airo.db")
        try:
            sid = store.upsert_source(conn, "fakefree", "1", "Free site")
            store.insert_readings(conn, sid, [
                {"observed_utc": f"2026-07-{i + 1:02d}T00:00:00+00:00",
                 "pm25": 5.0 + i}
                for i in range(n)])
        finally:
            conn.close()

    # -- reads ---------------------------------------------------------


class TestTheApiSurfaceEndToEnd(ServingCase):
    """Every route and every refusal, over a real socket.

    Found by mutation: seventeen branches in QuietHandler could be
    removed without a test noticing — including the static-file
    fallthrough, so the dashboard itself was never fetched over HTTP
    by anything.
    """
    def test_a_static_file_is_still_served(self):
        """The API check is a prefix test; without it every static request is
        routed to the API and the dashboard 404s."""
        status, body = self.call("/dashboard.html")
        self.assertEqual(200, status)
        self.assertIn("<html", body.lower())

    def test_latest_is_served_when_there_is_a_reading(self):
        poller.LATEST_PATH.write_text(json.dumps({"aqi": 12, "band": "Good"}),
                                      encoding="utf-8")
        status, body = self.call("/api/latest")
        self.assertEqual(200, status)
        self.assertEqual(12, json.loads(body)["aqi"])

    def test_latest_before_the_first_poll_is_a_404_not_an_empty_reading(self):
        """An empty object would render as a blank dashboard rather than as
        "nothing has been measured yet"."""
        status, body = self.call("/api/latest")
        self.assertEqual(404, status)
        self.assertIn("no reading", body)

    def test_series_returns_points_for_each_source(self):
        self.readings()
        status, body = self.call("/api/series?days=3650")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertEqual("au", data["scale"])
        self.assertEqual(1, len(data["series"]))
        self.assertTrue(data["series"][0]["points"])

    def test_one_unreadable_timestamp_does_not_blank_every_chart(self):
        """A stored timestamp is only as good as whatever wrote it, and
        migrate_from_csv() passes the old file's `utc` column through
        untouched. This used to raise inside the handler and 500 the whole
        series, so one bad row anywhere in the history took out every chart
        for every source."""
        import store
        self.readings(n=5)
        conn = store.connect(self.data / "airo.db")
        try:
            conn.execute("INSERT INTO readings (source_id, observed_utc, pm25) "
                         "VALUES ((SELECT id FROM sources LIMIT 1), 'not-a-date', 9.0)")
            conn.commit()
        finally:
            conn.close()

        status, body = self.call("/api/series?days=3650")

        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertEqual(1, data["unreadable_rows"],
                         "a bad row was silently dropped instead of counted")
        self.assertEqual(5, len(data["series"][0]["points"]),
                         "the good readings did not survive the bad one")

    def test_flagged_readings_are_sent_even_though_the_chart_omits_them(self):
        """Two different things travel here, and the difference is the point.

        A *fault* stays out of the drawn series -- a blocked inlet swamps the
        axis and every average. Extreme *air* is drawn, because it is the
        reading someone most needs to see, and used to be the only one the
        chart refused. Both arrive in `suspect` as well, so nothing is
        excluded and unmentioned, which is the one thing the policy forbids.
        """
        import store
        self.readings(n=3)
        conn = store.connect(self.data / "airo.db")
        try:
            sid = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
            store.insert_readings(conn, sid, [
                # extreme air: the instrument agrees with itself
                {"observed_utc": "2026-07-20T00:00:00+00:00", "pm25": 900.0,
                 "pm25_a": 890.0, "pm25_b": 910.0},
                # a fault: it does not
                {"observed_utc": "2026-07-20T01:00:00+00:00", "pm25": 800.0,
                 "pm25_a": 1600.0, "pm25_b": 90.0}])
        finally:
            conn.close()

        status, body = self.call("/api/series?days=3650")
        data = json.loads(body)

        self.assertEqual(200, status)
        drawn = [p["pm25"] for p in data["series"][0]["points"]]
        self.assertIn(900.0, drawn, "extreme air was left off the chart again")
        self.assertNotIn(800.0, drawn, "a sensor fault was drawn as air quality")

        flagged = {s["pm25"]: s["quality"] for s in data["suspect"]}
        self.assertEqual({900.0: "extreme", 800.0: "suspect"}, flagged,
                         "a flagged reading was dropped without a word")

    def test_series_can_be_bucketed(self):
        self.readings(n=20)
        status, body = self.call("/api/series?days=3650&bucket=1440")
        self.assertEqual(200, status)
        points = json.loads(body)["series"][0]["points"]
        self.assertTrue(all("min" in p and "max" in p for p in points),
                        "bucketed points must carry min and max, not a mean "
                        "alone — the spikes are the signal")

    def test_a_head_request_is_guarded_like_the_others(self):
        status, _ = self.call("/dashboard.html", method="HEAD",
                              headers={"Host": "airo.attacker.invalid"})
        self.assertEqual(403, status)

    def test_a_head_request_from_loopback_is_allowed(self):
        status, _ = self.call("/dashboard.html", method="HEAD")
        self.assertEqual(200, status)

    # -- writes --------------------------------------------------------

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        """Written against a raw socket, because the point of the guard is
        that the server answers on the Content-Length *without* reading the
        body — so a client that sends one gets its connection closed mid-write
        and urllib raises instead of returning the status."""
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(
                b"POST /api/settings HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"X-Airo-Token: " + poller.server_token().encode() + b"\r\n"
                b"Content-Length: 99999999\r\n\r\n")
            s.sendall(b"{}")
            reply = s.recv(200).decode("latin-1")
        finally:
            s.close()
        self.assertIn("413", reply.splitlines()[0])

    def test_a_json_array_is_not_a_settings_patch(self):
        status, body = self.call("/api/settings", method="POST", raw=b"[1,2,3]")
        self.assertEqual(400, status)
        self.assertIn("object", body)

    def test_a_body_that_is_not_json_is_named_as_such(self):
        status, body = self.call("/api/settings", method="POST",
                                 raw=b"poll_minutes=30")
        self.assertEqual(400, status)
        self.assertIn("not JSON", body)

    def test_invalid_settings_come_back_as_field_errors(self):
        status, body = self.call("/api/settings", method="POST",
                                 payload={"poll_minutes": 0})
        self.assertEqual(400, status)
        self.assertIn("poll_minutes", json.loads(body)["errors"])

    # -- discovery -----------------------------------------------------

    def test_discovery_returns_sites_and_marks_the_suggested_one(self):
        # `providers` pinned deliberately. Omitting it makes the handler search
        # every registered network — which is right for a user and wrong for a
        # test, because the real ones are reached over the internet. Without
        # this the suite makes live API calls, runs slowly, and fails when a
        # public service is having a bad day.
        status, body = self.call("/api/sources/discover", method="POST",
                                 payload={"radius_km": 25,
                                          "providers": ["fakefree"]})
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertIn("sites", data)
        self.assertIn("probe_limit", data)

    def test_discovery_without_a_location_says_to_set_one(self):
        self.write_config({"sources": []})
        status, body = self.call("/api/sources/discover", method="POST",
                                 payload={"radius_km": 25,
                                          "providers": ["fakefree"]})
        self.assertEqual(400, status)
        self.assertIn("location", body)

    def test_discovery_refuses_a_network_it_does_not_have(self):
        status, body = self.call("/api/sources/discover", method="POST",
                                 payload={"providers": ["madeup"]})
        self.assertEqual(400, status)
        self.assertIn("madeup", body)

    # -- geocoding -----------------------------------------------------
    #
    # Nobody knows their own latitude. The page asks for an address and the
    # server turns it into coordinates, so these check the contract between
    # them -- never the real Nominatim, which is somebody else's service and
    # not something a test suite should depend on or hammer.

    def stub_geocode(self, matches=None, boom=None):
        real = poller.geocode

        def fake(place, limit=5):
            if boom:
                raise boom
            return matches if matches is not None else []

        poller.geocode = fake
        self.addCleanup(lambda: setattr(poller, "geocode", real))

    def test_an_address_becomes_coordinates(self):
        # Synthetic, per rule 2b: a fixture built from a real home address is
        # that address committed to the repository.
        self.stub_geocode([{"name": "Testville", "label": "Testville, Example State, AU",
                            "latitude": -33.5000, "longitude": 151.0000}])
        status, body = self.call("/api/geocode", method="POST",
                                 payload={"query": "12 Example St, Testville"})
        self.assertEqual(200, status)
        match = json.loads(body)["matches"][0]
        self.assertEqual(-33.5000, match["latitude"])
        self.assertEqual("Testville", match["name"])

    def test_every_match_carries_the_full_label(self):
        """The short name alone cannot disambiguate. A bare postcode like
        "9109" matches a place on three different continents, and a user shown
        three rows all reading "9109" has no way to pick theirs.

        The fixture stays inside the synthetic frame even though the point it
        makes is about somewhere else entirely: rule 2b is about the shape of
        what is committed, not about whose place it is, and a two-decimal pair
        in a source file is a real fix wherever it points."""
        self.stub_geocode([{"name": "Riverside",
                            "label": "Riverside, Example Province, Farland",
                            "latitude": -33.51, "longitude": 151.02}])
        _, body = self.call("/api/geocode", method="POST", payload={"query": "9109"})
        self.assertIn("Farland", json.loads(body)["matches"][0]["label"])

    def test_an_empty_address_is_refused_before_anything_is_sent(self):
        """What the user types leaves the machine, so an empty box must not
        become a request at all."""
        asked = []
        real = poller.geocode
        poller.geocode = lambda *a, **kw: asked.append(a) or []
        self.addCleanup(lambda: setattr(poller, "geocode", real))
        status, body = self.call("/api/geocode", method="POST", payload={"query": "   "})
        self.assertEqual(400, status)
        self.assertIn("query", json.loads(body)["errors"])
        self.assertEqual([], asked, "an empty query was sent to the geocoder")

    def test_a_lookup_failure_says_coordinates_still_work(self):
        """It is a call to somebody else's service, so failing is ordinary.
        A dead end here would strand a user who cannot proceed without a
        location -- the manual route has to be named."""
        self.stub_geocode(boom=OSError("no route to host"))
        status, body = self.call("/api/geocode", method="POST",
                                 payload={"query": "somewhere"})
        self.assertEqual(502, status)
        self.assertIn("coordinates", json.loads(body)["error"])

    def test_no_match_is_a_success_with_an_empty_list(self):
        """Not an error: the address was looked up fine, it just found
        nothing. Reporting it as a failure would send the user hunting for a
        problem with Airo instead of with what they typed."""
        self.stub_geocode([])
        status, body = self.call("/api/geocode", method="POST",
                                 payload={"query": "asdfghjkl"})
        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body)["matches"])

    def test_geocoding_needs_the_token_like_every_other_write(self):
        """It spends a third party's rate limit and discloses what was typed,
        so it sits behind the same guard as the rest."""
        self.stub_geocode([])
        status, _ = self.call("/api/geocode", method="POST",
                              payload={"query": "somewhere"},
                              headers={"X-Airo-Token": "not-the-token"})
        self.assertEqual(403, status)

    # -- backup --------------------------------------------------------

    def test_export_without_a_destination_asks_for_one(self):
        status, body = self.call("/api/backup/export", method="POST", payload={})
        self.assertEqual(400, status)
        self.assertIn("where", body)

    def test_inspect_without_a_path_asks_for_one(self):
        """Without the check the empty string is treated as a path and comes
        back "no such file: ." -- a refusal that reads like a bug in Airo
        rather than a field the user left blank."""
        status, body = self.call("/api/backup/inspect", method="POST", payload={})
        self.assertEqual(400, status)
        self.assertIn("give the archive", body)

    def test_restore_without_a_path_asks_for_one(self):
        status, body = self.call("/api/backup/restore", method="POST", payload={})
        self.assertEqual(400, status)

    def test_restoring_a_file_that_is_not_there_is_refused(self):
        status, body = self.call(
            "/api/backup/restore", method="POST",
            payload={"path": str(Path(self.tmp.name) / "nope.tar.gz")})
        self.assertEqual(400, status)

    def test_a_restore_the_tool_refuses_is_reported_not_swallowed(self):
        """restore() declines to overwrite without force. The handler must
        pass that refusal on rather than reporting success."""
        import store
        self.readings()
        out = Path(self.tmp.name) / "archives"
        _, made = self.call("/api/backup/export", method="POST",
                            payload={"directory": str(out)})
        archive = json.loads(made)["path"]

        status, body = self.call("/api/backup/restore", method="POST",
                                 payload={"path": archive, "force": False})

        self.assertEqual(409, status)
        self.assertIn("refused", body)

    def test_an_export_that_fails_is_not_reported_as_a_backup(self):
        """create() can fail after the destination check passes — a full disk,
        a vanished directory. Reporting that as a backup is how someone finds
        out at restore time."""
        import backup
        real = backup.create
        backup.create = lambda **kw: 1
        try:
            status, body = self.call(
                "/api/backup/export", method="POST",
                payload={"directory": str(Path(self.tmp.name) / "out")})
        finally:
            backup.create = real
        self.assertEqual(500, status)
        self.assertIn("not written", body)

    def test_a_key_that_is_not_text_is_refused(self):
        status, body = self.call("/api/keys", method="POST",
                                 payload={"provider": "fakepaid", "key": 12345})
        self.assertEqual(400, status)
        self.assertIn("text", body)


class TestEveryValidatorRefusal(SettingsCase):
    """Each branch of each validator, since a rule that cannot refuse is not a
    rule. Found by mutation: twelve of them could be removed silently."""

    def bad(self, patch):
        clean, errors = poller.validate_settings(patch)
        self.assertTrue(errors, f"{patch} was accepted")
        return errors

    # -- text ----------------------------------------------------------

    def test_a_missing_name_becomes_empty_not_the_word_none(self):
        clean, errors = poller.validate_settings({"location": {"name": None}})
        self.assertEqual({}, errors)
        self.assertEqual("", clean["location"]["name"],
                         'None became the string "None"')

    def test_a_number_is_not_a_name(self):
        self.assertIn("location.name", self.bad({"location": {"name": 42}}))

    def test_surrounding_space_is_trimmed(self):
        clean, _ = poller.validate_settings({"location": {"name": "  Home  "}})
        self.assertEqual("Home", clean["location"]["name"])

    # -- numbers -------------------------------------------------------

    def test_a_value_above_the_ceiling_is_refused(self):
        self.assertIn("at most", self.bad({"poll_minutes": 10_000})["poll_minutes"])

    def test_a_value_below_the_floor_is_refused(self):
        self.assertIn("at least",
                      self.bad({"alerts": {"threshold_aqi": -5}})["alerts.threshold_aqi"])

    def test_a_nullable_number_accepts_null(self):
        clean, errors = poller.validate_settings(
            {"alerts": {"threshold_pm25": None}})
        self.assertEqual({}, errors)
        self.assertIsNone(clean["alerts"]["threshold_pm25"])

    def test_a_non_nullable_number_does_not(self):
        self.assertIn("alerts.threshold_aqi",
                      self.bad({"alerts": {"threshold_aqi": None}}))

    def test_text_is_not_a_number(self):
        self.assertIn("poll_minutes", self.bad({"poll_minutes": "fifteen"}))

    # -- quiet hours ---------------------------------------------------

    def test_no_quiet_hours_is_a_valid_answer(self):
        for empty in (None, [], ()):
            clean, errors = poller.validate_settings(
                {"alerts": {"quiet_hours": empty}})
            self.assertEqual({}, errors, f"{empty!r} was refused")
            self.assertIsNone(clean["alerts"]["quiet_hours"])

    def test_one_hour_is_not_a_window(self):
        self.assertIn("two hours",
                      self.bad({"alerts": {"quiet_hours": [22]}})["alerts.quiet_hours"])

    def test_a_window_of_three_is_not_a_window(self):
        self.assertIn("alerts.quiet_hours",
                      self.bad({"alerts": {"quiet_hours": [1, 2, 3]}}))

    def test_a_fractional_hour_is_refused(self):
        self.assertIn("whole",
                      self.bad({"alerts": {"quiet_hours": [22.5, 7]}})["alerts.quiet_hours"])

    def test_a_boolean_hour_is_refused(self):
        """True == 1 in Python, so a naive check quietly sets quiet hours to
        01:00."""
        self.assertIn("alerts.quiet_hours",
                      self.bad({"alerts": {"quiet_hours": [True, 7]}}))

    # -- sources -------------------------------------------------------

    def test_sources_must_be_a_list(self):
        """The message matters, not just the refusal. Iterating a dict yields
        its keys, so without this check each key is refused as "expected an
        object" -- true, useless, and it tells the caller to fix the wrong
        thing."""
        errors = self.bad({"sources": {"provider": "fakefree"}})
        self.assertIn("expected a list", errors["sources"])

    def test_a_source_must_be_an_object(self):
        self.assertIn("sources", self.bad({"sources": ["fakefree/1"]}))

    def test_a_source_without_a_site_id_is_refused(self):
        """A source with no site id polls nothing, forever, silently."""
        errors = self.bad({"sources": [{"provider": "fakefree", "site_id": "  "}]})
        self.assertIn("site_id", errors["sources"])

    def test_a_site_id_carrying_markup_or_a_path_is_refused(self):
        """site_id is not just an identifier: it becomes a CSV filename in
        store.export_csv and an attribute value in the dashboard's source
        picker. Both of those were near-misses rather than exploits, which is
        exactly why the guard belongs here and not at either sink -- a third
        sink added later inherits it for free."""
        for bad_id in ('../../../../etc/passwd',
                       '1" onmouseover="x',
                       '1/../2',
                       'a' * 65):
            with self.subTest(site_id=bad_id):
                errors = self.bad(
                    {"sources": [{"provider": "fakefree", "site_id": bad_id}]})
                self.assertIn("site_id", errors["sources"])

    def test_an_ordinary_site_id_still_passes(self):
        """The guard is worth nothing if it rejects what the providers return.
        Every one of these is a shape a real discovery response produces."""
        for good_id in ("42", "near-dead", "SYD_1", "a.b-c_1"):
            with self.subTest(site_id=good_id):
                clean, errors = poller.validate_settings(
                    {"sources": [{"provider": "fakefree", "site_id": good_id}]})
                self.assertEqual({}, errors)
                self.assertEqual(good_id, clean["sources"][0]["site_id"])

    def test_a_source_naming_an_unknown_provider_is_refused_by_name(self):
        errors = self.bad({"sources": [{"provider": "atlantis", "site_id": "1"}]})
        self.assertIn("atlantis", errors["sources"])
        self.assertIn("fakefree", errors["sources"],
                      "the message does not say what would be valid")

    def test_a_provider_is_matched_case_insensitively(self):
        clean, errors = poller.validate_settings(
            {"sources": [{"provider": "FakeFree", "site_id": "1"}]})
        self.assertEqual({}, errors)
        self.assertEqual("fakefree", clean["sources"][0]["provider"])

    # -- structure -----------------------------------------------------

    def test_a_nested_group_must_be_an_object(self):
        self.assertIn("alerts", self.bad({"alerts": 3}))

    def test_an_empty_data_dir_means_the_default(self):
        clean, errors = poller.validate_settings({"data_dir": "   "})
        self.assertEqual({}, errors)
        self.assertEqual("", clean["data_dir"])


class TestChoosingAFolder(SettingsCase):
    """A browser page cannot pick a directory -- a file input hands over
    contents, never a path. The server is on the same machine, so it opens the
    real dialog."""

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/choose-folder",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Airo-Token": poller.server_token()})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def chooser(self, stdout, code=0):
        """Stand in for the desktop's own dialog."""
        real = poller.subprocess.run
        poller.subprocess.run = lambda *a, **kw: type(
            "R", (), {"stdout": stdout, "stderr": "", "returncode": code})()
        self.addCleanup(lambda: setattr(poller.subprocess, "run", real))

    def test_a_chosen_folder_comes_back_with_its_writability(self):
        target = Path(self.tmp.name) / "picked"
        self.chooser(str(target) + "\n")
        status, body = self.post({"prompt": "Where?"})
        self.assertEqual(200, status)
        self.assertEqual(str(target), body["path"])
        self.assertTrue(body["writable"],
                        "a folder was offered without checking it can be used")

    def test_a_chosen_folder_that_cannot_be_written_says_so_immediately(self):
        """Told at the moment of choosing rather than at the moment of saving:
        an unmounted drive looks exactly like a typo, and finding out later is
        how someone ends up logging into nowhere."""
        if os.name == "nt":
            self.skipTest("directory permissions are not how Windows refuses this")
        blocked = Path(self.tmp.name) / "locked"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.chooser(str(blocked / "inside") + "\n")
        try:
            status, body = self.post({})
        finally:
            blocked.chmod(0o700)
        self.assertEqual(200, status)
        self.assertFalse(body["writable"])
        self.assertIn("cannot write", body["error"])

    def test_cancelling_is_not_an_error(self):
        """Every one of these dialogs reports cancellation as a non-zero exit
        with no output. Showing that as a failure would put a red message on
        the screen because the user changed their mind."""
        self.chooser("", code=1)
        status, body = self.post({})
        self.assertEqual(200, status)
        self.assertIsNone(body["path"])
        self.assertEqual("cancelled", body["reason"])

    def test_a_system_with_no_chooser_says_so_rather_than_nothing(self):
        """A fallback that quietly returns nothing is a feature that silently
        does nothing -- this project has shipped that four times. The page
        needs to know to tell the user to type the path instead."""
        real = poller.subprocess.run
        def missing(*a, **kw):
            raise FileNotFoundError("no such chooser")
        poller.subprocess.run = missing
        try:
            picked, reason = poller.choose_folder()
        finally:
            poller.subprocess.run = real
        self.assertIsNone(picked)
        self.assertIn("no folder chooser", reason)

    def test_the_chooser_is_behind_the_same_guards(self):
        """It puts a window on the user's desktop. Another origin may not."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/choose-folder",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(403, status)

    def test_every_platform_has_a_real_implementation(self):
        """Not one platform working and the others returning a constant.

        Each platform is asked for its commands explicitly, so this means the
        same thing on every runner.
        """
        for os_name, platform, expected in (
                ("nt", "win32", "FolderBrowserDialog"),
                ("posix", "darwin", "choose folder"),
                ("posix", "linux", "zenity")):
            pairs = poller.folder_chooser_commands(
                "Pick a folder", os_name=os_name, platform=platform)
            self.assertTrue(pairs, f"{platform} has no chooser at all")
            flat = " ".join(" ".join(c) for c, _ in pairs)
            self.assertIn(expected, flat,
                          f"{platform} does not put up a real dialog")

    def test_linux_tries_more_than_one_chooser(self):
        """Neither zenity nor kdialog is guaranteed present, and a desktop
        with only the other would otherwise report no chooser at all."""
        pairs = poller.folder_chooser_commands(
            "Pick a folder", os_name="posix", platform="linux")
        self.assertGreaterEqual(len(pairs), 2)
        self.assertIn("kdialog", " ".join(" ".join(c) for c, _ in pairs))

    # -- the prompt is data, on every platform -------------------------

    #: Strings that would end the surrounding literal and start a statement,
    #: if the prompt were still being interpolated into a script body.
    HOSTILE = [
        'x" & (do shell script "touch /tmp/airo-pwned") & "',
        'x"\nend tell\ndo shell script "touch /tmp/airo-pwned"\ntell application "Finder"\nset y to "',
        "x'; Start-Process calc.exe; $z='",
        'x`n Start-Process calc.exe `n',
        "'; rm -rf ~ ;'",
    ]

    def test_a_hostile_prompt_never_becomes_script(self):
        """The prompt reaches the picker as a value, not as source.

        This was an injection. The prompt went into an AppleScript string
        literal, so a double quote closed it and the rest became AppleScript —
        `do shell script "..."` in a prompt ran as the user. PowerShell had the
        same shape with a single quote.

        Asserted by looking for the prompt inside the *script* argument rather
        than by pattern-matching the payload: any check that hunts for known
        bad strings is a check somebody gets around. If the prompt is nowhere
        in the script, there is nothing to escape.
        """
        for payload in self.HOSTILE:
            for os_name, platform in (("nt", "win32"), ("posix", "darwin"),
                                      ("posix", "linux")):
                for cmd, env in poller.folder_chooser_commands(
                        payload, os_name=os_name, platform=platform):
                    # The script body is whichever argument follows the flag
                    # that introduces it. Everything else is argv, which is
                    # not parsed by anything.
                    for flag in ("-e", "-Command"):
                        if flag not in cmd:
                            continue
                        script = cmd[cmd.index(flag) + 1]
                        self.assertNotIn(
                            payload, script,
                            f"{platform}: the prompt is inside the script body, "
                            f"so a quote in it ends the literal")

    def test_the_prompt_still_reaches_the_dialog(self):
        """The control for the test above.

        Without this, deleting the prompt entirely would look like a fix — the
        payload would be absent from the script for the least useful reason,
        and every picker would ask "Choose a folder" forever.
        """
        wanted = "Where should the backup go?"
        for os_name, platform in (("nt", "win32"), ("posix", "darwin"),
                                  ("posix", "linux")):
            pairs = poller.folder_chooser_commands(
                wanted, os_name=os_name, platform=platform)
            delivered = any(wanted in " ".join(cmd) or wanted in env.values()
                            for cmd, env in pairs)
            self.assertTrue(delivered,
                            f"{platform} never receives the prompt at all")

    def test_the_macos_picker_takes_the_prompt_as_an_argument(self):
        """osascript passes anything after the script to `on run argv`, where
        it is a string value and cannot be anything else."""
        cmd, _ = poller.folder_chooser_commands(
            "Pick a folder", os_name="posix", platform="darwin")[0]
        self.assertEqual("osascript", cmd[0])
        script = cmd[cmd.index("-e") + 1]
        self.assertIn("on run argv", script,
                      "the script takes no arguments, so the prompt has "
                      "nowhere to go but inside it")
        self.assertEqual("Pick a folder", cmd[-1],
                         "the prompt is not being passed as an argument")

    def test_the_windows_picker_reads_the_prompt_from_the_environment(self):
        """PowerShell -Command takes one string and there is no argv to use,
        so the prompt travels beside the command rather than in it."""
        cmd, env = poller.folder_chooser_commands(
            "Pick a folder", os_name="nt", platform="win32")[0]
        self.assertEqual("Pick a folder", env.get("AIRO_FOLDER_PROMPT"))
        script = cmd[cmd.index("-Command") + 1]
        self.assertIn("$env:AIRO_FOLDER_PROMPT", script)

    def test_the_windows_picker_emits_a_path_not_a_script_block(self):
        """The if-body had doubled braces, left over from an f-string that no
        longer interpolated. That makes the body a script block *literal*:
        PowerShell emits its source text and the caller takes that for a path.

        Nobody has run the Windows picker, which is how it survived.
        """
        cmd, _ = poller.folder_chooser_commands(
            "Pick a folder", os_name="nt", platform="win32")[0]
        script = cmd[cmd.index("-Command") + 1]
        self.assertNotIn("{{", script,
                         "the if-body is a script block literal, so the "
                         "picker returns its own source instead of a path")
        self.assertIn("{ Write-Output $d.SelectedPath }", script)

    def test_the_macos_picker_brings_itself_to_the_front(self):
        """A dialog nobody can see is the same as no dialog.

        The server runs detached, with no controlling terminal and no Dock
        entry, so a picker it opens has no foreground presence and macOS puts
        it *behind* the window the user is looking at. Clicking Browse then
        appears to do nothing at all.
        """
        cmd, _ = poller.folder_chooser_commands(
            "Pick a folder", os_name="posix", platform="darwin")[0]
        script = cmd[cmd.index("-e") + 1]
        self.assertIn("activate", script,
                      "the macOS picker opens behind the settings window and "
                      "reads as a dead button")
        self.assertLess(script.index("activate"), script.index("choose folder"),
                        "activate must come before the dialog, or it brings "
                        "nothing forward")

    def test_the_macos_picker_is_valid_applescript(self):
        """Compiled, not run — running it would block on a dialog.

        Compiled *with* a hostile prompt too: if the payload were still being
        interpolated, this is where a broken script would show up.
        """
        import subprocess
        if sys.platform != "darwin":
            self.skipTest("osacompile is macOS-only")
        for prompt in ["Pick a folder"] + self.HOSTILE:
            cmd, _ = poller.folder_chooser_commands(
                prompt, os_name="posix", platform="darwin")[0]
            script = cmd[cmd.index("-e") + 1]
            r = subprocess.run(["osacompile", "-o", "/dev/null", "-e", script],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(0, r.returncode,
                             f"the picker script does not compile: {r.stderr}")


class TestARejectedRequestCanStillReadItsAnswer(SettingsCase):
    """(setUp below brings up a real server, as TestTheEndpointServesIt does —
    a socket is the only place this behaviour exists.)"""
    """A refusal the client cannot read is not a refusal, it is a reset.

    Every guard in do_POST rejects before reading the body — deliberately,
    since the point of refusing a form-encoded write is not to process it. But
    leaving bytes unread and then closing sends an RST: POSIX clients usually
    still get the response, Windows does not, and reports
    `WinError 10053` from the middle of reading it.

    So the guard was correct on every platform and its explanation was
    readable on only some. Found by CI on windows-latest, where
    `test_a_form_encoded_write_is_refused` failed with a connection reset
    rather than the 415 it was asserting.

    Asserted here by checking the body is consumed, because the symptom itself
    is platform-specific and cannot be reproduced on this machine.
    """

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    #: Big enough that the kernel receive buffer still holds unread bytes
    #: when the server closes, which is what turns a FIN into an RST. Chosen
    #: by measurement, not by guess: at 100 B and 8 kB the client still reads
    #: the response cleanly on macOS even with the drain removed, so a test
    #: built on a small body is decorative. At 200 kB the reset is reliable
    #: here, and the symptom that CI first caught on Windows reproduces.
    UNREAD_BODY = b"x" * 200_000

    def _post_raw(self, headers, body, path="/api/settings"):
        """Send a request on a raw socket and read the answer to EOF.

        Returns the status line. Raises whatever the socket raises — which is
        the point: an undrained body means the server closes with data still
        in the receive buffer, the peer sends RST, and the client gets
        ConnectionResetError instead of the refusal that explains what it did
        wrong.
        """
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            s.sendall(
                (f"POST {path} HTTP/1.1\r\n"
                 f"Host: 127.0.0.1:{self.port}\r\n"
                 f"{head}"
                 f"Content-Length: {len(body)}\r\n"
                 f"Connection: close\r\n\r\n").encode() + body)
            data = b""
            while True:
                got = s.recv(8192)
                if not got:
                    break
                data += got
            return data.split(b"\r\n")[0].decode("latin-1")
        finally:
            s.close()

    def test_a_refused_content_type_still_returns_a_readable_answer(self):
        """The refusal must survive the trip.

        Verified to fail with the drain removed: at this body size the client
        gets ConnectionResetError rather than the 415.
        """
        status = self._post_raw(
            {"Content-Type": "application/x-www-form-urlencoded"},
            self.UNREAD_BODY)
        self.assertIn("415", status, f"expected a readable refusal, got {status!r}")

    def test_a_refused_origin_still_returns_a_readable_answer(self):
        status = self._post_raw(
            {"Origin": "https://evil.example",
             "Content-Type": "application/json"},
            b'{"x":"' + self.UNREAD_BODY + b'"}')
        self.assertIn("403", status, f"expected a readable refusal, got {status!r}")

    def test_a_refused_token_still_returns_a_readable_answer(self):
        status = self._post_raw(
            {"Content-Type": "application/json",
             "X-Airo-Token": "not-the-token"},
            b'{"x":"' + self.UNREAD_BODY + b'"}')
        self.assertIn("403", status, f"expected a readable refusal, got {status!r}")

    def test_an_oversized_body_is_still_refused_without_being_read(self):
        """The one case that must NOT be drained. A client declaring a
        gigabyte may never send it, and draining would block the handler
        waiting for bytes that are not coming — so the guard that exists to
        avoid reading it would be the thing that hangs on it."""
        import inspect
        src = inspect.getsource(poller.QuietHandler._drain_body)
        self.assertIn("MAX_BODY", src)
        self.assertIn("return", src.split("MAX_BODY", 1)[1][:200],
                      "an oversized body is drained rather than refused")

    # -- the drain itself, unit level -----------------------------------
    #
    # The socket tests above pass on macOS *with the drain removed*, because
    # POSIX clients usually still receive the response before the reset. Only
    # Windows shows the symptom. So the behaviour is asserted directly here,
    # where it is the same on every platform — otherwise the tests would be
    # green for the wrong reason on the machine they are written on, which is
    # precisely the failure this whole fix came from.

    class _Fake:
        """Just enough handler to exercise _drain_body."""

        def __init__(self, declared, available):
            import io
            self.headers = {"Content-Length": str(declared)}
            self.rfile = io.BytesIO(available)
            self.MAX_BODY = poller.QuietHandler.MAX_BODY

        _drain_body = poller.QuietHandler._drain_body

    def test_the_drain_consumes_exactly_the_declared_body(self):
        body = b"a" * 5000
        fake = self._Fake(len(body), body + b"NEXT-REQUEST")
        fake._drain_body()
        self.assertEqual(b"NEXT-REQUEST", fake.rfile.read(),
                         "the body was not fully consumed, so the socket "
                         "still holds bytes and closing it sends a reset")

    def test_the_drain_runs_once(self):
        """do_POST marks the body read when it genuinely reads it. Draining
        again would consume the *next* request on a keep-alive connection."""
        fake = self._Fake(4, b"abcdEXTRA")
        fake._body_read = True
        fake._drain_body()
        self.assertEqual(b"abcdEXTRA", fake.rfile.read())

    def test_the_drain_refuses_an_oversized_body(self):
        fake = self._Fake(poller.QuietHandler.MAX_BODY + 1, b"")
        fake._drain_body()          # must return at once, not block
        self.assertEqual(b"", fake.rfile.read())

    def test_a_short_body_does_not_hang_the_drain(self):
        """A client that declares more than it sends must not stall the
        handler forever; the read returns empty and the loop stops."""
        fake = self._Fake(5000, b"only-this")
        fake._drain_body()
        self.assertEqual(b"", fake.rfile.read())



class TestHostParsing(unittest.TestCase):
    """Unit-level, because the loopback check is the one guard whose input is
    an attacker-chosen string rather than a header we set."""

    def test_loopback_forms_are_recognised(self):
        for host in ("127.0.0.1", "127.0.0.1:8787", "localhost", "localhost:8787",
                     "[::1]", "[::1]:8787", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertTrue(poller.host_is_loopback(host))

    def test_names_that_merely_look_like_loopback_are_not(self):
        for host in ("localhost.attacker.invalid", "127.0.0.1.attacker.invalid",
                     "notlocalhost", "evil.invalid", "", None,
                     "127.0.0.2", "10.0.0.1"):
            with self.subTest(host=host):
                self.assertFalse(poller.host_is_loopback(host))

    def test_an_origin_from_elsewhere_is_rejected(self):
        for origin in ("https://evil.invalid", "http://localhost.evil.invalid",
                       "http://192.168.1.5:8787"):
            with self.subTest(origin=origin):
                self.assertFalse(poller.origin_is_allowed(origin))

    def test_our_own_origins_are_accepted(self):
        for origin in ("http://127.0.0.1:8787", "http://localhost:8787",
                       "http://[::1]:8787", "", None):
            with self.subTest(origin=origin):
                self.assertTrue(poller.origin_is_allowed(origin))




# The synthetic frame, as everywhere else. Rule 2b: no real coordinate in the
# repo, and these tests assert plumbing rather than geography.
HOME_LAT, HOME_LON = -33.5000, 151.0000


class TestDetectingATimezone(ServingCase):
    """`/api/timezone`. Explicit and user-initiated, not a side effect of Save.

    Deriving the zone silently on every save would spend a network call on
    somebody who may have typed their coordinates precisely to avoid one --
    the same line setup.py draws. A button says what it is about to do before
    it does it.
    """

    def setUp(self):
        super().setUp()
        import weather
        self.weather = weather
        self._at = weather.timezone_at
        self.asked = []
        self.answer = "Australia/Brisbane"
        weather.timezone_at = lambda lat, lon, **kw: (
            self.asked.append((lat, lon)) or self.answer)
        self.addCleanup(lambda: setattr(weather, "timezone_at", self._at))

    def test_it_answers_with_the_zone_for_the_stored_location(self):
        self.write_config(self.configured(
            location={"latitude": HOME_LAT, "longitude": HOME_LON}))
        status, body = self.call("/api/timezone", "POST", {})
        self.assertEqual(200, status, body)
        self.assertEqual("Australia/Brisbane", json.loads(body)["timezone"])
        self.assertEqual([(HOME_LAT, HOME_LON)], self.asked)

    def test_a_location_in_the_request_wins_over_the_stored_one(self):
        """The page sends what is in its fields, which may not be saved yet --
        the whole point is to fill the timezone in before pressing Save."""
        self.write_config(self.configured(
            location={"latitude": HOME_LAT, "longitude": HOME_LON}))
        self.call("/api/timezone", "POST",
                  {"location": {"latitude": HOME_LAT - 0.1,
                                "longitude": HOME_LON + 0.1}})
        self.assertEqual([(HOME_LAT - 0.1, HOME_LON + 0.1)], self.asked)

    def test_with_no_coordinates_it_says_to_find_an_address_first(self):
        self.write_config(self.configured(location={}))
        status, body = self.call("/api/timezone", "POST", {})
        self.assertEqual(400, status)
        self.assertIn("address", json.loads(body)["error"])
        self.assertEqual([], self.asked, "it called out with nothing to ask")

    def test_a_lookup_that_fails_leaves_the_field_usable(self):
        """Dead-ending here would be worse than not offering the button: the
        user can always type an IANA name, and the message says so."""
        self.answer = None
        self.write_config(self.configured(
            location={"latitude": HOME_LAT, "longitude": HOME_LON}))
        status, body = self.call("/api/timezone", "POST", {})
        self.assertEqual(502, status)
        self.assertIn("Australia/Brisbane", json.loads(body)["error"])

    def test_it_does_not_save_anything(self):
        """Offered, not applied -- the same contract as the address lookup. A
        wrong answer must be correctable before it becomes the config."""
        self.write_config(self.configured(
            location={"latitude": HOME_LAT, "longitude": HOME_LON}))
        self.call("/api/timezone", "POST", {})
        cfg = json.loads(poller.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertFalse((cfg.get("location") or {}).get("timezone"))

    def test_it_is_behind_the_token_like_every_other_outbound_call(self):
        self.write_config(self.configured(
            location={"latitude": HOME_LAT, "longitude": HOME_LON}))
        status, _ = self.call("/api/timezone", "POST", {},
                              headers={"X-Airo-Token": "wrong"})
        self.assertEqual(403, status)
        self.assertEqual([], self.asked)


class TestTheTimezoneFieldIsWiredUp(SettingsCase):
    """The page markup, checked for the connections a payload cannot prove.

    Twice now a helper has been fully tested while its *call site* was gone --
    the chart's axis ceiling and setup's timezone resolver -- each leaving a
    green suite against a broken product. So the wiring gets its own checks.
    """

    def page(self):
        return (ROOT / "settings.html").read_text(encoding="utf-8")

    def test_the_field_exists_and_the_save_sends_it(self):
        page = self.page()
        self.assertIn("id=\"l-tz\"", page, "no timezone field on the page")
        i = page.index("$('l-save').onclick")
        self.assertIn("timezone:", page[i:i + 400],
                      "Save does not send the timezone")

    def test_the_detect_button_is_connected(self):
        page = self.page()
        self.assertIn("$('l-tz-go').onclick", page,
                      "the Detect button does nothing")
        self.assertIn("/api/timezone", page)

    def test_the_help_text_distinguishes_configured_from_in_force(self):
        """A page showing only the configured name cannot say whether it took,
        which is precisely the case somebody needs explaining."""
        page = self.page()
        i = page.index("function tzHelp")
        block = page[i:i + 1600]
        self.assertIn("in_force", block)
        self.assertIn("database_available", block)
        self.assertIn("machine", block)

    def test_the_payload_carries_what_that_help_text_reads(self):
        """The two halves are checked against each other rather than each
        being asserted alone -- a field renamed on one side is exactly the
        failure a page test and a payload test can both miss."""
        payload = poller.settings_payload(self.configured())
        page = self.page()
        i = page.index("function tzHelp")
        block = page[i:i + 1600]
        for key in ("configured", "in_force", "machine", "database_available"):
            self.assertIn(key, payload["timezone"],
                          f"the payload has no {key!r}")
            self.assertIn(f"tz.{key}", block,
                          f"the page never reads {key!r}")


def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


class TestTheHeadlineExplainsItself(SettingsCase):
    """A reader looking at 48 above three sources reading 11.9, 14.4 and 9.2
    µg/m³ has every reason to think it is an average of them. It is not: it is
    one source's concentration converted to an index, and until this landed no
    surface said so.

    Decided in Python rather than in the page (rule 7's spirit): it is a
    statement about how a health-relevant figure was derived, and three
    surfaces describing one number three ways is how they drift.
    """

    def explain(self, pm25=11.9, index=47.6, scale="au", site="Riverside"):
        name, sc = poller.get_scale({"aqi_scale": scale})
        return poller.explain_headline(name, sc, pm25, index,
                                       {"site_name": site}, "nearest")

    def test_it_says_the_number_is_an_index_and_names_the_scale(self):
        out = self.explain()
        self.assertEqual("Australian AQI", out["scale_label"])
        self.assertEqual(48, out["index"])

    def test_it_shows_the_arithmetic_with_this_readings_own_numbers(self):
        """A formula alone still leaves the reader doing the sum. The point is
        that they can check the figure in front of them."""
        out = self.explain(pm25=11.9, index=47.6)
        self.assertEqual("11.9 × 100 ÷ 25 = 48", out["worked"])

    def test_it_says_plainly_that_it_is_not_an_average(self):
        out = self.explain()
        self.assertFalse(out["is_an_average_of_sources"])
        self.assertIn("not an average", out["rule_note"])
        self.assertIn("Riverside", out["rule_note"])

    def test_the_worked_sum_uses_the_configured_scale_not_a_constant(self):
        """25 is the Australian standard. A page writing `÷ 25` itself would
        be wrong on any other scale — which is exactly how the Australian
        bands ended up on a US install once already."""
        au = self.explain(scale="au")
        self.assertIn("÷ 25", au["formula"])

        name, sc = poller.get_scale({"aqi_scale": "us_epa"})
        us = poller.explain_headline(name, sc, 11.9, 49.6,
                                     {"site_name": "X"}, "nearest")
        self.assertIsNone(us["formula"],
                          "a piecewise scale was given a single-factor sum")
        self.assertIsNone(us["worked"])
        self.assertEqual("US EPA AQI", us["scale_label"])

    def test_a_raw_scale_says_there_is_no_index(self):
        name, sc = poller.get_scale({"aqi_scale": "raw"})
        out = poller.explain_headline(name, sc, 11.9, 11.9,
                                      {"site_name": "X"}, "nearest")
        self.assertIn("no index", out["basis"])

    def test_it_reaches_latest_json_where_the_surfaces_read_it(self):
        """The call-site check. Four helpers in this project have been fully
        tested while nothing called them."""
        import inspect
        src = inspect.getsource(poller)
        self.assertIn('"headline_explained": explain_headline(', src,
                      "explain_headline is never put into latest.json")


class TestTheSensorRoutes(SettingsCase):
    """`/api/sources/probe` and `/api/indoor`, over the wire.

    Driven through a real server rather than by calling the functions, because
    the thing worth checking is the wiring: a probe nothing routes to is a
    helper with tests, and this project has shipped five of those.
    """

    def setUp(self):
        super().setUp()
        self.write_config(self.configured())
        handler = partial(poller.QuietHandler, directory=str(ROOT))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # `server_close()` joins outstanding handler threads only when they are
        # not daemons. As daemons it returns immediately, so a request still in
        # flight keeps its database connection open past teardown -- and
        # Windows will not delete a file another handle still has open.
        self.httpd.daemon_threads = False
        self.httpd.block_on_close = True
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        # After the accept loop has stopped, so the join below is over a
        # settled set of handler threads rather than a moving one.
        self.httpd.server_close()
        # And the connection object those threads created is released only
        # when the last reference goes. An unclosed SQLite handle blocking a
        # temp-dir delete is one of the four Windows-only failures already
        # written down in CONVENTIONS; a collection here drops it.
        import gc
        gc.collect()
        super().tearDown()

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Airo-Token": poller.server_token()})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_a_sensor_that_cannot_be_read_is_a_client_error(self):
        """400, not 200 with an error inside. A page that only looks at the
        status would offer to add a sensor that does not answer."""
        status, body = self.post("/api/sources/probe",
                                 {"provider": "nonsense", "site_id": "1"})
        self.assertEqual(400, status)
        self.assertFalse(body["ok"])

    def test_an_empty_sensor_id_is_refused_before_any_request(self):
        """Asked for before anything is fetched. Sending an empty id to a
        provider produces whichever error that provider happens to give for a
        malformed URL, which is not an answer anybody can act on."""
        status, body = self.post("/api/sources/probe",
                                 {"provider": "qld", "site_id": "   "})
        self.assertEqual(400, status)
        self.assertIn("sensor id is required", body["error"])

    def test_the_indoor_comparison_is_served(self):
        """It answers even with nothing to compare — the panel needs a reason
        to show, and an empty response would leave somebody who has just added
        a sensor unsure whether it worked."""
        status, body = self.get("/api/indoor?days=7")
        self.assertEqual(200, status)
        self.assertIn("verdict", body)
        self.assertIn("why", body)
        self.assertEqual(7, body["days"])


class TestEveryChoiceListHasTheSameShape(SettingsCase):
    """The settings page renders every dropdown through one helper, and that
    helper reads `name`.

    A list served as `{"value": ...}` renders the right words with an empty
    value behind them — the select looks correct, and the form reports
    "unknown network: (none)" on submit. Nothing in Python notices, because
    the payload is valid; the mismatch only exists between the two.
    """

    def choice_lists(self):
        payload = poller.settings_payload(self.configured())
        return (payload.get("choices") or {}).items()

    def test_every_entry_is_a_name_and_a_label(self):
        wrong = []
        for key, entries in self.choice_lists():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, str):
                    continue          # a bare string is rendered as both
                if not isinstance(entry, dict):
                    wrong.append(f"{key}: {entry!r} is neither")
                elif "name" not in entry:
                    wrong.append(f"{key}: {sorted(entry)} has no 'name'")
        self.assertEqual(
            [], wrong,
            "the page's options() helper reads `name`; these would render "
            "with an empty value and fail on submit")

    def test_the_helper_still_reads_name(self):
        """Asserted against the page, so changing the helper without changing
        the payload fails here rather than in a form somebody is using."""
        page = (ROOT / "settings.html").read_text(encoding="utf-8")
        helper = page[page.index("function options("):]
        helper = helper[:helper.index("\n}")]
        self.assertIn("o.name", helper)

    def test_the_lists_are_not_empty(self):
        """A helper reading the right key off an empty list also renders
        nothing, and would pass everything above."""
        for key, entries in self.choice_lists():
            if isinstance(entries, list):
                self.assertTrue(entries, f"{key} is served empty")


class TestASettingsSaveDoesNotDestroyACredential(SettingsCase):
    """The page rebuilds the whole `sources` list from what it was served, and
    what it is served deliberately omits credentials — a page in a browser must
    never be handed a key.

    So every save wrote the list back *without* the read key of any private
    sensor. The sensor kept polling until the next restart and then 404ed
    forever, with nothing in the config to say why. It happened on a real
    install within minutes of the feature existing.
    """

    def configured_with_private(self):
        cfg = self.configured()
        cfg["sources"] = [
            {"provider": "purpleair", "site_id": "pa-inside",
             "site_name": "Indoor", "enabled": True, "placement": "indoor",
             "read_key": "SENSOR-READ-KEY"},
        ]
        return cfg

    def test_saving_settings_keeps_a_read_key_the_page_never_saw(self):
        self.write_config(self.configured_with_private())
        served = poller.settings_payload(poller.load_config())
        # The *value*, not the substring: the payload legitimately carries
        # `has_read_key`, which is how the page shows a key is set without
        # being handed one.
        self.assertNotIn("SENSOR-READ-KEY", json.dumps(served),
                         "the settings payload handed a credential to the page")
        self.assertIs(True, served["sources"][0]["has_read_key"])

        # Exactly what the page sends back: the served fields, no credential.
        cfg, errors = poller.apply_settings({"sources": [
            {"provider": s["provider"], "site_id": s["site_id"],
             "site_name": s["site_name"], "enabled": s["enabled"],
             "placement": s.get("placement")}
            for s in served["sources"]]})

        self.assertEqual({}, errors)
        self.assertEqual("SENSOR-READ-KEY", cfg["sources"][0]["read_key"],
                         "saving settings destroyed the sensor's read key")

    def test_placement_survives_the_round_trip(self):
        """Without it the page sends placement back empty and an indoor sensor
        becomes 'unknown' — still excluded from the outdoor headline, so the
        symptom is a sensor that shows nothing rather than an obvious error."""
        self.write_config(self.configured_with_private())
        served = poller.settings_payload(poller.load_config())
        self.assertEqual("indoor", served["sources"][0]["placement"])

        cfg, errors = poller.apply_settings({"sources": [
            {"provider": s["provider"], "site_id": s["site_id"],
             "site_name": s["site_name"], "enabled": s["enabled"],
             "placement": s.get("placement")}
            for s in served["sources"]]})
        self.assertEqual({}, errors)
        self.assertEqual("indoor", cfg["sources"][0]["placement"])

    def test_the_settings_route_still_refuses_to_carry_a_credential(self):
        """Carrying the stored key must not become a way to *set* one here.

        /api/keys is the only route that writes a credential, and that is the
        whole reason it can be written assuming the body may reach a log. A
        settings patch containing a read key is refused, so the carry-over
        below is a preservation and never an assignment.
        """
        _, errors = poller.validate_settings({"sources": [
            {"provider": "purpleair", "site_id": "pa-inside",
             "enabled": True, "read_key": "REPLACED"}]})
        self.assertTrue(errors, "a settings patch was allowed to set a key")

    def test_an_explicit_key_wins_over_the_stored_one(self):
        """Checked at the helper, since the route above refuses to deliver
        one. `/api/keys` writes through this path when it rewrites a source."""
        carried = poller._keep_credentials(
            [{"provider": "purpleair", "site_id": "1", "read_key": "OLD"}],
            [{"provider": "purpleair", "site_id": "1", "read_key": "NEW"}])
        self.assertEqual("NEW", carried[0]["read_key"])

    def test_removing_a_source_still_removes_it(self):
        """A list is replaced rather than merged so that deleting works.
        Carrying credentials must not quietly resurrect a deleted source."""
        self.write_config(self.configured_with_private())
        cfg, _ = poller.apply_settings({"sources": []})
        self.assertEqual([], cfg["sources"])

    def test_every_secret_field_is_carried_not_just_read_key(self):
        """Enumerated from SECRET_FIELD_NAMES. A second credential field added
        later would otherwise be destroyed by the same route, and the failure
        would look identical: a source that works until the next save."""
        import inspect
        src = inspect.getsource(poller._keep_credentials)
        self.assertIn("SECRET_FIELD_NAMES", src,
                      "the carry-over names one field by hand")


if __name__ == "__main__":
    unittest.main()
