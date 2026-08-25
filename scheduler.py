#!/usr/bin/env python3
"""
Airo scheduling — install the background poller on macOS, Linux or Windows.

The data layer has always been portable; only scheduling was macOS-specific.
This module puts the three platform mechanisms behind one interface:

  macOS    launchd user agent      (~/Library/LaunchAgents/*.plist)
  Linux    systemd --user timer    (~/.config/systemd/user/*.{service,timer})
  Windows  Task Scheduler          (schtasks.exe)

All three follow the same model the project has always used: wake on an
interval, poll for a couple of seconds, exit. There is no resident process,
so a scheduler reporting "not running" between polls is correct.

Standard library only.

The tray is a different shape of thing -- resident, starts at login, no
interval -- but uses the same three platform mechanisms, so it lives here too.

    python3 scheduler.py install [--interval 15]
    python3 scheduler.py uninstall
    python3 scheduler.py status
    python3 scheduler.py start | stop | restart

    python3 scheduler.py install-tray      # menu-bar / system-tray app, at login
    python3 scheduler.py uninstall-tray
"""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABEL = "com.donnish.airo"
TRAY_LABEL = "com.donnish.airo.tray"
TRAY_NAME = "airo-tray"
SERVICE_NAME = "airo"


def data_dir():
    """Where the poller keeps its data, asked rather than assumed.

    Scheduler log paths must land beside the readings, not inside the project,
    or a migrated install writes its logs to a folder nothing else reads.

    The fallback stays -- `scheduler.py` has to work well enough to *uninstall*
    when the rest of the project is broken, which is exactly when somebody
    reaches for it -- but it no longer answers `$PROJECT/data`. That is the
    shape CONVENTIONS rule 2a records as having created a stray empty database
    beside the real one and reported zero rows for a working install: the one
    path that is certainly wrong, offered as the safe default. It answers the
    same `~/.airo/data` poller would, honouring `$AIRO_DATA` as poller does,
    and says out loud that it is guessing. What it cannot see without poller
    is `data_dir` from the config; a log written to the default while the
    readings live on an external disk is a misplaced log, not a lost database.
    """
    try:
        sys.path.insert(0, str(HERE))
        import poller
        return Path(poller.DATA)
    except Exception as e:
        env = os.environ.get("AIRO_DATA", "").strip()
        guess = Path(env).expanduser() if env else Path.home() / ".airo" / "data"
        print(f"WARN could not import poller ({type(e).__name__}: {e}); "
              f"assuming readings are in {guess}. If a data_dir is configured, "
              f"scheduler logs will not land beside them.", file=sys.stderr)
        return guess


def python_exe():
    return sys.executable or shutil.which("python3") or "python3"


def system():
    s = platform.system().lower()
    if s.startswith("darwin"):
        return "macos"
    if s.startswith("windows"):
        return "windows"
    return "linux"


#: Commands that address the *logged-in session* rather than files. Each one
#: is keyed on the uid or the current session, not on HOME, so redirecting HOME
#: does not contain them.
SESSION_MANAGERS = ("launchctl", "systemctl", "schtasks")


def run(cmd, check=False):
    """Run a command, refusing the ones that would reach into a session this
    process does not own.

    The guard lives here, in the real runner, rather than at each call site.
    A test that stubs `run` to assert on the arguments is then unaffected --
    it never reaches a subprocess and never touches anything -- while an
    actual invocation under a redirected HOME is refused. Putting it at the
    call sites broke four such tests, which was the wrong layer sounding the alarm.
    """
    if cmd and cmd[0] in SESSION_MANAGERS and not owns_the_live_session():
        return subprocess.CompletedProcess(
            cmd, 1, "",
            f"refusing to run {cmd[0]} against the logged-in session: HOME is "
            f"{os.environ.get('HOME')!r}, which is not this user's home "
            f"directory. Agents are addressed by uid rather than by HOME, so "
            f"this would act on the real session instead of on this one.")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


# ----------------------------------------------------------------- macOS

def _plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def registered_working_dir():
    """Which folder the registered agent actually runs in, or None.

    The label is fixed, so with two checkouts -- or a folder that has been
    moved or renamed -- launchd happily reports *the other install* as healthy
    while this one never runs. Everything looks fine and nothing is collected.
    That has already cost hours once.

    Read from the plist rather than parsed out of `launchctl print`, whose
    output format is not a contract and has changed between macOS releases.
    """
    if system() != "macos":
        return None
    try:
        with _plist_path().open("rb") as f:
            return plistlib.load(f).get("WorkingDirectory") or None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def agent_belongs_to_this_project():
    """(ok, message). False when a *different* copy owns the schedule.

    Returns True when nothing is registered: that is "not installed", which is
    a different problem with a different answer, reported elsewhere.
    """
    where = registered_working_dir()
    if where is None:
        return True, ""
    if Path(where).resolve() == HERE.resolve():
        return True, ""
    return False, (
        f"an agent named {LABEL} is registered, but for a different folder:\n"
        f"      registered: {where}\n"
        f"      this one  : {HERE}\n"
        f"      That copy is polling and this one never will.")


def _uid():
    """The launchd domain this user's agents live in.

    Its own function so the macOS backend can be *run* on Linux and Windows,
    where os.getuid does not exist. That matters more than it sounds: the
    launchd tests used to build their own plist dict and assert on that, so
    they passed while macos_install() wrote StartInterval in minutes instead
    of seconds -- a 60x error that would have polled every fifteen seconds,
    and five green tests named after the thing it broke.
    """
    return os.getuid()


#: The home this process started under, captured before anything can redirect
#: it. Windows has no passwd database to ask, so this is the only anchor
#: available there — and it is the right one for the case that matters, since
#: a test redirects HOME *after* importing the module it is testing.
_HOME_AT_IMPORT = os.environ.get("HOME") or os.environ.get("USERPROFILE")


def owns_the_live_session():
    """Whether this process may act on the logged-in user's launchd session.

    `launchctl` addresses agents as `gui/<uid>/<label>`. That is keyed on the
    **uid and a fixed label** — not on HOME, not on which copy of Airo is
    asking. So a process running under a redirected HOME still boots out the
    real agent belonging to the real login session, and the only thing the
    redirection protects is the plist file it then fails to find.

    That is not hypothetical. It is how the developer's own poller stopped:
    a bundle test ran `--uninstall` with HOME pointed at a temp directory,
    deleted a plist that was never there, and unloaded the live agent. The
    readings kept their last entry at the minute the suite ran, the plists sat
    untouched on disk, and nothing anywhere reported an error — the last poll
    in the log is a clean success.

    Comparing HOME against the passwd entry is the check that catches it,
    because a redirected HOME is exactly the signal that this process is not
    the login session: a test, a sandbox, a `sudo -u`, a bundle being
    exercised. Each of those may manage *its own* files and none of them may
    reach into somebody else's running session.
    """
    try:
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError):
        # Windows: no passwd database, and `expanduser` reads the very
        # variables being checked, so it cannot be the reference. Returning
        # True here was the first version and it made the guard do nothing on
        # the platform where Task Scheduler is the session manager — the
        # failure mode this exists to stop, on a different OS.
        real = _HOME_AT_IMPORT
    current = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not real or not current:
        return True          # nothing to compare; not a login session
    return os.path.realpath(current) == os.path.realpath(real)


def macos_install(interval_minutes):
    # Prefer a root Airo.app if the checkout has one -- it exists purely so macOS
    # attributes notifications and background activity to "Airo" rather than to
    # "python3". Nothing here builds it: it came from install.sh, deleted in Aug
    # 2026, and this line has said "if scheduler.py install built one" ever since,
    # which is a job that never moved across. A fresh checkout takes the fallback.
    app = HERE / "Airo.app" / "Contents" / "MacOS" / "Airo"
    program = [str(app)] if app.exists() else [python_exe(), str(HERE / "poller.py"), "--once"]

    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": program,
        "StartInterval": int(interval_minutes) * 60,
        "RunAtLoad": True,
        "WorkingDirectory": str(HERE),
        "StandardOutPath": str(data / "launchd.out.log"),
        "StandardErrorPath": str(data / "launchd.err.log"),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)

    uid = _uid()
    run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    r = run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
    if r.returncode != 0:
        return False, f"launchctl bootstrap failed: {r.stderr.strip()}"
    return True, f"launchd agent installed ({interval_minutes} min): {path}"


def macos_uninstall():
    uid = _uid()
    run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    p = _plist_path()
    if p.exists():
        p.unlink()
    return True, "launchd agent removed"


def macos_start():
    uid = _uid()
    path = _plist_path()
    if not path.exists():
        return False, "not installed — run: python3 scheduler.py install"
    run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
    r = run(["launchctl", "kickstart", f"gui/{uid}/{LABEL}"])
    if r.returncode != 0:
        return False, f"launchctl kickstart failed: {r.stderr.strip()}"
    return True, "agent started"


def macos_stop():
    uid = _uid()
    # bootout unloads the agent but leaves the plist, so start can bring it
    # back without a reinstall.
    r = run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    if r.returncode != 0 and "No such process" not in (r.stderr or ""):
        return False, f"launchctl bootout failed: {r.stderr.strip()}"
    return True, "agent stopped (plist kept; start to resume)"


def macos_status():
    r = run(["launchctl", "list"])
    for line in r.stdout.splitlines():
        parts = line.split()
        # Match the label exactly. `com.donnish.airo.tray` contains
        # `com.donnish.airo` as a substring, so a loose match reports the
        # tray's pid as though it were the poller's.
        if len(parts) >= 3 and parts[2] == LABEL:
            # PID '-' between polls is correct: this is a periodic task.
            return True, f"registered with launchd (pid={parts[0]}, last exit={parts[1]})"
    return False, "not registered with launchd"


# ----------------------------------------------------------------- Linux
#
# systemd --user, not cron: a user timer runs under the logged-in session, so
# it inherits the desktop environment a notification needs, and it survives
# without root. The trade-off is that everything here is conditional on
# systemctl existing -- several distributions and every container do not have
# it. Where it is missing the unit files are still written and the caller is
# told to enable them, rather than reporting success for a schedule that will
# never fire. That distinction is the whole reason these return (ok, message)
# instead of a bool.

def _systemd_dir():
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_word(value):
    """Quote one word for a systemd unit line.

    `ExecStart=` is split on whitespace, so an unquoted path containing a space
    becomes several arguments and the service fails on every run -- silently,
    because the units install cleanly, the timer enables, systemctl reports
    success, and only the poll fails, in the journal of a user unit nobody
    thinks to read. The symptom is that no readings arrive.

    macOS and Windows were immune by accident rather than design: a launchd
    plist holds ProgramArguments as an *array*, and the schtasks command was
    already quoting its parts. Only the backend that built a command by string
    interpolation was exposed, which is the lesson rather than a fact about
    Linux.

    `%` is a systemd specifier introducer -- %h is the home directory, %n the
    unit name -- so a literal one must be doubled or the path expands into
    something else entirely, which is a stranger failure to debug than a space.
    """
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{s}"'


def linux_install(interval_minutes):
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)

    (d / f"{SERVICE_NAME}.service").write_text(f"""[Unit]
Description=Airo air quality poller
Documentation=https://github.com/Donnishcomau/airo

[Service]
Type=oneshot
WorkingDirectory={_systemd_word(HERE)}
ExecStart={_systemd_word(python_exe())} {_systemd_word(HERE / 'poller.py')} --once
""", encoding="utf-8")

    # OnUnitActiveSec rather than OnCalendar so the cadence is an interval,
    # matching launchd's StartInterval and Windows' /MO minutes.
    # Persistent=true recovers a poll missed while the machine was asleep,
    # which is the same guarantee the gap-backfill provides for the data.
    (d / f"{SERVICE_NAME}.timer").write_text(f"""[Unit]
Description=Airo air quality poller ({interval_minutes} min)

[Timer]
OnBootSec=1min
OnUnitActiveSec={int(interval_minutes)}min
Persistent=true
Unit={SERVICE_NAME}.service

[Install]
WantedBy=timers.target
""", encoding="utf-8")

    if not shutil.which("systemctl"):
        return False, ("systemd unit files written to "
                       f"{d}, but systemctl was not found — enable them manually")

    run(["systemctl", "--user", "daemon-reload"])
    r = run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"])
    if r.returncode != 0:
        return False, f"systemctl enable failed: {r.stderr.strip()}"
    return True, f"systemd user timer installed ({interval_minutes} min): {d}"


def linux_uninstall():
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.timer"])
    d = _systemd_dir()
    removed = []
    for name in (f"{SERVICE_NAME}.timer", f"{SERVICE_NAME}.service"):
        p = d / name
        if p.exists():
            p.unlink()
            removed.append(name)
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"])
    return True, f"systemd timer removed ({', '.join(removed) or 'nothing to remove'})"


def linux_start():
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    r = run(["systemctl", "--user", "start", f"{SERVICE_NAME}.timer"])
    if r.returncode != 0:
        return False, f"systemctl start failed: {r.stderr.strip()}"
    return True, "timer started"


def linux_stop():
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    r = run(["systemctl", "--user", "stop", f"{SERVICE_NAME}.timer"])
    if r.returncode != 0:
        return False, f"systemctl stop failed: {r.stderr.strip()}"
    return True, "timer stopped"


def linux_status():
    """(active, message). False means "not polling", for any of three reasons.

    A timer can be absent, present but not enabled, or enabled on a machine
    with no systemctl to ask -- and the message distinguishes them, because
    the fix differs. `is-active` alone would answer the first two the same way.
    """
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    r = run(["systemctl", "--user", "is-active", f"{SERVICE_NAME}.timer"])
    active = r.stdout.strip() == "active"
    n = run(["systemctl", "--user", "list-timers", f"{SERVICE_NAME}.timer",
             "--no-pager", "--no-legend"])
    detail = n.stdout.strip().split("\n")[0] if n.stdout.strip() else ""
    return active, (f"systemd timer active — {detail}" if active
                    else "systemd timer not active")


# ----------------------------------------------------------------- Windows
#
# schtasks.exe, driven as a subprocess. No COM, no pywin32 -- hard rule 1 means
# the standard library and nothing else, and schtasks is present on every
# supported Windows.
#
# Two things about it are easy to get wrong. It reports failures on stdout as
# often as on stderr, so every call here reads both; and /TR takes a single
# string in which the executable and its arguments are separately quoted, which
# is why the command below is assembled the way it is rather than as a list.

def windows_install(interval_minutes):
    # pythonw.exe runs without opening a console window every poll, which
    # matters a great deal when that happens 96 times a day.
    exe = python_exe()
    pythonw = Path(exe).with_name("pythonw.exe")
    if pythonw.exists():
        exe = str(pythonw)

    cmd = [
        "schtasks", "/Create", "/TN", SERVICE_NAME,
        "/TR", f'"{exe}" "{HERE / "poller.py"}" --once',
        "/SC", "MINUTE", "/MO", str(int(interval_minutes)),
        "/F",
    ]
    r = run(cmd)
    if r.returncode != 0:
        return False, f"schtasks failed: {(r.stderr or r.stdout).strip()}"
    return True, f"scheduled task installed ({interval_minutes} min): {SERVICE_NAME}"


def windows_uninstall():
    r = run(["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"])
    if r.returncode != 0:
        return False, f"schtasks delete failed: {(r.stderr or r.stdout).strip()}"
    return True, "scheduled task removed"


def windows_start():
    run(["schtasks", "/Change", "/TN", SERVICE_NAME, "/ENABLE"])
    r = run(["schtasks", "/Run", "/TN", SERVICE_NAME])
    if r.returncode != 0:
        return False, f"schtasks run failed: {(r.stderr or r.stdout).strip()}"
    return True, "scheduled task started"


def windows_stop():
    r = run(["schtasks", "/Change", "/TN", SERVICE_NAME, "/DISABLE"])
    if r.returncode != 0:
        return False, f"schtasks disable failed: {(r.stderr or r.stdout).strip()}"
    run(["schtasks", "/End", "/TN", SERVICE_NAME])
    return True, "scheduled task stopped"


def windows_status():
    """(registered, message). Registered is not the same as enabled.

    /Query succeeds for a task that windows_stop() disabled, so True here means
    "Airo is installed", not "Airo is polling". Reported this way deliberately:
    the two states have different fixes, and conflating them would have `status`
    tell someone to reinstall when all they need is `start`.
    """
    r = run(["schtasks", "/Query", "/TN", SERVICE_NAME])
    if r.returncode != 0:
        return False, "scheduled task not found"
    return True, "scheduled task registered"


# ------------------------------------------------------------------- tray
#
# The tray is resident and starts at login, unlike the poller which wakes on
# an interval and exits. Same platform mechanisms, different lifecycle.

def tray_binary():
    """Path to the tray binary, wherever this install keeps it.

    Three shapes, in order of how much we trust them:

      * installed app -- we are running from inside it, so the binary is the
        one two directories up from the staged payload. Found first because in
        an installed app it is the only correct answer, and a stale checkout
        elsewhere on the machine must not win.
      * development checkout -- release preferred over debug
      * neither, in which case the caller explains how to build it

    Kept here rather than in the tray so there is one answer to "where is the
    binary", shared by install, uninstall and status.
    """
    inside_app = HERE.parent.parent / "MacOS" / "Airo"
    if inside_app.exists():
        return inside_app

    for profile in ("release", "debug"):
        candidate = HERE / "tray" / "target" / profile / TRAY_NAME
        if candidate.exists():
            return candidate
    return None


def _tray_plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{TRAY_LABEL}.plist"


def macos_tray_install(binary):
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": TRAY_LABEL,
        "ProgramArguments": [str(binary)],
        # KeepAlive, not StartInterval: unlike the poller this one is supposed
        # to stay resident. If it crashes, bring it back.
        "KeepAlive": True,
        "RunAtLoad": True,
        "WorkingDirectory": str(HERE),
        "EnvironmentVariables": {
            "AIRO_HOME": str(HERE),
            "AIRO_PYTHON": python_exe(),
        },
        "StandardOutPath": str(data / "tray.out.log"),
        "StandardErrorPath": str(data / "tray.err.log"),
    }
    path = _tray_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)

    uid = _uid()
    run(["launchctl", "bootout", f"gui/{uid}/{TRAY_LABEL}"])

    # bootout is asynchronous: the label can still be registered for a moment
    # after it returns, and bootstrap then fails with a bare "Input/output
    # error". Retry briefly rather than leaving a flaky install step.
    last = ""
    for attempt in range(6):
        r = run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        if r.returncode == 0:
            return True, f"tray installed and started: {path}"
        last = (r.stderr or r.stdout).strip()
        time.sleep(0.5)
    return False, f"launchctl bootstrap failed after retries: {last}"


def macos_tray_uninstall():
    uid = _uid()
    run(["launchctl", "bootout", f"gui/{uid}/{TRAY_LABEL}"])
    p = _tray_plist_path()
    if p.exists():
        p.unlink()
    return True, "tray agent removed"


def linux_tray_install(binary):
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{TRAY_NAME}.service").write_text(f"""[Unit]
Description=Airo tray
Documentation=https://github.com/Donnishcomau/airo
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory={_systemd_word(HERE)}
Environment={_systemd_word(f'AIRO_HOME={HERE}')}
Environment={_systemd_word(f'AIRO_PYTHON={python_exe()}')}
ExecStart={_systemd_word(binary)}
Restart=on-failure

[Install]
WantedBy=graphical-session.target
""", encoding="utf-8")
    if not shutil.which("systemctl"):
        return False, f"unit written to {d}, but systemctl was not found"
    run(["systemctl", "--user", "daemon-reload"])
    r = run(["systemctl", "--user", "enable", "--now", f"{TRAY_NAME}.service"])
    if r.returncode != 0:
        return False, f"systemctl enable failed: {r.stderr.strip()}"
    return True, f"tray service installed and started: {d}"


def linux_tray_uninstall():
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", f"{TRAY_NAME}.service"])
    p = _systemd_dir() / f"{TRAY_NAME}.service"
    if p.exists():
        p.unlink()
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"])
    return True, "tray service removed"


def windows_tray_install(binary):
    # ONLOGON rather than an interval -- the tray stays resident.
    r = run([
        "schtasks", "/Create", "/TN", TRAY_NAME,
        "/TR", f'"{binary}"', "/SC", "ONLOGON", "/F",
    ])
    if r.returncode != 0:
        return False, f"schtasks failed: {(r.stderr or r.stdout).strip()}"
    run(["schtasks", "/Run", "/TN", TRAY_NAME])
    return True, f"tray scheduled task installed: {TRAY_NAME}"


def windows_tray_uninstall():
    r = run(["schtasks", "/Delete", "/TN", TRAY_NAME, "/F"])
    if r.returncode != 0:
        return False, f"schtasks delete failed: {(r.stderr or r.stdout).strip()}"
    return True, "tray scheduled task removed"


TRAY_BACKENDS = {
    "macos": (macos_tray_install, macos_tray_uninstall),
    "linux": (linux_tray_install, linux_tray_uninstall),
    "windows": (windows_tray_install, windows_tray_uninstall),
}


def install_tray():
    binary = tray_binary()
    if binary is None:
        return False, ("tray binary not built. Build it first:\n"
                       "    cd tray && cargo build --release\n"
                       "(needs Rust: https://rustup.rs or `brew install rust`)")
    return TRAY_BACKENDS[system()][0](binary)


def uninstall_tray():
    return TRAY_BACKENDS[system()][1]()


# ----------------------------------------------------------------- dispatch

# One tuple per platform, in a fixed order: install, uninstall, status, start,
# stop. Every one of the fifteen returns (ok: bool, message: str) -- the single
# contract that lets everything above this line be platform-blind, and the
# thing to preserve when a fourth platform arrives.
#
# The positional indexing below (`BACKENDS[system()][3]`) reads poorly and a
# dict of named callables would be plainly better. Left alone here because this
# is a commenting pass and that is a behaviour-shaped change; worth doing on
# its own, with the platform tests to catch a mis-ordering.
BACKENDS = {
    "macos": (macos_install, macos_uninstall, macos_status,
              macos_start, macos_stop),
    "linux": (linux_install, linux_uninstall, linux_status,
              linux_start, linux_stop),
    "windows": (windows_install, windows_uninstall, windows_status,
                windows_start, windows_stop),
}


def install(interval_minutes=15):
    return BACKENDS[system()][0](interval_minutes)


def uninstall():
    return BACKENDS[system()][1]()


def status():
    return BACKENDS[system()][2]()


def start():
    return BACKENDS[system()][3]()


def stop():
    return BACKENDS[system()][4]()


def restart():
    stop()
    return start()


def main():
    ap = argparse.ArgumentParser(description="Install Airo's background poller.")
    ap.add_argument("action", choices=("install", "uninstall", "status",
                                       "start", "stop", "restart",
                                       "install-tray", "uninstall-tray"))
    ap.add_argument("--interval", type=int, default=15,
                    help="minutes between polls (default 15)")
    args = ap.parse_args()

    print(f"platform: {system()}")
    if args.action == "install":
        ok, msg = install(args.interval)
    elif args.action == "uninstall":
        ok, msg = uninstall()
    elif args.action == "start":
        ok, msg = start()
    elif args.action == "stop":
        ok, msg = stop()
    elif args.action == "restart":
        ok, msg = restart()
    elif args.action == "install-tray":
        ok, msg = install_tray()
    elif args.action == "uninstall-tray":
        ok, msg = uninstall_tray()
    else:
        ok, msg = status()
        binary = tray_binary()
        print(f"tray binary: {binary or 'not built (cd tray && cargo build --release)'}")

    print(msg)
    # No special case for status: it used to `return 1` here when not ok,
    # which is what the line below already does. Dead code that reads as a
    # handled case is worse than no code -- somebody maintaining this has to
    # work out what it was for before they can be sure removing it is safe.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
