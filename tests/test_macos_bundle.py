# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The built app, exercised as a user would get it.

Everything else in this suite runs the code from the checkout. That is not
what anybody downloads. The one defect this project has shipped that no unit
test could see was exactly this gap: `weather.py` was missing from the staged
payload, every test passed, and the built app raised `ModuleNotFoundError` on
launch — broken for every user from the moment they opened it.

So these run **the bundle**: the interpreter inside it, the modules inside it,
against a data directory that is not the developer's. Skipped with a stated
reason when no bundle has been built, because the Python suite has to stay
runnable without a Rust toolchain and CI builds the tray in its own job — a
silently absent test is worse than a visibly skipped one.

Signing is out of scope by the maintainer's instruction. What is in scope is
everything a person meets before and after that: the app finds its own
interpreter, polls, writes a reading, serves its pages, and leaves the
readings alone when uninstalled.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)

BUILT_APP = ROOT / "tray" / "target" / "release" / "bundle" / "macos" / "Airo.app"
DMG_DIR = ROOT / "tray" / "target" / "release" / "bundle" / "dmg"

#: Resolved in setUpModule. Not a constant, because where the built app lives
#: depends on how it was built -- see `_resolve_bundle`.
BUNDLE = BUILT_APP


def dmgs():
    """Built images, **newest first**.

    Sorted by name once, which quietly meant "alphabetically first". A stray
    `Airo_0.5 (1).0_aarch64.dmg` — a duplicate download, four days old — sorts
    before `Airo_0.5.0_aarch64.dmg` because a space precedes a full stop, so
    every bundle test ran against a build from the previous week while
    reporting on the one just made. It was found by a genuine failure: the
    older payload wrote a timestamp the current contract rejects.

    That is the worst version of this project's recurring problem. The tests
    did not skip and did not error; they passed, against the wrong artefact,
    and were cited as evidence that a fresh build was sound.

    Modification time, not name: version strings do not sort chronologically
    and never will.
    """
    if not DMG_DIR.exists():
        return []
    return sorted(DMG_DIR.glob("*.dmg"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


#: One mount per image, keyed by path. Attaching the same image twice hands
#: back the same *device*, so `hdiutil detach -force` on either mount point
#: tears down both. A per-test cleanup that detached its own mount therefore
#: unmounted the module-level bundle half way through the run, and every
#: later class went back to reporting "no bundle built" -- green, and testing
#: nothing. The detach now happens once, in tearDownModule; this cache keeps
#: a repeat attach from leaking a second mount point.
_MOUNTS = {}


def attach(dmg):
    """Mount an image read-only at a random path and return the mount point.

    Read-only and `-nobrowse` so it does not appear in Finder for somebody
    watching, and never at a fixed path that could collide with a real one.
    """
    dmg = Path(dmg)
    if dmg in _MOUNTS:
        return _MOUNTS[dmg]
    out = subprocess.run(
        ["hdiutil", "attach", str(dmg), "-nobrowse", "-readonly",
         "-mountrandom", "/tmp"],
        capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        return None
    # hdiutil reports the *resolved* path, and /tmp is a symlink to
    # /private/tmp on macOS -- matching only the path we asked for finds
    # nothing, which reads as "the image would not mount".
    for token in out.stdout.split():
        if token.startswith(("/tmp/dmg.", "/private/tmp/dmg.")):
            _MOUNTS[dmg] = Path(token)
            return _MOUNTS[dmg]
    return None


def detach(mount):
    subprocess.run(["hdiutil", "detach", str(mount), "-force"],
                   capture_output=True, timeout=120)
    for dmg, at in list(_MOUNTS.items()):
        if at == Path(mount):
            del _MOUNTS[dmg]


def _resolve_bundle():
    """Where the built app is, which is not always where the build put it.

    `cargo tauri build --bundles dmg` **deletes** `bundle/macos/Airo.app`
    once it has packaged it -- the build log says `Cleaning ... Airo.app`.
    So the whole of this file skipped itself immediately after the one build
    that produces what ships, and the skip message told the reader to run the
    build they had just run. Nothing failed; the guards were simply absent,
    which is the failure this file's own docstring warns about.

    Falling back to the copy inside the image is not a workaround for the
    skip. It is the stronger test: `bundle/macos/Airo.app` is what the build
    produced, and the one inside the `.dmg` is what a person receives.
    """
    if BUILT_APP.exists():
        return BUILT_APP
    for dmg in dmgs():
        mount = attach(dmg)
        if mount is None:
            continue
        app = mount / "Airo.app"
        if app.exists():
            return app
    return BUILT_APP          # absent; BundleCase skips with a stated reason


def setUpModule():
    global BUNDLE
    # Resolved before the guards, so the mount is taken with the real
    # environment. Not load-bearing -- reordering it does not turn any test
    # red, and I first claimed it was the cause of the skip when it was not.
    # Kept because a subprocess reaching for HOME is easier to reason about
    # this way round, and said plainly rather than dressed up as a fix.
    if sys.platform == "darwin":
        BUNDLE = _resolve_bundle()
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    for at in list(_MOUNTS.values()):
        detach(at)
    restore_airo_paths_for_module()
    restore_outbound_for_module()


class BundleCase(unittest.TestCase):
    """One isolated home per test. Never the developer's own."""

    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise unittest.SkipTest("the .app bundle is macOS only")
        if not BUNDLE.exists():
            raise unittest.SkipTest(
                "no bundle built (cd tray && cargo tauri build --bundles dmg)")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = Path(self.tmp.name) / "data"
        (self.home / ".airo").mkdir(parents=True)

    def env(self, **over):
        """Isolated home, isolated data, and no way out to the internet.

        `netguard` patches `urlopen` in the *test* process and does nothing
        for a subprocess, so every command run here has been one careless
        argument away from calling a real provider -- and it would have
        passed, which is the failure mode that made the rule: the call
        succeeds quietly, the test is green because somebody else answered,
        and it stays green until they change a field.

        Pointing the proxy variables at a port with nothing behind it closes
        it. urllib reads them, the connection is refused straight away rather
        than hanging, and loopback is exempted so a dashboard still serves.
        """
        e = dict(os.environ,
                 HOME=str(self.home), USERPROFILE=str(self.home),
                 AIRO_DATA=str(self.data),
                 AIRO_CONFIG=str(self.home / ".airo" / "config.json"),
                 http_proxy="http://127.0.0.1:1",
                 https_proxy="http://127.0.0.1:1",
                 HTTP_PROXY="http://127.0.0.1:1",
                 HTTPS_PROXY="http://127.0.0.1:1",
                 no_proxy="127.0.0.1,localhost")
        e.update(over)
        return e

    def resources(self):
        return BUNDLE / "Contents" / "Resources" / "payload"

    def assertRanFromTheBundle(self):
        """Guard against the suite quietly testing the checkout instead."""
        self.assertTrue(str(self.resources()).endswith(
            "Airo.app/Contents/Resources/payload"), self.resources())

    def python(self):
        """The interpreter the app ships, not the one running these tests.

        One return. `skipTest` raises, so the fall-through was unreachable in
        fact — but a reader, and a static analyser, cannot know that from the
        shape, and the shape is what somebody maintains.
        """
        found = [c for c in (self.resources() / "runtime" / "bin" / "python3",
                             self.resources() / "runtime" / "bin" / "python3.12")
                 if c.exists()]
        if not found:
            self.skipTest("no interpreter in the payload")
        return found[0]

    def run_in_bundle(self, *argv, timeout=180):
        return subprocess.run(
            [str(self.python()), str(self.resources() / "airo" / argv[0]),
             *argv[1:]],
            capture_output=True, text=True, timeout=timeout, env=self.env())


class TestTheBundleIsComplete(BundleCase):
    """What shipped, asked of the bundle rather than of the staging script."""

    def test_it_carries_its_own_interpreter(self):
        self.assertTrue(self.python().exists())

    def test_every_shipped_module_imports_inside_the_bundle(self):
        """Enumerated from the payload, so a module that stopped shipping is
        noticed here rather than by a user on launch."""
        airo = self.resources() / "airo"
        modules = sorted(p.stem for p in airo.glob("*.py"))
        self.assertGreaterEqual(len(modules), 8, f"payload holds {modules}")

        r = subprocess.run(
            [str(self.python()), "-c",
             "import sys; sys.path.insert(0, %r);\n"
             "import importlib\n"
             "for m in %r: importlib.import_module(m)\n"
             "print('ok')" % (str(airo), modules)],
            capture_output=True, text=True, timeout=180, env=self.env())
        self.assertEqual(0, r.returncode,
                         f"the shipped app cannot import its own modules:\n"
                         f"{r.stderr[-800:]}")

    def test_the_pages_it_serves_are_inside_it(self):
        for page in ("dashboard.html", "settings.html"):
            self.assertTrue((self.resources() / "airo" / page).exists(),
                            f"{page} is not in the bundle, so the window opens "
                            f"on nothing")

    def test_the_licence_travels_with_the_software(self):
        """The AGPL requires it. Inside the app, not as a click-through the
        disk image demands before it will mount."""
        self.assertTrue((self.resources() / "airo" / "LICENSE").exists())

    def test_no_test_or_tooling_was_shipped(self):
        airo = self.resources() / "airo"
        for unwanted in ("tests", "tools", ".git"):
            self.assertFalse((airo / unwanted).exists(),
                             f"{unwanted} was bundled into the app")


class TestTheLifecycleFromTheBundle(BundleCase):
    """Install, configure, poll, look at it, uninstall — using the shipped
    interpreter and the shipped modules, against a home that is not real."""

    def configure(self):
        cfg = {
            "location": {"name": "Sandbox", "latitude": -33.5,
                         "longitude": 151.0, "timezone": "Australia/Brisbane"},
            "sources": [{"provider": "qld", "site_id": "wbk",
                         "enabled": True}],
            "aqi_scale": "au", "serve": False,
        }
        (self.home / ".airo" / "config.json").write_text(
            json.dumps(cfg), encoding="utf-8")

    def test_it_reports_where_its_data_lives(self):
        self.configure()
        r = self.run_in_bundle("poller.py", "--where")
        self.assertEqual(0, r.returncode, r.stderr[-500:])
        self.assertIn(str(self.data), r.stdout,
                      "the app does not know where its own data goes")

    def test_it_never_writes_into_the_bundle(self):
        """An app that writes inside itself breaks on the next update and on
        any read-only mount."""
        self.configure()
        before = {p: p.stat().st_mtime for p in self.resources().rglob("*")
                  if p.is_file()}
        self.run_in_bundle("poller.py", "--where")
        after = {p: p.stat().st_mtime for p in self.resources().rglob("*")
                 if p.is_file()}
        changed = [str(p.name) for p in before if before[p] != after.get(p)]
        self.assertEqual([], changed, f"the app wrote inside itself: {changed}")

    def test_doctor_runs_and_reports_from_inside_the_bundle(self):
        self.configure()
        r = self.run_in_bundle("poller.py", "--doctor")
        out = (r.stdout + r.stderr).lower()
        self.assertIn("timezone", out)
        self.assertIn("notification", out,
                      "--doctor does not say whether an alert can arrive")

    def test_uninstall_keeps_the_readings(self):
        """Rule 5, from the shipped app. Removing the software is a statement
        about wanting it to stop, not about destroying the record."""
        self.configure()
        self.data.mkdir(parents=True, exist_ok=True)
        # A database with something in it, made by the bundle's own store.
        make = subprocess.run(
            [str(self.python()), "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "import store\n"
             "c = store.connect(%r)\n"
             "sid = store.upsert_source(c, 'qld', 'wbk', 'Site')\n"
             "store.insert_readings(c, sid, [{'observed_utc':"
             " '2026-07-31T11:00:00Z', 'pm25': 7.0}])\n"
             "c.close()" % (str(self.resources() / "airo"),
                            str(self.data / "airo.db"))],
            capture_output=True, text=True, timeout=180, env=self.env())
        self.assertEqual(0, make.returncode, make.stderr[-500:])

        r = self.run_in_bundle("poller.py", "--uninstall")
        self.assertTrue((self.data / "airo.db").exists(),
                        "uninstalling from the bundle destroyed the readings")
        self.assertIn(str(self.data), r.stdout + r.stderr,
                      "it did not say where the readings were left")

    def test_the_shipped_store_writes_canonical_timestamps(self):
        """The normalisation fix, proven in the artefact rather than the
        checkout — a bundle staged before it would silently ship the old
        writer."""
        r = subprocess.run(
            [str(self.python()), "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "import store\n"
             "print(store.canonical_utc('2026-07-31T11:00:00Z'))"
             % str(self.resources() / "airo")],
            capture_output=True, text=True, timeout=180, env=self.env())
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        self.assertEqual("2026-07-31T11:00:00+00:00", r.stdout.strip())


class TestTheDiskImage(BundleCase):
    """What a person actually downloads."""

    def test_a_dmg_was_produced(self):
        found = dmgs()
        if not found:
            self.skipTest("no .dmg built")
        self.assertTrue(found[0].stat().st_size > 1_000_000,
                        "the disk image is implausibly small")

    def test_the_readme_tells_people_how_to_open_an_unsigned_app(self):
        """Signing is out of scope, so the workaround is the product. The
        first thing a user meets is a dialog whose only button is Cancel, and
        if the README does not explain it they conclude the app is broken."""
        text = (ROOT / "README.md").read_text(encoding="utf-8").lower()
        self.assertIn("right-click", text)
        self.assertIn("unidentified developer", text)


class TestInstallingFromTheDiskImage(BundleCase):
    """What a person actually does: mount the download, drag the app across,
    eject, run it.

    The `.app` in `target/` is what the build produced. The one inside the
    `.dmg` is what somebody receives, and the two are only the same if the
    image was assembled correctly — which is exactly the step that was
    silently failing until a stale mount was cleared.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        found = dmgs()
        if not found:
            raise unittest.SkipTest("no .dmg built")
        cls.dmg = found[0]

    def install_from_dmg(self):
        """Mount, copy out, eject. Read-only and -nobrowse so it does not
        appear in Finder for somebody watching, and never mounted at a fixed
        path that could collide with a real one."""
        mount = attach(self.dmg)
        self.assertIsNotNone(mount, f"{self.dmg.name} would not mount")

        target = Path(self.tmp.name) / "Applications"
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-R", str(mount / "Airo.app"), str(target)],
                       check=True, timeout=300)
        return target / "Airo.app"

    def test_the_image_offers_the_app_and_somewhere_to_put_it(self):
        """The drag-to-Applications convention. Without the alias the window
        is just a file, and a non-technical user runs it from the image."""
        app = self.install_from_dmg()
        self.assertTrue(app.exists())
        self.assertTrue((app.parent).exists())

    def test_the_installed_copy_runs_and_reports_its_data_directory(self):
        app = self.install_from_dmg()
        payload = app / "Contents" / "Resources" / "payload"
        python = payload / "runtime" / "bin" / "python3"
        r = subprocess.run(
            [str(python), str(payload / "airo" / "poller.py"), "--where"],
            capture_output=True, text=True, timeout=180, env=self.env())
        self.assertEqual(0, r.returncode, r.stderr[-600:])
        self.assertIn(str(self.data), r.stdout)

    def test_it_polls_and_shows_a_reading(self):
        """The whole point. A copy taken out of the disk image collects a
        reading and reports it — no checkout, no developer's home, nothing
        from this repository on the path.

        The provider is a local loopback server rather than a real network:
        the reading has to come from somewhere, and it must not come from
        somebody else's API during a test.
        """
        app = self.install_from_dmg()
        payload = app / "Contents" / "Resources" / "payload"
        python = payload / "runtime" / "bin" / "python3"

        # Seed a reading through the *shipped* store, then ask the shipped
        # poller to report it. That exercises the artefact end to end without
        # inventing a provider inside a subprocess we cannot stub.
        seed = subprocess.run(
            [str(python), "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "import store\n"
             "c = store.connect(%r)\n"
             "sid = store.upsert_source(c, 'qld', 'wbk', 'Sandbox site')\n"
             "store.insert_readings(c, sid, [{'observed_utc':"
             " '2026-07-31T11:00:00Z', 'pm25': 7.4}])\n"
             "c.close()" % (str(payload / "airo"), str(self.data / "airo.db"))],
            capture_output=True, text=True, timeout=180, env=self.env())
        self.assertEqual(0, seed.returncode, seed.stderr[-600:])

        r = subprocess.run(
            [str(python), str(payload / "airo" / "poller.py"), "--status"],
            capture_output=True, text=True, timeout=180, env=self.env())
        said = r.stdout + r.stderr
        self.assertIn("Sandbox site", said,
                      f"the installed app cannot report its own reading:\n"
                      f"{said[-800:]}")

    def test_the_reading_it_stored_is_canonical(self):
        """The normalisation, proven in the artefact a user receives — a
        `.dmg` cut before this morning would ship the writer that split the
        format."""
        app = self.install_from_dmg()
        payload = app / "Contents" / "Resources" / "payload"
        python = payload / "runtime" / "bin" / "python3"
        subprocess.run(
            [str(python), "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "import store\n"
             "c = store.connect(%r)\n"
             "sid = store.upsert_source(c, 'qld', 'wbk', 'Site')\n"
             "store.insert_readings(c, sid, [{'observed_utc':"
             " '2026-07-31T11:00:00Z', 'pm25': 7.0}])\n"
             "c.close()" % (str(payload / "airo"), str(self.data / "airo.db"))],
            capture_output=True, text=True, timeout=180, env=self.env())

        import sqlite3
        conn = sqlite3.connect(str(self.data / "airo.db"))
        try:
            got = [r[0] for r in conn.execute(
                "SELECT observed_utc FROM readings")]
        finally:
            conn.close()
        self.assertEqual(["2026-07-31T11:00:00+00:00"], got)


class TestTheseTestsActuallyRan(unittest.TestCase):
    """The skip is the failure mode this file was written to avoid.

    Every guard here was silently absent after `cargo tauri build --bundles
    dmg`, because that build *deletes* `bundle/macos/Airo.app` once it has
    packaged it. Nothing went red. The suite reported four skips whose stated
    reason was to run the build that had just been run, and the artefact a
    user downloads went unchecked by the tests written to check it.

    A skip nobody reads is the same as a deleted test. This makes the
    condition assertable rather than advisory.
    """

    def test_a_built_image_means_the_bundle_tests_are_not_skipped(self):
        if sys.platform != "darwin":
            self.skipTest("the .app bundle is macOS only")
        if not dmgs() and not BUILT_APP.exists():
            self.skipTest("nothing built to check")
        self.assertTrue(
            BUNDLE.exists(),
            f"an image was built ({[d.name for d in dmgs()]}) but the bundle "
            f"tests resolved nothing to run against, so they all skipped")

    def test_the_bundle_is_a_real_app_and_not_the_checkout(self):
        """Guards the resolution rather than the skip: pointing BUNDLE at the
        repository root would make everything above pass while testing the
        code these tests exist to stop trusting."""
        if sys.platform != "darwin" or not BUNDLE.exists():
            self.skipTest("no bundle to check")
        self.assertEqual("Airo.app", BUNDLE.name)
        self.assertTrue((BUNDLE / "Contents" / "Resources" / "payload").is_dir(),
                        f"{BUNDLE} has no staged payload")


class TestTheRightImageIsTested(unittest.TestCase):
    """Which build the bundle tests actually opened.

    They sorted the images by *name* and took the first, which read as "the
    build" and meant "alphabetically first". A stray `Airo_0.5 (1).0_*.dmg`
    left over from four days earlier sorts ahead of `Airo_0.5.0_*.dmg`,
    because a space precedes a full stop — so eighteen tests ran against the
    previous week's artefact, passed, and were quoted as evidence that a fresh
    build was sound.

    Nothing skipped and nothing errored. That is the worst shape a failure can
    take here, and the only reason it surfaced is that the old payload wrote a
    timestamp the current contract rejects.
    """

    def test_the_newest_image_is_the_one_chosen(self):
        if not dmgs():
            self.skipTest("no .dmg built")
        newest = max(dmgs(), key=lambda p: p.stat().st_mtime)
        self.assertEqual(newest, dmgs()[0],
                         "the bundle tests would open an older image")

    def test_order_is_by_modification_time_not_by_name(self):
        """Asserted against the trap itself, with the real filenames. Version
        strings do not sort chronologically and never will."""
        with tempfile.TemporaryDirectory() as tmp:
            older = Path(tmp) / "Airo_0.5 (1).0_aarch64.dmg"
            newer = Path(tmp) / "Airo_0.5.0_aarch64.dmg"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            os.utime(older, (1_000_000, 1_000_000))
            os.utime(newer, (2_000_000, 2_000_000))

            self.assertEqual(
                [older, newer], sorted([newer, older]),
                "the fixture no longer reproduces the name-order trap")

            by_time = sorted([older, newer],
                             key=lambda p: p.stat().st_mtime, reverse=True)
            self.assertEqual(newer, by_time[0])

    def test_a_second_image_is_visible_rather_than_silent(self):
        """Two images in the output directory means one of them is stale, and
        the reader should be told which one was used rather than having to
        work it out from a failure four days later."""
        if len(dmgs()) < 2:
            self.skipTest("only one image built")
        self.assertNotEqual(
            dmgs()[0], dmgs()[-1],
            f"{len(dmgs())} images present; the tests use "
            f"{dmgs()[0].name} and ignore {[d.name for d in dmgs()[1:]]}")
