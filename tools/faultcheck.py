#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Break something on purpose and check a test notices.

    python3 tools/faultcheck.py faults.json

A test that has never failed is a claim nobody has checked. This project has
found a false test in almost every change made to it by reintroducing the fault
the test was written for -- usually a test asserting less than its name claims.

Doing that by hand is what this replaces, because doing it by hand went wrong
three ways in one week and each way produced a *confident wrong answer*:

  * **The baseline was already broken.** Seven faults were injected against a
    suite with two pre-existing errors. Every fault reported the same two test
    names, all seven read as RED, and none of them proved anything. A run here
    refuses to start unless the baseline is green.

  * **The restore did not restore.** Python invalidates a `.pyc` by modified
    time and size. Writing a fault, running it, and writing the original back a
    moment later leaves a file that is correct on disk and *stale in the
    cache* -- same size, same second -- so the next run silently executes the
    faulted bytecode. A migration was debugged for twenty minutes on that
    basis: `inspect.getsource` showed one thing and SQLite was handed another.
    Bytecode writing is disabled for every run here and caches are cleared
    after each restore.

  * **The fault was injected into the test rather than the code.** Deleting an
    assertion cannot fail; five faults came back green and read as evidence.
    Nothing here can prevent that, but the report names the file each fault
    touched, so a run that only edits `tests/` is visible as what it is.

  * **The fault did not change anything, or never ran.** `[][:0] + [...]` and
    `x if False else y` read as faults and behave identically; a fault in a
    branch the target suite never reaches cannot be caught by definition. Both
    came back green as though the property were guarded.

    Two of the three cases are now detected rather than described. A fault
    that leaves the module's syntax tree identical is refused up front -- that
    is every comment-only or whitespace edit. A fault on a line the suite never
    executes is reported as UNRUN, separately from GREEN, because "nothing
    caught it" and "nothing ran it" call for opposite responses: the first is a
    missing test, the second is a fault aimed at the wrong line.

    The third case, an edit that runs and is genuinely equivalent, cannot be
    detected from here. A GREEN is still a question rather than an answer.

A fault file is JSON:

    [
      {"name": "the headline stops excluding indoor",
       "file": "poller.py",
       "find": "fusion.fuse(outdoor, rule",
       "replace": "fusion.fuse(readings, rule",
       "target": "tests.test_indoor"}
    ]

`target` is optional and defaults to the whole suite. `find` must appear
exactly once: a fault that matches twice is not the fault anybody described.

Exit status is 0 only if every fault was caught.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def clear_bytecode():
    """Remove every `__pycache__` this project owns.

    The reason this exists rather than being left to Python: a restored file
    of the same size, written in the same second as the faulted one, does not
    invalidate its cached bytecode. The file is right and the interpreter runs
    the wrong thing, which is indistinguishable from the code being wrong.
    """
    removed = 0
    for cache in ROOT.rglob("__pycache__"):
        if "tray/target" in str(cache):
            continue                      # Rust build output, not ours
        shutil.rmtree(cache, ignore_errors=True)
        removed += 1
    return removed


def run_tests(target=None):
    """Run the suite, or one target. Returns (passed, failing test names)."""
    cmd = [sys.executable, "-m", "unittest"]
    cmd += [target] if target else ["discover", "-s", "tests"]
    # Belt and braces with clear_bytecode(): nothing written means nothing to
    # go stale, even between two runs inside the same second.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          env=env, timeout=3600)
    failing = sorted({
        line.split("(")[0].replace("FAIL: ", "").replace("ERROR: ", "").strip()
        for line in (proc.stderr or "").splitlines()
        if line.startswith(("FAIL: ", "ERROR: "))})
    if not failing and proc.returncode != 0:
        # A suite that did not run at all reports no FAIL lines and a non-zero
        # exit -- a syntax error from a clumsy injection looks exactly like
        # this, and reading it as "nothing caught it" is how a fault that broke
        # the build was recorded as an untested gap.
        failing = ["<the suite did not run>"]
    return proc.returncode == 0, failing


def changed_lines(original, faulted):
    """Line numbers that differ, 1-based. Where the fault actually landed."""
    import difflib
    before, after = original.splitlines(), faulted.splitlines()
    moved = []
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            moved.extend(range(j1 + 1, j2 + 1))
        elif tag == "delete":
            moved.append(j1 + 1)
    return sorted(set(moved))


def statement_starts(source, lines):
    """Map each line to the statement that contains it.

    Coverage records the *first* line of a multi-line statement, so a fault
    that only alters a continuation line — a string split across three lines,
    an argument on its own row — looks unexecuted however often it runs. That
    is a false UNRUN, and a false UNRUN is as misleading as the false GREEN
    this check exists to remove.

    Falls back to the line itself when the file will not parse, which is the
    case for a fault that breaks the syntax on purpose.
    """
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(lines)

    # Statements only. Any node would do for finding *a* span, and the
    # smallest containing node is usually an expression -- for a string split
    # across three lines that is the string itself, whose first line is still
    # a continuation. Coverage attributes execution to the statement.
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is not None and end is not None:
            spans.append((start, end))

    out = set()
    for line in lines:
        covering = [(s, e) for s, e in spans if s <= line <= e]
        # The smallest span that contains it, then its first line: the
        # innermost statement is what coverage attributes the execution to.
        out.add(min(covering, key=lambda se: se[1] - se[0])[0]
                if covering else line)
    return out


def is_a_no_op(path, original, faulted):
    """True when the edit cannot change behaviour, whatever it looks like.

    Compares syntax trees, so a comment, a docstring or whitespace-only change
    is caught. It does not catch an edit that is semantically equivalent but
    structurally different -- `[][:0] + [...]` parses differently and behaves
    identically -- and nothing here claims to.
    """
    if path.suffix != ".py":
        return False
    import ast
    try:
        return (ast.dump(ast.parse(original), include_attributes=False)
                == ast.dump(ast.parse(faulted), include_attributes=False))
    except SyntaxError:
        return False        # a fault that will not parse is a real change


def lines_the_suite_runs(path, target):
    """Which lines of `path` the target suite actually executes.

    A fault on a line nothing runs cannot be caught, and reporting that as
    "nothing caught it" sends somebody looking for a missing test when the
    fault was simply aimed at the wrong place. Returns None when coverage is
    not installed, which downgrades this check rather than failing the run --
    coverage is a development dependency and hard rule 1 keeps it out of what
    ships.
    """
    try:
        import coverage  # noqa: F401
    except ImportError:
        return None

    try:
        # `resolve()` on both sides. On macOS a temp directory is handed out as
        # /var/... and resolves to /private/var/..., so comparing one resolved
        # path with one that is not raises rather than matching — the same
        # symlink that made the disk-image mount matcher find nothing.
        included = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None
    cmd = [sys.executable, "-m", "coverage", "run",
           f"--include={included}", "-m", "unittest"]
    cmd += [target] if target else ["discover", "-s", "tests"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-m", "coverage", "erase"],
                   cwd=ROOT, capture_output=True, env=env)
    subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env,
                   timeout=3600)
    out = subprocess.run([sys.executable, "-m", "coverage", "json", "-o", "-"],
                         cwd=ROOT, capture_output=True, text=True, env=env)
    subprocess.run([sys.executable, "-m", "coverage", "erase"],
                   cwd=ROOT, capture_output=True, env=env)
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    for name, info in (data.get("files") or {}).items():
        if Path(name).name == path.name:
            return set(info.get("executed_lines") or [])
    return set()


def apply_fault(fault):
    """Write the fault. Returns the original text for restoring."""
    path = ROOT / fault["file"]
    original = path.read_text(encoding="utf-8")
    found = original.count(fault["find"])
    if found != 1:
        raise ValueError(
            f"{fault['file']}: the text to replace appears {found} times, "
            f"not once — this is not the fault the description claims")
    path.write_text(original.replace(fault["find"], fault["replace"], 1),
                    encoding="utf-8")
    clear_bytecode()
    return original


def restore(fault, original):
    path = ROOT / fault["file"]
    path.write_text(original, encoding="utf-8")
    clear_bytecode()
    if path.read_text(encoding="utf-8") != original:
        raise RuntimeError(f"{fault['file']} could not be restored")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("faults", help="a JSON file describing the faults")
    ap.add_argument("--target", help="default unittest target for all faults")
    args = ap.parse_args()

    faults = json.loads(Path(args.faults).read_text(encoding="utf-8"))
    targets = {f.get("target") or args.target for f in faults}

    print(f"\n  Confirming the baseline is green before injecting anything.\n")
    clear_bytecode()
    for target in sorted(targets, key=lambda t: (t is None, t)):
        ok, failing = run_tests(target)
        label = target or "the whole suite"
        if not ok:
            print(f"  {RED}BASELINE FAILING{RESET} {label}: "
                  f"{', '.join(failing[:4])}")
            print(f"\n  {RED}Refusing to inject.{RESET} Every fault would "
                  f"report these names and none of it would mean anything.\n")
            return 2
        print(f"  {GREEN}ok  {RESET} baseline green: {label}")

    # Every fault checked for ambiguity before any of them is applied. Doing
    # it lazily meant a bad spec raised a traceback half way through, after
    # some faults had run and while the report was incomplete — which is a
    # worse answer than either "all caught" or "here is what was missed".
    problems = []
    for fault in faults:
        path = ROOT / fault["file"]
        if not path.exists():
            problems.append(f"{fault['name']}: {fault['file']} does not exist")
            continue
        original = path.read_text(encoding="utf-8")
        found = original.count(fault["find"])
        if found != 1:
            problems.append(
                f"{fault['name']}: the text to replace appears {found} times "
                f"in {fault['file']}, not once")
            continue
        if is_a_no_op(path, original,
                      original.replace(fault["find"], fault["replace"], 1)):
            problems.append(
                f"{fault['name']}: the edit leaves {fault['file']}'s syntax "
                f"tree identical, so it cannot change behaviour — a green "
                f"from it would mean nothing")
    if problems:
        print(f"  {RED}These faults are not what they claim:{RESET}")
        for problem in problems:
            print(f"    - {problem}")
        print(f"\n  {DIM}A `find` matching twice replaces whichever comes "
              f"first, which is not the fault anybody described.{RESET}\n")
        return 2

    print()
    results = []
    for fault in faults:
        target = fault.get("target") or args.target
        original = apply_fault(fault)
        # Read while the fault is still in place. Reading it after the restore
        # was the first version, and it compared the file with itself: the
        # diff was always empty, the coverage check was skipped every time,
        # and a fault on a line nothing runs reported GREEN — which is the
        # exact failure this check exists to catch.
        faulted = (ROOT / fault["file"]).read_text(encoding="utf-8")
        ran = None
        try:
            _, failing = run_tests(target)
            if not failing:
                # Measured while the fault is still applied. Doing it after
                # the restore compared line numbers from the faulted file
                # against coverage of the original — so any fault that
                # *inserts* a line reported that line as never executed,
                # whatever the suite actually ran. CI caught that; the local
                # run could not, because coverage is installed under the real
                # home and the suite redirects it.
                ran = lines_the_suite_runs(ROOT / fault["file"], target)
        finally:
            restore(fault, original)

        caught = bool(failing)
        status = "red" if caught else "green"
        detail = ", ".join(failing[:2]) if caught else "NOTHING CAUGHT IT"

        if not caught and ran is not None:
            # Distinguish "no test covers this" from "no test *runs* this".
            # They read the same in a report and call for opposite responses:
            # the first is a missing test, the second is a fault pointed at
            # the wrong line, and treating the second as the first sends
            # somebody hunting for a gap that is not there.
            touched = statement_starts(faulted,
                                       changed_lines(original, faulted))
            if touched and not (touched & ran):
                status = "unrun"
                detail = (f"the suite never executes line(s) "
                          f"{sorted(touched)[:4]} — this fault could not "
                          f"be caught by anything")

        results.append((fault, status, detail))
        mark = {"red": f"{RED}RED  {RESET}",
                "green": f"{YELLOW}GREEN{RESET}",
                "unrun": f"{YELLOW}UNRUN{RESET}"}[status]
        print(f"  {mark} {fault['name']}")
        print(f"       {DIM}{fault['file']} -> {detail}{RESET}")

    unrun = [f for f, status, _ in results if status == "unrun"]
    missed = [f for f, status, _ in results if status == "green"]
    print()
    if unrun:
        print(f"  {YELLOW}{len(unrun)} fault(s) were never executed:{RESET}")
        for fault in unrun:
            print(f"    - {fault['name']} ({fault['file']})")
        print(f"  {DIM}Aimed at a line the suite does not reach. Not a "
              f"missing test — a misplaced fault.{RESET}")
    if missed:
        print(f"  {YELLOW}{len(missed)} fault(s) went unnoticed:{RESET}")
        for fault in missed:
            print(f"    - {fault['name']} ({fault['file']})")
        print(f"  {DIM}The line ran and nothing failed. Either a missing "
              f"test, or a layer below caught it,{RESET}")
        print(f"  {DIM}or the edit is equivalent to what it replaced. "
              f"Establish which rather than assuming.{RESET}")
    if unrun or missed:
        print()
        return 1

    print(f"  {GREEN}Every fault was caught.{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
