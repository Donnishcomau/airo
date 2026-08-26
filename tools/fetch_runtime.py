#!/usr/bin/env python3
"""Fetch the Python runtime that ships inside the installed app.

Why this exists
---------------
Airo's Python side has no dependencies and runs on a stock interpreter. That
is fine for a developer and not fine for the person this installer is for:
"python3: command not found", or a version too old for the syntax, is where
someone with no technical knowledge stops for good, and no README wording
recovers them. So the app carries its own interpreter.

That is a deliberate reversal of "explicitly not doing: bundling a Python
runtime" -- see ROADMAP §3f for the reasoning and what it costs.

What it does NOT change
-----------------------
Hard rule 1 still holds: the Python side imports only the standard library,
and this script does too. Shipping an interpreter is not permission to depend
on packages, and CI still fails if a dependency manifest appears.

The supply chain, stated plainly
--------------------------------
This downloads a binary someone else built. That is a real cost and the
mitigations are the ones any downloaded binary deserves:

  * the version is pinned here, not resolved at build time -- "latest" means
    the build is not reproducible and a bad upstream release ships silently
  * the SHA-256 is recorded here and checked before anything is extracted
  * a mismatch is fatal and loud; it never falls back to an unverified copy
  * the source, licence and refresh procedure are in ARCHITECTURE

Refreshing it is a deliberate act: change RELEASE and VERSION, run this with
--print-checksums to get the new digests from upstream's SHA256SUMS, paste
them in, and commit the change on its own so the diff shows what moved.

Usage
-----
    python3 tools/fetch_runtime.py            # fetch for this machine
    python3 tools/fetch_runtime.py --all      # every supported architecture
    python3 tools/fetch_runtime.py --print-checksums
"""

import argparse
import fnmatch
import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

# Pinned. Upstream is astral-sh/python-build-standalone: relocatable CPython
# builds, PSF-licensed like CPython itself. 3.12 because CI already tests
# against it, so the interpreter we ship is one the suite has run on.
RELEASE = "20260728"
VERSION = "3.12.13"

# sha256 of each install_only tarball, taken from the release's SHA256SUMS.
# Verified before extraction. If upstream ever republishes a tag in place,
# this is what notices.
# One triple per platform an installer is built for. Each is a separate 69 MB
# download and a separate thing that can break, so this list is deliberately
# short rather than exhaustive.
#
# macOS is **Apple Silicon only**, deliberately: every Mac Apple has sold since
# 2020 is arm64, and Intel means a second runtime, build and test target for a
# shrinking population. An Intel user gets a clear message pointing at the
# command-line install, not a mysterious failure.
#
# Windows and Linux are x86_64 only, matching the CI runners. Their installers
# are **built but not verified on real hardware** -- see ROADMAP S3f. Producing
# them catches build breakage early and gives those users something to
# download; calling them tested would be a promise nobody has kept.
CHECKSUMS = {
    "aarch64-apple-darwin":
        "12d6700f7e8f222639f0ee5bbd173082c3041aeb65af8f9828e4216bc8047de6",
    "x86_64-pc-windows-msvc":
        "8a0e1ded37e11f4c72b9671bf134bb478b1b2d55efe53a3d6e589b166f1bf2e1",
    "x86_64-unknown-linux-gnu":
        "fd9d70e1e1ed3f6caccb4e2eefe570aa07589c8f86ddf0e87f68a96cd14272e1",
}

# Where the interpreter sits inside an extracted runtime. Not the same shape on
# every platform: the unix builds put it under bin/, the Windows build puts
# python.exe at the top level. Assuming the unix path is how a Windows bundle
# ships with an interpreter that nothing can find -- and the failure appears
# only on the platform nobody here can test.
INTERPRETER = {
    "aarch64-apple-darwin": "bin/python3",
    "x86_64-unknown-linux-gnu": "bin/python3",
    "x86_64-pc-windows-msvc": "python.exe",
}

BASE_URL = ("https://github.com/astral-sh/python-build-standalone/"
            f"releases/download/{RELEASE}")

# Where the fetched runtimes land. Gitignored: a 30 MB binary tree does not
# belong in the repository, and it is reproducible from this file.
RUNTIME_DIR = HERE / "tray" / "runtime"


def archive_name(triple):
    return f"cpython-{VERSION}+{RELEASE}-{triple}-install_only.tar.gz"


def host_triple():
    """The target triple for the machine running this.

    Deliberately explicit rather than clever: an unrecognised platform should
    say so, not guess and produce an app that fails on first launch.
    """
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        raise SystemExit(
            "Airo's installer supports Apple Silicon Macs only (M1 and later).\n"
            "This machine reports an Intel processor.\n"
            "The command-line install still works on Intel: see README.")
    if sys.platform == "win32":
        if machine in ("amd64", "x86_64"):
            return "x86_64-pc-windows-msvc"
        raise SystemExit(
            f"no pinned Windows runtime for {machine} (x86_64 only so far).\n"
            "The command-line install still works: see README.")
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        raise SystemExit(
            f"no pinned Linux runtime for {machine} (x86_64 only so far).\n"
            "The command-line install still works: see README.")
    raise SystemExit(
        f"no pinned runtime for {sys.platform}/{machine}. "
        f"Supported: {', '.join(sorted(CHECKSUMS))}.")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: Tk/Tcl ships inside the standalone runtime and Airo never uses it — the UI
#: is a web dashboard and a Rust tray, no Python GUI. It has to go for a
#: concrete reason beyond size: on Linux the AppImage bundler walks every
#: shared object in the tree, finds `_tkinter` linked against `libtcl9.0.so`,
#: cannot resolve it (Tcl is not on the runner and its own copy sits in a
#: place the bundler does not search), and aborts the whole bundle. Removing
#: the toolkit removes the dangling dependency. Matched by precise names so
#: nothing else — sqlite3, ssl, the interpreter — is touched.
_GUI_EXACT = {"tkinter", "turtledemo", "idlelib", "turtle.py"}
_GUI_GLOBS = ("_tkinter.*", "libtcl*", "libtk*", "tcl*.dll", "tk*.dll",
              "tcl8.*", "tcl9.*", "tk8.*", "tk9.*", "thread2.*", "thread3.*")


def strip_unused_gui(root):
    """Remove the Tk/Tcl toolkit from an extracted runtime. Returns the paths
    removed, so the caller can say what it did rather than deleting silently."""
    root = Path(root)
    removed = []
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.exists():
            continue  # a parent already went
        name = path.name
        hit = name in _GUI_EXACT or any(fnmatch.fnmatch(name, g) for g in _GUI_GLOBS)
        if hit:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(root)))
    return removed


def fetch(triple, into=None):
    """Download, verify and extract one runtime. Returns its directory.

    Verification happens before extraction, not after: an archive that fails
    its checksum must never have been unpacked anywhere, because whatever
    comes next would find a plausible-looking tree and use it.
    """
    into = Path(into) if into else RUNTIME_DIR
    want = CHECKSUMS.get(triple)
    if want is None:
        raise SystemExit(f"no checksum pinned for {triple}")

    target = into / triple
    if (target / INTERPRETER[triple]).exists():
        print(f"  already present: {target}")
        return target

    url = f"{BASE_URL}/{archive_name(triple)}"
    print(f"  fetching {archive_name(triple)}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "runtime.tar.gz"
        try:
            with urllib.request.urlopen(url, timeout=300) as r, \
                    archive.open("wb") as out:
                shutil.copyfileobj(r, out)
        except OSError as e:
            raise SystemExit(f"could not download {url}: {e}")

        got = digest(archive)
        if got != want:
            # Fatal and loud. A runtime that is not the one we pinned is not
            # a runtime we are willing to ship, whatever the reason.
            raise SystemExit(
                f"checksum mismatch for {triple}\n"
                f"  expected {want}\n"
                f"  got      {got}\n"
                f"Refusing to extract. If upstream republished this release, "
                f"update CHECKSUMS deliberately in a commit of its own.")
        print(f"  checksum ok")

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extractall(Path(tmp) / "x", filter="data")
            except TypeError:
                tar.extractall(Path(tmp) / "x")   # filter= arrived in 3.12
        # The archive contains a single `python/` directory.
        shutil.move(str(Path(tmp) / "x" / "python"), str(target))

    gone = strip_unused_gui(target)
    if gone:
        print(f"  stripped Tk/Tcl ({len(gone)} paths) — unused, and it breaks "
              f"the Linux AppImage bundler")
    print(f"  extracted to {target}")
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true",
                    help="fetch every architecture, not just this machine's")
    ap.add_argument("--print-checksums", action="store_true",
                    help="print upstream's digests for this pinned release")
    ap.add_argument("--into", help="where to put them (default tray/runtime)")
    args = ap.parse_args()

    if args.print_checksums:
        url = f"{BASE_URL}/SHA256SUMS"
        print(f"from {url}\n")
        with urllib.request.urlopen(url, timeout=120) as r:
            for line in r.read().decode("utf-8", "replace").splitlines():
                if any(archive_name(t) == line.split()[-1] for t in CHECKSUMS):
                    print(f"  {line}")
        return 0

    triples = sorted(CHECKSUMS) if args.all else [host_triple()]
    print(f"Python {VERSION} (build {RELEASE})")
    for triple in triples:
        fetch(triple, args.into)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
