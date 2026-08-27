# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The gate that fails when the suite writes into the real `~/.airo`.

Four separate routes into the maintainer's own install have been found and
closed. The fourth one deleted three of their backup archives: a module-level
`Path.home()` froze the real path at *import*, which is before `homeguard` can
redirect anything, and a rotation in `backup.auto()` unlinked everything older
than its own test output.

The guards in `homeguard` cover redirection. The contracts in
`test_contracts.py` cover the shapes somebody has already thought of — no
home-relative path resolved at import, no unguarded session manager. Neither
can cover the shape nobody has thought of yet, and that is the one that gets
you: every one of the four routes was invisible until after it had happened.

`tools/check.py` therefore hashes the real `~/.airo` before the suite and
after it and fails on any difference. This file is what stops *that* check
from quietly rotting: a gate nobody has watched fail is a claim nobody has
checked, and this one guards data that cannot be regenerated.

Everything here runs against a fake home in a temp directory. Proving a
destructive bug is caught must not require reproducing it on the real install
— which is exactly what happened while this was being built: the same
archives were deleted a second time. The lesson is in the shape of these tests rather than in a
comment: the injection runs in a subprocess with `HOME` pointed elsewhere, so
even a path frozen at import lands somewhere disposable.
"""

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check as chk  # noqa: E402
from homeguard import (  # noqa: E402
    redirect_airo_paths_for_module, restore_airo_paths_for_module)
from netguard import (  # noqa: E402
    block_outbound_for_module, restore_outbound_for_module)


def setUpModule():
    """Redirected like every other suite, even though nothing here writes.

    This file reads the real `~/.airo` only through `snapshot_user_data`, and
    only ever with HOME already pointed at a temp directory. The redirection
    is still installed, for the same reason the contract asks for it: "this
    one is careful" is the sentence in front of every route into the real
    install that has been found so far.
    """
    block_outbound_for_module()
    redirect_airo_paths_for_module()


def tearDownModule():
    restore_airo_paths_for_module()
    restore_outbound_for_module()


class FakeHome:
    """A throwaway `~/.airo` with something in it worth losing."""

    def __init__(self, tmp):
        self.home = Path(tmp)
        self.airo = self.home / ".airo"
        (self.airo / "backups").mkdir(parents=True)
        (self.airo / "config.json").write_text(
            json.dumps({"location": {"name": "Testville"}}), encoding="utf-8")
        for name in ("one", "two", "three"):
            (self.airo / "backups" / f"airo-backup-{name}.tar.gz").write_bytes(
                f"pretend archive {name}".encode())

    def snapshot(self):
        """The same hashing check.py does, pointed at this fake home."""
        out = {}
        for path in sorted(self.airo.rglob("*")):
            if path.is_file():
                out[path.relative_to(self.airo).as_posix()] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
        return out


class TestTheGateNoticesWhatMatters(unittest.TestCase):
    """`snapshot_user_data` and `describe_user_data_change`, on a fake home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeHome(self.tmp.name)

    def with_home(self, func):
        saved = {n: os.environ.get(n) for n in ("HOME", "USERPROFILE")}
        for n in saved:
            os.environ[n] = str(self.fake.home)
        try:
            return func()
        finally:
            for n, v in saved.items():
                if v:
                    os.environ[n] = v
                else:
                    os.environ.pop(n, None)

    def test_it_sees_every_file(self):
        snap = self.with_home(chk.snapshot_user_data)
        self.assertIn("config.json", snap)
        self.assertEqual(3, len([k for k in snap if k.startswith("backups/")]))

    def test_keys_read_the_same_on_every_platform(self):
        """Windows renders a relative path with backslashes, so the same file
        is a different key there. For a gate whose entire output is a list of
        filenames, that is the difference between a report somebody can act on
        and one they have to translate — and it is what failed CI first."""
        snap = self.with_home(chk.snapshot_user_data)
        self.assertTrue(any(k.startswith("backups/") for k in snap),
                        f"no key uses forward slashes: {sorted(snap)}")
        self.assertEqual([], [k for k in snap if "\\" in k],
                         "a key carries a backslash separator")

    def test_a_symlink_is_not_counted_as_separate_content(self):
        """`~/.airo/latest.json` is a symlink to `~/.airo/data/latest.json`.

        Counted on its own account, the agent rewriting the target showed up
        twice: once under `data/latest.json`, which the allowance covers, and
        once under `latest.json`, which it does not — so an ordinary poll was
        reported as the suite damaging the install. A gate that cries wolf on
        normal operation is one nobody reads.
        """
        target = self.fake.airo / "data"
        target.mkdir()
        (target / "latest.json").write_text('{"pm25": 1}', encoding="utf-8")
        (self.fake.airo / "latest.json").symlink_to(target / "latest.json")

        snap = self.with_home(chk.snapshot_user_data)
        self.assertIn("data/latest.json", snap, "the real file was skipped too")
        self.assertNotIn("latest.json", snap,
                         "the symlink was hashed as separate content")

    def test_rewriting_through_the_symlink_is_still_seen_at_the_target(self):
        """Skipping the link must not create a blind spot: whatever writes
        through it changes the file, and the file is watched."""
        target = self.fake.airo / "data"
        target.mkdir()
        (target / "latest.json").write_text('{"pm25": 1}', encoding="utf-8")
        link = self.fake.airo / "latest.json"
        link.symlink_to(target / "latest.json")

        before = self.with_home(chk.snapshot_user_data)
        link.write_text('{"pm25": 999}', encoding="utf-8")
        after = self.with_home(chk.snapshot_user_data)

        self.assertNotEqual(before, after,
                            "a write through the symlink went unnoticed")
        self.assertIn("data/latest.json",
                      chk.describe_user_data_change(before, after))

    def test_a_deletion_is_a_difference(self):
        before = self.with_home(chk.snapshot_user_data)
        (self.fake.airo / "backups" / "airo-backup-one.tar.gz").unlink()
        after = self.with_home(chk.snapshot_user_data)
        self.assertNotEqual(before, after)
        self.assertIn("DELETED", chk.describe_user_data_change(before, after))

    def test_a_modification_is_a_difference(self):
        before = self.with_home(chk.snapshot_user_data)
        (self.fake.airo / "config.json").write_text("{}", encoding="utf-8")
        after = self.with_home(chk.snapshot_user_data)
        self.assertIn("modified", chk.describe_user_data_change(before, after))

    def test_a_new_file_is_a_difference(self):
        before = self.with_home(chk.snapshot_user_data)
        (self.fake.airo / "stray.json").write_text("{}", encoding="utf-8")
        after = self.with_home(chk.snapshot_user_data)
        self.assertIn("created", chk.describe_user_data_change(before, after))

    def test_content_is_compared_not_size_and_time(self):
        """The case that actually happened: a rotation deletes an archive and
        writes another of the same size in the same second. A stat comparison
        waves that through; hashing does not."""
        target = self.fake.airo / "backups" / "airo-backup-one.tar.gz"
        original = target.read_bytes()
        before = self.with_home(chk.snapshot_user_data)

        replacement = bytes(len(original))          # same length, different bytes
        self.assertEqual(len(original), len(replacement))
        self.assertNotEqual(original, replacement)
        target.write_bytes(replacement)
        os.utime(target, (0, 0))                    # and an older mtime

        after = self.with_home(chk.snapshot_user_data)
        self.assertNotEqual(before, after,
                            "a same-size replacement went unnoticed")

    def test_no_change_is_no_difference(self):
        """A gate that always fires gets turned off by the first person it
        inconveniences, which is worse than not having it."""
        self.assertEqual(self.with_home(chk.snapshot_user_data),
                         self.with_home(chk.snapshot_user_data))

    def test_a_missing_home_is_not_an_error(self):
        """A fresh machine has no `~/.airo`, and the gate must not fail there —
        CI runners are exactly that, and a gate that fails on every clean
        checkout teaches everyone to ignore it."""
        with tempfile.TemporaryDirectory() as empty:
            saved = {n: os.environ.get(n) for n in ("HOME", "USERPROFILE")}
            for n in saved:
                os.environ[n] = empty
            try:
                self.assertEqual({}, chk.snapshot_user_data())
            finally:
                for n, v in saved.items():
                    if v:
                        os.environ[n] = v
                    else:
                        os.environ.pop(n, None)


class TestTheGateCatchesTheBugThatCostRealData(unittest.TestCase):
    """End to end, against a fake home, with the original defect reintroduced.

    This is the regression test for the incident itself. It reinstates the
    exact shape — `Path.home()` resolved at module import, before any
    redirection — runs the real `test_backup` suite against it in a subprocess
    whose HOME is disposable, and asserts the gate reports a deletion.

    The subprocess is the point. A frozen path is frozen at *import*, so the
    only way to make it land somewhere harmless is to have the whole process
    start with a different HOME. Doing this in-process would reproduce the
    data loss to prove the data loss is caught, which is not a trade anyone
    should make and which I made once already while writing this.
    """

    def test_the_frozen_home_defect_is_caught(self):
        source = ROOT / "backup.py"
        original = source.read_text(encoding="utf-8")
        self.assertIn("def backup_dir():", original,
                      "the fix this guards has moved; update the injection")

        broken = original.replace(
            "def backup_dir():",
            'BACKUP_DIR = Path.home() / ".airo" / "backups"\n\n\n'
            "def backup_dir():\n    return BACKUP_DIR\n\n\n"
            "def _superseded_backup_dir():", 1)

        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeHome(tmp)
            before = fake.snapshot()

            env = dict(os.environ, HOME=str(fake.home),
                       USERPROFILE=str(fake.home))
            source.write_text(broken, encoding="utf-8")
            try:
                subprocess.run(
                    [sys.executable, "-m", "unittest", "tests.test_backup"],
                    cwd=str(ROOT), env=env, capture_output=True, timeout=600)
            finally:
                source.write_text(original, encoding="utf-8")

            self.assertEqual(original, source.read_text(encoding="utf-8"),
                             "the injection was not undone")

            after = fake.snapshot()
            self.assertNotEqual(
                before, after,
                "the frozen-home defect was reintroduced and nothing in the "
                "fake home changed — this test is no longer exercising it")
            self.assertIn("DELETED", chk.describe_user_data_change(before, after),
                          "archives were not deleted, so the regression this "
                          "guards is not being reproduced")

    def test_the_current_code_leaves_a_fake_home_alone(self):
        """The other half, and the one that says the fix works: the same suite,
        unmodified, against the same fake home, changes nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeHome(tmp)
            before = fake.snapshot()

            env = dict(os.environ, HOME=str(fake.home),
                       USERPROFILE=str(fake.home))
            subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_backup"],
                cwd=str(ROOT), env=env, capture_output=True, timeout=600)

            self.assertEqual(before, fake.snapshot(),
                             "the suite still writes into the home it is "
                             "given: " + chk.describe_user_data_change(
                                 before, fake.snapshot()))


class TestTheGateIsWiredIntoTheRunner(unittest.TestCase):
    """A gate that exists and is never called is not a gate.

    Four helpers in this project have been fully tested while their call site
    was deleted. This one guards data that cannot be regenerated, so its call
    site gets a test of its own.
    """

    def source(self):
        return (ROOT / "tools" / "check.py").read_text(encoding="utf-8")

    def test_the_test_gate_snapshots_before_and_after(self):
        body = self.source()
        gate = body[body.index("def gate_tests("):body.index("def gate_compile(")]
        self.assertEqual(
            2, gate.count("snapshot_user_data()"),
            "gate_tests does not take both a before and an after snapshot")
        self.assertIn("user_data_damage(", gate,
                      "the two snapshots are taken and never compared")

    def test_a_difference_fails_the_gate_rather_than_warning(self):
        body = self.source()
        gate = body[body.index("def gate_tests("):body.index("def gate_compile(")]
        # The branch, not just the assignment. Checking only that
        # `res.ok = False` appears after the damage call stayed true when the
        # condition was replaced with `if False:` — the line was still there,
        # unreachable, and the test could not tell.
        import ast
        tree = ast.parse(body)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "gate_tests")
        guarded = [n for n in ast.walk(fn)
                   if isinstance(n, ast.If)
                   and isinstance(n.test, ast.Name) and n.test.id == "damage"]
        self.assertEqual(
            1, len(guarded),
            "no `if damage:` branch in gate_tests — damaging the real "
            "~/.airo does not fail the run")
        assigns = ast.dump(guarded[0])
        self.assertIn("'ok'", assigns, "the branch does not set res.ok")
        self.assertIn("Return", assigns,
                      "the gate reports damage and carries on regardless")

    def test_the_snapshot_never_opens_the_database_through_store(self):
        """It would run migrations on the maintainer's own install in order to
        check whether anything ran on the maintainer's own install.

        Code lines only. The comment explaining why `store.connect` is avoided
        names it, and a substring search over the whole file cannot tell a
        warning from the thing it warns about — the same slip as the
        `Path.cwd()` check in test_backup.
        """
        # Strip comments *and* docstrings. The reasoning for avoiding
        # store.connect lives in a docstring that names it, and a line-based
        # filter that only knows about `#` reads that as the call itself.
        import ast
        tree = ast.parse(self.source())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.update(doc.splitlines())
        code = [l for l in self.source().splitlines()
                if not l.strip().startswith("#") and l not in docstrings]
        self.assertEqual([], [l for l in code if "store.connect" in l])


class TestAPollingAgentIsNotDamage(unittest.TestCase):
    """The developer's own poller does not stop for a test run.

    It rewrites the database and appends to a log every fifteen minutes, so
    the first version of this gate failed the moment a poll landed mid-suite.
    A check that fires on normal operation gets switched off within a day,
    which is worse than not having one — the file's own
    `test_no_change_is_no_difference` says so.

    Ignoring those files outright would be the other error: a test corrupting
    the database is the worst case there is. They are checked differently
    rather than skipped — the database may not lose readings and a log may
    only grow.
    """

    def damage(self, before, after, rows=(10, 10), sizes=None):
        sizes = sizes or ({}, {})
        return chk.user_data_damage(before, after, rows[0], rows[1],
                                    sizes[0], sizes[1])

    def test_a_poll_rewriting_the_database_is_not_damage(self):
        before = {"data/airo.db": "aaa", "data/poller.log": "l1",
                  "config.json": "cfg"}
        after = {"data/airo.db": "bbb", "data/poller.log": "l2",
                 "config.json": "cfg"}
        self.assertEqual(
            [], self.damage(before, after, rows=(18494, 18628),
                            sizes=({"data/poller.log": 10},
                                   {"data/poller.log": 20})))

    def test_a_deleted_backup_is_damage(self):
        """The thing that actually happened."""
        before = {"backups/airo-backup-one.tar.gz": "a"}
        found = self.damage(before, {})
        self.assertTrue(found)
        self.assertIn("DELETED", found[0])

    def test_a_changed_config_is_damage(self):
        found = self.damage({"config.json": "a"}, {"config.json": "b"})
        self.assertTrue(found, "the user's settings were rewritten silently")

    def test_readings_going_backwards_is_damage_even_if_files_look_normal(self):
        """The case the file comparison cannot see: the database is *supposed*
        to change, so only the row count says whether it lost anything."""
        found = self.damage({"data/airo.db": "a"}, {"data/airo.db": "b"},
                            rows=(18494, 12))
        self.assertTrue(found)
        self.assertIn("READINGS LOST", found[0])

    def test_a_truncated_log_is_damage(self):
        """The agent only appends. A log that shrank was written by something
        else, and losing a log loses the record of what happened."""
        found = self.damage({"data/poller.log": "a"}, {"data/poller.log": "b"},
                            sizes=({"data/poller.log": 900},
                                   {"data/poller.log": 10}))
        self.assertTrue(found)
        self.assertIn("truncated", found[0])

    def test_a_deleted_database_is_damage_despite_being_agent_owned(self):
        """Being a file the agent writes does not license removing it."""
        found = self.damage({"data/airo.db": "a"}, {}, rows=(10, None))
        self.assertTrue(found)
        self.assertIn("DELETED", found[0])

    def test_a_key_file_is_never_agent_owned(self):
        found = self.damage({"purpleair.key": "a"}, {"purpleair.key": "b"})
        self.assertTrue(found, "an API key was rewritten and it passed")

    def test_the_allowance_is_narrow_and_named(self):
        """Enumerated so the allowance cannot quietly widen. Anything outside
        `data/` is compared byte for byte, always."""
        for name in ("config.json", "backups/x.tar.gz", "purpleair.key",
                     "data-location"):
            self.assertFalse(chk._is_agent_write(name), name)
        for name in ("data/airo.db", "data/latest.json", "data/poller.log"):
            self.assertTrue(chk._is_agent_write(name), name)

    def test_sqlite_checkpointing_its_own_files_is_not_damage(self):
        """`-wal` and `-shm` belong to SQLite, which creates and removes them
        as it checkpoints. Nothing outside SQLite may reason about their
        lifetime — this project learned that once already, when a migration
        test asserted a `-wal` still existed and passed on macOS while failing
        on Linux.

        Treating them as agent writes was not enough: the rule for those is
        "never deleted", and SQLite deletes these routinely. The gate failed
        about one run in three, and a gate that fails at random is one nobody
        reads.
        """
        before = {"data/airo.db": "a", "data/airo.db-wal": "w",
                  "data/airo.db-shm": "s"}
        after = {"data/airo.db": "b"}
        self.assertEqual([], self.damage(before, after, rows=(100, 101)))

    def test_a_sidecar_appearing_is_not_damage_either(self):
        before = {"data/airo.db": "a"}
        after = {"data/airo.db": "b", "data/airo.db-wal": "w"}
        self.assertEqual([], self.damage(before, after, rows=(100, 101)))

    def test_the_database_itself_is_still_protected(self):
        """The exemption is for SQLite's sidecars, not for the database. A
        rule that quietly covered `airo.db` because its name is a prefix of
        `airo.db-wal` would exempt the only file that matters."""
        found = self.damage({"data/airo.db": "a"}, {}, rows=(100, None))
        self.assertTrue(found)
        self.assertIn("DELETED: data/airo.db", found[0])

    def test_the_exemption_does_not_reach_outside_the_data_directory(self):
        for name in ("backups/x-wal", "config.json-wal", "purpleair.key-shm"):
            self.assertFalse(chk._is_sqlite_sidecar(name), name)
        for name in ("data/airo.db-wal", "data/airo.db-shm"):
            self.assertTrue(chk._is_sqlite_sidecar(name), name)


class TestTheAllowlistKeepsUpWithWhatTheAgentWrites(unittest.TestCase):
    """Every file the poller writes under the data directory has to be known
    to the gate, or the gate fails a run for doing its job.

    That happened: the dark-source detector added `source_failures.json`, the
    maintainer's own agent rewrote it during a two-minute test run, and the
    gate reported that the suite had damaged their install. The gate was
    right — something had changed — and the allowlist had not kept up.

    A gate that cries wolf is a gate that gets switched off, and this one is
    the last line between a test run and somebody's data. So the list is
    checked against the code rather than maintained by memory.

    Enumerated from `poller.py`'s own `DATA / "..."` expressions, because a
    list typed by hand stops covering the moment somebody adds a file — the
    failure this exact contract is about.
    """

    def written_under_data(self):
        """Filenames poller.py composes against DATA, from its syntax tree."""
        tree = ast.parse((ROOT / "poller.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            # DATA / "name.json"  or  DATA / SOME_CONSTANT
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            left = node.left
            if not (isinstance(left, ast.Name) and left.id == "DATA"):
                continue
            right = node.right
            if isinstance(right, ast.Constant) and isinstance(right.value, str):
                names.add(right.value)
            elif isinstance(right, ast.Name):
                # A module constant holding the filename; resolve it.
                for n2 in ast.walk(tree):
                    if (isinstance(n2, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == right.id
                                    for t in n2.targets)
                            and isinstance(n2.value, ast.Constant)
                            and isinstance(n2.value.value, str)):
                        names.add(n2.value.value)
        return names

    def test_every_file_the_agent_writes_is_allowed_or_a_log(self):
        missing = []
        for name in sorted(self.written_under_data()):
            rel = f"data/{name}"
            if rel in chk.AGENT_WRITES or name.endswith(".log"):
                continue
            missing.append(rel)

        self.assertEqual(
            [], missing,
            "poller.py writes these under the data directory and the user-data "
            "gate does not know about them, so the maintainer's own agent will "
            "fail an unrelated test run:\n  " + "\n  ".join(missing))

    def test_the_walk_actually_finds_something(self):
        """Once every file is allowed, "found nothing" and "the walk is
        broken" are the same result."""
        found = self.written_under_data()

        self.assertIn("latest.json", found,
                      "the AST walk found no DATA-relative filenames, so this "
                      "contract is checking an empty set")

    def test_the_allowlist_names_no_file_that_is_gone(self):
        """An entry for a file nothing writes any more is an exemption that
        would quietly cover the next thing to take that name."""
        written = {f"data/{n}" for n in self.written_under_data()}
        # readings.csv is written by the legacy export path, not composed
        # against DATA, so it is expected to be absent from the walk.
        stale = [a for a in chk.AGENT_WRITES
                 if a not in written and not a.endswith("readings.csv")]

        self.assertEqual([], stale,
                         f"allowlist entries nothing writes: {stale}")
