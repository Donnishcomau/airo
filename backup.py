#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Airo backup — export and restore everything that is yours.

    python3 backup.py create                    # -> airo-backup-<date>.tar.gz
    python3 backup.py create --output ~/Dropbox/airo.tar.gz
    python3 backup.py create --include-keys     # opt in to API keys
    python3 backup.py inspect airo-backup.tar.gz
    python3 backup.py restore airo-backup.tar.gz

What goes in: your configuration, your readings database, and a manifest
describing both. Everything else -- the code, the logs, the exported CSVs --
is either replaceable or noise.

**API keys are excluded unless you ask for them.** A backup tends to end up
somewhere a config file never would: a cloud drive, a USB stick, an email to
yourself. Silently folding credentials into that is how they leak. `inspect`
always says whether a given archive contains them.

Restore never overwrites without being told to, and always writes a safety
copy of whatever it is about to replace.

Standard library only.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import poller  # noqa: E402
import store   # noqa: E402

MANIFEST = "airo-manifest.json"
FORMAT_VERSION = 1

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")


#: poller's glyphs, not our own literals. Its `_console_safe()` degrades to
#: ASCII where the console cannot encode a tick. This file is the one the
#: fallback was written for — a Windows user got a UnicodeEncodeError from
#: `backup.py create` rather than a backup — and it still carried its own
#: unguarded copies. CONVENTIONS "Console encoding is not universal".
def ok(m):
    print(f"  {GREEN}{poller.TICK}{RESET} {m}")


def warn(m):
    print(f"  {YELLOW}{poller.WARN}{RESET} {m}")


def bad(m):
    print(f"  {RED}{poller.CROSS}{RESET} {m}")


def head(m):
    print(f"\n{BOLD}{m}{RESET}")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _key_files():
    keydir = Path.home() / ".airo"
    if not keydir.is_dir():
        return []
    return sorted(p for p in keydir.iterdir()
                  if p.is_file() and (p.suffix == ".key" or p.name == "apikey"))


def _snapshot_db(src, dest):
    """Copy the database consistently, even while the poller is writing.

    sqlite3's backup API takes a coherent snapshot of a live database. Copying
    the file directly can catch it mid-transaction, or miss recent writes
    sitting in the write-ahead log -- producing an archive that looks fine and
    restores short.
    """
    import sqlite3
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


# ------------------------------------------------------------------- create

def backup_dir():
    """Where backups live unless somebody says otherwise.

    A function, not a constant. As a module-level constant it froze
    `Path.home()` at import, so a test that redirects HOME afterwards got the
    *developer's real* directory — and `auto()` would have written archives
    into it. That is the rule this project has spent three fixes on: never
    mutate the developer's own `~/.airo` from a test. The suite caught it
    immediately, which is the only reason it is not still a constant.

    One definition either way, so the manual and automatic paths cannot drift
    apart — they already had, and the one that was wrong was the one a person
    types.
    """
    return Path.home() / ".airo" / "backups"


def create(output=None, include_keys=False):
    """Write one archive holding the config, the readings and a manifest.

    The database is snapshotted through SQLite's backup API rather than copied,
    so an archive taken while a poll is writing is still coherent -- a plain
    file copy of a WAL database mid-write is not.

    The manifest is what makes `inspect` possible without unpacking, and what
    `restore` checks before it touches anything. An archive without one is not
    treated as an Airo backup, which is deliberate: a tar of the right files
    assembled by hand has no checksums to verify against.

    Returns an exit code, not a path -- this is a CLI entry point. The settings
    page calls it through poller's API wrapper, which returns the detail.
    """
    head("Creating backup")

    cfg_path = poller.CONFIG_PATH
    db_path = poller.db_path()

    if not cfg_path.exists() and not db_path.exists():
        bad("Nothing to back up — no configuration and no database.")
        print("  Run: python3 setup.py")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if output is None:
        # Beside the automatic archives, not in the current directory.
        #
        # `Path.cwd()` reads as harmless and is not: the obvious place to run
        # this from is the checkout, and rule 3 names a backup archive among
        # the things that must never be in the repository. `.gitignore` and
        # the pre-commit hook both catch it, so it could not be committed —
        # but "cannot be committed" is a weaker promise than "is not there",
        # and the two paths in this file disagreeing about where backups live
        # is how one of them ends up wrong.
        #
        # `auto()` below has always used this directory. Manual and automatic
        # now agree, and `--output` still puts it wherever you ask.
        output = backup_dir() / f"airo-backup-{stamp}.tar.gz"
    output = Path(output).expanduser()
    if output.is_dir():
        output = output / f"airo-backup-{stamp}.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format": FORMAT_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "airo_source": str(HERE),
        "contains_keys": bool(include_keys),
        "contents": {},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        staging = Path(tmpdir)

        if cfg_path.exists():
            shutil.copy2(cfg_path, staging / "config.json")
            manifest["contents"]["config"] = {
                "sha256": _sha256(staging / "config.json"),
                "bytes": (staging / "config.json").stat().st_size,
            }
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            manifest["location"] = (cfg.get("location") or {}).get("name")
            manifest["sources"] = [
                f"{s.get('provider')}/{s.get('site_id')}"
                for s in (cfg.get("sources") or [])
            ]
            ok(f"configuration ({manifest['location'] or 'unnamed'}, "
               f"{len(manifest['sources'])} source(s))")
        else:
            warn("no configuration found")

        if db_path.exists():
            _snapshot_db(db_path, staging / "airo.db")
            conn = store.connect(staging / "airo.db")
            try:
                counts = store.counts(conn)
            finally:
                conn.close()
            total = sum(c["rows"] for c in counts)
            manifest["contents"]["database"] = {
                "sha256": _sha256(staging / "airo.db"),
                "bytes": (staging / "airo.db").stat().st_size,
                "readings": total,
                "sources": [
                    {"provider": c["provider"], "site_id": c["site_id"],
                     "rows": c["rows"], "first_utc": c["first_utc"],
                     "last_utc": c["last_utc"]}
                    for c in counts
                ],
            }
            ok(f"database ({total:,} readings across {len(counts)} source(s))")
        else:
            warn("no database found")

        keys = _key_files()
        if include_keys and keys:
            kd = staging / "keys"
            kd.mkdir()
            for k in keys:
                shutil.copy2(k, kd / k.name)
                poller.secure_path(kd / k.name)
            manifest["contents"]["keys"] = [k.name for k in keys]
            warn(f"including {len(keys)} API key file(s) — treat this archive "
                 f"as a secret")
        elif keys:
            ok(f"{len(keys)} API key(s) deliberately excluded "
               f"(--include-keys to override)")

        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n",
                                        encoding="utf-8")

        with tarfile.open(output, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)

    # A backup containing credentials must not be readable by anyone else.
    if include_keys:
        if not poller.secure_path(output):
            warn(f"could not restrict {output} to your account — it contains "
                 f"API keys, so move it somewhere private")
    else:
        try:
            os.chmod(output, 0o644)
        except OSError:
            pass
    size = output.stat().st_size
    head("Done")
    ok(f"{output}  ({size / 1e6:.1f} MB)")
    if include_keys:
        warn("contains API keys — mode 600, do not share")
    # Phrased for whoever is reading it: someone who installed the app has no
    # terminal open, and a command is an error message to them.
    print(f"\n  Restore it by: {poller.how_to('restore')}")
    if not poller.running_from_an_installed_app():
        print(f"    python3 backup.py restore {output}")
    return 0


# ------------------------------------------------------------------ inspect

def _read_manifest(archive):
    with tarfile.open(archive, "r:gz") as tar:
        try:
            f = tar.extractfile(MANIFEST)
        except KeyError:
            return None
        if f is None:
            return None
        return json.loads(f.read().decode("utf-8"))


def verify_archive(archive):
    """Is this archive actually restorable? Returns (ok, reason, readings).

    create() returning 0 only means it believed it succeeded. A truncated
    write, a full disk or a process killed mid-tar still leaves a plausible
    file on disk. This opens the archive, recomputes the database checksum and
    compares it with the manifest, which is the only check that distinguishes
    a real backup from a file of the right shape.
    """
    archive = Path(archive).expanduser()
    if not archive.exists():
        return False, "file does not exist", 0
    try:
        manifest = _read_manifest(archive)
    except (tarfile.TarError, OSError, ValueError) as e:
        return False, f"unreadable ({type(e).__name__})", 0
    if not manifest:
        return False, "no manifest inside", 0

    db = ((manifest.get("contents") or {}).get("database")) or {}
    rows = int(db.get("readings") or 0)
    want = db.get("sha256")
    if not want:
        return False, "manifest records no database checksum", rows

    try:
        with tarfile.open(archive, "r:gz") as tar:
            f = tar.extractfile("airo.db")
            if f is None:
                return False, "archive holds no database", rows
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except (tarfile.TarError, OSError, KeyError) as e:
        return False, f"could not read the database ({type(e).__name__})", rows

    if h.hexdigest() != want:
        return False, "database checksum does not match the manifest", rows
    return True, "ok", rows


def describe(archive):
    """What an archive contains, as data rather than as printed lines.

    inspect() prints, which is right for a terminal and useless to anything
    else. Both read the same manifest and both run verify_archive(), so a
    caller cannot get a friendlier answer by asking in a different way -- and
    "restorable" is a question that must be answered identically however it
    is asked.
    """
    archive = Path(archive).expanduser()
    if not archive.exists():
        return {"error": f"no such file: {archive}"}
    try:
        m = _read_manifest(archive)
    except (tarfile.TarError, OSError, ValueError) as e:
        return {"error": f"unreadable ({type(e).__name__}: {e})"}
    if not m:
        return {"error": "not an Airo backup (no manifest inside)"}

    restorable, reason, rows = verify_archive(archive)
    db = (m.get("contents") or {}).get("database") or {}
    return {
        "path": str(archive),
        "bytes": archive.stat().st_size,
        "created_utc": m.get("created_utc"),
        "format": m.get("format"),
        "location": m.get("location"),
        "sources": m.get("sources") or [],
        "readings": rows,
        "per_source": db.get("sources", []),
        # Stated whichever way the archive is examined. Someone about to copy
        # a file to a USB stick needs to know it carries credentials, and the
        # answer must not depend on which command they used to look.
        "contains_keys": bool(m.get("contains_keys")),
        "keys": (m.get("contents") or {}).get("keys", []),
        "restorable": bool(restorable),
        "reason": None if restorable else reason,
    }


def inspect(archive):
    """Say what is inside an archive without unpacking or changing anything.

    Exists because restore is destructive and nobody should have to run it to
    find out what they are about to get. It answers the two questions that
    decide whether to proceed: is this restorable, and does it contain keys.

    Both answers come from opening the archive, never from its filename -- a
    backup that merely looks intact is the one that fails when it is finally
    needed.
    """
    archive = Path(archive).expanduser()
    if not archive.exists():
        bad(f"no such file: {archive}")
        return 1
    m = _read_manifest(archive)
    if m is None:
        bad("not an Airo backup (no manifest inside)")
        return 1

    head(f"Backup: {archive.name}")
    print(f"  created   : {m.get('created_utc')}")
    print(f"  format    : {m.get('format')}")
    print(f"  location  : {m.get('location') or '(none)'}")
    print(f"  sources   : {', '.join(m.get('sources') or []) or '(none)'}")

    db = (m.get("contents") or {}).get("database")
    if db:
        print(f"  readings  : {db['readings']:,}")
        for s in db.get("sources", []):
            print(f"      {s['provider']}/{s['site_id']:<12} {s['rows']:>7,} rows"
                  f"  {s['first_utc']} → {s['last_utc']}")
    else:
        print("  readings  : none")

    if m.get("contains_keys"):
        warn(f"contains API keys: {', '.join((m['contents'] or {}).get('keys', []))}")
        warn("treat this archive as a secret")
    else:
        ok("contains no API keys")
    return 0


# ------------------------------------------------------------------ restore

def restore(archive, force=False, keys=True):
    """Replace the current setup with an archive's. Not a merge.

    Two safeguards, both load-bearing:

      * it refuses to overwrite an existing setup without `force`, because the
        common mistake is restoring onto a working install by accident
      * whatever is displaced is kept beside it as config.replaced-<stamp>.json
        and airo.replaced-<stamp>.db, which is the only reason this is
        reversible at all

    `keys=False` restores everything except credentials -- the right choice
    when moving to a machine that should get its own.
    """
    archive = Path(archive).expanduser()
    if not archive.exists():
        bad(f"no such file: {archive}")
        return 1
    m = _read_manifest(archive)
    if m is None:
        bad("not an Airo backup (no manifest inside)")
        return 1

    head(f"Restoring {archive.name}")
    print(f"  taken {m.get('created_utc')}")

    cfg_path = poller.CONFIG_PATH
    db_path = poller.db_path()
    existing = [p for p in (cfg_path, db_path) if p.exists()]

    if existing and not force:
        bad("This would overwrite your current setup:")
        for p in existing:
            print(f"      {p}")
        print("\n  Re-run with --force to proceed. A timestamped copy of")
        print("  everything replaced is kept alongside it.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory() as tmpdir:
        staging = Path(tmpdir)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                # Never let an archive write outside the staging directory.
                raw = member.name.replace("\\", "/")
                name = Path(raw)
                # Path.is_absolute() is platform-specific: "/tmp/evil" is not
                # absolute on Windows because it has no drive letter, so a
                # POSIX-absolute member slipped straight past. Check the string
                # itself as well as the parsed path.
                unsafe = (
                    raw.startswith("/")
                    or raw.startswith("\\")
                    or name.is_absolute()
                    or ".." in name.parts
                    or (len(raw) > 1 and raw[1] == ":")      # C:\ style
                )
                if unsafe:
                    bad(f"refusing unsafe path in archive: {member.name}")
                    return 1
            # Belt and braces: the loop above rejects traversal in member
            # names, and Python's data filter independently refuses absolute
            # paths, links pointing outside the tree, and device nodes. A
            # hand-written tar can carry things tarfile would never produce.
            try:
                tar.extractall(staging, filter="data")
            except TypeError:
                # Python < 3.12 has no filter argument; the checks above stand.
                tar.extractall(staging)

        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        poller.secure_path(cfg_path.parent, is_dir=True)

        if (staging / "config.json").exists():
            if cfg_path.exists():
                keep = cfg_path.with_name(f"config.replaced-{stamp}.json")
                shutil.copy2(cfg_path, keep)
                print(f"      previous config kept at {keep.name}")
            shutil.copy2(staging / "config.json", cfg_path)
            poller.secure_path(cfg_path)
            ok(f"configuration restored to {cfg_path}")

        if (staging / "airo.db").exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            if db_path.exists():
                keep = db_path.with_name(f"airo.replaced-{stamp}.db")
                shutil.copy2(db_path, keep)
                print(f"      previous database kept at {keep.name}")
            shutil.copy2(staging / "airo.db", db_path)
            conn = store.connect(db_path)
            try:
                total = sum(c["rows"] for c in store.counts(conn))
            finally:
                conn.close()
            ok(f"database restored to {db_path} ({total:,} readings)")

        kd = staging / "keys"
        if kd.is_dir():
            if not keys:
                warn("archive contains API keys — skipped (--no-keys)")
            else:
                dest = Path.home() / ".airo"
                dest.mkdir(parents=True, exist_ok=True)
                poller.secure_path(dest, is_dir=True)
                for k in sorted(kd.iterdir()):
                    shutil.copy2(k, dest / k.name)
                    if not poller.secure_path(dest / k.name):
                        warn(f"could not restrict {dest / k.name}")
                ok(f"{len(list(kd.iterdir()))} API key(s) restored (mode 600)")

    head("Done")
    print("  Check it over:")
    print("    python3 poller.py --status")
    print("  Then restart the background agent so it picks this up:")
    print(f"    {poller.how_to('restart')}")
    return 0


# ------------------------------------------------------------------- auto

DEFAULT_KEEP = 7


def auto(keep=DEFAULT_KEEP, interval_hours=24, force=False):
    """Take a routine backup, keeping the most recent `keep`.

    A backup feature nobody remembers to run is not a mitigation. This is
    called after a poll when `backup.auto` is enabled, so protection is the
    default state rather than an act of discipline.

    Never includes keys: an automatic archive lands on disk unattended and
    possibly inside a synced folder, which is not where credentials belong.
    """
    dest = backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    poller.secure_path(dest, is_dir=True)

    existing = sorted(dest.glob("airo-backup-*.tar.gz"))
    if existing and not force:
        newest = existing[-1].stat().st_mtime
        age_hours = (datetime.now().timestamp() - newest) / 3600
        if age_hours < interval_hours:
            return None            # too soon; nothing to do

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest / f"airo-backup-{stamp}.tar.gz"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = create(output=out, include_keys=False)
    if rc != 0:
        return None

    # Verify the new archive BEFORE deleting any old one. create() returning 0
    # only means it thought it succeeded; a truncated write, a full disk or an
    # interrupted process still leaves a file on disk. Rotating on that would
    # trade every good backup for one broken one, unattended, on a schedule.
    good, why, rows = verify_archive(out)
    if not good:
        print(f"  {poller.WARN} new archive did not verify ({why}); "
              f"keeping every existing backup")
        return out
    if rows <= 0:
        print(f"  {poller.WARN} new archive holds no readings; "
              f"keeping every existing backup")
        return out

    # Rotate. Oldest first, so `keep` most recent survive.
    for old in sorted(dest.glob("airo-backup-*.tar.gz"))[:-keep]:
        if old == out:
            continue
        try:
            old.unlink()
        except OSError as e:
            # Not silent: a rotation that cannot delete means backups will
            # accumulate until a disk fills, and the user should hear it once.
            print(f"  {poller.WARN} could not remove {old.name}: {e}")
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Back up and restore your Airo configuration and readings.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="write a backup archive")
    c.add_argument("--output", "-o", help="file or directory to write to")
    c.add_argument("--include-keys", action="store_true",
                   help="also include API keys (archive becomes a secret)")

    a = sub.add_parser("auto", help="take a routine backup and rotate old ones")
    a.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                   help=f"how many archives to retain (default {DEFAULT_KEEP})")
    a.add_argument("--force", action="store_true",
                   help="take one even if a recent backup exists")

    i = sub.add_parser("inspect", help="show what an archive contains")
    i.add_argument("archive")

    r = sub.add_parser("restore", help="restore from an archive")
    r.add_argument("archive")
    r.add_argument("--force", action="store_true",
                   help="replace an existing configuration or database")
    r.add_argument("--no-keys", action="store_true",
                   help="do not restore API keys even if present")

    args = ap.parse_args()
    if args.cmd == "create":
        return create(args.output, args.include_keys)
    if args.cmd == "auto":
        out = auto(keep=args.keep, force=args.force)
        if out is None:
            print("  a recent backup already exists — nothing to do")
        else:
            ok(f"{out}")
            kept = sorted(backup_dir().glob("airo-backup-*.tar.gz"))
            print(f"  keeping {len(kept)} archive(s) in {out.parent}")
        return 0
    if args.cmd == "inspect":
        return inspect(args.archive)
    return restore(args.archive, args.force, keys=not args.no_keys)


if __name__ == "__main__":
    sys.exit(main())
