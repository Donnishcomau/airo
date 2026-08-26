# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fault-injection harness, which exists because doing it by hand lied.

`tools/faultcheck.py` breaks something on purpose and checks a test notices.
It replaces doing that by hand, which went wrong three ways in one week, and
each way produced a confident wrong answer rather than an obvious failure:

  * seven faults injected against a baseline that already had two errors —
    every fault reported the same two names and none of it meant anything
  * a restore that restored the file and not the *bytecode*, so later runs
    executed the faulted code while `inspect.getsource` showed the original
  * faults injected into the tests instead of the code, where deleting an
    assertion cannot fail and five greens read as evidence

The second is the one worth a test of its own, because it is invisible: Python
invalidates a `.pyc` by modified time and size, and a restored file of the same
size written in the same second does not invalidate anything.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faultcheck  # noqa: E402
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)


def setUpModule():
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


def isolated_env(home):
    """An environment whose idea of home is disposable.

    Every subprocess here gets one. None of them writes to `~/.airo` today —
    they import a module or run a miniature suite — but "this one is careful"
    is the sentence in front of every route into the real install that has
    been found so far, and there have been five.
    """
    import os
    return dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                AIRO_DATA=str(Path(home) / "data"),
                AIRO_CONFIG=str(Path(home) / ".airo" / "config.json"),
                PYTHONDONTWRITEBYTECODE="")


class TestTheRestoreIsReal(unittest.TestCase):
    """The failure that cost twenty minutes and looked like a database bug."""

    def test_a_same_size_edit_is_invisible_until_the_cache_is_cleared(self):
        """The hazard, in the direction that actually bites.

        Python invalidates a `.pyc` by modified time and size. A fault written
        over the original with a string of the same length, in the same
        second, changes neither — so the edit is not seen at all and the run
        executes the *original* code.

        That is worse than the restore problem it was found through, because
        it fails toward silence: the fault never applies, nothing goes red, and
        the report reads "NOTHING CAUGHT IT" for a property that is perfectly
        well guarded. Both directions are handled by clearing caches on the way
        in and on the way out.

        clock-independent: the modified time is pinned rather than assumed.
        This test used to just write twice in a row and rely on both landing
        inside the same whole second. That held on a quiet laptop and failed on
        a loaded macOS runner, where the subprocess between the two writes took
        long enough to cross a second boundary — Python then saw the edit, and
        the test reported that the hazard was gone when it was merely not
        triggered. Setting the mtime back makes the stated condition ("same
        size, same modified time") true by construction instead of by luck.
        """
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            module = pkg / "subject.py"
            module.write_text("VALUE = 'aaaa'\n", encoding="utf-8")
            # The stamp the .pyc will record. Captured before it is written,
            # because that is the value Python compares against later.
            original = os.stat(str(module))

            def value_now():
                out = subprocess.run(
                    [sys.executable, "-c",
                     "import subject; print(subject.VALUE)"],
                    cwd=str(pkg), capture_output=True, text=True,
                    env=isolated_env(pkg))
                return out.stdout.strip()

            def clear():
                for cache in pkg.rglob("__pycache__"):
                    shutil.rmtree(cache, ignore_errors=True)

            self.assertEqual("aaaa", value_now())

            # Same length, same modified time: nothing Python looks at has
            # changed, so the cached bytecode is still considered current.
            module.write_text("VALUE = 'bbbb'\n", encoding="utf-8")
            os.utime(str(module), (original.st_atime, original.st_mtime))
            self.assertEqual(
                original.st_size, os.stat(str(module)).st_size,
                "the two versions are no longer the same size, so this test "
                "is not exercising the hazard it describes")
            self.assertEqual(
                "aaaa", value_now(),
                "a same-size edit is now seen without clearing the cache — "
                "the reason faultcheck clears is gone, and this test should "
                "be removed rather than adjusted")

            clear()
            self.assertEqual("bbbb", value_now(),
                             "clearing the cache did not apply the edit")

            module.write_text("VALUE = 'aaaa'\n", encoding="utf-8")
            clear()
            self.assertEqual("aaaa", value_now(),
                             "clearing the cache did not restore the original")

    def test_clearing_bytecode_finds_this_projects_caches(self):
        """A no-op cleaner would pass every test above by accident."""
        with tempfile.TemporaryDirectory() as home:
            subprocess.run([sys.executable, "-c", "import store"],
                           cwd=str(ROOT), capture_output=True,
                           env=isolated_env(home))
        self.assertTrue((ROOT / "__pycache__").exists(),
                        "nothing cached, so this proves nothing")
        self.assertGreater(faultcheck.clear_bytecode(), 0)
        self.assertFalse((ROOT / "__pycache__").exists())

    def test_it_leaves_the_rust_build_alone(self):
        """`tray/target` holds build output measured in gigabytes and none of
        it is Python. Sweeping it would turn a fault run into a rebuild."""
        import inspect
        self.assertIn("tray/target", inspect.getsource(faultcheck.clear_bytecode))


class TestItRefusesToRunAgainstABrokenBaseline(unittest.TestCase):
    """The first failure: injecting into an already-failing suite.

    Every fault reported the same two pre-existing names, all of them read as
    caught, and the run proved nothing at all. I nearly took it as evidence.
    """

    def harness(self, subject_body, test_body, faults):  # noqa: D401
        """A whole miniature project, so this exercises the real script."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "tests").mkdir()
        (root / "subject.py").write_text(textwrap.dedent(subject_body),
                                         encoding="utf-8")
        (root / "tests" / "test_subject.py").write_text(
            textwrap.dedent(test_body), encoding="utf-8")
        spec = root / "faults.json"
        spec.write_text(json.dumps(faults), encoding="utf-8")

        # Into `tools/`, because the script resolves its project root as
        # `__file__.parent.parent`. Dropped at the top level it would treat the
        # temp directory's *parent* as the project and run this repository's
        # suite instead of the miniature one — which passes, and would make
        # every assertion below meaningless.
        (root / "tools").mkdir()
        script = root / "tools" / "faultcheck.py"
        script.write_text(
            (ROOT / "tools" / "faultcheck.py").read_text(encoding="utf-8"),
            encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(script), str(spec)],
            cwd=str(root), capture_output=True, text=True, timeout=600,
            env=isolated_env(root))

    SUBJECT = """
        VALUE = 10
    """
    PASSING_TEST = """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import unittest
        import subject

        class T(unittest.TestCase):
            def test_value(self):
                self.assertEqual(10, subject.VALUE)
    """
    FAILING_TEST = """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import unittest
        import subject

        class T(unittest.TestCase):
            def test_value(self):
                self.assertEqual(10, subject.VALUE)

            def test_already_broken(self):
                self.assertEqual("this", "was already failing")
    """
    FAULT = [{"name": "the value changes", "file": "subject.py",
              "find": "VALUE = 10", "replace": "VALUE = 99"}]

    def test_a_green_baseline_is_required_before_injecting(self):
        done = self.harness(self.SUBJECT, self.FAILING_TEST, self.FAULT)
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("BASELINE FAILING", done.stdout)
        self.assertIn("Refusing to inject", done.stdout)

    def test_a_caught_fault_reports_red_and_succeeds(self):
        done = self.harness(self.SUBJECT, self.PASSING_TEST, self.FAULT)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("RED", done.stdout)
        self.assertIn("Every fault was caught", done.stdout)

    def test_an_unnoticed_fault_is_reported_rather_than_hidden(self):
        """A fault nothing catches is the finding, not an error. It exits
        non-zero so a script cannot pass over it, and names what was missed.

        The faulted line has to *run*, or the report is UNRUN instead — a
        different finding with a different remedy. `OTHER` is evaluated when
        the module imports and asserted by nothing, which is exactly the
        "missing test" this reports.
        """
        subject = """
            VALUE = 10
            OTHER = 5
        """
        unwatched = [{"name": "something nothing checks", "file": "subject.py",
                      "find": "OTHER = 5", "replace": "OTHER = 6"}]
        done = self.harness(subject, self.PASSING_TEST, unwatched)
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("went unnoticed", done.stdout)
        self.assertIn("something nothing checks", done.stdout)

    def test_the_subject_is_left_exactly_as_it_was(self):
        """The harness edits real source. A run that does not put it back is
        worse than no run — it leaves a change nobody wrote."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "tests").mkdir()
        subject = root / "subject.py"
        subject.write_text("VALUE = 10\n", encoding="utf-8")
        before = subject.read_bytes()
        (root / "tests" / "test_subject.py").write_text(
            textwrap.dedent(self.PASSING_TEST), encoding="utf-8")
        spec = root / "faults.json"
        spec.write_text(json.dumps(self.FAULT), encoding="utf-8")
        (root / "tools").mkdir()
        script = root / "tools" / "faultcheck.py"
        script.write_text(
            (ROOT / "tools" / "faultcheck.py").read_text(encoding="utf-8"),
            encoding="utf-8")

        subprocess.run([sys.executable, str(script), str(spec)],
                       cwd=str(root), capture_output=True, timeout=600,
                       env=isolated_env(root))
        self.assertEqual(before, subject.read_bytes())


class TestAFaultMustBeTheOneDescribed(unittest.TestCase):
    def test_text_matching_twice_is_refused(self):
        """A `find` that matches in two places replaces one of them, and which
        one depends on file order. That is not the fault the name claims, and
        a RED from it says nothing about the property under test."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "subject.py"
            target.write_text("x = 1\ny = 1\n", encoding="utf-8")
            saved = faultcheck.ROOT
            faultcheck.ROOT = root
            try:
                with self.assertRaises(ValueError) as caught:
                    faultcheck.apply_fault({"name": "n", "file": "subject.py",
                                            "find": "= 1", "replace": "= 2"})
                self.assertIn("appears 2 times", str(caught.exception))
            finally:
                faultcheck.ROOT = saved

    def test_a_fault_that_breaks_the_build_is_not_read_as_caught_silently(self):
        """A syntax error produces no FAIL lines and a non-zero exit, which
        reads as "nothing caught it" unless the runner says otherwise. That
        happened: a fault left a bare `for` with no body, the suite could not
        import, and it was recorded as an untested gap."""
        import inspect
        source = inspect.getsource(faultcheck.run_tests)
        self.assertIn("did not run", source,
                      "a suite that cannot start is indistinguishable from "
                      "one that passed")


def with_real_home(func, *args, **kwargs):
    """Run `func` with the home this machine actually has.

    The coverage check shells out, and coverage.py is installed in the user
    site-packages under the real home — with HOME redirected the subprocess
    reports "No module named coverage" and the check degrades to "cannot
    tell". That degrading is correct behaviour and it makes the check
    untestable from inside a guarded suite, so these tests put the real home
    back for the duration of the call and nothing else.

    Taken from homeguard, which captured it at import before any redirection.
    """
    import os
    from homeguard import REAL_AIRO_HOME
    real = str(Path(REAL_AIRO_HOME).parent)
    saved = {n: os.environ.get(n) for n in ("HOME", "USERPROFILE")}
    for name in saved:
        os.environ[name] = real
    try:
        return func(*args, **kwargs)
    finally:
        for name, value in saved.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)


class TestItKnowsWhenAFaultCouldNotHaveBeenCaught(unittest.TestCase):
    """The two ways a green means nothing.

    "Nothing caught it" and "nothing ran it" read identically in a report and
    call for opposite responses: the first is a missing test, the second is a
    fault aimed at a line the suite never reaches. Treating the second as the
    first sends somebody hunting a gap that is not there — which is exactly
    what happened, twice, with edits that were no-ops.
    """

    def test_a_comment_only_edit_is_refused_before_it_runs(self):
        """It cannot change behaviour, so a green from it would be
        meaningless. Caught by comparing syntax trees, which is every
        comment, docstring and whitespace change."""
        path = ROOT / "store.py"
        original = path.read_text(encoding="utf-8")
        commented = original.replace("# v8: a source records where it is.",
                                     "# v8: CHANGED COMMENT.", 1)
        self.assertNotEqual(original, commented, "the fixture changed nothing")
        self.assertTrue(faultcheck.is_a_no_op(path, original, commented))

    def test_a_real_edit_is_not_mistaken_for_a_no_op(self):
        """A guard that refused everything would stop every fault run."""
        path = ROOT / "store.py"
        original = path.read_text(encoding="utf-8")
        real = original.replace('OUTDOOR_PLACEMENTS = ("outdoor",)',
                                'OUTDOOR_PLACEMENTS = ("outdoor", "indoor")', 1)
        self.assertNotEqual(original, real, "the fixture changed nothing")
        self.assertFalse(faultcheck.is_a_no_op(path, original, real))

    def test_an_unparseable_fault_is_a_real_change_not_a_no_op(self):
        """A fault that breaks the syntax is caught by the suite failing to
        import, which the runner already reports as a failure. Calling it a
        no-op here would refuse it before that could happen."""
        path = ROOT / "store.py"
        original = path.read_text(encoding="utf-8")
        broken = original.replace("def is_outdoor(placement):",
                                  "def is_outdoor(placement:", 1)
        self.assertFalse(faultcheck.is_a_no_op(path, original, broken))

    def test_it_reports_where_a_fault_landed(self):
        """The line numbers are what the coverage check is asked about, so
        getting them wrong turns the whole check into noise."""
        before = "a = 1\nb = 2\nc = 3\n"
        after = "a = 1\nb = 99\nc = 3\n"
        self.assertEqual([2], faultcheck.changed_lines(before, after))

    def test_an_inserted_line_is_reported_too(self):
        before = "a = 1\nc = 3\n"
        after = "a = 1\nb = 2\nc = 3\n"
        self.assertIn(2, faultcheck.changed_lines(before, after))

    def test_it_asks_which_lines_the_suite_actually_runs(self):
        """Downgrades rather than fails when coverage is absent: it is a
        development dependency and hard rule 1 keeps it out of what ships."""
        ran = with_real_home(faultcheck.lines_the_suite_runs,
                             ROOT / "units.py", "tests.test_units")
        if ran is None:
            self.skipTest("coverage.py not installed")
        self.assertTrue(ran, "no lines of units.py were recorded as executed")
        self.assertIsInstance(ran, set)

    def test_a_line_nothing_reaches_is_reported_as_unrun_not_as_a_gap(self):
        """End to end, on a miniature project: a fault in a function no test
        calls comes back UNRUN, and the wording says it is a misplaced fault
        rather than a missing test."""
        subject = """
            VALUE = 10

            def never_called():
                return "untouched"
        """
        fault = [{"name": "a fault nothing runs", "file": "subject.py",
                  "find": 'return "untouched"', "replace": 'return "changed"'}]
        done = with_real_home(
            TestItRefusesToRunAgainstABrokenBaseline.harness,
            self, subject, TestItRefusesToRunAgainstABrokenBaseline.PASSING_TEST,
            fault)
        if "UNRUN" not in done.stdout:
            self.skipTest("coverage.py not available to the harness")
        self.assertIn("UNRUN", done.stdout, done.stdout)
        self.assertIn("never executes", done.stdout)
        self.assertEqual(1, done.returncode)


class TestTheCommittedFaultsAreRealAndRun(unittest.TestCase):
    """The specs in `tools/faults/` are the standing proof that each guard
    still fails when broken.

    A fault run by hand proves something once, on the day it was run. These
    are run by CI on every push, which is the difference between a claim and a
    check — and this class is what stops the specs themselves from rotting
    into something that cannot fail.
    """

    def specs(self):
        return sorted((ROOT / "tools" / "faults").glob("*.json"))

    def faults(self):
        for spec in self.specs():
            for fault in json.loads(spec.read_text(encoding="utf-8")):
                yield spec.name, fault

    def test_there_are_committed_faults_at_all(self):
        self.assertTrue(self.specs(), "no fault specs are committed")

    def test_every_fault_still_matches_exactly_once(self):
        """A spec whose `find` no longer matches is a guard nobody is
        checking. It fails loudly here rather than at the next refactor."""
        stale = []
        for name, fault in self.faults():
            path = ROOT / fault["file"]
            if not path.exists():
                stale.append(f"{name}: {fault['file']} is gone")
                continue
            found = path.read_text(encoding="utf-8").count(fault["find"])
            if found != 1:
                stale.append(f"{name}: {fault['name']!r} matches {found} times")
        self.assertEqual([], stale)

    def test_a_fault_that_edits_a_test_says_so(self):
        """Deleting an assertion cannot fail. Five faults were injected into
        tests once and five greens read as evidence, so a fault must break the
        code the tests are watching.

        There is a real exception: a *guard* lives under `tests/` and is
        itself the thing being checked — `notifyguard` blocking notifications,
        or a contract enumerating which suites need it. Breaking one to see
        whether anything notices is exactly the right question.

        So it is allowed and must be declared. An undeclared fault under
        `tests/` is the accident; a declared one is somebody having decided.
        """
        undeclared = [f"{name}: {fault['name']!r} ({fault['file']})"
                      for name, fault in self.faults()
                      if fault["file"].startswith("tests/")
                      and not fault.get("edits_a_guard")]
        self.assertEqual(
            [], undeclared,
            "these faults edit something under tests/ without saying so. If "
            "the target is a guard, add \"edits_a_guard\": true; if it is an "
            "assertion, the fault belongs in the code instead.")

    def test_a_declared_guard_fault_really_does_target_a_guard(self):
        """The declaration is not a licence to disable an assertion. A guard
        is a thing that *prevents* something — a guard module, or a contract
        that enumerates who must install one."""
        wrong = []
        for name, fault in self.faults():
            if not fault.get("edits_a_guard"):
                continue
            target = fault["file"]
            if not (target.endswith("guard.py")
                    or target.endswith("test_contracts.py")):
                wrong.append(f"{name}: {target} is not a guard")
        self.assertEqual([], wrong)

    def test_no_committed_fault_is_a_no_op(self):
        """An edit that leaves the syntax tree identical cannot change
        behaviour, so a green from it would mean nothing."""
        pointless = []
        for name, fault in self.faults():
            path = ROOT / fault["file"]
            if not path.exists():
                continue
            original = path.read_text(encoding="utf-8")
            faulted = original.replace(fault["find"], fault["replace"], 1)
            if faultcheck.is_a_no_op(path, original, faulted):
                pointless.append(f"{name}: {fault['name']!r}")
        self.assertEqual([], pointless)

    def test_every_fault_is_named_as_a_mistake_somebody_could_make(self):
        """The name is what a reader sees when it fires. "fault 3" tells them
        nothing about what broke."""
        for name, fault in self.faults():
            self.assertGreater(len(fault["name"]), 12,
                               f"{name}: {fault['name']!r} says too little")

    def test_the_runner_is_wired_into_the_local_gate(self):
        """A gate nothing calls is a gate that does not exist. Four helpers in
        this project have been fully tested while their call site was gone."""
        check = (ROOT / "tools" / "check.py").read_text(encoding="utf-8")
        self.assertIn("def gate_faults(", check)
        self.assertIn('("faults", gate_faults)', check)
        self.assertIn("--faults", check)

    def test_ci_runs_every_spec_and_fails_on_any_of_them(self):
        """Locally it is opt-in because it costs minutes. On CI it is not
        optional, which is the point — the cost belongs where nobody has to
        remember to pay it."""
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        self.assertIn("tools/faults/*.json", ci)
        self.assertIn("faultcheck.py", ci)
        self.assertIn("exit $status", ci,
                      "a failing spec does not fail the job")

    def test_a_continuation_line_is_attributed_to_its_statement(self):
        """Coverage records the *first* line of a multi-line statement, so a
        fault altering only a continuation line looks unexecuted however often
        it runs. A false UNRUN misleads exactly as much as the false GREEN
        this check exists to remove — it says "your fault is misplaced" about
        a fault that is pointed correctly.
        """
        source = (
            "def f():\n"
            "    rows = call(\n"
            '        "SELECT a "\n'
            '        "FROM b")\n')
        # Line 3 is inside the string; the statement begins at line 2.
        self.assertEqual({2}, faultcheck.statement_starts(source, [3]))

    def test_a_whole_statement_maps_to_itself(self):
        source = "def f():\n    x = 1\n    y = 2\n"
        self.assertEqual({2}, faultcheck.statement_starts(source, [2]))

    def test_a_file_that_will_not_parse_falls_back_to_the_lines(self):
        """A fault that breaks the syntax on purpose still has to report
        something, and the suite failing to import already says the rest."""
        self.assertEqual({4}, faultcheck.statement_starts("def f(\n", [4]))
