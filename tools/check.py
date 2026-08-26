#!/usr/bin/env python3
"""Run locally what CI runs remotely, in one command.

Why this exists
---------------
The expensive failures in this project are not the ones the suite catches. They
are the ones found *after* pushing: a policy check nobody ran, a page whose
JavaScript no longer parses, a module that quietly acquired a dependency. Each
costs a CI round of four minutes plus the context of coming back to it, and
they arrive one at a time because CI stops at the first failure.

So this runs every gate CI runs and reports *all* of them, rather than the
first. One command, one answer, before the push.

    python3 tools/check.py                 # everything available here
    python3 tools/check.py --fast          # skip the Rust build
    python3 tools/check.py --no-coverage   # skip the coverage gate
    python3 tools/check.py --tz-sweep      # also run the date-sensitive
                                           # suites in four awkward timezones

What it cannot do
-----------------
It cannot run Windows or Linux. That is not a gap this script can close, and
pretending otherwise would be worse than saying it: every genuinely expensive
failure in this project's recent history was platform-specific and green on
macOS. So it ends by naming what only CI can answer, rather than printing a
tick that means less than it looks.

That closing list is maintained by hand and is the honest half of this script.
A claim of "every gate CI runs" is worth exactly as much as the list of the
ones it does not, and four gates were missing from both for a while: the
timezone sweep, the getElementById cross-check, `tray/ui/index.html`, and the
scan for a key pasted into source. Three of them were cheap and are now here.
The fourth -- running the *whole* suite in each of four zones -- costs four
times ninety seconds, so `--tz-sweep` runs the date-sensitive suites instead
and the closing list says which part of it only CI does.

Coverage
--------
Uses coverage.py when it is installed, and says so plainly when it is not
rather than silently skipping. This is a **development** dependency: the
shipped Python side still imports nothing outside the standard library, which
is what hard rule 1 actually guarantees and what
`test_contracts.py::TestTheZeroDependencyRuleHolds` enforces. See CONVENTIONS.

    pip install coverage
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOOR_FILE = ROOT / "tools" / "coverage-floor.json"

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


class Result:
    """One gate, and what it decided."""

    def __init__(self, name):
        self.name = name
        self.ok = None          # None = skipped
        self.detail = ""
        self.seconds = 0.0


def _have_coverage():
    try:
        import coverage  # noqa: F401
        return True
    except ImportError:
        return False


def run(cmd, cwd=None, timeout=900, env=None):
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True,
                          text=True, timeout=timeout, env=env)


# --------------------------------------------------------------- the gates

def snapshot_user_data():
    """Everything under the real `~/.airo`, by content, right now.

    Hashed rather than stat-compared. A rotation that deletes an archive and
    writes another of the same size in the same second is exactly the case a
    size-and-mtime check waves through, and it is the case that happened.

    Read-only, and it never opens the database through `store` — that would
    run migrations on the developer's own install to check whether anything
    ran on the developer's own install.
    """
    root = Path.home() / ".airo"
    if not root.exists():
        return {}
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink():
            # A symlink is not separate content. `~/.airo/latest.json` points
            # at `~/.airo/data/latest.json`, so the agent rewriting the target
            # showed up twice: once under a name the allowance covers and once
            # under a name it does not, and the gate reported the poller as
            # damage. The target is walked on its own account.
            continue
        try:
            # `as_posix()`, not `str()`. Windows renders a relative path with
            # backslashes, so the same file is a different key there and the
            # messages this gate prints read differently per platform — for a
            # check whose entire output is a list of filenames, that is the
            # difference between a report somebody can act on and one they
            # have to translate.
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
        except OSError as e:
            out[path.relative_to(root).as_posix()] = (
                f"unreadable: {type(e).__name__}")
    return out


#: Files the running poller legitimately rewrites while the suite runs. The
#: developer's own agent polls on a schedule and does not stop for a test run,
#: so these change for reasons that have nothing to do with the tests.
#:
#: Ignoring them outright would be wrong — a test corrupting the database is
#: the worst case there is. They are checked differently instead: the database
#: must not lose readings, and logs may only grow. Everything else, including
#: config.json and every backup archive, must be byte-identical.
AGENT_WRITES = ("data/airo.db", "data/latest.json", "data/readings.csv",
                "data/alert_state.json", "data/forecast_pending.json",
                "data/forecast_skill.json",
                # The dark-source detector's state, rewritten every poll.
                # Missed when that landed, so the gate failed a whole run on a
                # file the agent is supposed to touch. The gate was right; the
                # list had not kept up. A contract now enumerates these from
                # poller.py so the next one cannot be forgotten.
                "data/source_failures.json",
                # A writability probe: created and removed inside one call, so
                # it is normally invisible. Listed because "normally" is doing
                # a lot of work there -- a snapshot taken in the wrong
                # millisecond would fail the run on a file that no longer
                # exists by the time anybody looked.
                "data/.airo-write-test")

#: SQLite's own files. It creates and removes these as it checkpoints, and
#: nothing outside SQLite may reason about their lifetime — a lesson this
#: project learned once already, when a migration test asserted a `-wal` still
#: existed and passed on macOS while failing on Linux.
#:
#: They are excluded entirely rather than treated as agent writes, because the
#: rule for those is "never deleted" and SQLite deletes these routinely. That
#: made the gate fail about one run in three: a checkpoint landing during the
#: suite read as somebody removing a file.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _is_sqlite_sidecar(name):
    return name.startswith("data/") and name.endswith(SQLITE_SIDECARS)


def _is_agent_write(name):
    return name in AGENT_WRITES or (
        name.startswith("data/") and name.endswith(".log"))


def readings_count():
    """Rows in the real database, or None. Never through `store`.

    `store.connect()` runs migrations, and opening the developer's install to
    check whether anything touched the developer's install would be its own
    joke. A plain connection only reads.
    """
    import sqlite3
    db = Path.home() / ".airo" / "data" / "airo.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


def user_data_damage(before, after, rows_before, rows_after, sizes_before,
                     sizes_after):
    """What changed that the running agent cannot explain. Empty if nothing.

    The distinction this draws is the whole reason the gate is usable. A poll
    landing mid-run rewrites the database and appends to a log, every time,
    and a check that called that a failure would be switched off within a day
    — which is worse than not having one.
    """
    damage = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        if _is_sqlite_sidecar(name):
            continue
        if not _is_agent_write(name):
            damage.append(f"{'DELETED' if name not in after else 'changed'}: "
                          f"{name}")
            continue
        if name not in after:
            damage.append(f"DELETED: {name}")          # the agent never removes
        elif name.endswith(".log") and \
                sizes_after.get(name, 0) < sizes_before.get(name, 0):
            damage.append(f"log truncated: {name}")    # the agent only appends
    if (rows_before is not None and rows_after is not None
            and rows_after < rows_before):
        damage.append(f"READINGS LOST: {rows_before:,} -> {rows_after:,}")
    return damage


def user_data_sizes():
    """Byte sizes under the real `~/.airo`, for the append-only log check."""
    root = Path.home() / ".airo"
    if not root.exists():
        return {}
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            try:
                out[path.relative_to(root).as_posix()] = path.stat().st_size
            except OSError:
                # A file that vanished between the walk and the stat, which is
                # ordinary while the agent is rotating a log. Its absence is
                # already caught by the hash snapshot; recording no size here
                # only means the append-only check skips it, and a missing
                # size must never be read as "it shrank to zero".
                continue
    return out


def describe_user_data_change(before, after):
    """What moved, in the words somebody needs to fix it."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    parts = []
    if removed:
        parts.append(f"DELETED {len(removed)}: {', '.join(removed[:4])}")
    if changed:
        parts.append(f"modified {len(changed)}: {', '.join(changed[:4])}")
    if added:
        parts.append(f"created {len(added)}: {', '.join(added[:4])}")
    return " | ".join(parts)


def gate_tests(res, under_coverage=False):
    """The suite. Run under coverage when the coverage gate is also wanted, so
    the slowest thing here happens once rather than twice — this ran the suite
    twice at first and took over two minutes, which is exactly the friction it
    exists to remove."""
    if under_coverage:
        run([sys.executable, "-m", "coverage", "erase"])
        cmd = [sys.executable, "-m", "coverage", "run",
               "--source=.", "--omit=tests/*,tools/*,setup.py",
               "-m", "unittest", "discover", "-s", "tests"]
    else:
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    # Rule: never mutate the developer's own ~/.airo from a test. Checked
    # here rather than trusted, because it has now failed four times and the
    # last one deleted three of the maintainer's backup archives — a module
    # constant resolved `Path.home()` at *import*, before `homeguard` could
    # redirect anything.
    #
    # The guards in tests/ cover the shapes somebody has already thought of.
    # This covers the rest, by asking the only question that actually matters:
    # is their install byte-for-byte what it was before the suite ran.
    before = snapshot_user_data()
    rows_before, sizes_before = readings_count(), user_data_sizes()

    r = run(cmd)

    after = snapshot_user_data()
    rows_after, sizes_after = readings_count(), user_data_sizes()
    damage = user_data_damage(before, after, rows_before, rows_after,
                              sizes_before, sizes_after)
    if damage:
        res.ok = False
        res.detail = ("THE SUITE DAMAGED YOUR REAL ~/.airo — "
                      + " | ".join(damage[:6]))
        res.user_data_touched = True
        return

    res.ok = r.returncode == 0
    tail = (r.stderr or r.stdout).strip().splitlines()
    res.detail = tail[-1] if tail else ""
    if not res.ok:
        # The failing assertions, not the whole run.
        fails = [l for l in (r.stderr or "").splitlines()
                 if l.startswith(("FAIL:", "ERROR:", "AssertionError"))]
        res.detail = " | ".join(fails[:3]) or res.detail


def gate_compile(res):
    files = [str(p) for p in sorted(ROOT.glob("*.py"))]
    files += [str(p) for p in sorted((ROOT / "tools").glob("*.py"))]
    r = run([sys.executable, "-m", "py_compile", *files])
    res.ok = r.returncode == 0
    res.detail = f"{len(files)} modules" if res.ok else r.stderr.strip()[:200]


def gate_json(res):
    bad = []
    for rel in ("config.example.json", "tray/tauri.conf.json"):
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append(f"{rel}: {e}")
    res.ok = not bad
    res.detail = "; ".join(bad) or "config.example.json, tauri.conf.json"


def pages():
    """Every page with inline script, wherever it lives.

    A root glob missed `tray/ui/index.html` entirely — the one page CI checks
    that is not at the root, and the one whose JavaScript nobody runs locally
    because it renders inside a Tauri window rather than a browser tab. The
    root glob is kept as a glob, so a new page at the root is in scope without
    an edit here; the tray's page is named because it is somewhere else.
    """
    found = sorted(ROOT.glob("*.html"))
    tray = ROOT / "tray" / "ui" / "index.html"
    if tray.exists():
        found.append(tray)
    return found


def gate_page_scripts(res):
    """The pages' inline JavaScript: it parses, and it addresses elements that
    exist.

    A page that no longer parses is a blank dashboard, and nothing in the
    Python suite would notice. Neither would it notice the quieter one:
    `getElementById("bandLabel")` against markup where that id has been
    renamed returns null, and the panel it fills stays empty on a page that
    parses perfectly and renders everything else — the failure this project
    files under "a page that parsed, passed, and rendered the wrong thing".
    CI cross-checks the two. This did not, which is the kind of gap that makes
    "run check.py before pushing" cost a CI round anyway.
    """
    node = shutil.which("node")
    if not node:
        res.detail = "node not installed"
        return
    pattern = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
    bad = []
    checked = 0
    references = 0
    for page in pages():
        html = page.read_text(encoding="utf-8")
        blocks = pattern.findall(html)
        if not blocks:
            continue
        script = blocks[-1]
        # Ids from the whole page, references from the script — the same
        # asymmetry CI uses, and the right one: an id may be declared in
        # markup the script never touches, but a reference to an id that is
        # not in the markup is a panel that stays empty.
        ids = set(re.findall(r"id=['\"]([A-Za-z0-9_]+)['\"]", html))
        used = set(re.findall(
            r"getElementById\(['\"]([A-Za-z0-9_]+)['\"]\)", script))
        references += len(used)
        missing = sorted(used - ids)
        if missing:
            bad.append(f"{page.name}: getElementById targets missing from the "
                       f"markup: {missing}")
        tmp = ROOT / f".check-{page.stem}.js"
        try:
            tmp.write_text(script, encoding="utf-8")
            r = run([node, "--check", str(tmp)])
            checked += 1
            if r.returncode != 0:
                bad.append(f"{page.name}: {r.stderr.strip().splitlines()[0]}")
        finally:
            tmp.unlink(missing_ok=True)
    res.ok = not bad
    # The reference count is reported rather than kept quiet: two of the three
    # pages address no element by id at all, so their half of this gate passes
    # by having nothing to check. A number somebody can see is the difference
    # between knowing that and assuming otherwise.
    res.detail = "; ".join(bad) or (f"{checked} page(s), {references} element "
                                    f"reference(s)")


def gate_rust(res, fast=False):
    if not shutil.which("cargo"):
        res.detail = "cargo not installed"
        return
    if fast:
        res.detail = "skipped (--fast)"
        return
    # The bundler resolves bundle.resources even for a check, so the payload
    # has to exist. Staging without a runtime is supported and quick.
    run([sys.executable, "tools/stage_bundle.py"])
    r = run(["cargo", "test"], cwd=ROOT / "tray")
    res.ok = r.returncode == 0
    m = re.search(r"test result: \w+\. (\d+) passed", r.stdout or "")
    res.detail = f"{m.group(1)} tests" if m and res.ok else \
        (r.stdout or r.stderr).strip().splitlines()[-1][:160]


def gate_policy(res):
    """The checks that exist because something got committed once."""
    problems = []

    if list(ROOT.glob("*.sh")):
        problems.append("a control script is back at the repo root")

    for name in ("requirements.txt", "Pipfile", "pyproject.toml",
                 "poetry.lock", "environment.yml"):
        if (ROOT / name).exists():
            problems.append(f"{name} exists — the shipped side takes no "
                            f"runtime dependencies")

    tracked = run(["git", "ls-files"]).stdout.split()
    for f in tracked:
        low = f.lower()
        if low.endswith((".db", ".key", ".sqlite")) or low.startswith("data/"):
            problems.append(f"{f} is tracked and should not be")
        if Path(f).name in ("config.json", "INTERNAL.md"):
            problems.append(f"{f} is tracked and should not be")

    res.ok = not problems
    res.detail = "; ".join(problems) or "no scripts, no manifests, no user data"


#: The zones CI runs the suite in, and why each one is there. Los Angeles
#: observes daylight saving; Kolkata is half an hour off the hour, Chatham
#: three quarters *and* on DST at once, St John's the same in the other
#: hemisphere. Between them they cover the cases where a UTC-hour bucket
#: straddles two local hours — which is what named the wrong evening hour for
#: half the year, in a tool whose entire finding is about which hour is worst.
#:
#: Kept in step with `.github/workflows/ci.yml` by hand, and the sweep says so
#: when it runs: a local sweep of three zones against CI's four is a pass that
#: means less than it looks.
AWKWARD_ZONES = ("America/Los_Angeles", "Asia/Kolkata", "Pacific/Chatham",
                 "America/St_Johns")

#: What makes a suite's result depend on which zone it runs in. Enumerated by
#: marker rather than listed: a hand-written list of modules in this project
#: went three stale once already and read as complete the whole time.
CLOCK_MARKERS = ("astimezone", "localtime", "zoneinfo", "fromtimestamp",
                 "utcoffset", "tzset", "localDateKey", "local_date")


def date_sensitive_suites():
    """The suites whose answers move with the local zone."""
    found = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in CLOCK_MARKERS):
            found.append(f"tests.{path.stem}")
    return found


def gate_tz_sweep(res):
    """The date-sensitive suites, in each of the four zones CI uses.

    Opt-in, because the honest version of this gate is what CI does — the
    *whole* suite, four times — and that is four times ninety seconds. What
    runs here is the part that pays for itself: the suites that name a local
    clock at all. A module that becomes zone-dependent without mentioning one
    of the markers is outside this and inside CI's, and the closing list says
    so rather than leaving the difference implied.

    Skipped on Windows, which does not honour TZ. Setting it there would test
    nothing while looking exactly like it had — the shape of failure this
    project calls a platform fallback that silently does nothing.
    """
    if os.name == "nt":
        res.detail = "Windows does not honour TZ; CI runs this on Linux"
        return
    suites = date_sensitive_suites()
    if len(suites) < 3:
        # A vacuum guard. If the markers stop matching, this gate would run
        # nothing in four zones and report a tick for it, which is worse than
        # not having it.
        res.ok = False
        res.detail = (f"only {len(suites)} date-sensitive suite(s) found — the "
                      f"markers have stopped matching, so this gate is about "
                      f"to pass by running almost nothing")
        return
    failed = []
    for zone in AWKWARD_ZONES:
        r = run([sys.executable, "-m", "unittest", *suites],
                env={**os.environ, "TZ": zone})
        if r.returncode != 0:
            first = [l for l in (r.stderr or "").splitlines()
                     if l.startswith(("FAIL:", "ERROR:"))]
            failed.append(f"{zone}: {first[0] if first else 'failed'}")
    res.ok = not failed
    res.detail = "; ".join(failed) or (
        f"{len(suites)} suite(s) x {len(AWKWARD_ZONES)} zone(s); CI runs the "
        f"whole suite in each")


#: A key pasted into source, matched by shape. PurpleAir and OpenAQ both issue
#: UUIDs, so the shape is the same one CI looks for — uppercase, because that
#: is how they are issued and because a case-insensitive version matches every
#: lowercase hex digest in the tree. Kept character-for-character identical to
#: the CI expression: two copies of one rule that differ by a hair fail on
#: opposite sides and cost a round each.
KEY_SHAPE = re.compile(
    r"[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}")

#: What CI names as a credentials file, by shape rather than by one path.
SECRET_FILES = re.compile(r"(^|/)(apikey|\.env|secrets\.json)$|\.key$")


def gate_secrets(res):
    """No credentials file tracked, and no key pasted into source.

    Hard rule 2: never log, print or commit an API key. The policy gate below
    catches a tracked `.key`, and stopped there — a key pasted into a fixture,
    a comment or a JSON example is the same leak by an easier route, and it is
    the one CI scans for and this did not.

    Read from `git ls-files` rather than by walking the tree. A local `.venv`,
    a scratch file or somebody's downloads folder inside the checkout are not
    committed and not this gate's business; failing on one would be a local
    red that CI cannot reproduce, which is how a gate gets ignored.
    """
    problems = []
    tracked = run(["git", "ls-files"]).stdout.split()

    for name in tracked:
        if SECRET_FILES.search(name):
            problems.append(f"{name} is a credentials file and is tracked")

    scanned = 0
    for name in tracked:
        if not name.endswith((".py", ".sh", ".json", ".rs")):
            continue
        path = ROOT / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            found = KEY_SHAPE.search(line)
            if found:
                # The match itself is not printed. A gate that echoes the key
                # it found has published it to a terminal, a scrollback buffer
                # and whatever CI keeps — which is the thing rule 2 forbids.
                problems.append(f"{name}:{n} looks like an API key")

    res.ok = not problems
    res.detail = "; ".join(problems[:4]) or (
        f"no credentials tracked, no key shape in {scanned} source file(s)")


def gate_faults(res):
    """Every committed fault still turns a test red.

    A guard that has stopped guarding looks exactly like one that works. These
    are the runs that tell them apart, and they are here rather than in a
    notebook because a check done once proves something once.

    Off by default: each fault runs the target suite, so a full pass costs
    minutes. `--faults` locally, and CI runs it on every push, which is where
    the cost belongs.
    """
    specs = sorted((ROOT / "tools" / "faults").glob("*.json"))
    if not specs:
        res.detail = "no fault specs committed"
        return
    missed, total = [], 0
    for spec in specs:
        total += len(json.loads(spec.read_text()))
        r = run([sys.executable, "tools/faultcheck.py", str(spec)],
                timeout=3600)
        if r.returncode != 0:
            first = [l.strip() for l in (r.stdout or "").splitlines()
                     if l.strip().startswith("- ")]
            missed.append(f"{spec.name}: {first[0] if first else 'failed'}")
    res.ok = not missed
    res.detail = ("; ".join(missed) if missed
                  else f"{total} fault(s) across {len(specs)} spec(s), all caught")


def gate_coverage(res, floors):
    """Report against the floors. The data comes from the tests gate, which
    already ran the suite under coverage — see gate_tests."""
    try:
        import coverage  # noqa: F401
    except ImportError:
        res.detail = "coverage.py not installed — pip install coverage"
        return

    out = run([sys.executable, "-m", "coverage", "json", "-o", "-"]).stdout
    run([sys.executable, "-m", "coverage", "erase"])
    try:
        data = json.loads(out)
    except Exception:
        res.ok = False
        res.detail = "could not read the coverage report"
        return

    total = data["totals"]["percent_covered"]
    below = []
    for name, floor in sorted(floors.get("modules", {}).items()):
        got = data["files"].get(name, {}).get("summary", {}).get(
            "percent_covered")
        if got is None:
            continue
        if got + 0.05 < floor:
            below.append(f"{name} {got:.1f}% < {floor}%")

    floor_total = floors.get("total", 0)
    if total + 0.05 < floor_total:
        below.append(f"TOTAL {total:.1f}% < {floor_total}%")

    res.ok = not below
    where = floors.get("measured_on", {})
    note = ""
    if where.get("platform") and not sys.platform.startswith("linux"):
        # Said, not hidden. The floor is measured where it is enforced, and a
        # local run on another platform legitimately reads a little
        # differently — scheduler.py exercises a different branch here. A
        # local pass is therefore necessary, not sufficient.
        note = f"  [floor measured on {where['platform']}; this is {sys.platform}]"
    res.detail = (f"{total:.1f}% overall (floor {floor_total}%){note}"
                  if res.ok else "; ".join(below) + note)
    res.total = total
    res.per_module = {n: v["summary"]["percent_covered"]
                      for n, v in data["files"].items()}


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fast", action="store_true",
                    help="skip the Rust build, which dominates the runtime")
    ap.add_argument("--no-coverage", action="store_true",
                    help="skip the coverage gate")
    ap.add_argument("--tz-sweep", action="store_true",
                    help="also run the date-sensitive suites in the four "
                         "awkward timezones CI uses. CI runs the whole suite "
                         "in each; this runs the part that names a clock")
    ap.add_argument("--faults", action="store_true",
                    help="also re-run every committed fault, which re-proves "
                         "that each guard still fails when broken. Minutes, "
                         "not seconds — CI runs it on every push")
    ap.add_argument("--raise-floor", action="store_true",
                    help="rewrite the floors to today's measurements. Run this "
                         "deliberately, in its own commit, so a ratchet is "
                         "visible in review")
    args = ap.parse_args()

    floors = json.loads(FLOOR_FILE.read_text()) if FLOOR_FILE.exists() else {}

    want_coverage = not args.no_coverage and _have_coverage()
    gates = [
        ("tests", lambda r: gate_tests(r, under_coverage=want_coverage)),
        ("compile", gate_compile),
        ("json", gate_json),
        ("page scripts", gate_page_scripts),
        ("policy", gate_policy),
        ("secrets", gate_secrets),
        ("rust", lambda r: gate_rust(r, args.fast)),
    ]
    if not args.no_coverage:
        gates.append(("coverage", lambda r: gate_coverage(r, floors)))
    if args.tz_sweep:
        gates.append(("timezones", gate_tz_sweep))
    if args.faults:
        gates.append(("faults", gate_faults))

    print(f"\n  Running {len(gates)} gates. Everything is reported, not just "
          f"the first failure.\n")

    results = []
    for name, fn in gates:
        res = Result(name)
        started = time.time()
        try:
            fn(res)
        except Exception as e:                      # a gate must not crash the run
            res.ok = False
            res.detail = f"{type(e).__name__}: {e}"
        res.seconds = time.time() - started
        results.append(res)

        mark = (f"{GREEN}ok  {RESET}" if res.ok
                else f"{RED}FAIL{RESET}" if res.ok is False
                else f"{YELLOW}skip{RESET}")
        print(f"  {mark} {res.name:14} {res.seconds:5.1f}s  {DIM}{res.detail}{RESET}")

    if args.raise_floor:
        cov = next((r for r in results if r.name == "coverage"), None)
        if cov is not None and getattr(cov, "total", None) is not None:
            new = {"total": round(cov.total, 1),
                   "modules": {k: round(v, 1) for k, v in cov.per_module.items()}}
            FLOOR_FILE.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")
            print(f"\n  floors rewritten to today's measurements in "
                  f"{FLOOR_FILE.relative_to(ROOT)}")

    failed = [r for r in results if r.ok is False]
    skipped = [r for r in results if r.ok is None]

    print()
    if skipped:
        print(f"  {YELLOW}Not checked here:{RESET} "
              f"{', '.join(r.name for r in skipped)}")
    # Said every time, pass or fail. Every expensive failure in this project's
    # recent history was platform-specific and green on the machine it was
    # written on, so a local pass has to state its own limits — and the list
    # is specific, because "some things only CI does" is not something anybody
    # can act on. Four gates were missing from this script while its own
    # docstring claimed every gate CI runs; the three cheap ones are now here,
    # and what remains is named.
    print(f"  {DIM}Only CI can answer:{RESET}")
    print(f"  {DIM}  - Windows and Linux, on Python 3.9 and 3.12. Every "
          f"expensive failure here was platform-specific{RESET}")
    print(f"  {DIM}    and green on the machine it was written on.{RESET}")
    if not args.tz_sweep:
        print(f"  {DIM}  - the four awkward timezones. `--tz-sweep` runs the "
              f"date-sensitive suites in them;{RESET}")
        print(f"  {DIM}    CI runs the whole suite in each.{RESET}")
    else:
        print(f"  {DIM}  - the *whole* suite in each of the four timezones. "
              f"The sweep above ran only the{RESET}")
        print(f"  {DIM}    suites that name a local clock.{RESET}")
    if not args.faults:
        print(f"  {DIM}  - every committed fault still turning a test red. "
              f"`--faults` locally; CI on every push.{RESET}")
    print(f"  {DIM}  - the tray building and its tests passing on all three "
          f"platforms.{RESET}")
    print(f"  {DIM}  - a matching Signed-off-by on every commit in the pull "
          f"request (CLA.md).{RESET}")

    if failed:
        print(f"\n  {RED}{len(failed)} gate(s) failed:{RESET} "
              f"{', '.join(r.name for r in failed)}\n")
        return 1
    print(f"\n  {GREEN}All local gates pass.{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
