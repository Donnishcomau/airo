#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assemble exactly what the installed app ships, in one place.

Why a staging directory rather than pointing the bundler at the repository
-----------------------------------------------------------------------
Two reasons, both learned the hard way:

  * The bundler does not follow `..` out of its own directory, so a resource
    listed as `../poller.py` silently produces an empty folder. Silently is
    the problem: the build succeeds and the app is broken.
  * A glob mapped to a destination *flattens* it. `runtime/**/*` put
    `lib/python3.12/__future__.py` at `runtime/__future__.py` and destroyed
    the interpreter's directory tree, again without failing.

Staging also makes "what ships" a decision rather than an accident. This file
is the list, and it is deliberately explicit: a glob over the repository would
ship whatever happened to be lying about -- a scratch file, a stale export,
somebody's real config.json.

What must never ship
--------------------
`config.json`, anything under `data/`, `*.key`, `INTERNAL.md`. Those are the
user's, and rules 2, 2a, 2b exist because they have escaped before. There is a
test asserting none of them can reach the payload.
"""

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PAYLOAD = HERE / "tray" / "payload"

# The Python side, listed rather than globbed. A new module shipping is then a
# deliberate line in this file rather than a side effect of existing.
MODULES = [
    "poller.py", "store.py", "fusion.py", "scheduler.py",
    "setup.py", "backup.py", "analyse.py", "forecast.py",
    "weather.py", "units.py",
]

# Pages the local server serves.
PAGES = ["dashboard.html", "settings.html"]

# Shipped so a fresh install has a template to read; carries no real values.
# LICENSE travels with the software because the AGPL requires it to -- but as
# a file inside the app, not as a click-through the disk image demands before
# it will mount. Nothing in the AGPL conditions *use* on acceptance.
TEMPLATES = ["config.example.json", "LICENSE"]

# Never, under any circumstances.
FORBIDDEN_NAMES = {"config.json", "config.local.json", "INTERNAL.md"}
FORBIDDEN_SUFFIXES = {".key", ".db", ".csv"}


def stage(runtime_dir=None, into=None):
    """Build the payload tree. Returns its path.

    Rebuilt from scratch each time: a stale file left behind from a previous
    layout would ship, and nothing would notice.
    """
    into = Path(into) if into else PAYLOAD
    if into.exists():
        shutil.rmtree(into)
    (into / "airo").mkdir(parents=True)

    for name in MODULES + PAGES + TEMPLATES:
        src = HERE / name
        if not src.exists():
            raise SystemExit(f"cannot stage: {name} is missing from {HERE}")
        shutil.copy2(src, into / "airo" / name)

    # The interpreter. Optional so the payload can be staged and checked
    # without a 69 MB fetch, but an app built without one cannot run.
    runtime_dir = Path(runtime_dir) if runtime_dir else _host_runtime()
    if runtime_dir and runtime_dir.exists():
        shutil.copytree(runtime_dir, into / "runtime", symlinks=True)
    else:
        print("  ! no runtime staged — run tools/fetch_runtime.py first")

    _refuse_anything_private(into)
    return into


def _host_runtime():
    """Where fetch_runtime.py put this machine's interpreter."""
    try:
        sys.path.insert(0, str(HERE / "tools"))
        import fetch_runtime
        return fetch_runtime.RUNTIME_DIR / fetch_runtime.host_triple()
    except (ImportError, SystemExit):
        return None


def _refuse_anything_private(root):
    """Fail the build rather than ship someone's location or key.

    Checked after staging rather than trusted from the list above, because the
    list is a human artefact and the runtime tree is copied wholesale.
    """
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
            offenders.append(path.relative_to(root))
    if offenders:
        raise SystemExit(
            "refusing to build: the payload contains files that are the "
            "user's, not ours:\n  " + "\n  ".join(str(o) for o in offenders))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runtime", help="interpreter directory to ship")
    ap.add_argument("--into", help="where to stage (default tray/payload)")
    args = ap.parse_args()

    out = stage(args.runtime, args.into)
    files = sum(1 for p in out.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"staged {files:,} files ({size / 1e6:.0f} MB) into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
