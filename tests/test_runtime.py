"""The Python runtime the installed app carries.

Airo's own code needs no dependencies, but the person the installer is for
does not have a Python at all -- "python3: command not found" is where they
stop. So the app ships one, which means the project now has a supply chain of
exactly one item, and the checks that go with it.

Nothing here touches the network. The download is stubbed, because the
property worth testing is what happens to a file that is *not* the one we
pinned, and that must not depend on a public service being up.
"""

import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import fetch_runtime  # noqa: E402

# Tests must never write into the developer's own ~/.airo (CONVENTIONS).
# poller's paths are module-level and resolved at import, so any test reaching
# code that calls log() writes to the real file unless they are redirected.
# A suite run appended two fixture strings to a live install's log, between two
# real polls, and the maintainer reasonably read that as their monitor dying.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)



def a_tarball(contents=b"#!/bin/sh\necho hello\n"):
    """A tarball shaped like the real one: a single `python/` directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("python/bin/python3")
        info.size = len(contents)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(contents))
    return buf.getvalue()


class FakeDownload:
    """Stand in for urlopen, serving bytes we control."""

    def __init__(self, payload):
        self.payload = payload
        self.asked = []

    def __call__(self, url, timeout=None):
        self.asked.append(url)
        outer = self

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()
                return False

        return Response(outer.payload)


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self._urlopen = fetch_runtime.urllib.request.urlopen
        self.triple = sorted(fetch_runtime.CHECKSUMS)[0]

    def tearDown(self):
        fetch_runtime.urllib.request.urlopen = self._urlopen

    def serve(self, payload):
        fake = FakeDownload(payload)
        fetch_runtime.urllib.request.urlopen = fake
        return fake

    def pin_to(self, payload):
        """Pin the checksum to whatever we are about to serve."""
        real = dict(fetch_runtime.CHECKSUMS)
        fetch_runtime.CHECKSUMS[self.triple] = hashlib.sha256(payload).hexdigest()
        self.addCleanup(lambda: fetch_runtime.CHECKSUMS.update(real))


class TestAnUnverifiedRuntimeIsNeverUnpacked(RuntimeCase):
    """The load-bearing test. A runtime that is not the one we pinned is not a
    runtime we are willing to ship, whatever the reason -- a republished tag,
    a compromised mirror, a truncated download."""

    def test_a_mismatched_checksum_is_fatal(self):
        self.serve(a_tarball(b"not the runtime we pinned"))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as caught:
                fetch_runtime.fetch(self.triple, into=td)
            self.assertIn("checksum mismatch", str(caught.exception))

    def test_nothing_is_extracted_when_the_checksum_fails(self):
        """Verification happens *before* extraction. Unpacking first and
        checking afterwards leaves a plausible-looking tree on disk that
        whatever runs next would find and use."""
        self.serve(a_tarball(b"wrong"))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                fetch_runtime.fetch(self.triple, into=td)
            left = list(Path(td).rglob("*"))
            self.assertEqual([], left, f"a rejected runtime was unpacked: {left}")

    def test_the_message_says_what_to_do_about_it(self):
        """A checksum failure is either a real problem or a deliberate
        upstream change. The message has to distinguish them, or the reflex is
        to edit the constant until it passes."""
        self.serve(a_tarball(b"wrong"))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as caught:
                fetch_runtime.fetch(self.triple, into=td)
            said = str(caught.exception)
            self.assertIn("expected", said)
            self.assertIn("deliberately", said)

    def test_a_matching_checksum_does_extract(self):
        """The control. Without it every test above passes against a fetcher
        that refuses everything."""
        payload = a_tarball()
        self.pin_to(payload)
        self.serve(payload)
        with tempfile.TemporaryDirectory() as td:
            target = fetch_runtime.fetch(self.triple, into=td)
            self.assertTrue((target / "bin" / "python3").exists(),
                            "a verified runtime was not unpacked")

    def test_an_unpinned_architecture_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                fetch_runtime.fetch("sparc-unknown-solaris", into=td)


class TestTheRuntimeIsPinned(RuntimeCase):
    """A build that resolves "latest" at build time is not reproducible, and a
    bad upstream release ships without anyone deciding to."""

    def test_the_version_and_release_are_literal(self):
        self.assertRegex(fetch_runtime.VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(fetch_runtime.RELEASE, r"^\d{8}$")
        self.assertNotIn("latest", fetch_runtime.BASE_URL)

    def test_every_supported_architecture_has_a_checksum(self):
        self.assertTrue(fetch_runtime.CHECKSUMS)
        for triple, digest in fetch_runtime.CHECKSUMS.items():
            with self.subTest(triple=triple):
                self.assertRegex(digest, r"^[0-9a-f]{64}$",
                                 f"{triple} has no usable checksum")

    def test_apple_silicon_is_the_supported_architecture(self):
        """Apple Silicon only, deliberately -- every Mac sold since 2020 is
        arm64, and Intel means a second runtime, build and test target for a
        shrinking population."""
        self.assertIn("aarch64-apple-darwin", fetch_runtime.CHECKSUMS)

    def test_an_intel_mac_is_told_plainly_rather_than_failing_oddly(self):
        """A decision the user meets as a sentence, not as a stack trace. The
        difference between "not supported, here is what to do" and a crash is
        the whole of whether they try the other path."""
        import platform
        real_machine, real_platform = platform.machine, sys.platform
        platform.machine = lambda: "x86_64"
        # sys.platform too, or this only tests an Intel Mac when it happens to
        # run on a Mac. On the Linux CI runner it was reaching the Linux branch
        # and passing for a reason unrelated to the thing being checked.
        sys.platform = "darwin"
        try:
            with self.assertRaises(SystemExit) as caught:
                fetch_runtime.host_triple()
        finally:
            platform.machine, sys.platform = real_machine, real_platform
        said = str(caught.exception)
        self.assertIn("Apple Silicon", said)
        self.assertIn("README", said, "told it is unsupported and not what to do")

    def test_each_supported_platform_resolves_to_its_own_triple(self):
        """Every branch of host_triple, exercised on every runner.

        The Intel-Mac check above stubbed only platform.machine, so on Linux it
        was passing through a branch that had nothing to do with Intel Macs.
        Stubbing both is what makes these tests mean the same thing everywhere
        they run.
        """
        import platform
        cases = [
            ("darwin", "arm64", "aarch64-apple-darwin"),
            ("darwin", "aarch64", "aarch64-apple-darwin"),
            ("win32", "AMD64", "x86_64-pc-windows-msvc"),
            ("linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ]
        real_machine, real_platform = platform.machine, sys.platform
        try:
            for plat, machine, expected in cases:
                sys.platform = plat
                platform.machine = lambda m=machine: m
                self.assertEqual(expected, fetch_runtime.host_triple(),
                                 f"{plat}/{machine} resolved wrongly")
                self.assertIn(expected, fetch_runtime.CHECKSUMS,
                              f"{expected} has no pinned checksum")
        finally:
            platform.machine, sys.platform = real_machine, real_platform

    def test_an_unsupported_architecture_says_so_on_every_platform(self):
        """Not just on macOS. A 32-bit Windows or an arm Linux gets a sentence
        naming the command-line install, not a KeyError three frames later."""
        import platform
        real_machine, real_platform = platform.machine, sys.platform
        try:
            for plat, machine in (("win32", "x86"), ("linux", "aarch64"),
                                  ("freebsd13", "amd64")):
                sys.platform = plat
                platform.machine = lambda m=machine: m
                with self.assertRaises(SystemExit) as caught:
                    fetch_runtime.host_triple()
                self.assertTrue(str(caught.exception).strip(),
                                f"{plat}/{machine} exits without saying why")
        finally:
            platform.machine, sys.platform = real_machine, real_platform

    def test_the_archive_name_matches_the_pinned_version(self):
        name = fetch_runtime.archive_name("aarch64-apple-darwin")
        self.assertIn(fetch_runtime.VERSION, name)
        self.assertIn(fetch_runtime.RELEASE, name)

    def test_the_fetcher_needs_nothing_but_the_standard_library(self):
        """Hard rule 1 covers the shipped code. A build tool that needed a
        package would put one back in by the side door."""
        import ast
        if not hasattr(sys, "stdlib_module_names"):
            # 3.10+. The project supports 3.9, and CI runs both, so this
            # skips rather than fails there — the check still runs on every
            # newer interpreter, which is enough to catch a stray dependency.
            self.skipTest("sys.stdlib_module_names needs Python 3.10+")
        tree = ast.parse((ROOT / "tools" / "fetch_runtime.py")
                         .read_text(encoding="utf-8"))
        stdlib = set(sys.stdlib_module_names)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                self.assertIn(name, stdlib, f"non-stdlib import: {name}")


class TestWhatShipsIsADecision(unittest.TestCase):
    """The payload is assembled by tools/stage_bundle.py rather than globbed
    out of the repository, so "what ships" is a list somebody wrote rather than
    whatever happened to be lying about."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import stage_bundle
        self.stage_bundle = stage_bundle

    def staged(self, td):
        return self.stage_bundle.stage(runtime_dir=Path(td) / "no-runtime",
                                       into=Path(td) / "payload")

    def test_every_module_the_app_needs_is_there(self):
        """The listed modules are staged.

        Necessary but nowhere near sufficient, and worth saying why: this walks
        MODULES and checks each arrived, so it compares the list against
        itself. It cannot notice a module the list has *never* heard of, and it
        did not — weather.py shipped in #6 while the payload never carried it,
        so the installed app raised ModuleNotFoundError on first import. The
        test below is the one that catches that.
        """
        with tempfile.TemporaryDirectory() as td:
            out = self.staged(td)
            for name in self.stage_bundle.MODULES + self.stage_bundle.PAGES:
                with self.subTest(name=name):
                    self.assertTrue((out / "airo" / name).exists(),
                                    f"{name} would not ship")

    def test_every_first_party_import_is_shipped(self):
        """Structural, and instant: what do the shipped modules import that is
        not standard library, and is all of it in the payload?

        Complements the subprocess import below rather than replacing it. This
        one is fast enough to run on every commit and names the missing module
        directly; that one is definitive because it asks Python. Between them,
        a module can be neither forgotten nor mis-listed.
        """
        import ast
        staged = {m[:-3] for m in self.stage_bundle.MODULES}
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        if not stdlib:
            self.skipTest("sys.stdlib_module_names needs Python 3.10+")

        needed = {}
        for name in self.stage_bundle.MODULES:
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif (isinstance(node, ast.ImportFrom)
                      and node.level == 0 and node.module):
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    if m not in stdlib:
                        needed.setdefault(m, name)

        for module, importer in sorted(needed.items()):
            self.assertIn(
                module, staged,
                f"{importer} imports {module!r}, which stage_bundle does not "
                f"ship — the installed app would raise ModuleNotFoundError on "
                f"first import. Add {module}.py to MODULES.")

    def test_the_staged_payload_actually_imports(self):
        """Run the payload, do not inspect it.

        Every list-based check here compares a list with itself. This one asks
        Python, in a subprocess with only the payload on its path, whether the
        thing we are about to ship can be imported at all.

        It is the check that would have caught weather.py: poller.py imported
        a module the bundler had never been told about, so the app was broken
        for every user from the moment they opened it, while every test passed.

        Imported rather than executed: importing runs the module top-level,
        which is where a missing dependency shows up, without polling anything
        or touching a real data directory.
        """
        import os
        with tempfile.TemporaryDirectory() as td:
            out = self.staged(td)
            airo = out / "airo"
            env = dict(os.environ)
            # An isolated home, so nothing here can read or write real data.
            env["HOME"] = td
            env["USERPROFILE"] = td
            env["AIRO_CONFIG"] = str(Path(td) / "config.json")
            env["AIRO_DATA"] = str(Path(td) / "data")
            env["PYTHONPATH"] = ""      # only the payload, nothing inherited

            for entry in ("poller", "setup", "backup", "analyse"):
                with self.subTest(module=entry):
                    r = subprocess.run(
                        [sys.executable, "-c", f"import {entry}"],
                        cwd=str(airo), env=env,
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(
                        0, r.returncode,
                        f"the staged payload cannot import {entry}: "
                        f"{r.stderr.strip()[-300:]}")

    def test_a_missing_module_fails_the_build_rather_than_shipping_without_it(self):
        real = list(self.stage_bundle.MODULES)
        self.stage_bundle.MODULES.append("not_a_real_module.py")
        try:
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(SystemExit):
                    self.staged(td)
        finally:
            self.stage_bundle.MODULES[:] = real

    def test_nothing_of_the_users_can_reach_the_payload(self):
        """Rules 2, 2a and 2b. A config carries a location and may carry a
        key; a database is years of one person's movements. Checked after
        staging rather than trusted from the list, because the runtime tree is
        copied wholesale and the list is a human artefact."""
        with tempfile.TemporaryDirectory() as td:
            out = self.staged(td)
            (out / "airo" / "config.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                self.stage_bundle._refuse_anything_private(out)
            self.assertIn("config.json", str(caught.exception))

    def test_a_stray_key_or_database_is_caught_too(self):
        with tempfile.TemporaryDirectory() as td:
            out = self.staged(td)
            for name in ("purpleair.key", "airo.db", "readings.csv"):
                with self.subTest(name=name):
                    stray = out / "airo" / name
                    stray.write_text("x", encoding="utf-8")
                    with self.assertRaises(SystemExit):
                        self.stage_bundle._refuse_anything_private(out)
                    stray.unlink()

    def test_stage_itself_refuses_a_private_file_it_copied(self):
        """Calling the check directly proves the check works, not that anything
        calls it — and removing the call from stage() left every other test
        here green.

        The realistic route in is the runtime tree, which is copied wholesale
        rather than listed, so anything sitting in it ships.
        """
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
            (runtime / "leftover.key").write_text("secret", encoding="utf-8")

            with self.assertRaises(SystemExit) as caught:
                self.stage_bundle.stage(runtime_dir=runtime,
                                        into=Path(td) / "payload")
            self.assertIn("leftover.key", str(caught.exception))

    def test_stage_accepts_a_clean_runtime(self):
        """The control for the test above."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
            out = self.stage_bundle.stage(runtime_dir=runtime,
                                          into=Path(td) / "payload")
            self.assertTrue((out / "runtime" / "bin" / "python3").exists())

    def test_a_clean_payload_passes(self):
        """The control: without it every test above passes against a check
        that refuses everything."""
        with tempfile.TemporaryDirectory() as td:
            out = self.staged(td)
            self.stage_bundle._refuse_anything_private(out)

    def test_staging_starts_from_nothing(self):
        """A file left from a previous layout would ship, and nothing would
        notice."""
        with tempfile.TemporaryDirectory() as td:
            out = self.staged(td)
            stale = out / "airo" / "from_an_older_layout.py"
            stale.write_text("# gone in the next build", encoding="utf-8")
            out = self.staged(td)
            self.assertFalse(stale.exists(), "a stale file survived a rebuild")


class TestTheRuntimeIsNotCommitted(unittest.TestCase):
    """69 MB of someone else's binaries. Reproducible from the pinned version
    and checksum, so it is built rather than stored."""

    def test_the_runtime_directory_is_ignored(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("tray/runtime/", ignored)

    def test_no_runtime_is_tracked(self):
        import subprocess
        out = subprocess.run(["git", "ls-files", "tray/runtime"],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertEqual("", out.stdout.strip(),
                         "a fetched runtime has been committed")

    def test_no_staged_payload_is_tracked(self):
        import subprocess
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("tray/payload/", ignored)
        out = subprocess.run(["git", "ls-files", "tray/payload"],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertEqual("", out.stdout.strip(),
                         "a staged payload has been committed")


class TestTheReleasePipeline(unittest.TestCase):
    """What a tag turns into.

    A release workflow is the one piece of automation whose output goes
    straight to strangers, and the one nobody exercises between releases. Its
    failures are therefore discovered by a user, months later, with no way to
    tell what was in the build.
    """

    def workflow(self):
        return (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")

    def commands(self):
        """Only what actually runs, taken by indentation.

        Two ways this went wrong before, both leaving a mutation undetected:

        * matching the whole file passed on a *comment* naming the command, so
          replacing the step with `run: true` stayed green
        * a loose continuation rule swallowed the release-notes body, which
          quotes `shasum` as advice to the reader -- so deleting the real
          checksum step stayed green too

        A block scalar ends when the indentation drops back, so that is what
        is used rather than a guess about which lines look like commands.
        """
        out, run_indent = [], None
        for line in self.workflow().splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if run_indent is not None:
                if indent > run_indent:
                    if not line.strip().startswith("#"):
                        out.append(line.strip())
                    continue
                run_indent = None
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("run:"):
                value = stripped[len("run:"):].strip()
                if value in ("|", ">", "|-", ">-", ""):
                    run_indent = indent
                else:
                    out.append(value)
        return "\n".join(out)

    def test_the_tests_run_before_anything_is_published(self):
        """Publishing an installer built from code that does not pass is
        worse than publishing nothing: it is a download somebody trusts."""
        text = self.commands()
        tests_at = text.index("unittest discover")
        build_at = text.index("cargo tauri build")
        self.assertLess(tests_at, build_at,
                        "the app is built before the suite has run")

    def test_the_runtime_is_fetched_and_therefore_verified(self):
        """fetch_runtime.py checks the pinned SHA-256 before extracting. A
        release that skipped it would ship whatever upstream served that
        morning."""
        self.assertIn("tools/fetch_runtime.py", self.commands(),
                      "the runtime is not actually fetched, only mentioned")

    def test_the_payload_is_staged_and_therefore_screened(self):
        """stage_bundle.py refuses to continue if the payload contains a
        config, a key or a database. Building without it would ship whatever
        happened to be in the tree."""
        text = self.commands()
        stage_at = text.index("tools/stage_bundle.py")
        build_at = text.index("cargo tauri build")
        self.assertLess(stage_at, build_at,
                        "the app is built before the payload is screened")

    def test_a_checksum_is_published_with_the_download(self):
        """Without one there is no way for anyone to tell whether what they
        downloaded is what was built."""
        self.assertIn("shasum -a 256", self.commands(),
                      "no checksum is actually computed")
        self.assertIn(".sha256", self.workflow(),
                      "the checksum is computed but never published")

    def test_the_runner_architecture_is_pinned(self):
        """macos-latest has moved between architectures before. Left floating,
        it would silently change what users download."""
        text = self.workflow()
        self.assertIn("runs-on: macos-14", text)
        self.assertNotIn("runs-on: macos-latest", text)

    def test_the_release_notes_admit_the_build_is_unsigned(self):
        """A user who is not warned meets a dialog that reads as malware and
        stops. Until notarisation lands, saying so is the whole mitigation."""
        text = self.workflow()
        self.assertIn("not yet signed", text)
        self.assertIn("Right-click", text)

    def test_the_release_notes_say_which_macs(self):
        text = self.workflow()
        self.assertIn("Apple Silicon", text)
        self.assertIn("Intel is not supported", text)

    def test_a_missing_artefact_fails_rather_than_publishing_nothing(self):
        """An empty release looks like a release. fail_on_unmatched_files
        turns "the build produced nothing" into a red run instead."""
        self.assertIn("fail_on_unmatched_files: true", self.workflow())

    def test_it_can_be_run_without_minting_a_version(self):
        """A workflow that only ever runs at release time is one whose
        breakage is discovered at release time."""
        self.assertIn("workflow_dispatch", self.workflow())

    def test_nothing_is_published_from_an_arbitrary_commit(self):
        """An installer built from a push to main is one somebody will
        eventually hand to a user, with no way to say what is in it."""
        text = self.workflow()
        head = text[:text.index("jobs:")]
        self.assertIn('tags: ["v*"]', head)
        self.assertNotIn("branches:", head)




class TestEveryPlatformIsActuallyBuilt(unittest.TestCase):
    """Three installers, or an honest reason why not.

    A platform that silently stops being built is invisible until somebody on
    it has nothing to download — and by then the tag is cut and the release
    page simply has a gap nobody can explain.

    Enumerated from the pinned runtimes rather than a list written here, so
    adding a fourth platform to CHECKSUMS without wiring its job is a red test
    rather than a discovery.
    """

    def workflow(self):
        return (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")

    RUNNER_FOR = {
        "aarch64-apple-darwin": "macos-",
        "x86_64-pc-windows-msvc": "windows-",
        "x86_64-unknown-linux-gnu": "ubuntu-",
    }

    def test_each_pinned_runtime_has_a_job_that_builds_on_it(self):
        text = self.workflow()
        for triple in fetch_runtime.CHECKSUMS:
            runner = self.RUNNER_FOR.get(triple)
            self.assertIsNotNone(
                runner, f"{triple} is pinned but no runner is mapped for it")
            self.assertTrue(
                f"runs-on: {runner}" in text,
                f"{triple} is pinned as a supported platform, but nothing in "
                f"the release workflow builds on {runner}*")

    def test_every_job_attaches_something_to_the_release(self):
        """A job that builds and attaches nothing is a job that looks green
        and ships no download."""
        text = self.workflow()
        jobs = text.count("runs-on:")
        attaches = text.count("action-gh-release")
        self.assertGreaterEqual(
            attaches, jobs - 1,      # the notes job attaches text, not files
            "a platform builds without publishing what it built")

    def test_every_pinned_runtime_says_where_its_interpreter_lives(self):
        """python-build-standalone does not lay the platforms out the same
        way. A triple added without its interpreter path ships a bundle whose
        Python is present and unfindable."""
        for triple in fetch_runtime.CHECKSUMS:
            self.assertIn(
                triple, fetch_runtime.INTERPRETER,
                f"{triple} is pinned but nothing says where its python is")
            self.assertTrue(fetch_runtime.INTERPRETER[triple])

    def test_the_untested_platforms_are_labelled_as_such(self):
        """Windows and Linux are built by CI and installed by nobody.

        Publishing them is right — those users get something to try, and
        breakage surfaces here rather than in a bug report. Publishing them
        as though they were tested is not. If someone verifies a platform on
        real hardware, this test is what they update.
        """
        # assertTrue, not assertIn: the haystack is the whole workflow, and a
        # failure that prints 250 lines of YAML buries the one sentence that
        # says what is wrong.
        text = self.workflow().lower()
        self.assertTrue("not yet tested on real hardware" in text,
                        "the release notes claim more than anyone has checked")
        for job in ("windows (x86_64, untested)", "linux (x86_64, untested)"):
            self.assertTrue(job in text,
                            f"{job!r} is missing — an unverified platform is "
                            f"not labelled in its job name")

    def test_the_notes_are_written_after_the_builds(self):
        """Otherwise the release page can describe a download that failed to
        build, which is worse than describing nothing."""
        text = self.workflow()
        self.assertIn("needs: [macos, windows, linux]", text)



def setUpModule():
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()


if __name__ == "__main__":
    unittest.main()
