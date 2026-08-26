#!/usr/bin/env python3
"""
Airo — the program. Everything except the store, the fusion decision and the
scheduling mechanics is here.

One poll does this: read the config, ask each configured provider what its
monitor is reading now, hand the readings to `store` to be written, ask
`fusion` which one is the honest headline, write `latest.json` for the tray and
the dashboard, and notify if the air has crossed a threshold. Then exit. There
is no resident process: a scheduler wakes this every fifteen minutes and it
takes about two seconds.

The same file also serves the dashboard and the settings page on loopback, and
carries the command line. That is deliberate consolidation rather than sprawl:
the settings API and the poll path must agree about what a valid setting is and
what an alert threshold means, and the cheapest way to guarantee that is for
them to read the same module-level constants.

Reading this file
-----------------
Sections in file order, each with a banner comment:

  paths and safety      where the config, data and log live, and the checks
                        that refuse an unsafe path
  aqi scales            SCALES, the only place band boundaries exist
  utilities             logging, config load/merge, key storage
  settings rules        SETTINGS_SCHEMA and the validators the settings page
                        and setup.py both call, so they cannot disagree
  finding monitors      discovery: what is near a location, and is it alive
  providers             one class per network; see the banner at Provider
  polling               poll_source, backfill_source, build_latest, do_poll
  alerts                thresholds, quiet hours, cooldown, notification
  server                the loopback HTTP server and QuietHandler
  status                --status, --doctor, and the data-directory guards
  main                  argparse and dispatch

How it fits the whole
---------------------
  imports        `store` (SQLite ingest and series) and `fusion` (which
                 reading is the headline) at module level, and `scheduler`
                 inside the three functions that register or remove a
                 background job -- deferred because importing it probes the
                 platform, which nothing else here should pay for.
  is imported by `setup.py`, `backup.py`, `analyse.py` and the tests, all of
                 which take their paths and validators from here rather than
                 deriving their own. (`forecast.py` stands alone: it is a set
                 of guardrails for a feature that does not exist yet.) Shell
                 must ask
                 `python3 -c "import poller; print(poller.DATA)"` for the same
                 reason: a second copy of that path created a stray empty
                 database once.
  is called by   the launchd/systemd/Task Scheduler job (`--once`), and the
                 Rust tray, which shells out for every action it offers and
                 decides nothing itself.
  writes         `~/.airo/data/airo.db` via `store`, and `latest.json`, which
                 is the contract with every UI. Change a field there and the
                 tray and the dashboard both need looking at.

What it assumes
---------------
  * **The interpreter is standard-library only.** Not a style preference: the
    installer ships a bare CPython, and CI fails if a dependency manifest
    appears. Anything needing a package belongs outside the Python side.
  * **The reading path is append-only.** Nothing in a poll updates a row; a
    bad value is rejected at ingest rather than written and corrected later,
    which is what lets `store.insert_readings` treat a present row as proof
    that window was covered. There is exactly one in-place update in the
    project -- `store.repair_sentinels`, which nulls a stored feed sentinel
    and keeps the row, because deleting it would erase the fact that we asked
    and the station answered.
  * **Raw µg/m³ is canonical.** An AQI is computed for display and never
    stored. The same air gives very different index values on different
    national scales, so a stored index would be a number whose meaning
    depends on a setting that can change.
  * **Providers publish at different cadences**, declared as
    `resolution_minutes`. Gap detection scales with it; a fixed threshold
    fires on every poll against an hourly feed.
  * **Local time has no DST offset.** True in Queensland, wrong elsewhere —
    see ROADMAP #5. Day bucketing in the dashboard depends on it too.
  * **The config path resolves before the data directory**, because the data
    directory can be configured inside the config. Reordering those two breaks
    startup in a way that looks like a missing file.

What must not be done here
--------------------------
  * **Never log or print an API key.** Keys live in `~/.airo/<provider>.key`,
    mode 600. `scrub_secrets()` exists so a settings payload cannot carry one
    to a browser, and it is the thing to extend when a new secret appears.
  * **Never silently discard a reading.** Faults and uncorroborated values are
    flagged and shown. If there is a fire next door, that is genuinely the air
    being breathed.
  * **Never store a derived AQI as the source of truth.**
  * **Never write a second copy of a band boundary, an alert default or a
    trend threshold.** `SCALES`, `ALERT_DEFAULTS` and `compute_trend()` are the
    single copies; every UI reads them from `latest.json`. Each of these has
    already been duplicated once and drifted.
"""

import argparse
import contextlib
import csv
import hmac
import io
import json
import math
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import forecast
import fusion
import store
import units
import weather

HERE = Path(__file__).resolve().parent

# Sent to third-party APIs in the User-Agent, where an identifying version is
# what their rate-limit policies ask for. Nominatim's in particular requires
# one. `tray/tauri.conf.json` carries its own copy for the bundler -- ROADMAP
# notes that two version numbers is one too many; this is the one the Python
# side uses.
VERSION = "0.6.1"


# ------------------------------------------------------------ paths and safety
#
# Resolved at import, in this order, because each depends on the one above:
# the config says where the data goes, and the log lives in the data directory.
#
# These three module-level names -- CONFIG_PATH, DATA, LOG_PATH -- are what
# every other component asks for rather than rebuilding. Shell scripts included:
# hardcoding `$PROJECT/data` once produced a stray empty database beside the
# real one and reported zero rows for a working install.
#
# The functions below them refuse paths that are unsafe rather than working
# around them, because a data directory that silently is not where the user
# thinks it is loses years of readings without deleting anything.


# Resolved before the data directory, which may be configured inside it.
# Config resolution itself depends on nothing but $AIRO_CONFIG and $HOME.
def _resolve_config_path():
    """Where the user's settings live.

    Outside the repository by default. A config file inside a git working tree
    holds a location and a chosen sensor -- personal data that must not be
    committable by accident, and that a contributor should never receive in a
    clone. The in-repo path is kept last so a development checkout still works.

    Order: $AIRO_CONFIG, ~/.airo/config.json, ./config.json
    """
    env = os.environ.get("AIRO_CONFIG", "").strip()
    if env:
        return Path(env).expanduser()
    user = Path.home() / ".airo" / "config.json"
    if user.exists():
        return user
    local = HERE / "config.json"
    if local.exists():
        return local
    return user          # first run: this is where setup will write


CONFIG_PATH = _resolve_config_path()


def config_path():
    """The same answer as CONFIG_PATH, resolved *when asked*.

    CONFIG_PATH is resolved at import, which is right for the poller: it runs
    as one process against one install, and every other module reads the
    constant. It is wrong for anything that must not freeze `$HOME` or
    `$AIRO_CONFIG` at import time -- `setup.py` deliberately resolves its
    paths late, because module constants there once froze the *developer's*
    home and a test redirecting HOME afterwards still got the real one.

    setup.py had its own `~/.airo/config.json`, hardcoded, which ignored
    $AIRO_CONFIG entirely: with that variable set, setup wrote a config the
    poller would never read, and reported success. One resolver, asked twice,
    cannot disagree with itself.
    """
    return _resolve_config_path()


def _resolve_data_dir():
    """Where readings live.

    Outside the repository, like the config. A database inside a git working
    tree is lost the moment someone re-clones, moves the folder or wipes an
    untracked directory -- and unlike config, this is years of irreplaceable
    history that cannot be regenerated.

    Order: $AIRO_DATA, config's data_dir, ~/.airo/data, ./data (pre-v0.6).

    The env var wins so a one-off run can point somewhere else without
    editing anything. `data_dir` in the config is the durable choice -- an
    external disk, a synced folder, a roomier volume -- because a setting the
    user can only express as an environment variable is one every scheduler,
    launch agent and shell has to be taught separately, and forgetting one
    silently starts a second empty database.

    An existing ./data is deliberately preferred over an empty ~/.airo/data so
    an upgrade never silently starts a blank database beside a full one. The
    move is explicit: `python3 poller.py --migrate-data`.
    """
    env = os.environ.get("AIRO_DATA", "").strip()
    if env:
        return Path(env).expanduser()

    configured = _configured_data_dir()
    if configured:
        return configured

    user = Path.home() / ".airo" / "data"
    legacy = HERE / "data"
    if (legacy / "airo.db").exists() and not (user / "airo.db").exists():
        return legacy
    return user


def _configured_data_dir():
    """`data_dir` from the config, if it names somewhere usable.

    Read directly rather than through load_config(), because load_config()
    needs DATA to resolve first -- and a config that cannot be parsed must
    not stop the poller finding a database it has been writing to for years.
    """
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = (raw or {}).get("data_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value.strip()).expanduser()


DATA = _resolve_data_dir()
LEGACY_DATA = HERE / "data"
CSV_PATH = DATA / "readings.csv"
LATEST_PATH = DATA / "latest.json"
ALERT_STATE_PATH = DATA / "alert_state.json"
#: ROADMAP #9 Phase C. The predictions made and not yet checked,
#: and the verified ledger the skill score is computed from. Beside
#: the readings rather than in the repo, like everything else of the
#: user's.
FORECAST_PENDING_PATH = DATA / "forecast_pending.json"
FORECAST_SKILL_PATH = DATA / "forecast_skill.json"
LOG_PATH = DATA / "poller.log"


EXAMPLE_CONFIG_PATH = HERE / "config.example.json"

#: Derived from VERSION rather than written out. As a literal it said "1.0"
#: for the whole of 0.5.0, while the Nominatim header three functions down
#: said "0.5" and weather.py said "0.5" again — four User-Agents, three
#: versions, none of them the project's. VERSION is the canonical one.
USER_AGENT = f"airo-poller/{VERSION} (personal air quality logging)"


def _console_safe():
    """Make stdout able to carry the symbols we print, or stop using them.

    Windows consoles default to cp1252, which cannot encode a tick. Printing
    one raises UnicodeEncodeError and takes the whole command down -- so a
    Windows user got a crash from `backup.py create` rather than a backup.

    Try to switch the stream to UTF-8; if that is not possible, fall back to
    ASCII markers. Never let decoration break a command.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    try:
        "\u2713\u2717\u26a0\u00b5".encode(enc or "ascii")
        return "\u2713", "\u2717", "!", "\u00b5g/m\u00b3"
    except (LookupError, UnicodeEncodeError):
        return "OK", "X", "!", "ug/m3"


TICK, CROSS, WARN, UGM3 = _console_safe()


def secure_path(path, is_dir=False, os_name=None, username=None):
    """Restrict a file or directory to its owner, on every platform.

    POSIX: chmod 600 / 700, which is the whole story.

    Windows: chmod only toggles the read-only attribute -- access there is
    governed by ACLs. So use icacls, which ships with Windows and needs no
    dependency: drop inherited permissions and grant the current user full
    control, which is the closest equivalent to 0600 the platform offers.

    Returns True if the restriction was applied. Callers should treat False as
    worth telling the user about: a credential file that is not actually
    protected should not silently look like one that is.

    `os_name` and `username` default to this machine's and exist to be tested.
    Branching on the global `os.name` meant the Windows path could only ever
    run on Windows, so it was carried untested from the day it was written --
    on the platform where getting it wrong means every account on the machine
    can read the key, because chmod there only toggles a read-only attribute.
    Same shape as folder_chooser_commands(), for the same reason.
    """
    path = Path(path)
    mode = 0o700 if is_dir else 0o600
    os_name = os.name if os_name is None else os_name

    if os_name != "nt":
        try:
            os.chmod(path, mode)
            return True
        except OSError:
            return False

    # Windows. Set the read-only-ish bit too so the intent is visible in the
    # file properties, then do the part that actually matters.
    try:
        os.chmod(path, mode)
    except OSError:
        pass

    user = username
    if user is None:
        user = os.environ.get("USERNAME") or ""
        if not user:
            try:
                import getpass
                user = getpass.getuser()
            except Exception:
                user = ""
    if not user:
        # A grant to the wrong principal is worse than no grant at all: it
        # looks applied and protects somebody else's account.
        return False

    grant = f"{user}:(OI)(CI)F" if is_dir else f"{user}:F"
    try:
        r = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def path_is_restricted(path, os_name=None):
    """Is this path readable only by its owner?

    Used to tell the user the truth in --status rather than assuming the
    attempt succeeded.

    None means "no answer" and False means "not protected". Collapsing the two
    would report a missing key file as an insecure one.
    """
    path = Path(path)
    os_name = os.name if os_name is None else os_name
    # Equivalent to the except clauses below -- stat() and icacls both fail on
    # a path that is not there, and both already answer None. A mutation sweep
    # reports this as untested and always will; it is kept because "does this
    # file exist" is the question being asked, and expressing it as an
    # exception would be a worse way to ask it.
    if not path.exists():
        return None
    if os_name != "nt":
        try:
            return oct(stat.S_IMODE(path.stat().st_mode))[-3:] in ("600", "700")
        except OSError:
            return None
    try:
        r = subprocess.run(["icacls", str(path)], capture_output=True,
                           text=True, timeout=20)
        if r.returncode != 0:
            return None
        # Inheritance removed and no broad principals granted.
        broad = ("Everyone", "BUILTIN\\Users", "Authenticated Users")
        return not any(b in r.stdout for b in broad)
    except (OSError, subprocess.SubprocessError):
        return None

# ------------------------------------------------------------------ aqi scales
#
# Raw ug/m3 is the canonical value everywhere -- in the CSV, in latest.json and
# in every calculation. An AQI number is only ever a presentation layer derived
# from it, because the same air gives wildly different AQI on different national
# scales and we must never bake one country's opinion into stored data.
#
# Each scale is a list of (pm25_upper, aqi_upper, band_name) segments. AQI is
# interpolated linearly within a segment, which is how piecewise index scales
# are defined. "raw" is the escape hatch: no index at all, just ug/m3.
#
# A `bands` entry is (ceiling, name) or (ceiling, name, advice). Advice is a
# health-relevant sentence and so lives here rather than in a renderer (rule 7,
# D8). It belongs to the band it is written beside and travels with it; a band
# with no advice serves none, and the surfaces show the name alone.
#
# It was in the dashboard until now, as a six-entry JS table joined to whatever
# the server sent BY POSITION. Every scale here has six bands, so the join
# always "worked" and always meant something different: `raw` band 2 is "Above
# WHO guideline" and received "Enjoy normal activities.", written for an
# Australian band topping out at 16.5 ug/m3. Advice attached by position is
# advice about a different concentration.

SCALES = {
    # Australia (NEPM): a simple linear scale where AQI 100 == the PM2.5
    # standard of 25 ug/m3, i.e. AQI = ug/m3 x 4. Bands per the national
    # Air Quality Index categories.
    #
    # The only scale with advice. These six sentences were written for these
    # six bands and are carried here verbatim.
    "au": {
        "label": "Australian AQI",
        "note": "100 = the NEPM PM2.5 standard of 25 ug/m3",
        "linear_standard": 25.0,
        "bands": [
            (33, "Very good",
             "Enjoy normal activities."),
            (66, "Good",
             "Enjoy normal activities."),
            (99, "Fair",
             "Sensitive people should reduce prolonged outdoor exertion."),
            (149, "Poor",
             "Close up and filter. Avoid outdoor exertion."),
            (200, "Very poor",
             "Everyone should avoid outdoor exertion."),
            (float("inf"), "Hazardous",
             "Stay indoors with filtration running."),
        ],
    },
    # United States EPA, as revised 6 May 2024 -- the "good" ceiling dropped
    # from 12.0 to 9.0 ug/m3 and the upper categories were lowered. Piecewise
    # linear over 24-hour PM2.5 breakpoints.
    #
    # No advice, deliberately. The Australian sentences above are not a
    # one-to-one fit and were never written for these categories: "Moderate"
    # reaches 35.4 ug/m3 where the Australian band carrying "Enjoy normal
    # activities." stops at 16.5, so reusing the list would repeat the bug it
    # was moved here to end. Wording for these six categories is the
    # maintainer's to write; until then the surfaces show the band name alone.
    "us_epa": {
        "label": "US EPA AQI",
        "note": "2024 revision; 9.0 ug/m3 = AQI 50",
        "breakpoints": [
            (0.0, 9.0, 0, 50, "Good"),
            (9.1, 35.4, 51, 100, "Moderate"),
            (35.5, 55.4, 101, 150, "Unhealthy for sensitive groups"),
            (55.5, 125.4, 151, 200, "Unhealthy"),
            (125.5, 225.4, 201, 300, "Very unhealthy"),
            (225.5, 325.4, 301, 500, "Hazardous"),
        ],
    },
    # No index -- report ug/m3 directly. Bands follow the WHO 2021 24-hour
    # guideline of 15 ug/m3 as the reference point.
    #
    # No advice either, for the same reason and more sharply: these bands are
    # cut against a guideline, not against a national index, and by position
    # "Above WHO guideline" was being shown "Enjoy normal activities." Wording
    # is the maintainer's to write. Absent is the honest state; a sentence
    # borrowed from another scale is not.
    "raw": {
        "label": "PM2.5 ug/m3",
        "note": "no index; WHO 2021 24-hour guideline is 15 ug/m3",
        "identity": True,
        "bands": [
            (15, "At or below WHO guideline"),
            (25, "Above WHO guideline"),
            (37.5, "Well above guideline"),
            (75, "High"),
            (150, "Very high"),
            (float("inf"), "Extreme"),
        ],
    },
}

DEFAULT_SCALE = "au"


# --------------------------------------------------------------- local time
#
# `config.json` has carried location.timezone since the first version and
# nothing ever read it. Six places resolved local time independently, each
# calling .astimezone() with no argument -- which means *the machine's* zone.
#
# The machine is very often not where the user is. A NAS or a Pi is usually
# left on UTC; a VM inherits its host; a laptop reports the zone it woke up in.
# Two of those six decide health-relevant behaviour: quiet hours of 22:00-07:00
# resolved against UTC in Brisbane silence 08:00 to 17:00 local and notify at
# 3am, and the evening window the whole project is built around slides by the
# same ten hours, so "is it worse after sunset" is asked about the wrong ones.
#
# One honest gap, stated rather than hidden: zoneinfo needs a system timezone
# database and Windows has none without the `tzdata` package. That is a runtime
# dependency, which rule 1 forbids, so there the configured zone cannot be
# applied and the machine's is used. That degradation is reported by --doctor
# and in the log rather than left to be found.

_ZONE_CACHE = {}

# A zone that exists in every copy of the IANA database. Used to tell "this
# platform has no timezone database" apart from "that name is a typo" -- on
# Windows *every* lookup raises ZoneInfoNotFoundError, so without a control
# the two are indistinguishable and a correct config gets blamed for a
# platform limitation.
_KNOWN_ZONE = "America/New_York"


def timezone_database_available():
    """Can this platform resolve IANA zone names at all?

    False on Windows, where CPython ships no timezone database and the package
    that supplies one would be a runtime dependency (rule 1). Asked rather
    than assumed: the first version of this counted an unresolvable zone as a
    fault, which made every healthy Windows install report a problem it could
    do nothing about, and CI is what said so.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return False
    try:
        ZoneInfo(_KNOWN_ZONE)
        return True
    except Exception:
        return False


def resolve_zone(name):
    """Turn a configured IANA name into a tzinfo. Returns (tz, note).

    `tz` is None to mean "use the machine's zone", which is what a bare
    .astimezone() already does -- so every caller can pass the result straight
    through without a branch. `note` is empty when nothing needs saying and a
    sentence when the configured zone could not be applied.

    Never raises. A typo in a config file must not stop a poll: recording the
    air in the wrong zone and complaining loudly beats recording nothing.
    """
    name = (name or "").strip()
    if not name:
        return None, "timezone not set; using this machine's zone"
    if name in _ZONE_CACHE:
        return _ZONE_CACHE[name]

    try:
        from zoneinfo import ZoneInfo
    except ImportError:                                   # Python < 3.9
        result = (None, f"this Python has no zoneinfo, so {name} cannot be "
                        f"applied; using this machine's zone")
    else:
        try:
            result = (ZoneInfo(name), "")
        except Exception as e:
            result = (None,
                      f"no timezone database entry for {name} "
                      f"({type(e).__name__}); using this machine's zone "
                      f"instead. On Windows this is expected: Python has no "
                      f"system timezone database there, and the package that "
                      f"supplies one would be a runtime dependency this "
                      f"project does not take.")
    _ZONE_CACHE[name] = result
    return result


def timezone_name(cfg):
    """The IANA zone recorded for the user's location, or ''."""
    return str((((cfg or {}).get("location") or {}).get("timezone")) or "").strip()


def local_zone(cfg):
    """The tzinfo local times should be read in, or None for the machine's."""
    return resolve_zone(timezone_name(cfg))[0]


def local_now(cfg, now=None):
    """`now` as a wall clock in the user's zone.

    Pass `now` (an aware datetime) to convert a known instant rather than
    reading the clock -- which is what makes this testable without waiting for
    3am to come round.
    """
    tz = local_zone(cfg)
    if now is None:
        return datetime.now(tz) if tz is not None else datetime.now().astimezone()
    return now.astimezone(tz) if tz is not None else now.astimezone()


def timezone_report(cfg):
    """Lines for --doctor: which zone is in force, and whether it is the
    configured one.

    Naming both is the point. "You configured Australia/Brisbane, this machine
    thinks UTC" is the sentence that explains a whole class of confusion --
    alerts at 3am, an evening analysis about the wrong ten hours -- and neither
    half says it alone.
    """
    name = timezone_name(cfg)
    tz, note = resolve_zone(name)
    machine = datetime.now().astimezone().tzname() or "unknown"
    offset = datetime.now().astimezone().utcoffset()
    machine_desc = f"{machine} (UTC{_offset_text(offset)})"

    if not name:
        return [f"timezone      not set — using this machine's: {machine_desc}",
                "              Set location.timezone so alerts and the evening",
                "              window follow where you are, not where this",
                "              computer thinks it is."]
    if tz is None and not timezone_database_available():
        # Not the user's fault and not fixable from here. Reported plainly,
        # and NOT counted against them: a healthy Windows install showing a
        # permanent problem it cannot clear teaches people to ignore --doctor,
        # which costs more than this gap does.
        return [f"timezone      {name} configured, but this platform has no",
                f"              timezone database, so it cannot be applied.",
                f"              Using this machine's zone: {machine_desc}",
                "              Expected on Windows: CPython ships no zone data",
                "              there and the package that supplies it would be",
                "              a runtime dependency this project does not take.",
                "              Times follow this computer's clock. If it is set",
                "              to where you live, nothing here is wrong."]
    if tz is None:
        return [f"timezone      {name} configured but NOT in force",
                f"              using this machine's zone instead: {machine_desc}",
                f"              {note}",
                "              This platform does have a timezone database, so",
                "              the name is most likely a typo."]
    return [f"timezone      {name} "
            f"(UTC{_offset_text(datetime.now(tz).utcoffset())}); "
            f"this machine: {machine_desc}"]


def timezone_is_a_problem(cfg):
    """Should --doctor count the timezone against this install?

    Only when the platform *could* have resolved the configured name and did
    not -- that is a typo, and fixable. Where there is no timezone database at
    all the configuration is fine and nothing the user does will change the
    answer, so it is reported and not counted. A --doctor that always shows a
    problem is a --doctor nobody reads.

    Its own function because the tally it feeds is not testable through
    run_doctor: that returns a count of everything, and an unrelated problem
    masks this one entirely. Asserting on the decision is the only way to
    assert on the decision.
    """
    return bool(timezone_name(cfg)
                and local_zone(cfg) is None
                and timezone_database_available())


def _offset_text(delta):
    if delta is None:
        return "?"
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def get_scale(cfg):
    """Resolve the configured scale, falling back loudly rather than silently."""
    name = str((cfg or {}).get("aqi_scale") or DEFAULT_SCALE).strip().lower()
    if name not in SCALES:
        log(f"WARN unknown aqi_scale {name!r}; falling back to {DEFAULT_SCALE!r}")
        name = DEFAULT_SCALE
    return name, SCALES[name]

# Defaults deliberately carry no real site. A fresh checkout must be pointed
# at a location by the user -- shipping someone else's suburb as the default
# is how the first version ended up with one person's own sensor in every install.
DEFAULT_CONFIG = {
    "location": {
        "name": "",
        "latitude": None,
        "longitude": None,
        "timezone": "",
    },
    # A list, because the point of the platform is comparing several
    # instruments at one place. One entry is the common case, not the model.
    "sources": [],
    "fusion": {
        "rule": "nearest",
    },
    "aqi_scale": DEFAULT_SCALE,
    "poll_minutes": 15,
    "serve_port": 8787,
    "serve": True,
    "backfill_days_on_first_run": 7,

    # How long to keep readings. 0 or absent means forever, which is the
    # default and the point of the tool: this record cannot be regenerated.
    "retention_days": 0,

    # Routine protection, on by default. A backup feature nobody remembers to
    # run protects nobody; this data cannot be regenerated.
    "auto_backup": {"enabled": True, "keep": 7, "interval_hours": 24},

    # Tell the user when a source has stopped working, rather than leaving a
    # warning in a log nobody reads while the record quietly develops a hole.
    "source_failure_alert_after": 4,
}

# Alerting reads these when the config is silent. They live here rather than as
# literals inside maybe_alert() because a settings UI has to show the user the
# value that is actually in force -- and a second copy of a default is a
# setting that displays one number while the alert fires on another.
ALERT_DEFAULTS = {
    "enabled": True,
    "threshold_aqi": 67,        # amber / "Fair" on the AU scale
    "threshold_pm25": None,     # scale-independent form; wins when both are set
    "rising_delta": 12,
    "cooldown_minutes": 60,
    "notify_when_clear": True,
    "quiet_hours": None,
    "sound": None,
}


def effective_alerts(cfg):
    """The alert settings actually in force, defaults filled in.

    Anything asking "what will this install do?" -- the alerting path, the
    settings API, --status -- must get its answer from here, so they cannot
    disagree about a threshold.
    """
    # Configured keys the defaults do not know about are carried through rather
    # than filtered out: a setting this function has not heard of is still the
    # user's, and dropping it here would silently change behaviour elsewhere.
    a = dict(ALERT_DEFAULTS)
    a.update(cfg.get("alerts") or {})
    a["enabled"] = bool(a["enabled"])
    a["notify_when_clear"] = bool(a["notify_when_clear"])
    return a


def migrate_config(cfg):
    """Lift older config shapes forward.

    v0.3 was flat: sensor_index/sensor_name/read_key at the top level.
    v0.4 nested them under a single "source".
    v0.5 takes a list of "sources", because a location can have several.

    Both older shapes are read rather than making anyone rewrite their config
    by hand. `airo config normalise` rewrites the file once, after which this
    stays a compatibility shim for people upgrading.
    """
    sources = list(cfg.get("sources") or [])

    # v0.4 single source
    single = cfg.get("source")
    if isinstance(single, dict) and single.get("site_id") is not None:
        if not any(str(s.get("site_id")) == str(single.get("site_id"))
                   and s.get("provider") == single.get("provider")
                   for s in sources):
            sources.append(dict(single))

    # v0.3 flat keys
    if cfg.get("sensor_index") is not None:
        legacy = {
            "provider": "purpleair",
            "site_id": cfg["sensor_index"],
            "site_name": cfg.get("sensor_name") or "",
            "read_key": cfg.get("read_key") or "",
        }
        if not any(s.get("provider") == "purpleair"
                   and str(s.get("site_id")) == str(legacy["site_id"])
                   for s in sources):
            sources.append(legacy)

    for s in sources:
        s.setdefault("provider", "purpleair")
        s.setdefault("enabled", True)

    cfg["sources"] = sources

    loc = dict(cfg.get("location") or {})
    if not loc.get("name"):
        # Fall back to the first source's name so an upgraded install still
        # shows something meaningful rather than an empty title.
        if cfg.get("sensor_name"):
            loc["name"] = cfg["sensor_name"]
        elif sources and sources[0].get("site_name"):
            loc["name"] = sources[0]["site_name"]
    cfg["location"] = loc
    return cfg


def enabled_sources(cfg):
    """The configured sources that are switched on.

    A source disabled in settings is skipped by polling but keeps its history:
    turning one off is not a request to forget what it already measured.
    """
    return [s for s in (cfg.get("sources") or []) if s.get("enabled", True)]


# ------------------------------------------------------------------ utilities

# Logs the scheduler owns rather than us. launchd and systemd append to these
# forever and never rotate them; on a machine running for years that is the
# only unbounded growth in the project. ROADMAP known issue D.
SCHEDULER_LOGS = ("launchd.out.log", "launchd.err.log")
LOG_MAX_BYTES = 2_000_000
LOG_KEEP_LINES = 2000


def _trim(path, max_bytes=LOG_MAX_BYTES, keep_lines=LOG_KEEP_LINES):
    """Truncate a log in place once it exceeds max_bytes.

    Rewrites rather than rotates: a second file would just be a second thing
    to forget about, and nothing here is worth archiving.
    """
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-keep_lines:]
            path.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except Exception:
        pass


def log(msg):
    """Append one timestamped line to the poller log, trimming it as it goes.

    Every scheduled poll writes here and nothing rotates it externally, so the
    trim is not housekeeping -- it is the only thing between a fifteen-minute
    job and an unbounded file. Local time, deliberately: this log is read by a
    person wondering what happened at nine last night, not by a machine.

    Never log a key. Anything derived from config or a provider response has to
    be considered for that before it goes in here.
    """
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        _trim(LOG_PATH)
        # The scheduler's own logs have no self-trimming at all, so do it here
        # -- this runs every poll and is a stat() unless the file is oversized.
        for name in SCHEDULER_LOGS:
            _trim(DATA / name)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _deep_merge(base, over):
    """Merge nested dicts. A plain dict.update() would drop sibling defaults
    inside 'location' and 'source' whenever a user set only one of their keys.
    """
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of the defaults
    if CONFIG_PATH.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"WARN could not parse config.json ({e}); using defaults")
    return migrate_config(cfg)


def key_path(slug):
    """Where one network's key lives. Outside the repo, deliberately."""
    return Path.home() / ".airo" / f"{str(slug).lower()}.key"


def save_key(slug, key):
    """Store a network key, restricted. Returns (path, restricted).

    `restricted` is read back rather than assumed. On Windows `os.chmod` only
    toggles the read-only attribute, so a key file can look protected and not
    be -- which is why secure_path() shells out to icacls there and why this
    reports what actually happened instead of returning True.

    An empty key removes the file. Clearing a credential has to be as easy as
    setting one, or the only way out is a terminal.
    """
    path = key_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_path(path.parent, is_dir=True)

    key = (key or "").strip()
    if not key:
        if path.exists():
            path.unlink()
        return path, None

    path.write_text(key, encoding="utf-8")
    secure_path(path)
    # Never log or return the value. Only ever whether it is there, and
    # whether the file it is in is safe.
    return path, path_is_restricted(path)


def probe_writable(path):
    """Can readings actually be written here? Returns (ok, message).

    Checked at the moment a directory is chosen rather than on the first poll.
    A path that cannot be written is how someone ends up logging into nowhere
    and not noticing for weeks -- and an unmounted removable drive looks
    exactly like a typo, so the distinction has to be made out loud, here.
    """
    try:
        candidate = Path(path).expanduser()
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".airo-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return False, f"cannot write there: {e}"
    return True, str(candidate)


# ------------------------------------------------------------- settings rules
#
# One table, consulted by both front ends. setup.py asks these questions in a
# terminal and the settings page asks them in a browser, and they are two views
# onto one file -- so "what is a valid poll interval" has to have exactly one
# answer. Two validators drift, and the drift shows up as a config the wizard
# accepts and the UI rejects, or worse, the other way round.

class Invalid(ValueError):
    """A setting that was refused, with the reason a user can act on."""


def _text(v, field):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise Invalid(f"{field}: expected text")
    return v.strip()


def _timezone(v, field):
    """An IANA zone name the machine can actually resolve, or empty.

    Empty is legitimate and is what every install had before this field was
    read: follow the machine's own zone.

    A name that cannot be resolved is refused rather than stored, because
    storing it puts a value in the config that every later lookup fails on --
    and --doctor then reports it back as the user's typo, which it is, except
    that the page accepted it without a word.

    Only where the platform HAS a timezone database. On Windows nothing
    resolves, so validating against resolution would refuse every zone
    including the correct one, locking somebody out of a setting because of a
    limitation they can do nothing about. There the value is stored and
    --doctor explains why it is not in force.
    """
    name = _text(v, field)
    if not name:
        return ""
    if timezone_database_available() and resolve_zone(name)[0] is None:
        raise Invalid(
            f"{field}: {name} is not a timezone this machine knows. "
            f"Use an IANA name such as Australia/Brisbane, or leave it empty "
            f"to follow this computer's own zone.")
    return name


def _flag(v, field):
    if not isinstance(v, bool):
        raise Invalid(f"{field}: expected true or false")
    return v


def _whole(low=None, high=None):
    def check(v, field):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
            raise Invalid(f"{field}: expected a whole number")
        v = int(v)
        if low is not None and v < low:
            raise Invalid(f"{field}: must be at least {low}")
        if high is not None and v > high:
            raise Invalid(f"{field}: must be at most {high}")
        return v
    return check


def _decimal(low=None, high=None, allow_none=False):
    def check(v, field):
        if v is None and allow_none:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise Invalid(f"{field}: expected a number")
        v = float(v)
        if low is not None and v < low:
            raise Invalid(f"{field}: must be at least {low}")
        if high is not None and v > high:
            raise Invalid(f"{field}: must be at most {high}")
        return v
    return check


def _units_override(value, where):
    """A per-quantity display-unit override, or the shorthand for a system.

    Rejects an unknown unit loudly rather than ignoring it. A silently dropped
    `"temperature": "kelvin"` leaves somebody staring at Celsius wondering why
    the setting did nothing, and the fix is a sentence they can read.
    """
    if isinstance(value, str):
        if value.lower() not in ("metric", "us"):
            raise Invalid(f"{where}: units must be 'metric', 'us', or a "
                          f"mapping like {{'temperature': 'f'}}")
        return value.lower()
    if not isinstance(value, dict):
        raise Invalid(f"{where}: units must be a mapping or a system name")
    out = {}
    for quantity, unit in value.items():
        if quantity not in units.QUANTITIES:
            raise Invalid(f"{where}: unknown quantity {quantity!r}; "
                          f"choose from {', '.join(sorted(units.QUANTITIES))}")
        allowed = sorted(u for q, u in units.CONVERSIONS if q == quantity)
        if str(unit).lower() not in allowed:
            raise Invalid(f"{where}: {quantity} cannot be shown in "
                          f"{unit!r}; choose from {', '.join(allowed)}")
        out[quantity] = str(unit).lower()
    return out


def _one_of(choices):
    def check(v, field):
        allowed = sorted(choices() if callable(choices) else choices)
        if v not in allowed:
            raise Invalid(f"{field}: must be one of {', '.join(map(str, allowed))}")
        return v
    return check


def _quiet_hours(v, field):
    """A [from, until] pair of hours, or nothing.

    Not a duration and not a range check: 22 to 7 is a perfectly ordinary
    overnight window that crosses midnight, and rejecting it as "backwards"
    would suppress the setting people most want.
    """
    if v in (None, [], ()):
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise Invalid(f"{field}: expected two hours, like [22, 7]")
    out = []
    for h in v:
        if isinstance(h, bool) or not isinstance(h, (int, float)) or h != int(h):
            raise Invalid(f"{field}: hours must be whole numbers")
        if not 0 <= int(h) <= 23:
            raise Invalid(f"{field}: hours must be between 0 and 23")
        out.append(int(h))
    return out


def _data_dir(v, field):
    """Empty means the default. Anything else is probed before it is accepted,
    because data_dir is configurable and that is a way to abandon a database."""
    v = _text(v, field)
    if not v:
        return ""
    ok, message = probe_writable(v)
    if not ok:
        raise Invalid(f"{field}: {message}")
    return message


def _sources(v, field):
    if not isinstance(v, list):
        raise Invalid(f"{field}: expected a list of sources")
    out = []
    for i, src in enumerate(v):
        where = f"{field}[{i}]"
        if not isinstance(src, dict):
            raise Invalid(f"{where}: expected an object")
        leaked = [k for k in src if str(k).lower() in SECRET_FIELD_NAMES]
        if leaked:
            # Keys have their own route, which writes them to a mode-600 file
            # outside the config. Accepting one here would put a credential
            # into the same object that gets echoed back and logged on error.
            raise Invalid(f"{where}: {leaked[0]} is set through the keys route, "
                          f"not with the rest of the settings")
        if not str(src.get("site_id") or "").strip():
            raise Invalid(f"{where}: site_id is required")
        provider = str(src.get("provider") or "").strip().lower()
        if provider not in PROVIDERS:
            raise Invalid(f"{where}: unknown provider {provider or '(none)'}; "
                          f"known: {', '.join(sorted(PROVIDERS))}")
        clean = dict(src)
        clean["provider"] = provider
        clean["enabled"] = bool(src.get("enabled", True))
        out.append(clean)
    return out


# Nested exactly as config.json is. A dict means "recurse"; anything else is
# the check for that field.
SETTINGS_SCHEMA = {
    "location": {
        "name": _text,
        "latitude": _decimal(-90, 90, allow_none=True),
        "longitude": _decimal(-180, 180, allow_none=True),
        "timezone": _timezone,
    },
    "sources": _sources,
    "fusion": {"rule": _one_of(lambda: fusion.RULES)},
    "aqi_scale": _one_of(lambda: SCALES.keys()),
    # Per quantity, and enumerated from units.CONVERSIONS rather than listed
    # here, so adding inHg or knots does not need this line edited -- the
    # same reason the scales and fusion rules are read from their own module.
    # Absent means "follow the machine's region", which is the default and
    # needs no entry.
    "units": _units_override,
    "poll_minutes": _whole(2, 1440),
    "serve": _flag,
    "serve_port": _whole(1, 65535),
    "backfill_days_on_first_run": _whole(0, 3650),
    "retention_days": _whole(0),
    "data_dir": _data_dir,
    "source_failure_alert_after": _whole(1),
    "auto_backup": {
        "enabled": _flag,
        "keep": _whole(1, 1000),
        "interval_hours": _whole(1, 8760),
    },
    "alerts": {
        "enabled": _flag,
        "threshold_aqi": _decimal(0),
        "threshold_pm25": _decimal(0, allow_none=True),
        "rising_delta": _decimal(0),
        "cooldown_minutes": _whole(0),
        "notify_when_clear": _flag,
        "quiet_hours": _quiet_hours,
        "sound": _text,
    },
}


def validate_settings(patch, schema=None, path=""):
    """Check a partial settings update. Returns (clean, errors).

    Partial on purpose: a settings page saves one panel at a time, and asking
    it to send back the whole config to change a threshold is how a stale tab
    reverts everything else.

    Unknown fields are refused rather than passed through. On a write from a
    UI an unrecognised key is a typo or a stale client, and silently storing it
    means the user believes they changed something that does nothing.
    """
    schema = SETTINGS_SCHEMA if schema is None else schema
    clean, errors = {}, {}
    for key, value in (patch or {}).items():
        where = f"{path}{key}"
        rule = schema.get(key)
        if rule is None:
            errors[where] = f"{where}: not a setting"
            continue
        if isinstance(rule, dict):
            if not isinstance(value, dict):
                errors[where] = f"{where}: expected an object"
                continue
            sub, sub_errors = validate_settings(value, rule, f"{where}.")
            if sub:
                clean[key] = sub
            errors.update(sub_errors)
            continue
        try:
            clean[key] = rule(value, where)
        except Invalid as e:
            errors[where] = str(e)
    return clean, errors


def save_config(cfg, path=None):
    """Write config.json atomically, restricted.

    Shared with setup.py so the file lands the same way whoever wrote it: a
    temp file replaced into place, so an interrupted write cannot truncate a
    working config, and mode 600 because it holds a location and may hold a
    private sensor's key.
    """
    path = CONFIG_PATH if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_path(path.parent, is_dir=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    secure_path(path)
    return path


def apply_settings(patch):
    """Validate a patch and merge it into the stored config. Returns (cfg, errors).

    Nothing is written when anything is refused. A half-applied settings save
    leaves the user with a config they did not ask for and no way to tell which
    half took.
    """
    clean, errors = validate_settings(patch)
    if errors:
        return None, errors
    existing = load_config()
    if "sources" in clean:
        clean = dict(clean, sources=_keep_credentials(
            existing.get("sources") or [], clean["sources"]))
    cfg = _deep_merge(existing, clean)
    # A list is replaced, not merged -- _deep_merge only recurses into dicts,
    # so removing a source works. Stated because the opposite would be a
    # source the user cannot delete.
    save_config(cfg)
    return cfg, {}


def _keep_credentials(before, after):
    """Carry each source's stored credential across a settings save.

    The settings page rebuilds the whole `sources` list from what it was
    served, and what it is served deliberately omits credentials -- a page in
    a browser must never be handed a key. So every save wrote the list back
    *without* the read key of any private sensor, silently destroying it: the
    sensor kept polling until the next restart and then 404ed forever, with
    nothing in the config to explain why.

    A list is still replaced rather than merged, so removing a source works.
    Only the credential is carried, matched on provider and site id, and only
    where the incoming entry does not set one itself -- so changing a key
    through the one route that stores them still takes effect.
    """
    def key_of(src):
        return (str((src or {}).get("provider") or "").lower(),
                str((src or {}).get("site_id") or ""))

    stored = {key_of(s): s for s in before}
    out = []
    for src in after:
        merged = dict(src)
        previous = stored.get(key_of(src)) or {}
        for field in SECRET_FIELD_NAMES:
            if not merged.get(field) and previous.get(field):
                merged[field] = previous[field]
        out.append(merged)
    return out


def get_api_key(src):
    """Resolve one source's API key without ever printing it.

    Order: provider-specific env var, then ~/.airo/<provider>.key, then the
    legacy ~/.airo/apikey (PurpleAir only).

    Keys live outside the repo on purpose so a config file can be shared or
    committed without leaking one. Providers needing no key return "".
    """
    slug = str((src or {}).get("provider") or "purpleair").lower()
    provider = PROVIDERS.get(slug)
    if provider is not None and not provider.needs_key:
        return ""

    env_name = getattr(provider, "key_env", "") or "PURPLEAIR_API_KEY"
    key = os.environ.get(env_name, "").strip()
    if key:
        return key

    keydir = Path.home() / ".airo"
    candidates = [keydir / f"{slug}.key"]
    if slug == "purpleair":
        candidates.append(keydir / "apikey")  # pre-v0.4 location
    for keyfile in candidates:
        if keyfile.exists():
            key = keyfile.read_text(encoding="utf-8").strip()
            if key:
                return key
    return ""


def network_status(cfg):
    """Every network, whether it is usable, and how to enable it if not.

    Surfaced in --status, latest.json, the dashboard and the tray. A network
    the user could be reading but is not is worth mentioning wherever they
    already are -- burying it in a setup command they ran once means they
    never discover it.
    """
    configured = {str(s.get("provider")) for s in enabled_sources(cfg)}
    out = []
    for slug, prov in sorted(PROVIDERS.items()):
        has_key = (not prov.needs_key) or bool(get_api_key({"provider": slug}))
        out.append({
            "provider": slug,
            "label": prov.label,
            "tier": prov.tier,
            "accuracy_note": prov.accuracy_note,
            "resolution_minutes": prov.resolution_minutes,
            "needs_key": prov.needs_key,
            "has_key": has_key,
            "in_use": slug in configured,
            "signup_url": prov.key_url or None,
            "licence": prov.licence,
            "attribution": prov.attribution,
            "coverage_note": prov.coverage_note,
        })
    return out


# Field names that carry credentials. Used by scrub_secrets() as a backstop
# behind settings_payload(), which builds its output field by field.
#
# Two layers rather than one because the primary defence is a list a human has
# to remember to extend, and this project already found a credential somewhere
# nobody expected: `read_key` -- the key for a *private* PurpleAir sensor --
# lives per source inside config.json, not in ~/.airo/<provider>.key with every
# other key. Anything serialising a config to a browser has to know that.
SECRET_FIELD_NAMES = (
    "read_key", "api_key", "apikey", "key", "secret", "token", "password",
)


def scrub_secrets(obj):
    """Return a copy of `obj` with every credential replaced by a has_* flag.

    Whether a key is set is useful to a settings page. The key itself never is,
    so it is not sent and then hidden in the UI -- it does not leave the
    process. Nested dicts and lists are walked, because a credential one level
    down is still a credential.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in SECRET_FIELD_NAMES:
                out[f"has_{k}"] = bool(v)
            else:
                out[k] = scrub_secrets(v)
        return out
    if isinstance(obj, list):
        return [scrub_secrets(v) for v in obj]
    return obj


def settings_payload(cfg=None):
    """Everything a settings UI needs to render, and nothing it must not see.

    Built field by field rather than by dumping the config, for two reasons.
    The config carries a credential (see SECRET_FIELD_NAMES), and the page must
    never restate a list Python owns -- the fusion rules and the scales come
    from `fusion.RULES` and `SCALES`, so a rule added in Python appears in the
    UI without anyone editing HTML, and a rule removed cannot linger there.

    Values reported are the *effective* ones. A settings page showing a blank
    where alerting is using a default is worse than showing nothing: it invites
    the user to believe no threshold is set while one is firing.
    """
    cfg = load_config() if cfg is None else cfg
    scale_name, scale = get_scale(cfg)

    sources = []
    for src in (cfg.get("sources") or []):
        slug = str(src.get("provider") or "").lower()
        prov = PROVIDERS.get(slug)
        sources.append({
            "provider": src.get("provider"),
            "site_id": src.get("site_id"),
            "site_name": src.get("site_name"),
            # Round-tripped, or the page sends it back empty on every save and
            # an indoor sensor quietly becomes 'unknown' — which is excluded
            # from the outdoor headline either way, so the symptom is a sensor
            # that shows nothing rather than an obvious error.
            "placement": src.get("placement"),
            "latitude": src.get("latitude"),
            "longitude": src.get("longitude"),
            "enabled": bool(src.get("enabled", True)),
            # This source's own private-sensor key, reported as presence only.
            "has_read_key": bool(src.get("read_key")),
            "label": prov.label if prov else src.get("provider"),
            "tier": prov.tier if prov else None,
            "accuracy_note": prov.accuracy_note if prov else None,
            "resolution_minutes": prov.resolution_minutes if prov else None,
            "needs_key": prov.needs_key if prov else None,
            "has_key": bool(get_api_key(src)) if prov else False,
            "known_provider": prov is not None,
        })

    db = db_path()
    payload = {
        "location": dict(cfg.get("location") or {}),
        "sources": sources,
        "fusion": {"rule": (cfg.get("fusion") or {}).get("rule", fusion.DEFAULT_RULE)},
        "aqi_scale": scale_name,
        # Resolved here rather than in the page, for the same reason the
        # scales and the fusion rules are: only this process knows the
        # machine's region, and a second implementation in JavaScript is a
        # second thing to keep in step. `region` is reported so the settings
        # page can say *why* it chose what it chose -- "because your Mac says
        # en_US" is an answer somebody can act on, and "°F" on its own is not.
        "units": units.resolve(cfg),
        "units_region": units.region(),
        "units_configured": dict(cfg.get("units") or {})
                            if isinstance(cfg.get("units"), dict)
                            else cfg.get("units"),
        "unit_labels": {q: units.label(q, cfg=cfg) for q in units.QUANTITIES},
        "poll_minutes": cfg.get("poll_minutes"),
        "serve": bool(cfg.get("serve", True)),
        "serve_port": cfg.get("serve_port"),
        "backfill_days_on_first_run": cfg.get("backfill_days_on_first_run"),
        "retention_days": cfg.get("retention_days"),
        "auto_backup": dict(cfg.get("auto_backup") or {}),
        "source_failure_alert_after": cfg.get("source_failure_alert_after"),
        "alerts": effective_alerts(cfg),
        "data": {
            "data_dir": str(DATA),
            "data_dir_configured": cfg.get("data_dir") or None,
            "database": str(db),
            "database_exists": db.exists(),
            "database_bytes": db.stat().st_size if db.exists() else 0,
            # A data_dir pointed somewhere new leaves a full database behind
            # and starts an empty one. Name it here too, not only in --status.
            "other_databases": [{"path": str(p), "rows": rows}
                                for p, rows in other_databases()],
        },
        "networks": network_status(cfg),
        "choices": {
            "fusion_rules": list(fusion.RULES),
            # Enumerated from PROVIDERS so a network added later appears in
            # the page without an HTML edit — the same reason the scales and
            # the fusion rules are served rather than written into settings.html.
            "providers": [
                # `name`, not `value`. Every other list here uses {name,
                # label} and the page's `options()` helper reads `name` — so
                # serving `value` produced a select showing the right words
                # with an empty value behind them, and the form reported
                # "unknown network: (none)" while displaying "PurpleAir".
                {"name": slug, "label": p.label,
                 "needs_key": bool(p.needs_key),
                 "placement": p.default_placement}
                for slug, p in sorted(PROVIDERS.items(),
                                      key=lambda kv: kv[1].label)],
            "aqi_scales": [{"name": n, "label": s["label"]} for n, s in sorted(SCALES.items())],
            "alert_fields": sorted(ALERT_DEFAULTS),
        },
        # Configured *and* in force *and* the machine's, because no two of
        # those three answer the question on their own. A page showing only
        # the configured name is lying by omission on any machine that cannot
        # apply it, and one showing only the machine's cannot explain why
        # alerts arrive at the wrong hour.
        "timezone": {
            "configured": timezone_name(cfg),
            "in_force": (timezone_name(cfg)
                         if local_zone(cfg) is not None else ""),
            "machine": datetime.now().astimezone().tzname() or "",
            "database_available": timezone_database_available(),
            "note": " ".join(line.strip() for line in timezone_report(cfg)),
        },
        "config_path": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.exists(),
        "scale_label": scale["label"],
    }
    # Backstop. Everything above was written to be safe; this is what catches
    # the field somebody adds in a year without reading this docstring.
    return scrub_secrets(payload)


# ---------------------------------------------------------- finding monitors
#
# The logic lives here rather than in setup.py because there are two front ends
# now. setup.py keeps the narration -- the terminal wording, the progress
# lines -- and calls these for the decisions, the same split step 3 made for
# validation. A settings page that re-implemented "which station should we
# suggest" would be free to suggest a different one.

TIER_LABEL = {"reference": "reference", "indicative": "indicative",
              "consumer": "consumer"}

# Probing costs one HTTP call per site, so it is capped. Ordered by distance,
# so the cap spends its budget on the sites a user is actually likely to pick.
PROBE_LIMIT = 12


def _by_distance(sites):
    return sorted(sites, key=lambda s: (s.get("distance_km") is None,
                                        s.get("distance_km") or 0))


def geocode(place, limit=5):
    """Turn something a person typed into candidate coordinates.

    A street address, a suburb, a postcode -- whatever they know. Nobody knows
    their own latitude, and asking for one is asking the user to do the tool's
    job; the settings page and the wizard both send text here instead.

    Returns a list of {name, label, latitude, longitude}, nearest-match first
    and already normalised. Normalising *here* rather than in each front end is
    the point: Nominatim's shape (display_name, lat/lon as strings, an address
    dict whose keys vary by country) would otherwise be parsed twice, and the
    page and the wizard would disagree about what a place is called.

    Sent to OpenStreetMap Nominatim, which means the text leaves the machine.
    That is disclosed wherever this is offered, and typing coordinates directly
    still skips it entirely. Their policy asks for an identifying User-Agent
    and at most one request a second; both are respected.
    """
    place = (place or "").strip()
    if not place:
        return []

    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": place, "format": "json", "limit": int(limit), "addressdetails": 1,
    })
    req = urllib.request.Request(url, headers={
        "User-Agent": f"airo/{VERSION} (https://github.com/Donnishcomau/airo)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode("utf-8", errors="replace"))

    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        label = item.get("display_name") or place
        # A short name for the UI. Nominatim's address keys differ by country
        # -- a suburb is `suburb` here, `neighbourhood` or `city_district`
        # elsewhere -- so several are tried before falling back to the first
        # comma-separated part of the full label, which is always something.
        addr = item.get("address") or {}
        short = next((addr[k] for k in ("suburb", "neighbourhood", "village",
                                        "town", "city_district", "city",
                                        "municipality", "county")
                      if addr.get(k)), None)
        out.append({
            "name": short or label.split(",")[0].strip(),
            "label": label,
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
        })
    return out


def probe_source(slug, site_id, read_key=None, key=None):
    """Ask one specific sensor what it is, before anybody commits to it.

    Discovery answers "what is near me". This answers "is *this* the sensor,
    and what is it" — the question somebody has when they own one, or have a
    sensor index from a map and want that one rather than whatever is closest.

    Verifying first is the same discipline discovery already follows: the
    nearest station published nothing and was once picked on distance
    alone, and the first poll reported that every source had failed. Adding a
    sensor id that does not resolve produces the same silence a day later,
    when nobody is looking at the screen that would have explained it.

    Returns what was found, including where the sensor is, so the caller can
    show it rather than asking the user to assert it. `read_key` is used and
    never stored: storing a credential stays the business of `/api/keys`,
    which is still the only route that writes one to disk.
    """
    slug = str(slug or "").strip().lower()
    provider = PROVIDERS.get(slug)
    if provider is None:
        return {"ok": False,
                "error": f"unknown network: {slug or '(none)'}",
                "networks": sorted(PROVIDERS)}

    site_id = str(site_id or "").strip()
    if not site_id:
        return {"ok": False, "error": "a sensor id is required"}

    src = {"provider": slug, "site_id": site_id}
    if (read_key or "").strip():
        src["read_key"] = read_key.strip()

    try:
        measures, meta = provider.current(src, key if key is not None
                                          else get_api_key(src))
    except Exception as e:
        # The message is the product here. "404" tells somebody nothing; the
        # two things that actually go wrong are a mistyped id and a private
        # sensor with no read key, and the reply says so.
        detail = f"{type(e).__name__}: {e}"
        hint = ""
        if "404" in detail or "not found" in detail.lower():
            hint = (" — check the sensor id, and whether the sensor is "
                    "private (a private one needs its read key)")
        elif "403" in detail or "401" in detail:
            hint = (" — this sensor is private; add its read key, or check "
                    "the key is the right one for this sensor")
        return {"ok": False, "error": f"could not read {slug}/{site_id}{hint}",
                "detail": detail}

    measures, _rejected = clean_measures(measures)
    placement = placement_for(src, meta, provider)
    return {
        "ok": True,
        "provider": slug,
        "site_id": site_id,
        "site_name": meta.get("site_name") or site_id,
        "latitude": meta.get("latitude"),
        "longitude": meta.get("longitude"),
        "pm25": measures.get("headline") if measures.get("headline") is not None
                else measures.get("now"),
        "placement": placement or "unknown",
        # Said plainly, because it changes what the sensor is allowed to mean
        # and the user is about to decide whether to add it.
        "placement_note": _placement_note(placement),
        "needs_read_key": bool(src.get("read_key")),
        "resolution_minutes": provider.resolution_minutes,
    }


def _placement_note(placement):
    if placement == "indoor":
        return ("This is an indoor sensor. It will be shown and charted, and "
                "kept out of the outdoor headline, alerts and analysis.")
    if placement == "outdoor":
        return "An outdoor sensor. It can speak for the air outside."
    return ("Where this sensor is could not be established, so it is treated "
            "as indoor would be: shown, but kept out of the outdoor headline. "
            "Set it explicitly if you know.")


def discover_sites(location, radius_km, slugs):
    """Search the given providers. Returns (sites, failures), nearest first.

    Failures are returned rather than raised: one network being down is not a
    reason to show the user nothing from the others, but it is a reason to say
    so rather than let them read an empty list as "no monitors near you".
    """
    found, failures = [], {}
    for slug in sorted(slugs or []):
        provider = PROVIDERS.get(slug)
        if provider is None:
            failures[slug] = "unknown network"
            continue
        try:
            sites = provider.discover(location.get("latitude"),
                                      location.get("longitude"),
                                      radius_km, get_api_key({"provider": slug}))
        except Exception as e:
            failures[slug] = f"{type(e).__name__}: {e}"
            continue
        for s in sites:
            s["provider"] = slug
        found.extend(sites)
    return _by_distance(found), failures


def probe_reporting(site):
    """Does this site actually return a PM2.5 reading right now?

    True / False / None, where None means the probe itself failed and we
    should not hold that against the station.

    This exists because distance alone chose a broken install. Setup suggested
    the station nearest a test location -- which publishes nothing -- and the
    brand-new user's first poll returned "every source failed / No data".
    Several of the nearest stations publish no PM2.5 at all:
    they are NO2 or ozone sites, or simply offline. A calibrated monitor that
    reports nothing is worth less than a farther one that reports.
    """
    provider = PROVIDERS.get(site.get("provider"))
    if provider is None:
        return None
    try:
        key = get_api_key({"provider": provider.slug})
        measures, _meta = provider.current(
            {"provider": provider.slug, "site_id": site.get("site_id"),
             "site_name": site.get("site_name")}, key)
    except RuntimeError:
        return False                      # the provider's own "no data" signal
    except Exception:
        return None                       # network, auth, parse -- not the site
    if not isinstance(measures, dict):
        return None
    # Judge on the channels that produce the CURRENT reading, not on any value
    # at all. One site in the network publishes a real 24-hour average while its live
    # channel returns the -9999 sentinel: history exists, but every poll would
    # display "No data", which is the broken install this check is for.
    for key_name in ("headline", "now", "10min"):
        if pm25num(measures.get(key_name)) is not None:
            return True
    return False


def annotate_reporting(found, limit=PROBE_LIMIT):
    """Mark the nearest sites with whether they are actually reporting.

    Returns (sites, probed, dead). The counts are returned rather than printed
    so both front ends can say it in their own words -- and so a UI can report
    that the cap was reached instead of implying every site was checked.
    """
    ordered = _by_distance(found)
    for site in ordered[:limit]:
        site["reporting"] = probe_reporting(site)
    probed = min(len(ordered), limit)
    dead = sum(1 for s in ordered[:limit] if s.get("reporting") is False)
    return ordered, probed, dead


def recommend(found):
    """Suggest the nearest reference monitor plus the nearest consumer sensor.

    Not simply "the closest two". Closest and most accurate are different
    questions and often different instruments: a consumer sensor 1 km away
    describes your street but over-reads in humidity; a regulatory monitor
    8 km away is calibrated but may be on the wrong side of the terrain.
    Pairing one of each is what lets Airo tell "there is a fire next door"
    from "the whole city is smoky" -- which is the entire point.
    """
    # Sort here rather than trusting the caller. Picking "the first of each
    # tier" from an unsorted list silently suggests the wrong sites, and the
    # mistake is invisible -- a plausible site id looks exactly like the right
    # one.
    ordered = _by_distance(found)
    # A station known not to be reporting is never suggested, however close.
    # Unprobed and unknown both stay eligible: absence of a probe is not
    # evidence of a fault, and suggesting nothing is worse than suggesting a
    # site we could not check.
    live = [s for s in ordered if s.get("reporting") is not False]
    best = {}
    for s in (live or ordered):
        provider = PROVIDERS.get(s.get("provider"))
        tier = TIER_LABEL.get(getattr(provider, "tier", None), "consumer")
        if tier not in best:
            best[tier] = s
    return [best[t] for t in ("reference", "indicative", "consumer") if t in best]


def export_terms():
    """{provider: (attribution, licence)} from the live registry.

    store.py holds longer redistribution wording per provider, but as a table
    -- and a table is a list somebody has to remember to extend. A network
    added without an entry exported with no attribution line at all, which for
    a CC BY feed omits the one thing the licence requires. This is enumerated
    from PROVIDERS, so a new provider carries its own terms into every export
    whether or not anyone remembered the table.
    """
    return {slug: (p.attribution, p.licence) for slug, p in PROVIDERS.items()}


def missing_keys(cfg):
    """Sources that need a key and haven't got one. Used by status and install."""
    out = []
    for src in enabled_sources(cfg):
        slug = str(src.get("provider") or "purpleair").lower()
        provider = PROVIDERS.get(slug)
        if provider is not None and provider.needs_key and not get_api_key(src):
            out.append(src)
    return out


def aqi_for(pm25, scale_name=DEFAULT_SCALE):
    """Convert raw ug/m3 to the configured index. None in, None out."""
    if pm25 is None:
        return None
    # A negative concentration is a sentinel, not a low reading. Returning
    # None here means band_for() says "No data" instead of "Very good".
    if pm25 < 0:
        return None
    scale = SCALES.get(scale_name) or SCALES[DEFAULT_SCALE]

    if scale.get("identity"):
        return round(pm25, 1)

    if "linear_standard" in scale:
        return round(pm25 / scale["linear_standard"] * 100, 1)

    # Piecewise linear interpolation across breakpoints.
    bps = scale["breakpoints"]
    for lo_pm, hi_pm, lo_aqi, hi_aqi, _ in bps:
        if pm25 <= hi_pm:
            # Clamp below the first segment's floor rather than going negative.
            pm = max(pm25, lo_pm) if pm25 >= lo_pm else lo_pm
            span = hi_pm - lo_pm
            if span <= 0:
                return float(lo_aqi)
            return round(lo_aqi + (hi_aqi - lo_aqi) * (pm - lo_pm) / span, 1)
    # Above the top breakpoint the index is undefined; report the ceiling.
    return float(bps[-1][3])


def explain_headline(scale_name, scale, pm25, index, chosen, rule):
    """How the headline index was arrived at, in terms a reader can check.

    Every field is something the number was actually computed from, so the
    explanation cannot drift away from the figure it explains -- the arithmetic
    shown is the arithmetic done.

    `formula` is None for a piecewise scale, where there is no single sum to
    quote. Saying so is the honest answer; inventing an average factor to have
    something to print would be a worse one, and it is the same reasoning as
    ug_per_index() returning None there.
    """
    site = (chosen or {}).get("site_name") or (chosen or {}).get("site_id")
    standard = scale.get("linear_standard")
    worked = None
    if scale.get("identity"):
        formula = None
        basis = "the raw concentration, with no index applied"
    elif standard:
        formula = f"index = µg/m³ × 100 ÷ {standard:g}"
        basis = scale.get("note")
        if pm25 is not None and index is not None:
            worked = (f"{pm25:g} × 100 ÷ {standard:g} = {round(index)}")
    else:
        formula = None                  # piecewise: no single factor exists
        basis = scale.get("note")

    return {
        "index": None if index is None else round(index),
        "pm25": pm25,
        "concentration_unit": "µg/m³",
        "scale_label": scale.get("label"),
        "formula": formula,
        # The sum with this reading's own numbers in it. Served rather than
        # assembled in the page, because the page would have to know the
        # scale's constant to write it -- and 25 is the Australian one. A US
        # EPA install is piecewise and has no such constant at all, which is
        # exactly the drift that put the Australian bands on a US install once
        # already (ARCHITECTURE §3).
        "worked": worked,
        "basis": basis,
        "from_source": site,
        "rule": rule,
        # The sentence the question actually needed. Three sources are listed
        # under a single number, and nothing said the number came from one of
        # them.
        "is_an_average_of_sources": False,
        "rule_note": (
            f"the reading from {site}, not an average of your sources"
            if site else "one source's reading, not an average"),
    }


def scale_bands(scale_name=DEFAULT_SCALE):
    """The bands of a scale, as [{"max": float, "name": str, "advice"?: str}].

    Served to the dashboard so it stops carrying its own copy. It had two --
    one for the colours and one for the chart background -- and both were the
    Australian numbers regardless of the configured scale, so a US EPA install
    read its index against Australian boundaries.

    `advice` is present only where the scale's table names one. The key is
    omitted rather than set to null: a surface tests for it, and a null would
    have to be filtered by every consumer to avoid rendering the word "null"
    under a band label. A scale with no advice is not a scale whose advice is
    empty -- it is one whose wording nobody has written yet, and the reader is
    better served by the band name alone than by a sentence meant for other
    air. See D8 and rule 7: this is the one place that decision is made.
    """
    def ceiling(value):
        """The top band has no ceiling. `null` says so; infinity cannot.

        json.dumps writes infinity as the bare literal `Infinity`, which is
        not JSON -- Python reads it back happily and every other parser
        refuses the file. Consumers treat a null max as unbounded.
        """
        value = float(value)
        return None if value == float("inf") else value

    def band(limit, name, advice=None):
        """One served band. The advice key exists or it does not."""
        out = {"max": ceiling(limit), "name": name}
        if advice:
            out["advice"] = advice
        return out

    scale = SCALES.get(scale_name) or SCALES[DEFAULT_SCALE]
    if "breakpoints" in scale:
        # A breakpoint row carries no advice slot, so a piecewise scale serves
        # none. Adding one is a table edit here, not a change of shape.
        return [band(hi_aqi, name)
                for _, _, _, hi_aqi, name in scale["breakpoints"]]
    return [band(*entry) for entry in scale["bands"]]


def ug_per_index(scale_name=DEFAULT_SCALE):
    """µg/m³ per one index point, or None where the scale is not linear.

    Exists so a UI can label an index with the measurement behind it without
    reimplementing the conversion -- and, more importantly, so it cannot get it
    wrong. The dashboard did exactly that: it multiplied by a hardcoded
    Australian standard of 25 in three places, whatever scale was configured.
    A US EPA install saw air of 30 µg/m³ described as 22.5, and a `raw` install
    saw it described as 7.5 -- both understatements, on the number a reader is
    most likely to check against a guideline.

    None is the honest answer for a piecewise scale, not a fallback: US EPA
    breakpoints have a different slope in every band, so no single factor
    exists. Callers must omit the figure rather than print an approximation --
    the exact value is already published in `averages_pm25` and per series
    point, and rule 6 keeps it that way.
    """
    scale = SCALES.get(scale_name) or SCALES[DEFAULT_SCALE]
    if scale.get("identity"):
        return 1.0                      # the index *is* µg/m³
    if "linear_standard" in scale:
        return scale["linear_standard"] / 100.0
    return None


def band_for(aqi, scale_name=DEFAULT_SCALE):
    """Name the band an index value falls in.

    Rounds before deciding, because the band must match the number the user is
    shown: 33.2 displays as "33" and has to be coloured as 33. This used to be
    an instruction in the docstring -- "callers must pass the displayed value"
    -- which is a rule that gets broken, and was, in eight places in the
    dashboard alone. ARCHITECTURE §3.4.
    """
    if aqi is None:
        return "No data"
    aqi = round(aqi)
    bands = scale_bands(scale_name)
    for band in bands:
        # A null ceiling is the open-ended top band, so everything lands in it.
        if band["max"] is None or aqi <= band["max"]:
            return band["name"]
    return bands[-1]["name"]


def http_get(url, key, as_text=False, timeout=30):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/csv" if as_text else "application/json",
    }
    # Keyless providers (government open-data feeds) must not be sent an
    # X-API-Key header at all -- urllib rejects a None value outright.
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return raw if as_text else json.loads(raw)


def to_celsius(value, unit):
    """Normalise a temperature to Celsius. None in, None out.

    Providers disagree: PurpleAir reports Fahrenheit, regulatory feeds report
    Celsius. Storing both in one column with the unit merely implied is how a
    cross-source comparison silently produces nonsense.
    """
    if value is None:
        return None
    if str(unit or "").upper().startswith("F"):
        return round((value - 32.0) * 5.0 / 9.0, 1)
    return value


def http_post_json(url, payload, key=None, timeout=30):
    """POST JSON and parse the JSON reply.

    Only NSW needs this so far; its observations endpoint takes a query
    document rather than query parameters.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def fnum(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# Government feeds signal "instrument offline" with an out-of-range sentinel
# rather than a null. Queensland uses -9999. Ingested as a reading it is not
# merely wrong, it is dangerous: on the Australian scale -9999 ug/m3 becomes
# AQI -39996, which falls below the first breakpoint and renders as
# "Very good" -- the most reassuring label the tool has, shown for air nobody
# measured. Observed live on two stations in the Queensland network; the
# fixtures that reproduce it use the synthetic slugs 'sou' and 'wbk'.
#
# PM2.5 is a mass concentration, so a negative value is not a low reading; it
# is the absence of one. Rule 5a says never silently discard a *reading* --
# this is not one, and it is reported as a fault rather than dropped quietly.
def pm25num(v):
    """Parse a PM2.5 value, rejecting sentinels and impossible magnitudes."""
    n = fnum(v)
    if n is None:
        return None
    if n < 0:
        return None
    return n


def clean_measures(measures):
    """Strip sentinel PM2.5 values from a provider's measures dict.

    Applied once at the provider boundary so every provider is covered,
    including any added later that forgets the guard. Returns (cleaned,
    rejected) where `rejected` names the keys that held a sentinel, so the
    caller can say so out loud.
    """
    if not isinstance(measures, dict):
        return measures, []
    cleaned, rejected = dict(measures), []
    for key, value in measures.items():
        if key in ("humidity", "temperature"):
            continue                      # genuinely negative below freezing
        n = fnum(value)
        if n is not None and n < 0:
            cleaned[key] = None
            rejected.append(key)
    return cleaned, rejected


# ------------------------------------------------------------------ csv store

def legacy_csv_present():
    """True if a pre-v0.5 single-source readings.csv is still on disk.

    The CSV write path is gone -- SQLite is the operational store now -- but
    an existing install's history lives in that file and must be imported
    before it is ignored. `--migrate-csv` does that; status nags until it has.
    """
    return CSV_PATH.exists()


def migrate_data_dir(dry_run=False, source=None):
    """Move readings into the directory Airo is currently using.

    Copies rather than moves, and only removes the original once the copy has
    been verified readable with the same row count. Losing a year of readings
    to a half-finished move would be unforgivable, and "it seemed to work" is
    not verification.

    The source defaults to the abandoned database with the most readings, then
    to the pre-v0.6 project folder. That default matters: this command is what
    the orphan warning tells people to run, and it used to look only at the
    project folder — so anyone who had moved data_dir was told to run a
    command that reported "nothing to migrate" and left their readings exactly
    where they were.
    """
    target = DATA

    if source is None:
        orphans = other_databases()
        if orphans:
            source = max(orphans, key=lambda pair: pair[1])[0]
        else:
            source = LEGACY_DATA
    source = Path(source)

    if source.resolve() == target.resolve():
        log(f"nothing to migrate: {source} is already the directory in use")
        return False
    if not (source / "airo.db").exists():
        log(f"nothing to migrate: no database at {source}")
        return False
    if (target / "airo.db").exists():
        # Both hold data. Merging is a different, riskier operation than a
        # move, so it is refused rather than guessed at.
        here = _db_row_count(target / "airo.db")
        there = _db_row_count(source / "airo.db")
        log(f"both directories hold a database — not merging automatically")
        log(f"  in use : {target} ({here:,} readings)")
        log(f"  orphan : {source} ({there:,} readings)")
        log("  Move the one you want to keep aside, then run this again.")
        log("  Both are plain SQLite; nothing has been changed.")
        return False

    before = _db_row_count(source / "airo.db")
    log(f"migrating {before:,} readings")
    log(f"  from {source}")
    log(f"  to   {target}")
    if dry_run:
        log("dry run — nothing changed")
        return False

    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)

    # Copy everything, including the WAL and shared-memory files: copying only
    # the .db of a database in WAL mode can leave recent writes behind.
    copied = []
    for item in sorted(source.iterdir()):
        if item.is_dir():
            continue
        shutil.copy2(item, target / item.name)
        copied.append(item.name)
    log(f"copied {len(copied)} file(s)")

    after = _db_row_count(target / "airo.db")
    if after != before:
        log(f"ERROR row count differs after copy ({before:,} -> {after:,})")
        log(f"      leaving the original at {source} untouched")
        return False
    log(f"verified {after:,} readings at the new location")

    # Only now is the original redundant. Renamed, not deleted -- the user can
    # remove it once they are satisfied.
    retired = source.with_name("data.migrated-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    source.rename(retired)
    log(f"original kept at {retired}")
    log("  delete it once you are happy; nothing reads it any more")
    return True


def _db_row_count(db_path):
    conn = store.connect(db_path)
    try:
        return sum(c["rows"] for c in store.counts(conn))
    finally:
        conn.close()


def migrate_legacy_csv(cfg, force=False):
    """Import a pre-v0.5 readings.csv into the database.

    Idempotent: re-running adds nothing, because the store dedups on
    (source, observed_utc). The CSV is left untouched on disk -- deleting a
    user's only copy of their history is not ours to do.
    """
    if not CSV_PATH.exists():
        log("no legacy readings.csv to migrate")
        return 0

    srcs = enabled_sources(cfg)
    if not srcs:
        raise SystemExit(
            "Cannot migrate: no sources configured. The legacy CSV holds one "
            "source's readings but does not record which, so add the source "
            "it came from to config.json first.")

    # The old file predates multi-source, so its rows belong to whichever
    # source was configured at the time -- the first one listed.
    src = srcs[0]
    provider = get_provider(src)
    conn = open_store()
    try:
        added, skipped = store.migrate_from_csv(
            conn, CSV_PATH,
            provider=provider.slug,
            site_id=src.get("site_id"),
            site_name=src.get("site_name"),
            latitude=src.get("latitude"),
            longitude=src.get("longitude"),
            resolution_minutes=provider.resolution_minutes,
        )
        log(f"migrated {added} rows from {CSV_PATH.name} into "
            f"{provider.slug}/{src.get('site_id')}"
            + (f" ({skipped} skipped)" if skipped else ""))
        return added
    finally:
        conn.close()


def write_json_atomic(path, obj):
    """Write JSON that a strict parser will accept.

    allow_nan=False is the point. Python's json emits `Infinity` and `NaN` as
    bare literals by default -- its own parser reads them back, so nothing
    looks wrong from here -- but they are not JSON, and every other language
    rejects the whole file. That is exactly what happened: a band ceiling of
    infinity went into latest.json, serde_json refused to parse it, and the
    tray showed "No reading yet" beside a database full of readings. A partial
    failure that reads as no data at all.

    Raising here is deliberate. A poll that cannot produce valid output should
    fail loudly rather than write a file the tray silently cannot read.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- providers
#
# A provider knows how to answer two questions about one monitoring site:
#   current(cfg, key) -> (measures, meta)   what is it reading now
#   history(cfg, key, start, end) -> [obs]  what did it read between two times
#
# Everything else -- scales, CSV shape, gap detection, alerting -- is provider
# agnostic and lives outside this section. Adding a country means adding one
# class here and nothing else.
#
# Providers deliberately differ in native resolution. PurpleAir publishes a
# 10-minute rolling average; regulatory networks publish hourly. Gap detection
# has to respect that or it will chase gaps that can never be filled, so every
# provider declares resolution_minutes.

class Provider:
    """Base class. Subclasses override slug/label and the two fetch methods."""

    slug = ""
    label = ""
    resolution_minutes = 10

    # How much the readings can be trusted in absolute terms. This is not
    # snobbery about consumer hardware -- it decides what Airo tells the user
    # to believe when two instruments disagree.
    #
    #   reference  Government regulatory monitor. Calibrated, maintained,
    #              legally traceable. Sparse, and often nowhere near you.
    #   indicative Lower-cost government or research instrument. Reliable
    #              trend, looser absolute accuracy.
    #   consumer   Community optical sensor. Dense and genuinely local, but
    #              over-reads 20-40% at high humidity and can fail in ways
    #              that look like real data.
    tier = "consumer"
    accuracy_note = ""

    #: Where this network's instruments are, when nothing more specific is
    #: known. A regulatory monitor is outdoor by definition -- a government
    #: agency does not site a compliance station in a kitchen -- and so is an
    #: aggregator of them.
    #:
    #: `None` means "this network varies, ask the sensor". Only PurpleAir does,
    #: and it answers through `location_type`.
    #:
    #: Declared on the class for the same reason `temperature_unit` is: the
    #: alternative is deciding it from the slug at each call site, which is the
    #: check-written-as-a-list shape this project keeps being bitten by.
    default_placement = "outdoor"

    #: The unit this provider's `current()` and `history()` report temperature
    #: in. Declared on the class rather than decided at each call site: the
    #: CSV importer used `"F" if provider == "purpleair" else "C"`, and a
    #: check written as a list stops covering the moment somebody adds
    #: something -- a second Fahrenheit network would have been silently wrong.
    #: `to_celsius()` normalises on the way in; nothing downstream should ever
    #: see this value.
    temperature_unit = "C"

    # Where this network actually has instruments. A state government feed
    # covers one state; offering it to someone in another is a dead end that
    # looks like the tool being broken. None means global.
    #
    # (south, west, north, east) in degrees.
    coverage_box = None
    coverage_note = "worldwide"

    needs_key = True
    key_env = ""
    key_url = ""
    attribution = ""
    licence = ""
    # Rolling averages this provider publishes directly. Anything not listed
    # is left empty in the CSV rather than being computed and passed off as
    # the sensor's own figure.
    publishes_averages = ()

    def current(self, src, key):
        raise NotImplementedError

    def history(self, src, key, start, end):
        raise NotImplementedError

    def covers(self, latitude, longitude):
        """Could this network plausibly have anything near here?

        A cheap geographic filter, not a promise -- discover() is still the
        authority. It exists so setup does not offer a Queensland feed to
        someone in Tasmania and then hunt outward to 200 km finding nothing.
        """
        if self.coverage_box is None:
            return True
        if latitude is None or longitude is None:
            return True
        south, west, north, east = self.coverage_box
        return south <= latitude <= north and west <= longitude <= east

    def discover(self, latitude, longitude, radius_km, key):
        """Find this provider's monitoring sites near a point.

        Returns [{site_id, site_name, latitude, longitude, distance_km}],
        nearest first. This is what makes setup a matter of "where do you
        live" rather than "paste a sensor id you found on a map".

        Providers that cannot search by location return [].
        """
        return []


#: PurpleAir's `location_type` as this project's vocabulary. Their API
#: documents 0 as outside and 1 as inside; anything else is a value that did
#: not exist when this was written, and guessing at it is how a new code
#: becomes a wrong answer rather than an admitted gap.
PURPLEAIR_PLACEMENT = {0: "outdoor", 1: "indoor"}


def purpleair_placement(value):
    """PurpleAir's location_type as a placement, or None if it did not say.

    None rather than 'unknown': the two mean different things to
    `upsert_source`, where None leaves a stored answer alone and 'unknown'
    would be an assertion. A field the API omitted is not a claim that nobody
    knows where the sensor is.
    """
    if value is None:
        return None
    try:
        return PURPLEAIR_PLACEMENT.get(int(value), "unknown")
    except (TypeError, ValueError):
        return "unknown"


class PurpleAirProvider(Provider):
    """PurpleAir consumer sensors. Hyperlocal, 10-minute, bring your own key."""

    slug = "purpleair"
    label = "PurpleAir"
    tier = "consumer"
    accuracy_note = ("community optical sensor; over-reads 20-40% at high "
                     "humidity, but genuinely local")
    resolution_minutes = 10
    needs_key = True
    temperature_unit = "F"   # its API reports Fahrenheit
    # The only network where it varies: anyone can put one anywhere, and a
    # great many are indoors. Asked of the sensor rather than assumed.
    default_placement = None
    key_env = "PURPLEAIR_API_KEY"
    key_url = "https://develop.purpleair.com"
    coverage_note = "worldwide, wherever someone has installed a sensor"
    attribution = "Powered by PurpleAir"
    licence = "PurpleAir ToS -- do not redistribute raw data (S4.3)"
    publishes_averages = ("now", "30min", "60min", "6hr", "24hr", "1week")

    API_BASE = "https://api.purpleair.com/v1"
    FIELDS = ",".join([
        "name", "latitude", "longitude", "altitude", "last_seen",
        # Where the sensor is, asked of the API rather than of the user. A
        # setup question about "location type" is answered wrongly by somebody
        # who has just unboxed a sensor, and getting it wrong puts a kitchen
        # reading under "avoid outdoor exertion".
        "location_type",
        "pm2.5", "pm2.5_10minute", "pm2.5_30minute", "pm2.5_60minute",
        "pm2.5_6hour", "pm2.5_24hour", "pm2.5_1week",
        # Per-channel values and the sensor's own confidence. A PurpleAir has
        # two laser counters; when they disagree the instrument is faulty or
        # obstructed, which is the most reliable fault signal available.
        "pm2.5_a", "pm2.5_b", "confidence", "channel_flags", "channel_state",
        "humidity", "temperature", "pressure", "rssi", "uptime",
    ])

    def _site_id(self, src):
        return src.get("site_id")

    def current(self, src, key):
        idx = self._site_id(src)
        q = {"fields": self.FIELDS}
        if src.get("read_key"):
            q["read_key"] = src["read_key"]
        url = f"{self.API_BASE}/sensors/{idx}?" + urllib.parse.urlencode(q)
        payload = http_get(url, key)
        s = payload.get("sensor") or {}

        # PurpleAir nests the rolling averages in a "stats" object on the
        # single-sensor endpoint; only the instantaneous pm2.5 is reliably at
        # the top level. Look in stats first, then top level, then per-channel.
        stats = s.get("stats") or {}
        stats_a = s.get("stats_a") or {}

        def field(name):
            for source in (stats, s, stats_a):
                v = fnum(source.get(name))
                if v is not None:
                    return v
            return None

        # Prefer the 10-minute per-channel averages, which live in stats_a /
        # stats_b, over the instantaneous top-level pm2.5_a / pm2.5_b.
        stats_b = s.get("stats_b") or {}
        ch_a = fnum(stats_a.get("pm2.5_10minute"))
        if ch_a is None:
            ch_a = fnum(s.get("pm2.5_a"))
        ch_b = fnum(stats_b.get("pm2.5_10minute"))
        if ch_b is None:
            ch_b = fnum(s.get("pm2.5_b"))

        # The 10-minute average is what we want: an instantaneous optical
        # reading swings hard on a passing car. When it is absent, fall back
        # to the instantaneous value rather than reporting nothing -- but say
        # so, because a noisier number presented as the usual one is the kind
        # of quiet substitution rule 5a exists to prevent. ARCHITECTURE §3.1
        # has described this since v0.4; it was documented and not implemented,
        # so a sensor without the average silently reported no data at all.
        headline = field("pm2.5_10minute")
        headline_is_fallback = False
        if headline is None:
            headline = field("pm2.5")
            headline_is_fallback = headline is not None

        measures = {
            "headline": headline,
            "now": field("pm2.5"),
            "pm25_a": ch_a,
            "pm25_b": ch_b,
            "confidence": fnum(s.get("confidence")),
            "30min": field("pm2.5_30minute"),
            "60min": field("pm2.5_60minute"),
            "6hr": field("pm2.5_6hour"),
            "24hr": field("pm2.5_24hour"),
            "1week": field("pm2.5_1week"),
            "humidity": fnum(s.get("humidity")),
            "temperature": fnum(s.get("temperature")),
        }
        meta = {
            "placement": purpleair_placement(s.get("location_type")),
            "site_id": idx,
            "site_name": s.get("name") or src.get("site_name"),
            "latitude": fnum(s.get("latitude")),
            "longitude": fnum(s.get("longitude")),
            "last_seen_utc": (
                datetime.fromtimestamp(s["last_seen"], timezone.utc)
                .isoformat(timespec="seconds") if s.get("last_seen") else None
            ),
            "temperature_unit": "F",  # PurpleAir reports Fahrenheit
            # Carried into latest.json so every surface can mark the number as
            # the noisier instantaneous one rather than presenting it as the
            # 10-minute average it usually is.
            "headline_is_fallback": headline_is_fallback,
        }
        return measures, meta

    def history(self, src, key, start, end):
        idx = self._site_id(src)
        obs = []
        # PurpleAir limits how much you can pull per call at a given average.
        # 10-minute averages: chunk into 2-day windows to stay inside limits.
        cur = start
        while cur < end:
            stop = min(cur + timedelta(days=2), end)
            q = {
                "start_timestamp": int(cur.timestamp()),
                "end_timestamp": int(stop.timestamp()),
                "average": 10,
                "fields": "pm2.5_atm,humidity,temperature",
            }
            if src.get("read_key"):
                q["read_key"] = src["read_key"]
            url = f"{self.API_BASE}/sensors/{idx}/history/csv?" + urllib.parse.urlencode(q)
            try:
                text = http_get(url, key, as_text=True)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                log(f"WARN backfill {cur:%Y-%m-%d} HTTP {e.code} {body}")
                cur = stop
                continue
            except Exception as e:
                log(f"WARN backfill {cur:%Y-%m-%d} {type(e).__name__}: {e}")
                cur = stop
                continue

            # Parse by header name; PurpleAir's column order is not guaranteed.
            for rec in csv.DictReader(io.StringIO(text)):
                ts = rec.get("time_stamp") or rec.get("timestamp")
                pm = rec.get("pm2.5_atm") or rec.get("pm2.5_atm_a") or rec.get("pm2.5")
                tsn, pmn = fnum(ts), fnum(pm)
                if tsn is None or pmn is None:
                    continue
                when = datetime.fromtimestamp(int(tsn), timezone.utc)
                # PurpleAir returns the averaging bucket containing the
                # boundary, so a request for exactly N days comes back with a
                # reading fractionally outside it. Harmless for backfill, but
                # every provider must honour the same contract or gap
                # detection reasons about a different window per source.
                if not (start <= when <= end):
                    continue
                obs.append({
                    "utc": when,
                    "pm25": pmn,
                    "humidity": fnum(rec.get("humidity")),
                    "temperature": fnum(rec.get("temperature")),
                })
            cur = stop
            time.sleep(1)  # be polite to the API
        obs.sort(key=lambda o: o["utc"])
        return obs


    def discover(self, latitude, longitude, radius_km, key):
        # PurpleAir searches by bounding box, not radius. Convert: one degree
        # of latitude is ~111 km; longitude shrinks with the cosine of the
        # latitude, which matters a great deal away from the equator.
        dlat = radius_km / 111.0
        dlon = radius_km / max(1.0, 111.0 * math.cos(math.radians(latitude)))
        q = {
            "fields": "name,latitude,longitude,last_seen",
            "location_type": 0,          # outdoor only
            "max_age": 3600,             # reported within the last hour
            "nwlat": latitude + dlat, "nwlng": longitude - dlon,
            "selat": latitude - dlat, "selng": longitude + dlon,
        }
        payload = http_get(f"{self.API_BASE}/sensors?" + urllib.parse.urlencode(q), key)
        fields = payload.get("fields") or []
        out = []
        for row in payload.get("data") or []:
            rec = dict(zip(fields, row))
            lat, lon = fnum(rec.get("latitude")), fnum(rec.get("longitude"))
            if lat is None or lon is None:
                continue
            out.append({
                "site_id": rec.get("sensor_index"),
                "site_name": rec.get("name") or str(rec.get("sensor_index")),
                "latitude": lat, "longitude": lon,
                "distance_km": fusion.haversine_km(latitude, longitude, lat, lon),
            })
        out.sort(key=lambda r: r["distance_km"])
        return out


# Queensland does not observe daylight saving, so its offset is a constant.
# Written down rather than inferred from the machine, which is the difference
# between a reading that means the same thing everywhere and one that depends
# on where it was fetched from.
QLD_TIMEZONE = timezone(timedelta(hours=10))


class QldProvider(Provider):
    """Queensland Government regulatory network.

    No API key, CC BY 4.0, hourly. This is the reference implementation for a
    direct government feed -- copy it to add NSW, VIC or another jurisdiction.
    Verified live against the published OpenAPI spec.
    """

    slug = "qld"
    label = "Queensland Government air monitoring"
    tier = "reference"
    accuracy_note = "calibrated regulatory monitor, hourly"
    resolution_minutes = 60
    needs_key = False
    attribution = "Contains Queensland Government data, CC BY 4.0"
    licence = "CC BY 4.0"
    coverage_box = (-29.5, 137.9, -9.0, 154.0)      # Queensland
    coverage_note = "Queensland, Australia"
    publishes_averages = ("24hr",)

    API_BASE = "https://airquality.des.qld.gov.au/v1"
    PM25_PARAMETER_ID = 31

    def current(self, src, key):
        station = str(src.get("site_id") or "").lower()
        # Ask for the last couple of days rather than the whole feed, then take
        # the newest. Ordering is not guaranteed, so sort rather than assume.
        since = datetime.now(timezone.utc) - timedelta(days=2)
        rows = self._measurements(station, {
            "start_date": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        if not rows:
            raise RuntimeError(f"no recent PM2.5 measurements for station {station!r}")
        rec = sorted(rows, key=lambda r: str(r.get("date_measured") or ""))[-1]

        measures = {
            "headline": fnum(rec.get("mvalue")),
            "now": fnum(rec.get("mvalue")),
            "24hr": fnum(rec.get("mvalue_running_avg")),
            "humidity": None,
            "temperature": None,
        }
        # Coordinates matter beyond decoration: the default fusion rule is
        # "nearest", and a source with no stored position is ranked behind
        # every source that has one -- so the closest instrument can be
        # silently passed over in favour of a farther one. That happens to any
        # source added by editing the config rather than through setup, since
        # only discover() was returning coordinates.
        lat, lon = self._station_position(station)
        meta = {
            "site_id": station,
            "site_name": src.get("site_name") or station,
            "last_seen_utc": self._iso(rec.get("date_measured")),
            "temperature_unit": "C",
            "latitude": lat,
            "longitude": lon,
        }
        return measures, meta

    # The station list is a few dozen fixed sites; fetching it per poll would
    # be a wasted request every time. Cached for the life of the process, which
    # is seconds -- the poller runs and exits.
    _STATIONS = None

    @classmethod
    def _station_position(cls, station):
        """Latitude and longitude for a station id, or (None, None)."""
        if cls._STATIONS is None:
            try:
                rows = http_get(f"{cls.API_BASE}/stations", key=None)
            except Exception:
                return None, None
            cls._STATIONS = {
                str(r.get("station_id") or "").lower():
                    (fnum(r.get("latitude")), fnum(r.get("longitude")))
                for r in (rows if isinstance(rows, list) else [])
            }
        return cls._STATIONS.get(str(station).lower(), (None, None))

    @staticmethod
    def _iso(s):
        """A Queensland timestamp as UTC.

        The feed publishes local time with no offset, and a naive datetime
        passed to astimezone() is interpreted in the *machine's* zone. That is
        right in Brisbane and wrong by the reader's offset anywhere else: the
        same reading landed at a different instant depending on who fetched
        it, which quietly misplaces every evening comparison for a user
        outside Queensland.

        Queensland does not observe daylight saving, so its offset is a
        constant +10:00 -- one of the few places where that is safe to assume.
        A timestamp that arrives *with* an offset is trusted as given.
        """
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=QLD_TIMEZONE)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _measurements(self, station, params):
        """Query the measurements endpoint, following pagination.

        The date parameters are start_date/end_date. Anything else -- an
        earlier draft used from_date/to_date -- is silently ignored by the
        API, which quietly returns the most recent 1000 rows instead of the
        window you asked for. Wrong data, no error.
        """
        # The API accepts pagesize up to 10,000 but reliably times out well
        # before that. 2,000 rows is ~12 weeks of hourly data per request and
        # returns comfortably inside the timeout.
        page_size = 2000
        page, out = 1, []
        while True:
            q = dict(params, pagesize=page_size, pagenumber=page)
            url = (f"{self.API_BASE}/stations/{urllib.parse.quote(station)}"
                   f"/parameters/{self.PM25_PARAMETER_ID}/measurements?"
                   + urllib.parse.urlencode(q))
            batch = http_get(url, key=None, timeout=60)
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
            if page > 60:  # ~120k rows; a runaway guard, not a real limit
                log("WARN qld pagination stopped at 60 pages")
                break
            time.sleep(0.5)  # be polite to a public service
        return out

    def history(self, src, key, start, end):
        station = str(src.get("site_id") or "").lower()
        try:
            rows = self._measurements(station, {
                "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except Exception as e:
            log(f"WARN backfill qld {type(e).__name__}: {e}")
            return []

        obs = []
        for rec in rows:
            iso, pm = self._iso(rec.get("date_measured")), fnum(rec.get("mvalue"))
            if iso is None or pm is None:
                continue
            when = datetime.fromisoformat(iso)
            # The API filters by date, not by instant, so a request for a
            # partial day returns the whole of it. Bound it here so every
            # provider honours the same contract.
            if not (start <= when <= end):
                continue
            obs.append({
                "utc": when,
                "pm25": pm,
                "humidity": None,
                "temperature": None,
            })
        obs.sort(key=lambda o: o["utc"])
        return obs


    def discover(self, latitude, longitude, radius_km, key):
        stations = http_get(f"{self.API_BASE}/stations", key=None)
        out = []
        for st in stations if isinstance(stations, list) else []:
            lat, lon = fnum(st.get("latitude")), fnum(st.get("longitude"))
            if lat is None or lon is None:
                continue
            d = fusion.haversine_km(latitude, longitude, lat, lon)
            if d is not None and d <= radius_km:
                out.append({
                    "site_id": st.get("station_id"),
                    "site_name": st.get("station_name") or st.get("station_id"),
                    "latitude": lat, "longitude": lon, "distance_km": d,
                })
        out.sort(key=lambda r: r["distance_km"])
        return out


class OpenAQProvider(Provider):
    """OpenAQ v3 -- global aggregator of regulatory monitors.

    One adapter covers Australia, the US, Canada, South Africa, the UK,
    Germany, France, Ireland and the Philippines. Requires a free API key.

    Licensing is per-source, not blanket: OpenAQ aggregates networks with
    differing terms and exposes a Licenses resource for exactly this reason.
    Anything redistributing this data must check the licence of each source
    rather than assuming CC BY.
    """

    slug = "openaq"
    label = "OpenAQ"
    tier = "reference"
    accuracy_note = ("aggregates national regulatory networks; accuracy is "
                     "that of the underlying station")
    resolution_minutes = 60
    needs_key = True
    key_env = "OPENAQ_API_KEY"
    key_url = "https://explore.openaq.org/register"
    coverage_note = "worldwide, wherever a national network publishes"
    attribution = "Data via OpenAQ"
    licence = "per-source -- check the OpenAQ Licenses resource for your station"
    publishes_averages = ()

    API_BASE = "https://api.openaq.org/v3"

    #: What a sensor must be measuring, and in what unit, before its number is
    #: stored as PM2.5. OpenAQ declares both on every sensor.
    #:
    #: `discover()` already filters to `parameter.name == "pm25"`, so a source
    #: added through setup is right. But a source is a user-editable record --
    #: §3d put the settings behind a page on purpose -- and `current()`
    #: re-fetches by id without re-asking what the id measures. A sensor id
    #: typed or pasted into settings that happens to point at an NO2 or an
    #: ozone instrument would be stored as PM2.5 and look entirely ordinary:
    #: plausible small numbers, no error, feeding corroboration and Phase B.
    #:
    #: Same discipline as `weather._check_units`, and the same reasoning: the
    #: response declares the unit, so compare it rather than trust it. A
    #: mismatch fails nowhere on its own.
    MEASURES = "pm25"
    UNITS = ("µg/m³", "ug/m3")

    def _sensor_id(self, src):
        return src.get("sensor_id") or src.get("site_id")

    def _check_measures_pm25(self, sensor, sensor_id):
        """Refuse a sensor that is not reporting PM2.5 in µg/m³.

        Raises rather than skipping. Rule 5a forbids discarding a reading
        silently, and this is the other side of it: the honest response to
        "this instrument is not measuring what you think" is to say so where
        `--doctor` and the log will show it, not to store the number and not
        to drop it without a word.

        A sensor that declares neither is accepted. OpenAQ aggregates many
        networks and an absent field is missing metadata, not a contradiction
        -- refusing on silence would break working installs to guard against
        a case nobody has seen.
        """
        parameter = sensor.get("parameter") or {}
        name = parameter.get("name")
        if name is not None and name != self.MEASURES:
            raise RuntimeError(
                f"OpenAQ sensor {sensor_id} measures {name!r}, not "
                f"{self.MEASURES!r}. Refusing to store it as PM2.5 — check "
                f"the sensor id on this source in Settings.")
        units = parameter.get("units")
        if units is not None and units not in self.UNITS:
            raise RuntimeError(
                f"OpenAQ sensor {sensor_id} reports PM2.5 in {units!r}, not "
                f"µg/m³. Refusing to store it: raw µg/m³ is the canonical "
                f"unit and a silent change here moves every threshold.")

    def current(self, src, key):
        sensor_id = self._sensor_id(src)
        payload = http_get(f"{self.API_BASE}/sensors/{sensor_id}", key)
        results = payload.get("results") or []
        if not results:
            raise RuntimeError(f"OpenAQ sensor {sensor_id!r} returned no results")
        s = results[0]
        self._check_measures_pm25(s, sensor_id)
        latest = s.get("latest") or {}
        coords = latest.get("coordinates") or {}

        measures = {
            "headline": fnum(latest.get("value")),
            "now": fnum(latest.get("value")),
            "humidity": None,
            "temperature": None,
        }
        meta = {
            "site_id": sensor_id,
            "site_name": src.get("site_name") or (s.get("name") or str(sensor_id)),
            "latitude": fnum(coords.get("latitude")),
            "longitude": fnum(coords.get("longitude")),
            "last_seen_utc": ((latest.get("datetime") or {}).get("utc")),
            "temperature_unit": "C",
        }
        return measures, meta

    def history(self, src, key, start, end):
        sensor_id = self._sensor_id(src)
        url = f"{self.API_BASE}/sensors/{sensor_id}/hours?" + urllib.parse.urlencode({
            "datetime_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datetime_to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 1000,
        })
        try:
            payload = http_get(url, key)
        except Exception as e:
            log(f"WARN backfill openaq {type(e).__name__}: {e}")
            return []

        # The same check as current(), once for the batch rather than per
        # row: every record in a sensor's series declares the same parameter,
        # and refusing row by row would fill the log with one message per hour
        # for a single misconfigured source.
        for rec in payload.get("results") or []:
            if rec.get("parameter"):
                self._check_measures_pm25(rec, sensor_id)
                break

        obs = []
        for rec in payload.get("results") or []:
            period = (rec.get("period") or {}).get("datetimeFrom") or {}
            when, pm = period.get("utc"), fnum(rec.get("value"))
            if not when or pm is None:
                continue
            try:
                dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            except ValueError:
                continue
            when = dt.astimezone(timezone.utc)
            # OpenAQ returns the hour bucket straddling the start boundary, so
            # datetime_from is inclusive of a period that began before it.
            # Harmless for backfill -- dedup absorbs the overlap -- but every
            # provider must honour the same contract or gap detection reasons
            # about different windows depending on the source.
            if not (start <= when <= end):
                continue
            obs.append({
                "utc": when,
                "pm25": pm,
                "humidity": None,
                "temperature": None,
            })
        obs.sort(key=lambda o: o["utc"])
        return obs


    def discover(self, latitude, longitude, radius_km, key):
        # OpenAQ caps radius at 25 km.
        radius_m = int(min(radius_km, 25.0) * 1000)
        url = f"{self.API_BASE}/locations?" + urllib.parse.urlencode({
            "coordinates": f"{latitude},{longitude}",
            "radius": radius_m,
            "limit": 100,
        })
        payload = http_get(url, key)
        out = []
        for loc in payload.get("results") or []:
            coords = loc.get("coordinates") or {}
            lat, lon = fnum(coords.get("latitude")), fnum(coords.get("longitude"))
            # A location may host several instruments; we want the PM2.5 one,
            # because a site id alone does not say what it measures.
            for sensor in loc.get("sensors") or []:
                param = (sensor.get("parameter") or {}).get("name")
                if param != "pm25":
                    continue
                out.append({
                    "site_id": sensor.get("id"),
                    "site_name": loc.get("name") or str(sensor.get("id")),
                    "latitude": lat, "longitude": lon,
                    "distance_km": fusion.haversine_km(latitude, longitude, lat, lon),
                })
        out.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0))
        return out


class NswProvider(Provider):
    """NSW Government air quality network.

    No API key, hourly, CC BY 4.0. Second reference implementation for a
    direct government feed -- QldProvider covers the GET-with-query-params
    shape, this one covers POST-a-query-document, which is the other common
    pattern. Verified live.
    """

    slug = "nsw"
    label = "NSW Government air quality network"
    tier = "reference"
    accuracy_note = "calibrated regulatory monitor, hourly"
    resolution_minutes = 60
    needs_key = False
    attribution = "Contains NSW Government data, CC BY 4.0"
    licence = "CC BY 4.0"
    coverage_box = (-37.6, 140.9, -28.1, 153.7)     # New South Wales and the ACT
    coverage_note = "New South Wales and the ACT"
    publishes_averages = ("24hr",)

    API_BASE = "https://data.airquality.nsw.gov.au/api/Data"

    def _sites(self):
        return http_get(f"{self.API_BASE}/get_SiteDetails", key=None)

    def _observations(self, site_id, start, end):
        return http_post_json(f"{self.API_BASE}/get_Observations", {
            "Parameters": ["PM2.5"],
            "Sites": [int(site_id)],
            "StartDate": start.strftime("%Y-%m-%d"),
            "EndDate": end.strftime("%Y-%m-%d"),
            "Categories": ["Averages"],
            "SubCategories": ["Hourly"],
            "Frequency": ["Hourly average"],
        }, timeout=45)

    @staticmethod
    def _observed_utc(rec):
        """NSW timestamps its hours 1..24, where 24 means midnight ending
        that date -- i.e. 00:00 the following day. Treating 24 as an hour
        value directly would raise, and shifting it to 23 would silently
        misplace every midnight reading by an hour."""
        try:
            date = datetime.strptime(str(rec.get("Date")), "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
        hour = int(rec.get("Hour") or 0)
        dt = date + timedelta(hours=hour)
        # Readings are stamped in NSW standard time (UTC+10, no DST applied
        # to the published series).
        return (dt - timedelta(hours=10)).replace(tzinfo=timezone.utc)

    def current(self, src, key):
        site = src.get("site_id")
        end = datetime.now(timezone.utc) + timedelta(days=1)
        start = end - timedelta(days=2)
        rows = self._observations(site, start, end)
        usable = [r for r in (rows or [])
                  if fnum(r.get("Value")) is not None
                  and self._observed_utc(r) is not None]
        if not usable:
            raise RuntimeError(f"no recent PM2.5 for NSW site {site!r}")
        rec = max(usable, key=lambda r: self._observed_utc(r))

        measures = {
            "headline": fnum(rec.get("Value")),
            "now": fnum(rec.get("Value")),
            "humidity": None,
            "temperature": None,
        }
        meta = {
            "site_id": site,
            "site_name": src.get("site_name") or str(site),
            "last_seen_utc": self._observed_utc(rec).isoformat(timespec="seconds"),
            "temperature_unit": "C",
        }
        return measures, meta

    def history(self, src, key, start, end):
        try:
            rows = self._observations(src.get("site_id"), start, end)
        except Exception as e:
            log(f"WARN backfill nsw {type(e).__name__}: {e}")
            return []
        obs = []
        for rec in rows or []:
            when, pm = self._observed_utc(rec), fnum(rec.get("Value"))
            if when is None or pm is None:
                continue
            if start <= when <= end:
                obs.append({"utc": when, "pm25": pm,
                            "humidity": None, "temperature": None})
        obs.sort(key=lambda o: o["utc"])
        return obs

    def discover(self, latitude, longitude, radius_km, key):
        out = []
        for st in self._sites() or []:
            lat, lon = fnum(st.get("Latitude")), fnum(st.get("Longitude"))
            if lat is None or lon is None:
                continue
            d = fusion.haversine_km(latitude, longitude, lat, lon)
            if d is not None and d <= radius_km:
                out.append({
                    "site_id": st.get("Site_Id"),
                    "site_name": (st.get("SiteName") or "").title() or str(st.get("Site_Id")),
                    "latitude": lat, "longitude": lon, "distance_km": d,
                })
        out.sort(key=lambda r: r["distance_km"])
        return out


PROVIDERS = {p.slug: p for p in (
    PurpleAirProvider(), QldProvider(), NswProvider(), OpenAQProvider(),
)}


def get_provider(src):
    """Resolve one source's provider. `src` is a single entry from `sources`."""
    slug = str((src or {}).get("provider") or "purpleair").strip().lower()
    if slug not in PROVIDERS:
        raise SystemExit(
            f"Unknown provider {slug!r}. Available: {', '.join(sorted(PROVIDERS))}\n"
            f"Set provider on the source in config.json."
        )
    return PROVIDERS[slug]


# ------------------------------------------------------------------ polling

def db_path():
    return DATA / "airo.db"


def open_store():
    """Open the readings database, creating and upgrading it if needed.

    The one place a connection is made, so schema version handling cannot be
    skipped by a caller that opens sqlite3 directly.
    """
    return store.connect(db_path())


def register_sources(conn, cfg):
    """Make sure every configured source exists in the database.

    Returns [(source_row_id, source_config, provider)] for the enabled ones.
    """
    out = []
    for src in enabled_sources(cfg):
        provider = get_provider(src)
        sid = store.upsert_source(
            conn,
            provider=provider.slug,
            site_id=src.get("site_id"),
            site_name=src.get("site_name") or None,
            latitude=src.get("latitude"),
            longitude=src.get("longitude"),
            resolution_minutes=provider.resolution_minutes,
            enabled=True,
            placement=placement_for(src, provider=provider),
        )
        out.append((sid, src, provider))
    return out


def placement_for(src, meta=None, provider=None):
    """Where a source is: what the user said, else what the provider says.

    The user wins. They can see the sensor; the API knows only how it was
    registered, and somebody who mounted an "indoor" unit under the eaves has
    a better view of the truth than the form they filled in years ago.

    Falls back to the provider's own default, because a regulatory network is
    outdoor by definition and should not need detecting. Without that, a fresh
    install of a government feed registers as 'unknown', which is excluded from
    describing outdoor air -- so the headline vanishes on a perfectly ordinary
    install. Five tests said so within a minute of the exclusion landing.

    Returns None when nothing knows, which `upsert_source` reads as "nothing to
    say" and leaves any stored answer alone. That matters on a poll that could
    not reach the API: a network failure must not forget which sensors are
    indoors.
    """
    configured = str((src or {}).get("placement") or "").strip().lower()
    if configured in store.PLACEMENTS:
        return configured
    detected = (meta or {}).get("placement")
    if detected:
        return detected
    return getattr(provider, "default_placement", None)


def poll_source(conn, sid, src, provider, cfg):
    """Fetch and store one source's current reading. Returns its stored row."""
    key = get_api_key(src)
    measures, meta = provider.current(src, key)
    measures, rejected = clean_measures(measures)
    if rejected:
        # Said out loud, not swallowed: the station is reporting a fault and
        # the user should know their nearest instrument is offline.
        log(f"{WARN} {provider.slug}/{src.get('site_id')}: sentinel value from "
            f"the feed ({', '.join(rejected)}) — treated as no measurement")

    now_utc = datetime.now(timezone.utc)
    pm_now = measures.get("now")
    pm_headline = measures.get("headline")
    # If the averaged value isn't published yet, fall back to the live value
    # so the dashboard and tray still show something meaningful.
    headline = pm_headline if pm_headline is not None else pm_now

    # observed_utc is when the air was measured, which for a regulatory feed
    # can be an hour before we asked. Using the fetch time here would make a
    # stale reading look current, so prefer what the provider reports.
    observed = meta.get("last_seen_utc") or now_utc.isoformat(timespec="seconds")

    # Learn from the provider what the user has not supplied: coordinates,
    # which the 'nearest' rule depends on, and placement, which decides whether
    # this reading may describe outdoor air at all.
    #
    # Placement is deliberately *not* gated on the coordinate condition below.
    # It was, in the first version, and that meant a sensor whose coordinates
    # were already known could never be identified as indoor -- which is every
    # sensor added through discovery, and every one the user typed a location
    # for.
    learned = placement_for(src, meta, provider)
    learns_coords = (meta.get("latitude") is not None
                     and src.get("latitude") is None)
    if learns_coords or learned is not None:
        store.upsert_source(
            conn, provider.slug, src.get("site_id"),
            site_name=meta.get("site_name"),
            latitude=meta.get("latitude") if learns_coords else src.get("latitude"),
            longitude=meta.get("longitude") if learns_coords else src.get("longitude"),
            resolution_minutes=provider.resolution_minutes,
            placement=learned)

    store.insert_readings(conn, sid, [{
        "observed_utc": observed,
        "fetched_utc": now_utc.isoformat(timespec="seconds"),
        "kind": "live",
        "pm25": headline,
        "pm25_now": pm_now,
        "pm25_30min": measures.get("30min"),
        "pm25_60min": measures.get("60min"),
        "pm25_6hr": measures.get("6hr"),
        "pm25_24hr": measures.get("24hr"),
        "pm25_1week": measures.get("1week"),
        "pm25_a": measures.get("pm25_a"),
        "pm25_b": measures.get("pm25_b"),
        "confidence": measures.get("confidence"),
        "humidity": measures.get("humidity"),
        # Normalise to Celsius on the way in. PurpleAir reports Fahrenheit and
        # regulatory feeds report Celsius; mixing both in one column with the
        # unit only implied is ROADMAP known issue C, and would silently
        # corrupt any cross-source comparison.
        "temperature": to_celsius(measures.get("temperature"),
                                  meta.get("temperature_unit")),
        "temperature_unit": "C",
    }])
    return headline


def backfill_source(conn, sid, src, provider, days=None, since_utc=None):
    """Repair one source's history. The never-lose-a-night guarantee."""
    end = datetime.now(timezone.utc)
    if since_utc is not None:
        # Overlap deliberately so a partially-written interval is refetched
        # rather than straddled. Dedup in the store makes this free.
        start = since_utc - timedelta(minutes=2 * provider.resolution_minutes)
    else:
        # `days or 7` turned an explicit 0 into 7: a user who answered "0" to
        # "Days of history to pull now" got a week of it anyway. Zero is a
        # meaningful answer here, not a missing one.
        start = end - timedelta(days=7 if days is None else max(0, days))

    # Nothing meaningful to fetch inside one reporting interval.
    if (end - start) < timedelta(minutes=provider.resolution_minutes + 5):
        return 0

    span_days = (end - start).days
    if span_days > 14:
        # A year of history is minutes of API calls. Without this the command
        # looks hung and people kill it half way. ROADMAP known issue A.
        log(f"backfilling {span_days} days for {provider.slug}/{src.get('site_id')} "
            f"— this may take a few minutes")

    obs = provider.history(src, get_api_key(src), start, end)
    rows = [{
        "observed_utc": o["utc"],
        "kind": "history",
        "pm25": o.get("pm25"),
        "humidity": o.get("humidity"),
        # Normalise on the way in, the same as capture_reading. This path did
        # neither: it copied the provider's value and set no unit at all,
        # which is worse than storing 'F' -- the v3 migration repairs a row
        # marked 'F' on open and cannot see a row marked nothing, so a
        # Fahrenheit backfill was permanent and undetectable.
        "temperature": to_celsius(o.get("temperature"),
                                  provider.temperature_unit),
        "temperature_unit": "C",
    } for o in obs if o.get("pm25") is not None and o.get("utc") is not None]

    added = store.insert_readings(conn, sid, rows)
    log(f"backfill {provider.slug}/{src.get('site_id')}: fetched {len(obs)} "
        f"observations, {added} new "
        f"({start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M})")
    return added


def gap_threshold_for(provider, cfg):
    """How long a silence has to be before we call it a gap.

    Scales with the provider's reporting interval. A fixed 25 minutes is right
    for PurpleAir's 10-minute average but would fire on every single poll
    against an hourly regulatory feed, where 30 minutes of silence is simply
    what normal looks like.

    The tolerance itself -- two intervals plus a grace minute -- is fusion's
    decision about when a source stops counting as current, and was written
    out here as a bare `* 2 + 5`. Two copies of one staleness rule drift, and
    the direction they drift in is a gap this reports and fusion does not, or
    the reverse.
    """
    poll_minutes = int(cfg.get("poll_minutes", 15) or 15)
    interval = max(provider.resolution_minutes, poll_minutes)
    return timedelta(minutes=interval * fusion.STALE_INTERVALS
                     + fusion.STALE_GRACE_MINUTES)


# The evening trapping window. Configurable because it describes a local
# phenomenon: cold-air drainage after sunset in a valley. Somewhere flat and
# coastal has no such window, and asserting one would be inventing advice.
DEFAULT_RISK_WINDOW = {"enabled": True, "start_hour": 15, "end_hour": 1}


def risk_window(cfg):
    """The configured window, defaults filled in, hours normalised to 0-23.

    One function because there were three readers and only one of them read
    the config. `compute_time_hint()` honoured `risk_window`; `analyse.py`
    held its own EVENING_START_HOUR/END_HOUR pair, and the dashboard had the
    numbers written out in about ten places. So a user who moved their window
    to 6pm got advice about 6pm from the menu bar, a report about 3pm from
    `analyse.py evening`, and a chart shaded from 3pm -- three answers to one
    configured question, none of them flagged as disagreeing.

    It is served in `latest.json` for the same reason the bands are: the page
    must not carry a second copy of a setting Python owns.
    """
    window = dict(DEFAULT_RISK_WINDOW)
    window.update(cfg.get("risk_window") or {})
    # Modulo rather than a range check: end_hour is 1 meaning 1am the next day,
    # and a window that wraps midnight is the normal case here, not the edge.
    window["start_hour"] = int(window.get("start_hour", 15)) % 24
    window["end_hour"] = int(window.get("end_hour", 1)) % 24
    window["enabled"] = bool(window.get("enabled", True))
    return window


def _next_poll_text(cfg):
    """When the scheduled agent is due again, phrased for a menu.

    The value is written at poll time, so "in N min" is exact at the moment
    latest.json lands and only ever drifts later -- which is the safe
    direction: a reading that claims to be fresher than it is would be worse
    than one that admits it is due.
    """
    try:
        mins = int(cfg.get("poll_minutes", 15))
    except (TypeError, ValueError):
        mins = 15
    if mins <= 0:
        return "on demand"
    if mins < 60:
        return f"in {mins} min"
    hours, rest = divmod(mins, 60)
    return f"in {hours} hr" if not rest else f"in {hours} hr {rest} min"


#: The daily shape this project was built around: PM2.5 climbing through the
#: late afternoon, peaking in the evening and clearing overnight. The hours
#: are one place's finding, not a universal truth, and they are the *prior* --
#: what is usually true at this time of day. The advice is written without a
#: house or a latitude in it, because these are the shipped defaults and the
#: reader's sunset, ceiling and fan are not the author's.
#
#: Held as minutes since midnight rather than decimal hours. Partly because
#: 16*60+35 says 16:35 and 16.58 does not, and partly because two decimals
#: side by side in a source file look exactly like a coordinate -- the rule
#: 2b check said so, and it was right to.
VENTILATION_WINDOWS = (
    (15 * 60, 16 * 60 + 35, "Pre-close",
     "The evening rise is coming. Have the house shut and purifiers on by "
     "around sunset."),
    (16 * 60 + 35, 17 * 60 + 15, "Close up now",
     "The PM2.5 climb starts about now. Shut up, then start the purifiers so "
     "they reach equilibrium within the hour."),
    (17 * 60 + 15, 25 * 60, "Risk window active",
     "The peak comes an hour or two into the evening and indoor levels lag "
     "outdoors — keep living-area purifiers running well past midnight. A fan "
     "on low helps mix the room."),
    (1 * 60, 10 * 60, "Overnight / early",
     "Past the worst. The daily minimum is in the early afternoon — that is "
     "when to air the house out."),
    (10 * 60, 15 * 60, "Cleanest part of the day",
     "Good window to open up and ventilate. Close again before the evening "
     "rise."),
)


def ventilation_advice(hour, index, scale_name, trend=None, bands=None):
    """What to do about the windows, from the clock *and* the reading.

    The clock alone is a prior: mornings are usually the clean part of the day.
    A reading is evidence about right now, and evidence wins — because
    the failure mode is not a missed opportunity to air the house, it is an
    open window during a smoke event.

    That happened. The panel was a pure function of the hour, and on a morning
    with a local fire nearby it read "Cleanest part of the day — good window
    to open up and ventilate" beside a headline well into the third band and
    rising sharply. The register already carried "telling somebody to ventilate
    during a smoke event" as a risk; the protection had been built for the
    inside-against-outside panel and this surface had none of it.

    Decided here rather than in the page for the reason `compute_trend` gives
    one function above: three surfaces describing one relationship three ways
    is how they drift, and one of those ways will be the wrong remedy. Rule 7.
    """
    trend = trend or {}
    minute = int(round(float(hour) * 60)) % (24 * 60)

    head, msg = "Window", ""
    for start, end, title, text in VENTILATION_WINDOWS:
        # The evening window wraps midnight, so it ends at 25:00 and the small
        # hours are compared a day later. A plain start <= m < end would leave
        # 00:30 in no window at all, and the panel would render empty.
        if start <= minute < end or start <= minute + 24 * 60 < end:
            head, msg = title, text
            break

    # Does the air allow an open window? Only the best band does, and only
    # when it is not climbing: low-and-rising is how an episode begins, and
    # indoor lags outdoor, so an open window at the start of one is the worst
    # available timing.
    #
    # No reading is not a clean reading. The safe direction with nothing to
    # go on is to stop recommending the window, not to assume the best.
    ceiling = _first_band_ceiling(scale_name, bands)
    climbing = trend.get("direction") in ("rising", "rising_fast")
    clean = index is not None and ceiling is not None and index <= ceiling
    may_open = clean and not climbing

    why = None
    if not may_open and "ventilate" in msg:
        if index is None:
            why = ("No current reading, so this is not the moment to trust "
                   "the usual pattern.")
            msg = ("Normally the window to air the house out. Wait for a "
                   "reading before opening up.")
        elif not clean:
            why = (f"The usual advice for this hour is to air the house out. "
                   f"Not today: the reading is {index:.0f}, above the clean "
                   f"band.")
            msg = ("Keep the house closed and run purifiers. This is usually "
                   "the clean part of the day, but the air says otherwise.")
        else:
            why = ("Still in the clean band, but climbing — which is how an "
                   "episode starts, and indoor levels lag outdoors.")
            msg = ("Hold off opening up: the reading is low but rising. "
                   "Check again once it settles.")
        head = "Not the window to open up"

    return {"headline": head, "advice": msg, "why": why,
            "may_ventilate": bool(may_open)}


def _first_band_ceiling(scale_name, bands=None):
    """The top of the cleanest band on this scale, or None.

    Read from the scale rather than hard-coded, so a reader on a different
    national scale gets their own definition of "clean enough to open a
    window" instead of Australia's.
    """
    try:
        table = bands if bands is not None else SCALES[scale_name]["bands"]
        for b in table:
            top = b.get("max") if isinstance(b, dict) else b[0]
            if top is not None:
                return float(top)
    except Exception:
        return None
    return None


def _local_hour(cfg, now=None):
    """The reader's local hour as a float, from their configured timezone.

    Not the server's clock: the advice is about their evening, and a machine
    running in another zone would shift the whole risk window.

    Through `local_now()`, which is the project's one timezone resolver. This
    re-resolved the zone by hand behind a bare `except Exception` — so a
    configured zone that could not be loaded (Windows ships no tz database,
    which is the common case, not an exotic one) silently fell back to the
    machine's zone and said nothing, while every other caller got
    `resolve_zone()`'s explanatory note. Same answer, one implementation, and
    the failure is now reported where the others are.
    """
    here = local_now(cfg, now or datetime.now(timezone.utc))
    return here.hour + here.minute / 60.0


def compute_trend(averages, scale_name):
    """Is it getting worse or better right now?

    Ten minutes against the hour: the short average moving away from the long
    one is the earliest honest signal, because indoor levels lag outdoors and
    a warning that arrives at the peak is no warning at all.

    Lives here rather than in each UI so the menu bar, the tray and the
    dashboard cannot disagree about which way the air is going.
    """
    ten, hour = averages.get("10min"), averages.get("60min")
    if ten is None or hour is None:
        return {"direction": "unknown", "delta": None, "text": None}
    delta = ten - hour
    if delta >= 5:
        return {"direction": "rising_fast", "delta": round(delta, 1),
                "text": f"Rising sharply — +{delta:.0f} on the hour average"}
    if delta >= 2:
        return {"direction": "rising", "delta": round(delta, 1),
                "text": f"Rising — +{delta:.0f} on the hour average"}
    if delta <= -5:
        return {"direction": "clearing", "delta": round(delta, 1),
                "text": f"Clearing — {delta:.0f} on the hour average"}
    return {"direction": "steady", "delta": round(delta, 1), "text": "Steady"}


def compute_time_hint(cfg, now=None):
    """Advice tied to the time of day, if the user has a risk window set.

    Deliberately about ventilation and filtration, never about health. "Close
    up before 5pm" is a statement about a window; "safe for your asthma" would
    be a medical claim, and Australian Consumer Law takes a dim view of those.
    """
    window = risk_window(cfg)
    if not window["enabled"]:
        return None

    start, end = window["start_hour"], window["end_hour"]
    # Resolved in the configured zone. These are the hours the project exists
    # to reason about, and on a machine in the wrong zone the window slides
    # wholesale -- "worse after sunset" asked about mid-morning.
    now = local_now(cfg, now)
    h = now.hour

    def inside(hour):
        return hour >= start or hour < end if start > end else start <= hour < end

    if inside(h):
        return {"state": "active", "severity": "high",
                "text": "Risk window active — keep filtering"}
    # Two hours of warning before it opens is enough to close up and let
    # purifiers reach equilibrium.
    lead = (start - h) % 24
    if lead <= 2:
        return {"state": "approaching", "severity": "medium",
                "text": f"Closing-up time — risk window starts at {start:02d}:00"}
    return {"state": "clear", "severity": "low",
            "text": "Low-risk hours — good time to ventilate"}


def weather_summary(conn, cfg):
    """What weather this install holds, or None if it holds none.

    None rather than an empty dict: every surface renders "is there weather?"
    as a presence check, and an empty dict is true.
    """
    location = cfg.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        span = store.weather_span(conn, store.place_key(lat, lon))
    except Exception:
        return None                 # an older database without the table
    if not span.get("hours"):
        return None
    return {"hours": span["hours"], "first": span["first"],
            "last": span["last"], "source": weather.SLUG,
            "attribution": weather.ATTRIBUTION, "licence": weather.LICENCE,
            "homepage": weather.HOMEPAGE}


def _served_sources(readings, result, as_view):
    """Every source that reported, fused view where there is one.

    Order follows `readings`, so the display does not reshuffle when a sensor
    is excluded from fusion — a list that reorders itself as conditions change
    is one nobody can read at a glance.
    """
    fused = {r.get("source_id"): r for r in (result.get("sources") or [])}
    return [as_view(fused.get(r["source_id"], r)) for r in readings]


def build_latest(conn, cfg):
    """Fuse every source into the single view the dashboard and tray read."""
    scale_name, scale = get_scale(cfg)
    location = cfg.get("location") or {}
    rule = (cfg.get("fusion") or {}).get("rule") or fusion.DEFAULT_RULE

    # Age, distance and staleness first, for every sensor regardless of where
    # it sits. These are facts about an instrument -- when it last spoke, how
    # far away it is, whether it has gone quiet -- not claims about the air
    # outside, so the exclusion below has no business reaching them.
    #
    # It did, once. The split used to happen before this line, which meant an
    # indoor sensor was never annotated at all: the dashboard renders age and
    # distance straight from the payload, so the row showed "-- / --" and no
    # stale tag no matter how long the sensor had been dead. A stale reading
    # with no age looks current, which is worse than showing nothing.
    #
    # fuse() annotates its own input unconditionally, so doing it here is not
    # a second pass with different results -- only an earlier one that the
    # excluded sensors also get.
    readings = fusion.annotate(store.latest_per_source(conn), location)

    # Only outdoor sensors may describe the air outside.
    #
    # `nearest` is the default rule and an indoor sensor is ~0 km away, so
    # without this split the headline becomes a reading from inside somebody's
    # house, rendered with outdoor advice ("avoid outdoor exertion") and firing
    # outdoor alerts when they cook. Corroboration is worse and quieter: it
    # cross-checks peers, so the indoor sensor disagreeing with the real ones
    # would mark the *outdoor* sensors uncorroborated.
    #
    # Both are fixed by the same split, because corroboration happens inside
    # fuse(). Everything collected is still carried to the surfaces below --
    # nothing is discarded, it is only stopped from meaning something it does
    # not (rule 5a).
    outdoor = [r for r in readings if store.is_outdoor(r["placement"])]

    # Historical peer ratios, at this hour of day, so a site that always runs
    # high after sunset is not repeatedly accused of lying while a genuine
    # one-off anomaly still gets flagged.
    hour_utc = datetime.now(timezone.utc).hour
    history = {}
    # `outdoor`, not `readings` — but only to avoid computing peer history for
    # a source fusion will never be handed. It enforces nothing: the exclusion
    # that matters is what `fuse()` receives, below, and swapping this back to
    # `readings` turns no test red because the extra history is simply unused.
    # Said here so nobody "fixes" it into looking load-bearing.
    for r in outdoor:
        try:
            # Same-hour history is the sharper comparison -- a valley traps
            # particulates at 6pm, not at noon -- but a young install has only
            # a handful of samples per hour. Fall back to all hours rather
            # than reporting "not enough history" when a usable answer exists.
            h = store.peer_ratio_history(conn, r["source_id"],
                                         hour_of_day=hour_utc, days=90)
            if (h.get("n") or 0) < fusion.MIN_HISTORY_SAMPLES:
                allhours = store.peer_ratio_history(conn, r["source_id"], days=90)
                if (allhours.get("n") or 0) > (h.get("n") or 0):
                    allhours["basis"] = "all hours"
                    h = allhours
            else:
                h["basis"] = "this hour of day"
            history[r["source_id"]] = h
        except Exception as e:
            log(f"WARN corroboration history failed for {r.get('site_id')}: {e}")
    result = fusion.fuse(outdoor, rule, location, history=history)

    pm = result.get("pm25")
    aqi = aqi_for(pm, scale_name)
    now_utc = datetime.now(timezone.utc)

    def as_view(r):
        v = aqi_for(r.get("pm25"), scale_name)
        return {
            "provider": r.get("provider"),
            "site_id": r.get("site_id"),
            "site_name": r.get("site_name"),
            "pm25": r.get("pm25"),
            "aqi": v,
            "band": band_for(round(v) if v is not None else None, scale_name),
            "observed_utc": r.get("observed_utc"),
            "age_minutes": (round(r["age_minutes"]) if r.get("age_minutes") is not None else None),
            "distance_km": (round(r["distance_km"], 2) if r.get("distance_km") is not None else None),
            "stale": r.get("stale"),
            "quality": r.get("quality"),
            "pm25_a": r.get("pm25_a"),
            "pm25_b": r.get("pm25_b"),
            "confidence": r.get("confidence"),
            "corroboration": r.get("corroboration"),
            "corroboration_note": r.get("corroboration_note"),
            "peer_ratio": r.get("peer_ratio"),
            "peer_pm25": r.get("peer_pm25"),
            "resolution_minutes": r.get("resolution_minutes"),
            "humidity": r.get("humidity"),
            "temperature": r.get("temperature"),
            "temperature_unit": r.get("temperature_unit"),
            # Served so a surface can separate indoor from outdoor without
            # guessing from a site name. A page that has to infer this gets it
            # wrong, and getting it wrong is the whole failure this prevents.
            "placement": str(r.get("placement") or "unknown"),
            # Why this row has no peer comparison, in words, next to the blank
            # it explains. An empty cell reads as missing data -- the first
            # question this dashboard was asked about an indoor sensor was
            # "is data being collected?", when it was, and the blanks were
            # what prompted it.
            #
            # Only where there is something to explain. An outdoor sensor's
            # row is the unremarkable case and repeating "this is an outdoor
            # sensor" on every one of them trains people to skip the line,
            # including on the row that means something.
            "placement_note": (
                None if store.is_outdoor(r.get("placement"))
                else _placement_note(str(r.get("placement") or "unknown"))),
        }

    # What is actually held, across every source.
    #
    # The dashboard's "Readings on disk" used to be the length of the series
    # the page was holding, and every historical panel follows the *headline*
    # source -- so adding a sensor nearer the house than the old one moved the
    # headline to an instrument installed thirty minutes earlier and the panel
    # went from 18,617 to 4. Nothing had been lost. The page said it had.
    #
    # A panel that names a fact about the database has to be given that fact.
    # The per-instrument panels are a different question and stay per
    # instrument -- they say so on their face.
    record = {"readings_total": 0, "first_utc": None, "last_utc": None,
              "sources_total": 0}
    try:
        rows = store.counts(conn)
        record["sources_total"] = len(rows)
        record["readings_total"] = sum(int(r.get("rows") or 0) for r in rows)
        firsts = [r["first_utc"] for r in rows if r.get("first_utc")]
        lasts = [r["last_utc"] for r in rows if r.get("last_utc")]
        record["first_utc"] = min(firsts) if firsts else None
        record["last_utc"] = max(lasts) if lasts else None
    except Exception as e:
        # Never at the cost of the reading. A count is a nicety; the headline
        # is the product.
        log(f"WARN record totals unavailable: {type(e).__name__}: {e}")

    chosen = result.get("source") or {}
    averages = {}
    if chosen:
        for label, col in (("now", "pm25_now"), ("30min", "pm25_30min"),
                           ("60min", "pm25_60min"), ("6hr", "pm25_6hr"),
                           ("24hr", "pm25_24hr"), ("1week", "pm25_1week")):
            averages[label] = aqi_for(chosen.get(col), scale_name)
    averages["10min"] = aqi

    latest = {
        "fetched_utc": now_utc.isoformat(timespec="seconds"),
        # The user's zone, so the tray and the dashboard agree with the
        # clock on their wall rather than with the server's.
        "fetched_local": local_now(cfg, now_utc).isoformat(timespec="seconds"),
        "location_name": location.get("name") or (chosen.get("site_name") if chosen else None),
        "location": {"latitude": location.get("latitude"),
                     "longitude": location.get("longitude")},
        "poll_minutes": int(cfg.get("poll_minutes", 15)),
        # Rendered strings, not raw timestamps. The tray has no date library
        # and rule 7 says it renders rather than decides -- so the phrasing is
        # settled once, here, and every surface says the same thing.
        "last_poll_text": "just now",
        # Where the database actually is. Every surface was printing a
        # hardcoded "data/airo.db", which is the pre-v0.6 location and wrong
        # for anyone who set data_dir -- telling a user their readings are
        # somewhere they are not.
        "data_dir": str(DATA),
        "next_poll_text": _next_poll_text(cfg),

        "aqi": aqi,
        "band": band_for(round(aqi) if aqi is not None else None, scale_name),
        "pm25_10min": pm,
        "averages_aqi": averages,
        # The band each rolling average falls in, decided here so no UI has to
        # re-derive one. band_for() is fed the ROUNDED value, because the
        # colour must match the figure actually displayed -- ARCHITECTURE §3,
        # the boundary bug that shipped once already.
        "averages_band": {
            k: band_for(round(v) if v is not None else None, scale_name)
            for k, v in (averages or {}).items()},

        "scale": scale_name,
        "scale_label": scale["label"],
        "scale_note": scale["note"],
        # What the big number on every surface actually is, in words, decided
        # here rather than in the page. Asked for by a user looking at a 48
        # above three source readings of 11.9, 14.4 and 9.2 µg/m³ and
        # reasonably concluding it must be some kind of average. It is not:
        # it is one source's concentration converted to an index.
        #
        # In Python because it is a statement about how a health-relevant
        # figure was derived, and rule 7 says those decisions do not live in a
        # renderer. It also means the dashboard, the tray and the detail
        # window cannot describe the same number three different ways.
        "headline_explained": explain_headline(scale_name, scale, pm, aqi,
                                               chosen, rule),
        # Carried so no surface has to know the boundaries for itself.
        "bands": scale_bands(scale_name),
        # Weather is CC BY 4.0 and owes attribution exactly as the government
        # feeds do (rule 4). Only claimed when weather has actually been
        # stored -- crediting a source an install has never used is the same
        # error as the footer once crediting PurpleAir to a Queensland-only
        # user.
        "weather": weather_summary(conn, cfg),
        # Likewise for the index-to-µg conversion: null where the scale is
        # piecewise and no single factor exists. See ug_per_index().
        "ug_per_index": ug_per_index(scale_name),

        # Provenance: which instrument produced the headline, how far away it
        # is and how old it is. The UI must always be able to show its working.
        "trend": compute_trend(averages, scale_name),
        # Decided here, rendered verbatim. See ventilation_advice(): the page
        # used to work this out from the clock alone and could recommend
        # opening the windows in the middle of an episode.
        "window_advice": ventilation_advice(
            hour=_local_hour(cfg),
            index=averages.get("10min"),
            scale_name=scale_name,
            trend=compute_trend(averages, scale_name)),
        "time_hint": compute_time_hint(cfg),
        # The hours themselves, not just the advice derived from them. The
        # dashboard shades the chart, buckets its nights and captions three
        # panels with this window, and had 15 and 1 written out in about ten
        # places -- so a configured window moved the menu-bar advice and left
        # every chart on the page describing the default.
        "risk_window": risk_window(cfg),
        "alerts": {
            "enabled": bool((cfg.get("alerts") or {}).get("enabled", True)),
            "threshold_aqi": (cfg.get("alerts") or {}).get("threshold_aqi"),
            "threshold_pm25": (cfg.get("alerts") or {}).get("threshold_pm25"),
            # A list, never null. The tray types this as a sequence, and
            # serde's `default` covers a *missing* field, not an explicit
            # null -- so `"quiet_hours": null` fails the whole parse and the
            # tray shows "no reading yet" beside a full database. That is the
            # same failure the Infinity band ceiling caused, in a different
            # field, and it hits the *default* configuration: anyone who never
            # set quiet hours. Found by pointing the tray at a generated demo.
            "quiet_hours": (cfg.get("alerts") or {}).get("quiet_hours") or [],
        },
        # Every bucket that appears in averages_aqi must appear here too.
        # "10min" was missing while its index was present -- the derived value
        # published without the canonical one it came from, which is backwards
        # (rule 6). The tray rendered "10 min  2" with no µg beside it while
        # every other row had one.
        "averages_pm25": dict(
            {k: (chosen.get(col) if chosen else None)
             for k, col in (("now", "pm25_now"),
                            ("30min", "pm25_30min"),
                            ("60min", "pm25_60min"),
                            ("6hr", "pm25_6hr"),
                            ("24hr", "pm25_24hr"),
                            ("1week", "pm25_1week"))},
            **{"10min": pm}),
        "fusion_rule": result.get("rule"),
        "fusion_note": result.get("note"),
        "fusion_degraded": result.get("degraded", False),
        "uncorroborated": result.get("uncorroborated", False),
        "corroboration_note": result.get("corroboration_note"),
        "provenance": fusion.describe(result),
        "source": as_view(chosen) if chosen else None,
        # Every source that reported, not only the ones fusion was allowed to
        # consider. The fused rows carry corroboration and are used as-is; an
        # indoor sensor has none, because it was never cross-checked against
        # peers measuring different air, and appears with those fields empty.
        #
        # Built from `readings` rather than from the fusion result because the
        # result is outdoor-only by design. Taking the list from there made the
        # indoor sensor vanish from every surface — collected, stored, and
        # invisible, which is discarding it by another route (rule 5a).
        "sources": _served_sources(readings, result, as_view),
        "record": record,
        "contributing": [as_view(r) for r in result.get("contributing") or []],
        "last_known": (as_view(result["last_known"])
                       if result.get("last_known") else None),
        # What else this user could be reading. The dashboard and tray use
        # this to offer networks rather than requiring the user to go looking.
        "networks": network_status(cfg),

        "attributions": sorted({
            PROVIDERS[r["provider"]].attribution
            for r in (result.get("sources") or [])
            if r.get("provider") in PROVIDERS
            and PROVIDERS[r["provider"]].attribution
        }),

        "humidity": chosen.get("humidity") if chosen else None,
        "temperature": chosen.get("temperature") if chosen else None,
        "temperature_unit": chosen.get("temperature_unit") if chosen else None,

        # Deprecated aliases so a dashboard page cached in someone's browser
        # keeps rendering after an upgrade. Remove in v0.6. (A stale open tab
        # is a real failure mode -- it caused an 'agent has stopped' false
        # alarm during development.)
        "au_aqi": aqi,
        "au_band": band_for(round(aqi) if aqi is not None else None, scale_name),
        "averages_au_aqi": averages,
        "sensor_name": chosen.get("site_name") if chosen else None,
        "sensor_index": chosen.get("site_id") if chosen else None,
    }
    return latest


FAILURE_STATE_PATH_NAME = "source_failures.json"


def _failure_state():
    path = DATA / FAILURE_STATE_PATH_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_failure_state(state):
    write_json_atomic(DATA / FAILURE_STATE_PATH_NAME, state)


def source_is_reporting(conn, source_id, provider, cfg, now=None):
    """Has this source produced an observation recently enough to count?

    Distinct from "did the fetch succeed", and the distinction is the whole
    point. PurpleAir does not stop answering when a sensor drops off the
    network: it serves that sensor's last known reading, with its original
    timestamp, for as long as you keep asking. The call returns 200, the
    parse succeeds, a PM2.5 figure is logged, and nothing has been observed.

    Judged against the provider's own cadence, for the reason `is_stale` gives
    in `fusion`: forty minutes of silence is an outage for a ten-minute
    consumer sensor and completely normal for an hourly regulatory feed.
    """
    last = store.last_observed(conn, source_id)
    if last is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - last) <= gap_threshold_for(provider, cfg)


#: What to say for each kind of silence. They call for opposite responses --
#: one sends you to your network and your API key, the other tells you to go
#: and look at the sensor -- and the wrong one wastes the reader's time on a
#: fault that is not there.
SILENCE_REASONS = {
    "unreachable": (
        "has stopped responding",
        "{n} failed polls over about {hours:.0f}h. Check the key, or run: "
        "python3 poller.py --doctor"),
    "stale": (
        "has stopped reporting",
        "The provider is still answering for it, but the last reading is "
        "about {hours:.0f}h old — so the sensor itself is likely offline "
        "rather than your network. Other sources are unaffected."),
}


def record_source_result(state, label, ok_now, threshold, cfg,
                         reason="unreachable"):
    """Count consecutive silent polls and notify once when a source goes dark.

    A provider changing its API, revoking a key or simply disappearing shows
    up as a warning in a log file. Meanwhile the record develops a hole that
    nobody notices until they go looking for a night that was never recorded.

    `ok_now` used to mean "the fetch did not raise", which is why the hole
    above happened anyway on a real install: a sensor was dark for about two
    days behind a provider that answered every time, so the counter reset on every
    poll. It now means "this source actually observed something", and `reason`
    says which kind of silence it is.
    """
    entry = state.get(label) or {"consecutive": 0, "notified": False}
    if ok_now:
        if entry.get("notified"):
            notify("Airo", f"{label} is working again",
                   "Readings from this source have resumed.")
            log(f"{label} recovered after {entry['consecutive']} silent polls")
        state[label] = {"consecutive": 0, "notified": False}
        return

    entry["consecutive"] = int(entry.get("consecutive", 0)) + 1
    if entry["consecutive"] >= threshold and not entry.get("notified"):
        entry["notified"] = True
        hours = entry["consecutive"] * int(cfg.get("poll_minutes", 15)) / 60
        headline, body = SILENCE_REASONS.get(reason,
                                             SILENCE_REASONS["unreachable"])
        notify("Airo", f"{label} {headline}",
               body.format(n=entry["consecutive"], hours=hours))
        log(f"ALERT {label} {headline} — {entry['consecutive']} "
            f"consecutive silent polls ({reason})")
    state[label] = entry


def capture_weather(conn, cfg, past_days=2):
    """Record the weather for the configured location. Never raises.

    Called on every poll. Weather is the *cause* the readings are the effect
    of (ROADMAP #9), and it is supplementary: a weather service being down
    must never cost a reading, so every failure here is logged and swallowed.
    That is the opposite of how a provider failure is treated, deliberately --
    a missing reading is the product failing, a missing hour of wind is not.

    Idempotent, so polling every fifteen minutes against an hourly service
    re-stores nothing. Returns the number of new hours.
    """
    location = cfg.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        return 0                    # nothing configured yet; not an error

    if not cfg.get("capture_weather", True):
        return 0

    try:
        rows = weather.recent(lat, lon, past_days=past_days)
    except weather.WeatherUnavailable as e:
        log(f"WARN weather unavailable: {e}")
        return 0
    except Exception as e:
        log(f"WARN weather failed: {type(e).__name__}: {e}")
        return 0

    try:
        return store.insert_weather(conn, store.place_key(lat, lon), rows,
                                    source=weather.SLUG)
    except Exception as e:
        log(f"WARN could not store weather: {type(e).__name__}: {e}")
        return 0


def backfill_weather(conn, cfg, days=None):
    """Fill weather back to where the readings start, or a given number of days.

    The point of this is that a correlation needs history on both sides. A
    year of PM2.5 against a week of wind is a week of evidence, so the default
    reaches back to the oldest reading rather than to an arbitrary window.

    Returns (hours_stored, oldest_covered) or (0, None).
    """
    location = cfg.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        log("no location configured — nothing to fetch weather for")
        return 0, None

    now = datetime.now(timezone.utc)
    if days:
        since = now - timedelta(days=int(days))
    else:
        # first_utc, not first — store.counts() names it after the column it
        # comes from. Getting this wrong reported "no readings yet" against a
        # full database, which reads as the feature being broken.
        spans = [c["first_utc"] for c in store.counts(conn) if c.get("first_utc")]
        if not spans:
            log("no readings yet — nothing to line weather up against")
            return 0, None
        since = datetime.fromisoformat(min(spans))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

    place = store.place_key(lat, lon)
    log(f"fetching weather from {since.date()} to {now.date()}")

    stored = 0
    for kind, start, end in weather.plan_backfill(since, now, now):
        try:
            if kind == "archive":
                rows = weather.history(lat, lon, start, end)
            else:
                span = max(1, (now - start).days + 1)
                rows = weather.recent(lat, lon, past_days=span)
        except weather.WeatherUnavailable as e:
            # Reported rather than swallowed: this one was asked for
            # explicitly, so silence would look like success.
            log(f"WARN {kind} weather failed: {e}")
            continue
        n = store.insert_weather(conn, place, rows, source=weather.SLUG)
        stored += n
        log(f"  {kind}: {len(rows)} hours fetched, {n} new")

    span = store.weather_span(conn, place)
    return stored, span.get("first")


def do_poll(cfg, key=None):
    """One cycle: for every source, repair gaps then take a live reading.

    A source that fails must not stop the others -- that is the whole point of
    having several. Each is isolated, and a total failure is distinguished
    from a partial one.
    """
    conn = open_store()
    try:
        registered = register_sources(conn, cfg)
        if not registered:
            log("WARN no sources configured — nothing to poll. "
                "Run: python3 setup.py")
            return None

        ok, failed = 0, []
        fail_state = _failure_state()
        threshold = int(cfg.get("source_failure_alert_after", 4) or 0)
        for sid, src, provider in registered:
            label = f"{provider.slug}/{src.get('site_id')}"
            try:
                # 1. gap check, per source and against its own cadence
                last = store.last_observed(conn, sid)
                if last is None:
                    n = backfill_source(
                        conn, sid, src, provider,
                        days=cfg.get("backfill_days_on_first_run", 7))
                    log(f"first run for {label}: seeded {n} historical rows")
                else:
                    gap = datetime.now(timezone.utc) - last
                    if gap > gap_threshold_for(provider, cfg):
                        log(f"gap of {gap} on {label} — backfilling")
                        backfill_source(conn, sid, src, provider, since_utc=last)

                # 2. live reading
                pm = poll_source(conn, sid, src, provider, cfg)
                log(f"  {label}: PM2.5 {pm} ug/m3")
                ok += 1
                if threshold:
                    # Not `True`. The fetch succeeding says the *provider* is
                    # up; it says nothing about the sensor. A PurpleAir sensor
                    # that has dropped off the network keeps being served with
                    # its last timestamp, so this branch ran on every poll for
                    # about two days on a real install while the record stood still
                    # and nobody was told.
                    record_source_result(
                        fail_state, label,
                        source_is_reporting(conn, sid, provider, cfg),
                        threshold, cfg, reason="stale")
            except Exception as e:
                failed.append(label)
                log(f"WARN {label} failed: {type(e).__name__}: {e}")
                if threshold:
                    record_source_result(fail_state, label, False, threshold,
                                         cfg, reason="unreachable")

        if threshold:
            try:
                _save_failure_state(fail_state)
            except Exception as e:
                log(f"WARN could not save failure state: {type(e).__name__}: {e}")

        if ok == 0:
            log(f"ERROR every source failed ({', '.join(failed)})")

        # 3. the weather that produced it. After the readings, because a
        #    reading is the product and weather is context for it -- if this
        #    is going to fail, it must fail with the readings already stored.
        try:
            fresh = capture_weather(conn, cfg)
            if fresh:
                log(f"weather: {fresh} new hour(s)")
        except Exception as e:                     # belt and braces
            log(f"WARN weather capture raised: {type(e).__name__}: {e}")

        # 4. fuse and publish
        latest = build_latest(conn, cfg)
        write_json_atomic(LATEST_PATH, latest)
        remember_data_dir()
        log(f"poll ok — {latest['scale_label']} {latest['aqi']} "
            f"({latest['band']}) via {latest['provenance']}"
            + (f" [{len(failed)} source(s) down]" if failed else ""))

        # 5. retention, only if the user asked for a finite window. Silent
        #    deletion of irreplaceable history would be indefensible, so this
        #    logs what it removed every time it removes anything.
        keep = int(cfg.get("retention_days") or 0)
        if keep > 0:
            try:
                removed, kept, oldest = store.prune(conn, keep)
                if removed:
                    log(f"retention: removed {removed:,} readings older than "
                        f"{keep} days ({kept:,} kept, oldest now {oldest})")
            except Exception as e:
                log(f"WARN retention failed: {type(e).__name__}: {e}")

        # 6. routine backup, if enabled. After the write, so the archive
        #    includes what just arrived, and never allowed to break polling.
        ab = cfg.get("auto_backup") or {}
        if ab.get("enabled", True) and ok > 0:
            try:
                import backup as _backup
                made = _backup.auto(keep=int(ab.get("keep", 7)),
                                    interval_hours=float(ab.get("interval_hours", 24)))
                if made:
                    log(f"routine backup written to {made}")
            except Exception as e:
                log(f"WARN automatic backup failed: {type(e).__name__}: {e}")

        # 7. alerting — never let a notification failure break data collection
        try:
            maybe_alert(latest, cfg)
        except Exception as e:
            log(f"WARN alerting failed: {type(e).__name__}: {e}")

        return latest
    finally:
        conn.close()


# ------------------------------------------------------------------ alerts

#: AppleScript that reads its text from `on run argv` rather than from an
#: interpolated body. Same shape as the folder chooser, and for the same
#: reason -- see notification_commands().
_OSA_NOTIFY = (
    "on run argv\n"
    "  set t to item 1 of argv\n"
    "  set s to item 2 of argv\n"
    "  set m to item 3 of argv\n"
    "  set snd to item 4 of argv\n"
    "  if snd is \"\" then\n"
    "    display notification m with title t subtitle s\n"
    "  else\n"
    "    display notification m with title t subtitle s sound name snd\n"
    "  end if\n"
    "end run"
)

#: PowerShell toast. Reads every string from the environment, because
#: -Command takes one string and there is no argv to use.
_PS_NOTIFY = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType=WindowsRuntime] > $null;"
    "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(2);"
    "$n = $x.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($x.CreateTextNode($env:AIRO_NOTIFY_TITLE)) > $null;"
    "$n.Item(1).AppendChild($x.CreateTextNode("
    "$env:AIRO_NOTIFY_SUBTITLE + ' ' + $env:AIRO_NOTIFY_MESSAGE)) > $null;"
    "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'Airo').Show($t)"
)


def notification_commands(title, subtitle, message, sound=None,
                          os_name=None, platform=None):
    """How to put a notification on screen, per platform.

    Returns a list of `(argv, env, stdin)` triples to try in order. `env` is
    an overlay on the current environment; `stdin` is text to feed the
    process, or None.

    This existed only for macOS. On Linux and Windows `notify()` shelled to
    osascript, raised FileNotFoundError, caught it, logged a warning and
    returned False -- so alerting, a headline feature enabled by default, did
    nothing at all on two of the three platforms Airo installs on, and said so
    only in a log nobody reads. The risk register already carried "an alert
    that never fires at all" about a bug in the firing logic; this was the
    same outcome by a different route.

    **The text is data, never part of a script body.** `message` contains a
    `site_name` that arrived in a provider's JSON, so it is third-party text
    heading for a shell. How it travels:

      macOS    as arguments to `on run argv`. osascript hands everything after
               the script to the run handler, where it is a string value and
               cannot be anything else.
      Linux    as argv to notify-send. argv is a list and no shell is
               involved, so a token is one token whatever it contains.
      Windows  as environment variables the script reads. PowerShell's
               -Command takes a single string, and its quoting rules are their
               own hazard.

    `os_name` and `platform` are arguments so every platform's commands can be
    inspected from any platform. Two thirds of this would be unreachable in CI
    otherwise, which is exactly how it stayed broken.
    """
    os_name = os.name if os_name is None else os_name
    platform = sys.platform if platform is None else platform
    title, subtitle, message = str(title), str(subtitle), str(message)
    sound = str(sound or "")

    if platform == "darwin":
        return [(["osascript", "-e", _OSA_NOTIFY,
                  title, subtitle, message, sound], {}, None)]

    if os_name == "nt" or platform.startswith("win"):
        env = {"AIRO_NOTIFY_TITLE": title,
               "AIRO_NOTIFY_SUBTITLE": subtitle,
               "AIRO_NOTIFY_MESSAGE": message}
        return [(["powershell", "-NoProfile", "-NonInteractive",
                  "-Command", _PS_NOTIFY], env, None)]

    # Linux and anything else with a freedesktop notifier. `--` so a title
    # beginning with a dash is not read as an option.
    return [(["notify-send", "--app-name=Airo", "--",
              f"{title}: {subtitle}", message], {}, None)]


def notification_report(os_name=None, platform=None):
    """Lines for --doctor: can an alert actually reach this screen?

    An alert that cannot be delivered should be visible before the night it
    matters rather than discovered afterwards -- and on a headless server the
    honest answer is "no, and that is expected".
    """
    cmds = notification_commands("Airo", "test", "test",
                                 os_name=os_name, platform=platform)
    program = cmds[0][0][0] if cmds else None
    if program and shutil.which(program):
        return [f"notifications  using {program}"]
    return [f"notifications  {program} was not found, so alerts cannot appear",
            f"               on screen. Install it, or expect alerts to be",
            f"               recorded in the log only."]


def notify(title, subtitle, message, sound=None, os_name=None, platform=None):
    """Put a notification on screen. Best-effort — never raises.

    A poll must not die because a desktop notifier did: the reading is the
    product and the notification is a courtesy. But a courtesy that silently
    never arrives is worse than one that says it cannot, so a total failure is
    logged rather than swallowed.
    """
    try:
        candidates = notification_commands(title, subtitle, message, sound,
                                           os_name=os_name, platform=platform)
    except Exception as e:
        log(f"WARN could not build a notification command: "
            f"{type(e).__name__}: {e}")
        return False

    problems = []
    for argv, env, stdin in candidates:
        try:
            environ = dict(os.environ)
            environ.update(env or {})
            subprocess.run(argv, input=stdin, text=True, timeout=10,
                           check=False, env=environ,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            problems.append(f"{argv[0]}: {type(e).__name__}: {e}")

    log(f"WARN no notification could be shown ({'; '.join(problems)}). "
        f"The alert is in the log; nothing appeared on screen.")
    return False


def _alert_state():
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_band": None, "last_alert_utc": None, "last_kind": None}


def _in_quiet_hours(cfg, now):
    """Is `now` inside the configured do-not-disturb window?

    The window wraps midnight, which is the normal case -- 22 to 7 is what
    people actually set. So the comparison is inclusive-start, exclusive-end
    when it does not wrap and a union of two ranges when it does; treating it
    as a simple `start <= h < end` would make every overnight window empty.

    Quiet hours suppress the notification, never the reading. The poll still
    runs and the air is still recorded, so the morning's history is complete.
    """
    q = (cfg.get("alerts") or {}).get("quiet_hours")
    if not q or len(q) != 2:
        return False
    start, end = int(q[0]), int(q[1])
    # The user's wall clock, not this machine's. A Pi left on UTC in Brisbane
    # was silencing 08:00-17:00 local -- the middle of the day -- and notifying
    # at 3am, which is how somebody ends up turning alerts off altogether.
    h = local_now(cfg, now).hour
    return (start <= h < end) if start < end else (h >= start or h < end)


def maybe_alert(latest, cfg):
    """Notify when air quality crosses into, or is climbing toward, the warning band.

    Two triggers:
      1. CROSSED  — the 10-minute average has entered the alert band (default 67 = Fair/amber)
      2. CLIMBING — rising fast enough that it is likely to get there shortly

    A cooldown prevents repeat alerts while conditions hover at the threshold.
    """
    a = effective_alerts(cfg)
    if not a["enabled"]:
        return None

    # threshold_aqi is expressed in the *configured* scale's units, so a
    # config tuned for Australia means something else entirely under US EPA.
    # threshold_pm25 is the scale-independent form and wins when both are set,
    # which is what anyone sharing a config across countries should use.
    scale_name, _ = get_scale(cfg)
    if a["threshold_pm25"] is not None:
        threshold = float(aqi_for(float(a["threshold_pm25"]), scale_name))
    else:
        threshold = float(a["threshold_aqi"])
    rise_delta = float(a["rising_delta"])
    cooldown = int(a["cooldown_minutes"])
    notify_clear = bool(a["notify_when_clear"])

    aqi = latest.get("aqi")
    if aqi is None:
        return None

    now = local_now(cfg)
    st = _alert_state()

    # cooldown
    if st.get("last_alert_utc"):
        try:
            since = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(st["last_alert_utc"])).total_seconds() / 60
            if since < cooldown:
                return None
        except Exception:
            pass

    avg = latest.get("averages_aqi") or {}
    ten, hour = avg.get("10min"), avg.get("60min")
    band = latest.get("band")
    was_over = bool(st.get("over_threshold"))
    ugm = latest.get("pm25_10min")

    kind = msg = None

    if aqi >= threshold and not was_over:
        kind = "crossed"
        title = f"Air quality: {band}"
        sub = f"AQI {aqi:.0f} — into the {band.lower()} band"
        msg = (f"{ugm:.1f} µg/m³. Close up and start the purifiers."
               if ugm is not None else "Close up and start the purifiers.")
    elif (not was_over and ten is not None and hour is not None
          and (ten - hour) >= rise_delta and ten >= threshold * 0.6):
        kind = "climbing"
        title = "Air quality climbing"
        sub = f"AQI {ten:.0f}, up {ten - hour:.0f} on the hour average"
        msg = "Trending toward the warning band. Worth closing up now."
    elif was_over and aqi < threshold * 0.85 and notify_clear:
        kind = "cleared"
        title = "Air quality cleared"
        sub = f"AQI {aqi:.0f} — back to {band.lower()}"
        msg = "Safe to ventilate again."

    # An uncorroborated reading still alerts -- a fire next door is genuinely
    # the air being breathed, and staying silent would be the more dangerous
    # error. But the notification must say the neighbours do not see it, so
    # the user can judge whether to act on it or go and check the sensor.
    if kind and latest.get("uncorroborated"):
        sub += " · not confirmed nearby"
        msg = (msg or "") + (
            " Nearby sources do not show this, so it may be a very local "
            "source or a sensor fault.")

    if not kind:
        # still track the band so a later crossing is detected correctly
        if was_over != (aqi >= threshold):
            ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            st["over_threshold"] = aqi >= threshold
            write_json_atomic(ALERT_STATE_PATH, st)
        return None

    if _in_quiet_hours(cfg, now):
        log(f"alert suppressed (quiet hours): {kind} AQI {aqi:.0f}")
    else:
        notify(title, sub, msg, sound=a.get("sound", "Ping"))
        log(f"ALERT {kind}: AQI {aqi:.0f} ({band})")

    write_json_atomic(ALERT_STATE_PATH, {
        "last_band": band,
        "last_kind": kind,
        "last_alert_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "over_threshold": aqi >= threshold,
    })
    return kind


# ------------------------------------------------------------------ server

def _serving_this_project(port, timeout=1.5):
    """Is the thing on this port our own server, for this same project?

    Distinguishes "a stale copy of Airo is squatting here" -- which the user
    must know about, because it serves old data and looks like a dead agent --
    from "something unrelated has the port", which is merely an inconvenience
    to route around.

    Asks /api/ping, which answers whether or not a poll has ever run. It used
    to ask /api/latest, which 404s until there are readings -- so on a fresh
    install the server was invisible to its own opener: first_run() started
    one, failed to find it, and never opened the settings page. That is the
    first thing a new user sees, and it did nothing.
    """
    for path in ("/api/ping", "/api/latest"):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError:
            # Something IS listening and answered with a status. That may be a
            # server predating /api/ping, so the older question is still worth
            # asking.
            continue
        except Exception:
            # Refused, timed out, or unreachable: nothing is there, and asking
            # a second time costs another timeout for an answer that cannot
            # change. Returning here matters -- running_server_port() probes up
            # to 22 candidates, so a needless second attempt per port doubled
            # the wait before `--open` gives up, and pushed serve_forever's own
            # collision check past the point a caller was still waiting.
            return None
        if path == "/api/ping" and payload.get("airo") is not True:
            return None        # something else answering on our path
        return payload.get("location_name") or True
    return None                # not us, or not answering


SERVE_PAGES = {"dashboard": "/dashboard.html", "settings": "/settings"}

# The running server, once serve_forever() has bound one.
_active_server = None

SERVE_PORT_MARKER_NAME = "serve-port"


def _remember_serve_port(port):
    """Record the port the server actually bound.

    It is not always the configured one: an unrelated program holding 8787
    makes serve_forever() move to the next free port, deliberately, because
    refusing to open a dashboard over a port clash is a bad trade. Anything
    that wants to *open* the dashboard therefore cannot assume the configured
    number -- and the tray assumed it, as a literal, for the whole of v0.5.
    """
    try:
        marker = CONFIG_PATH.parent / SERVE_PORT_MARKER_NAME
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(port), encoding="utf-8")
    except OSError:
        pass          # a missing marker only costs a slower search below


def running_server_port(cfg=None):
    """The port an Airo of this project is serving on, or None.

    Checks the remembered port first, then the configured one, then the range
    serve_forever() would have moved into. Cheap timeouts: this runs before
    opening a browser, and a user waiting on a menu click notices a second.
    """
    cfg = load_config() if cfg is None else cfg
    configured = int(cfg.get("serve_port", 8787) or 8787)

    candidates = []
    try:
        remembered = (CONFIG_PATH.parent / SERVE_PORT_MARKER_NAME).read_text(
            encoding="utf-8").strip()
        candidates.append(int(remembered))
    except (OSError, ValueError):
        pass
    candidates.append(configured)
    candidates.extend(range(configured + 1, configured + 21))

    for port in dict.fromkeys(candidates):        # de-duped, order kept
        if _serving_this_project(port, timeout=0.35) is not None:
            return port
    return None


def folder_chooser_commands(prompt="Choose a folder", os_name=None, platform=None):
    """The command(s) that would put up a folder picker on a given platform.

    Returns a list of `(argv, env)` pairs, tried in order. `env` is an overlay
    on the current environment and is empty for most.

    **The prompt is passed as data, never interpolated into a script body.**
    It used to be, and that was an injection: a caller-supplied string went
    into an AppleScript string literal, so a `"` closed the literal and
    everything after it became AppleScript. `do shell script "..."` in a
    prompt was arbitrary command execution as the user. The PowerShell branch
    had the same shape with `'`.

    Reaching it needed the token that guards /api/choose-folder, so this was
    one guard deep rather than open -- but "a secret is the only thing between
    a request body and a shell" is not a position to stay in when the fix is
    to stop building a script out of user text.

    How the prompt travels instead:

      macOS    as an argument to `on run argv`. osascript passes anything
               after the script to the run handler, where it is a string
               value and cannot be anything else.
      Windows  as an environment variable the script reads. PowerShell's
               -Command takes one string, so there is no argv to use, and
               quoting rules there are their own hazard.
      Linux    already safe: argv is a list and no shell is involved, so
               `--title=...` is one token whatever it contains.

    `os_name` and `platform` are arguments rather than reads of os.name and
    sys.platform, so a test can ask for another platform's commands without
    reaching into either module. Patching them globally mid-suite is not
    harmless: os.name is consulted by half the standard library, and a test
    doing it made the Windows runner build a macOS command.
    """
    os_name = os.name if os_name is None else os_name
    platform = sys.platform if platform is None else platform

    if os_name == "nt":
        # Single braces around the if-body. They were doubled -- left over
        # from an f-string that no longer interpolated -- which made the body
        # a *script block literal* rather than a statement: PowerShell would
        # emit the block's own source text and the caller would take that for
        # a path. Nobody had run the Windows picker to notice.
        script = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                  "$d.Description = $env:AIRO_FOLDER_PROMPT; "
                  "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }")
        return [(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                  script],
                 {"AIRO_FOLDER_PROMPT": prompt})]

    if platform == "darwin":
        # `activate` first, and the picker inside the same tell block.
        #
        # Without it the dialog is created by a process with no foreground
        # presence -- the server runs detached, with no controlling terminal
        # and no Dock entry -- so macOS opens it *behind* whatever the user is
        # looking at. From their side the Browse button does nothing at all,
        # which is worse than an error: there is nothing to report and nothing
        # to try.
        #
        # System Events is told to activate rather than this process, because
        # a bare osascript has no window of its own to bring forward.
        script = (
            'on run argv\n'
            '  set thePrompt to item 1 of argv\n'
            '  tell application "System Events"\n'
            '    activate\n'
            '    set chosen to choose folder with prompt thePrompt\n'
            '  end tell\n'
            '  return POSIX path of chosen\n'
            'end run'
        )
        return [(["osascript", "-e", script, prompt], {})]

    # Neither is guaranteed present, so try both before giving up.
    return [(["zenity", "--file-selection", "--directory", f"--title={prompt}"], {}),
            (["kdialog", "--getexistingdirectory", str(Path.home())], {})]


def choose_folder(prompt="Choose a folder"):
    """Ask the desktop for a folder, natively. Returns (path, reason).

    A browser page cannot choose a directory -- a file input gives you file
    contents, never a path -- so the settings page would otherwise be stuck
    asking people to type one. The server is on the user's own machine, so it
    can put up the real dialog and hand back what they picked.

    `path` is None when nothing was chosen; `reason` distinguishes "cancelled"
    from "this system has no picker", because those need different things said
    to the user. Every platform is implemented rather than one being a
    constant: a fallback that always returns nothing is a feature that
    silently does nothing, which this project has shipped four times.
    """
    for cmd, overlay in folder_chooser_commands(prompt):
        # The overlay carries the prompt where a command cannot take it as an
        # argument. Built on top of the real environment rather than replacing
        # it: a picker launched with an empty environment loses PATH, DISPLAY
        # and the session variables it needs to find a desktop at all.
        env = None
        if overlay:
            env = dict(os.environ)
            env.update(overlay)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=300, env=env)
        except FileNotFoundError:
            continue                      # try the next chooser
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"the folder chooser failed: {type(e).__name__}: {e}"
        picked = (r.stdout or "").strip()
        if picked:
            return picked, "chosen"
        # A non-zero exit with no output is how every one of these reports
        # cancellation, which is not an error and must not be shown as one.
        return None, "cancelled"

    return None, "no folder chooser is available on this system"


def _parsable_time(value):
    """Whether a stored timestamp can be placed on a chart at all."""
    try:
        datetime.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def running_from_an_installed_app():
    """Is this the packaged app rather than a checkout?

    The two audiences need different sentences. Someone who downloaded a disk
    image has no terminal open and no reason to want one; "run: python3
    setup.py" is, to them, an error message. Someone working in a checkout
    wants the command.
    """
    return HERE.name == "airo" and (HERE.parent / "runtime").is_dir()


def how_to(action):
    """One sentence telling *this* user how to do something.

    Kept in one place so a new message cannot quietly reintroduce a terminal
    instruction into an app that has no terminal. `action` is what the user
    wants, not the command that does it.
    """
    installed = running_from_an_installed_app()
    phrasing = {
        "configure": ("open Settings from the Airo menu",
                      "run: python3 setup.py"),
        "restart": ("quit and reopen Airo",
                    "run: python3 scheduler.py restart"),
        "restore": ("open Settings from the Airo menu and use Restore",
                    "run: python3 backup.py restore"),
        "check": ("open Settings from the Airo menu",
                  "run: python3 poller.py --doctor"),
    }
    return phrasing[action][0 if installed else 1]


def set_alerts(enabled):
    """Turn notifications on or off. Returns the value now in force.

    Lives here rather than in a shell script because whether alerting is on is
    a setting, and what a valid setting looks like is `validate_settings()`'s
    answer -- the same one the settings page gets. A shell script editing JSON
    is a second writer with its own idea of the shape.
    """
    cfg, errors = apply_settings({"alerts": {"enabled": bool(enabled)}})
    if errors:
        raise ValueError(f"could not change alerts: {errors}")
    return effective_alerts(cfg)["enabled"]


def recent_log(lines=40):
    """The tail of the poller's log, for someone asking what happened.

    Reads the whole file rather than seeking: these are trimmed on every poll,
    so they are small, and correctness beats cleverness for a diagnostic.
    """
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-int(lines):]


def stop_server():
    """Stop a local server this project started. Returns True if one was.

    Only ever stops *ours*: `_serving_this_project()` answers that question,
    and killing something else that happens to hold the port would be a
    surprising thing for a menu item to do.
    """
    port = running_server_port()
    if port is None:
        return False
    if _serving_this_project(port) is None:
        return False
    try:
        out = subprocess.run(["pkill", "-f", "poller.py --serve"],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def uninstall_everything():
    """Stop Airo running, and leave every reading exactly where it is.

    Returns a dict describing what was removed and what was kept.

    **Nothing here deletes data, ever.** Uninstalling is a statement about
    wanting the software to stop, not about wanting years of measurements
    destroyed -- and those cannot be regenerated. The directory is named so
    somebody who *does* want it gone can make that decision themselves, with
    their eyes open, which is the one way it should ever happen.

    That asymmetry is deliberate: an uninstaller that removes the software is
    reversible in ten minutes, and one that removes the record is not
    reversible at all.
    """
    import scheduler

    removed, kept, problems = [], [], []

    for label, action in (("background polling", scheduler.uninstall),
                          ("menu-bar app", scheduler.uninstall_tray)):
        try:
            ok, message = action()
            (removed if ok else problems).append(
                label if ok else f"{label}: {message}")
        except Exception as e:
            problems.append(f"{label}: {type(e).__name__}: {e}")

    rows = 0
    if db_path().exists():
        try:
            conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
            rows = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            conn.close()
        except Exception:
            pass

    if db_path().exists():
        kept.append(f"{rows:,} readings in {DATA}")
    if CONFIG_PATH.exists():
        kept.append(f"your settings in {CONFIG_PATH}")

    return {"removed": removed, "kept": kept, "problems": problems,
            "data_dir": str(DATA), "readings": rows}


def first_run(open_browser=True):
    """Everything a freshly installed app must do, safe to call every launch.

    The installer's job is not finished when files are copied: nothing is
    collected until a background poll is scheduled, and nothing is collected
    *usefully* until the user has said where they are. This is the one place
    that knows both, so the app can call it on every start and the answer is
    the same the second time.

    Idempotent by construction rather than by a "have I run?" flag: a flag is
    a thing that can be wrong, and the actions here are each already safe to
    repeat. Registering the agent replaces any existing registration;
    creating the data directory is a mkdir; opening the settings page only
    happens while there is nothing configured.

    Returns a dict describing what it did, so a caller can say so.
    """
    import scheduler

    did = {"data_dir": str(DATA), "scheduled": False, "configured": False,
           "opened": None, "problems": []}

    if not ensure_data_dir():
        did["problems"].append(f"cannot write to {DATA}")
        return did

    cfg = load_config()
    did["configured"] = bool(cfg.get("sources"))

    # Schedule the background poll whether or not anything is configured. An
    # unconfigured poll logs that it has nothing to do, which is harmless --
    # and the alternative is an app that silently collects nothing until
    # somebody remembers to come back and switch it on.
    try:
        ok, message = scheduler.install(int(cfg.get("poll_minutes", 15) or 15))
        did["scheduled"] = bool(ok)
        if not ok:
            did["problems"].append(message)
    except Exception as e:
        did["problems"].append(f"could not schedule background polls: {e}")

    # Only steer someone to the settings page when there is a reason to be
    # there. Opening it on every launch would be a browser tab nobody asked
    # for, every login, forever.
    if open_browser and not did["configured"]:
        url, _ = open_page("settings")
        did["opened"] = url

    return did


def page_url(what="dashboard", launch=True, wait_seconds=6.0):
    """Resolve the URL of a served page, starting a server if none is running.

    Every decision lives here rather than in whatever asked: whether a server
    is up, which port it actually got, and what URL that makes. The caller's
    job is to run one command -- hard rule 7 one level up. The tray previously
    held `http://127.0.0.1:8787/dashboard.html` as a literal, which is wrong
    for anyone who changed serve_port and wrong for everyone the moment an
    unrelated program takes 8787 and the server moves.

    Split from open_page() when settings moved into the app's own window: the
    tray needs the URL to point a webview at, and must not open a browser to
    get it. Opening a browser is now a separate decision made by the caller,
    which is the only difference between the two.

    Returns (url, started) or (None, started) if no server could be reached.
    """
    path = SERVE_PAGES.get(what)
    if path is None:
        raise ValueError(f"unknown page {what!r}; known: "
                         f"{', '.join(sorted(SERVE_PAGES))}")

    port = running_server_port()
    started = False
    if port is None and launch:
        # Detached, so this command can return and the page stays reachable.
        # serve_forever() refuses a port another Airo holds, so a race here
        # costs one exited process rather than two servers.
        try:
            subprocess.Popen(
                [sys.executable, str(HERE / "poller.py"), "--serve"],
                cwd=str(HERE), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            started = True
        except OSError as e:
            log(f"could not start the local server: {e}")
            return None, False

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            port = running_server_port()
            if port is not None:
                break
            time.sleep(0.25)

    if port is None:
        return None, started

    return f"http://127.0.0.1:{port}{path}", started


def open_page(what="dashboard", launch=True, wait_seconds=6.0):
    """Resolve a page's URL and open it in the user's browser.

    Still the right thing for the dashboard, which is a full-width reading
    surface a browser suits better than a 480px tray window. Settings no
    longer comes through here -- it is hosted in the app's own window, so
    configuring Airo never leaves the app. See ARCHITECTURE 2.8.
    """
    url, started = page_url(what, launch=launch, wait_seconds=wait_seconds)
    if url is None:
        return None, started

    # Ask the page whether it is there before putting it in front of somebody.
    #
    # A URL was handed to the browser on the strength of having *started* a
    # server, not on the server answering. When it had not come up — or had
    # come up and exited — the result was a tab reading "Problem loading
    # page", and clicking the menu item again produced another one. The
    # maintainer collected about fifteen.
    #
    # An unreachable page is worth saying out loud, and worth not opening: a
    # browser tab is the most expensive way possible to report that a local
    # server is down.
    if not page_answers(url):
        log(f"{url} is not answering; not opening a browser at it")
        return None, started

    if not launch_browser(url):
        log(f"could not open a browser; the page is at {url}")
    return url, started


def page_answers(url, timeout=1.5):
    """Whether *our* server is actually answering on `url`'s port.

    Asks `_serving_this_project`, which every other caller already uses,
    rather than making an independent HTTP request. Two notions of "the server
    is up" would be two things to keep in step, and the first version of this
    was exactly that: it bypassed the check the rest of the module makes, so
    three tests that legitimately simulate a running server started failing
    because a real request went out to a port nothing was on.

    That is the same shape as the browser call this function exists to guard —
    a side effect reaching past the seam the tests use. Twice in one change is
    the argument for having one seam per effect, not for being more careful.
    """
    port = _port_of(url)
    if port is None:
        return False
    return _serving_this_project(port, timeout=timeout) is not None


def _port_of(url):
    """The port in a URL, or None. Defaults to http's 80 when unstated."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def launch_browser(url):
    """Open `url` in the user's browser. True if something took it.

    `webbrowser.open()` returns a bool and this ignored it, so a browser that
    never appeared looked identical to one that did -- and the tray discards
    the result as well, which made the menu item a thing that does nothing and
    says nothing. Both halves of that are fixed; this is the half that decides
    whether it worked.

    On macOS `/usr/bin/open` is tried first, by absolute path. `webbrowser`
    ends up at `osascript -e 'open location ...'`, which hands the URL to the
    default handler without necessarily bringing it forward -- so a click can
    load the page into a window behind everything else, which to the person
    who clicked is indistinguishable from nothing happening. `open` activates
    the application. The absolute path matters because this runs from a
    launchd agent, whose PATH is not a login shell's.
    """
    if sys.platform == "darwin":
        try:
            done = subprocess.run(["/usr/bin/open", url], capture_output=True,
                                  text=True, timeout=15)
            if done.returncode == 0:
                return True
            log(f"/usr/bin/open refused {url}: {done.stderr.strip()}")
        except (OSError, subprocess.SubprocessError) as e:
            log(f"could not run /usr/bin/open ({type(e).__name__}: {e})")

    try:
        import webbrowser        # stdlib; imported here so --status never pays
        return bool(webbrowser.open(url))
    except Exception as e:       # a headless box has no browser to open
        log(f"no browser could be opened ({type(e).__name__}: {e})")
        return False


def _free_port(start, tries=20):
    """First genuinely free port at or after `start`.

    Deliberately does NOT set SO_REUSEADDR. Its meaning differs by platform:
    on Windows it permits binding a port another socket is *actively* using,
    so a probe with it set reports an occupied port as free and two servers
    end up fighting over it. Without it, a POSIX port in TIME_WAIT looks busy
    and gets skipped -- conservative, and the right way round.
    """
    import socket
    for candidate in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):      # Windows
                try:
                    sock.setsockopt(socket.SOL_SOCKET,
                                    socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    return None


class LoopbackHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that does not consult DNS to start.

    `HTTPServer.server_bind` calls `socket.getfqdn(host)` to fill in
    `server_name` -- a *reverse DNS lookup*, performed inside the constructor,
    before the server exists and before anything can be logged. On a machine
    where reverse resolution is slow, filtered or pointed at an unreachable
    resolver, that blocks for tens of seconds. It blocked for more than thirty
    on a CI runner, which is how it was found: the thread was alive, no server
    object existed, and nothing had been logged, because none of that code had
    been reached yet.

    The lookup buys nothing here. This server binds 127.0.0.1 only and the
    name is used solely to fill in CGI environment variables we never read, so
    it is set to the host we bound and DNS is left out of it entirely.

    Not a micro-optimisation: it is the difference between the dashboard
    appearing at once and appearing eventually, on exactly the networks --
    corporate, VPN'd, captive -- where a user is least inclined to assume the
    tool is fine.
    """

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve_forever(port):
    """Serve the dashboard and settings pages on loopback until interrupted.

    Bound to 127.0.0.1, never 0.0.0.0: the pages render a home location and
    every reading taken there, and the machine may well be on a cafe network.

    The two ways a port can already be taken need opposite responses, which is
    why this does not simply retry. Another Airo means refuse -- a second
    server would answer with a second copy of the data and make "which one am I
    looking at?" unanswerable, which is exactly the confusion a stale --serve
    from another directory caused before this guard existed. Anything else
    means move to the next free port, because refusing to draw local data over
    an unrelated program's choice of 8787 is a bad trade for the user.
    """
    handler = partial(QuietHandler, directory=str(HERE))
    try:
        httpd = LoopbackHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Work out which kind of collision this is before deciding what to do.
        other = _serving_this_project(port)
        if other is not None:
            # Another Airo is already there. Starting a second one would serve
            # a second copy of the data and make "which one am I looking at?"
            # an unanswerable question.
            raise SystemExit(
                f"Airo is already serving on port {port}"
                f"{f' ({other})' if isinstance(other, str) else ''}.\n"
                f"Open http://localhost:{port}/dashboard.html, or stop it with:\n"
                f"  pkill -f 'poller.py --serve'"
            )

        # Something unrelated holds the port. Move rather than refuse -- a
        # dashboard that will not start because an unrelated process took 8787
        # is a bad trade for the user.
        alt = _free_port(port + 1)
        if alt is None:
            raise SystemExit(
                f"Port {port} is in use by something else, and no free port was "
                f"found in the next 20. Set serve_port in {CONFIG_PATH}."
            )
        log(f"port {port} is in use by another program — using {alt} instead")
        log(f"  set serve_port in {CONFIG_PATH} to make this permanent")
        port = alt
        httpd = LoopbackHTTPServer(("127.0.0.1", port), handler)

    _remember_serve_port(port)
    # Held so a caller can stop it. Without this the only way to shut the
    # server down is to end the process, which makes "does serve_forever
    # actually record its port?" untestable except by inspecting source.
    global _active_server
    _active_server = httpd
    log(f"serving {HERE} at http://localhost:{port}/dashboard.html")
    try:
        httpd.serve_forever()
    finally:
        # Best effort: a stale marker only costs the next opener a short scan,
        # while leaving one behind after a crash costs nothing at all.
        try:
            (CONFIG_PATH.parent / SERVE_PORT_MARKER_NAME).unlink()
        except OSError:
            pass


# ------------------------------------------------- guarding the local server
#
# Binding to 127.0.0.1 keeps other machines out. It does not keep other *pages*
# out: every site the user visits can reach this server from inside their
# browser, with their machine doing the asking. While the API was read-only
# that bought an attacker little -- responses are unreadable cross-origin. The
# moment it accepts writes, an ordinary web page could repoint someone's
# monitoring at another suburb, or move data_dir and quietly abandon years of
# readings.
#
# Four independent checks, because each one alone has a hole:
#
#   Host          a name the attacker owns, resolved to 127.0.0.1, is
#                 same-origin as far as the browser is concerned -- that is
#                 DNS rebinding, and an Origin check does not see it
#   Origin        catches the ordinary cross-site request
#   Content-Type  requiring JSON means a cross-origin request can no longer be
#                 a CORS "simple request", so it needs a preflight, which is
#                 never answered
#   token         a per-process secret the page is given when it is served,
#                 unreadable to any other origin
#
# The token is deliberately *not* written to disk. Nothing else needs it -- the
# tray opens a URL and the page is handed the token by the server -- so a file
# would be one more credential at rest for no gain, and this one is worthless
# after a restart anyway.

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# What settings.html carries where its token goes. Substituted when the page is
# served; if this ever appears in a response, the static handler answered
# instead of _serve_settings_page() and every save on that page will fail.
SETTINGS_TOKEN_PLACEHOLDER = "__AIRO_TOKEN__"

_SERVER_TOKEN = None


def server_token():
    """The secret required on every mutating request, made once per process.

    Regenerated when the server restarts, which invalidates any settings page
    left open. That is the right trade: the page reloads, and a token that
    outlives the process it authenticates is a token worth stealing.
    """
    global _SERVER_TOKEN
    if _SERVER_TOKEN is None:
        _SERVER_TOKEN = secrets.token_urlsafe(32)
    return _SERVER_TOKEN


def _hostname_of(header):
    """The host part of a Host or Origin header, without port or scheme."""
    value = (header or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):                 # [::1]:8787
        return value[:value.index("]") + 1].lower() if "]" in value else value.lower()
    return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()


def host_is_loopback(header):
    """Whether a Host header names this machine's loopback interface.

    Checked on reads as well as writes. A rebound name that reaches
    /api/settings hands over the user's location at street resolution, which
    the risk register calls the highest-consequence category there is.
    """
    return _hostname_of(header) in LOOPBACK_HOSTNAMES


def origin_is_allowed(header):
    """Whether an Origin header, if sent at all, is one of our own.

    An absent Origin is allowed: browsers always attach one to a cross-origin
    request, so absence means this did not come from another site. A command
    line client with the token is a legitimate caller and has no Origin to
    send.
    """
    if not (header or "").strip():
        return True
    return _hostname_of(header) in LOOPBACK_HOSTNAMES


class QuietHandler(SimpleHTTPRequestHandler):
    """Static files, plus a small read-only JSON API over the database.

    The dashboard used to parse data/readings.csv directly. With several
    sources and a SQLite store that no longer works, and re-parsing the whole
    history in the browser every minute was wasteful anyway (ROADMAP #F).
    """

    def log_message(self, *args):
        pass  # don't spam the log with every asset request

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    #: Largest request body accepted, and so the most we will drain before
    #: giving up and closing.
    MAX_BODY = 1_000_000

    def _drain_body(self):
        """Read and discard an unread request body before answering.

        Every guard in do_POST rejects *before* the body is read -- that is the
        point of them, since the whole reason to refuse a form-encoded write is
        not to process it. But leaving bytes unread in the socket and then
        closing sends an RST, and the client gets a connection reset instead of
        the 403 or 415 explaining what it did wrong.

        POSIX clients mostly still see the response; Windows does not, and
        reports WinError 10053 from the middle of reading it. So a security
        guard that works everywhere produced an unreadable answer on one
        platform -- the refusal was correct and the explanation was lost.

        Bounded by MAX_BODY: past that the sender is not owed a tidy close.
        """
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if length > self.MAX_BODY:
            # Do NOT drain an oversized body. Refusing it before reading is
            # the entire point of that guard, and a client that declares a
            # gigabyte is exactly the one that may never send it -- draining
            # would block the handler waiting for bytes that are not coming.
            # A reset is the right outcome for a request we are refusing to
            # listen to.
            return
        remaining = max(length, 0)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _json(self, obj, code=200):
        # Before the response, so a rejected request can still read it.
        self._drain_body()
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        """Refuse anything not addressed to loopback, read or write."""
        if host_is_loopback(self.headers.get("Host")):
            return True
        self._json({"error": "this server answers on loopback only"}, 403)
        return False

    def do_HEAD(self):
        if not self._host_ok():
            return
        return super().do_HEAD()

    def do_POST(self):
        """Every mutating request runs the full chain before any routing.

        Routing last is deliberate: a request that fails a check must be
        refused whether or not the path it named exists, so probing for write
        endpoints tells an attacker nothing.
        """
        if not self._host_ok():
            return
        if not origin_is_allowed(self.headers.get("Origin")):
            return self._json({"error": "cross-origin request refused"}, 403)

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # Anything else is a CORS "simple request", which reaches a server
            # without a preflight. Refusing them is what forces a cross-origin
            # caller into a preflight this server never answers.
            return self._json({"error": "expected Content-Type: application/json"}, 415)

        supplied = self.headers.get("X-Airo-Token") or ""
        if not hmac.compare_digest(supplied, server_token()):
            return self._json(
                {"error": "missing or stale token — reload the settings page"}, 403)

        # The chain passes. Route.
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad Content-Length"}, 400)
        if length > self.MAX_BODY:
            return self._json({"error": "body too large"}, 413)
        try:
            self._body_read = True
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            return self._json({"error": f"body is not JSON: {e}"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "expected a JSON object"}, 400)

        try:
            if parsed.path == "/api/settings":
                cfg, errors = apply_settings(body)
                if errors:
                    # Every field that was wrong, not just the first. A form
                    # that reports one error per save is a form nobody
                    # finishes.
                    return self._json({"errors": errors}, 400)
                log(f"settings updated via the local UI: {', '.join(sorted(body))}")
                return self._json(settings_payload(cfg))

            if parsed.path.startswith("/api/backup/"):
                # Imported here rather than at module scope: backup.py imports
                # this module, so a top-level import would be a cycle. The
                # dependency genuinely runs this way -- backup reads the
                # poller's paths -- so the cycle is in the direction, not in
                # the design.
                import backup

                what = parsed.path.rsplit("/", 1)[-1]
                narration = io.StringIO()

                if what == "export":
                    where = str(body.get("directory") or "").strip()
                    if not where:
                        return self._json({"error": "choose where to save it"}, 400)
                    ok_dir, message = probe_writable(where)
                    if not ok_dir:
                        # Checked before writing rather than after failing, so
                        # an unmounted drive is named as such instead of
                        # surfacing as a traceback halfway through a tar.
                        return self._json({"errors": {"directory": message}}, 400)

                    include_keys = bool(body.get("include_keys"))
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    target = Path(message) / f"airo-backup-{stamp}.tar.gz"
                    with contextlib.redirect_stdout(narration):
                        code = backup.create(output=target, include_keys=include_keys)
                    if code or not target.exists():
                        return self._json(
                            {"error": "the backup was not written",
                             "detail": narration.getvalue()[-2000:]}, 500)

                    # create() returning 0 only means it believed it
                    # succeeded. A truncated write or a full disk leaves a
                    # plausible file behind, so the archive is read back
                    # before it is called a backup.
                    described = backup.describe(target)
                    log(f"backup written to {target} "
                        f"({'with' if include_keys else 'without'} keys)")
                    return self._json(described)

                if what == "inspect":
                    where = str(body.get("path") or "").strip()
                    if not where:
                        return self._json({"error": "give the archive's path"}, 400)
                    described = backup.describe(where)
                    return self._json(described,
                                      400 if described.get("error") else 200)

                if what == "restore":
                    where = str(body.get("path") or "").strip()
                    described = backup.describe(where)
                    if described.get("error"):
                        return self._json(described, 400)
                    if not described["restorable"]:
                        # Refused rather than attempted. Restoring from an
                        # archive that fails its own checksum would replace a
                        # working install with a broken one.
                        return self._json(
                            {"error": f"this archive is not restorable: "
                                      f"{described['reason']}"}, 400)
                    with contextlib.redirect_stdout(narration):
                        code = backup.restore(
                            where, force=bool(body.get("force")),
                            keys=bool(body.get("keys", True)))
                    text = narration.getvalue()
                    if code:
                        return self._json({"error": "restore refused",
                                           "detail": text[-2000:]}, 409)
                    log(f"restored from {where}")
                    return self._json({"restored": True,
                                       "detail": text[-2000:],
                                       "settings": settings_payload(load_config())})

                return self._json({"error": "unknown endpoint"}, 404)

            if parsed.path == "/api/choose-folder":
                # Behind the same guards as every other write. It is not a
                # write, but it puts a window on the user's desktop, and that
                # is not something another origin may do.
                picked, reason = choose_folder(
                    str(body.get("prompt") or "Choose a folder"))
                out = {"path": picked, "reason": reason}
                if picked:
                    ok_dir, message = probe_writable(picked)
                    out["writable"] = ok_dir
                    if not ok_dir:
                        out["error"] = message
                return self._json(out)

            if parsed.path == "/api/sources/probe":
                # Verifies one sensor and reports what it is. Accepts a
                # read_key and never stores one: writing a credential to disk
                # stays the business of /api/keys, which remains the only
                # route that does it. Nothing here echoes the key back, and
                # the error paths are written assuming the reply may end up in
                # a log.
                probed = probe_source(
                    body.get("provider"), body.get("site_id"),
                    read_key=body.get("read_key"))
                return self._json(probed, 200 if probed.get("ok") else 400)

            if parsed.path == "/api/keys":
                # The only route that accepts a credential, so it is the only
                # one whose error paths must be written assuming the body will
                # end up in a log if anyone is careless. Nothing here echoes
                # `key`, and the success response reports presence and file
                # protection -- never the value.
                slug = str(body.get("provider") or "").strip().lower()
                if slug not in PROVIDERS:
                    return self._json({"error": f"unknown network: {slug or '(none)'}"}, 400)
                key = body.get("key")
                if key is not None and not isinstance(key, str):
                    return self._json({"error": "key must be text"}, 400)

                site_id = body.get("site_id")
                if site_id not in (None, ""):
                    # A private PurpleAir sensor's read_key lives per source
                    # inside config.json rather than in ~/.airo/<provider>.key
                    # with every other key. It is written here rather than
                    # through /api/settings, which refuses credentials
                    # outright, so there is exactly one route that ever handles
                    # one.
                    cfg = load_config()
                    sources = list(cfg.get("sources") or [])
                    match = [s for s in sources
                             if str(s.get("site_id")) == str(site_id)
                             and str(s.get("provider") or "").lower() == slug]
                    if not match:
                        return self._json(
                            {"error": f"no configured source {slug}/{site_id}"}, 404)
                    for s in match:
                        if (key or "").strip():
                            s["read_key"] = key.strip()
                        else:
                            s.pop("read_key", None)
                    cfg["sources"] = sources
                    save_config(cfg)
                    log(f"read key {'set' if (key or '').strip() else 'cleared'} "
                        f"for {slug}/{site_id}")
                    return self._json(settings_payload(load_config()))

                path, restricted = save_key(slug, key)
                log(f"api key {'set' if (key or '').strip() else 'cleared'} for {slug}")
                return self._json({
                    "provider": slug,
                    "has_key": bool(get_api_key({"provider": slug})),
                    # False here is a real answer, not a failure to check. A
                    # key that merely looks protected is worse than one known
                    # not to be.
                    "restricted": restricted,
                    "path": str(path),
                    "settings": settings_payload(load_config()),
                })

            if parsed.path == "/api/geocode":
                # A POST for the same reason as discovery: it makes an
                # outbound call and takes a parameter, and it is behind the
                # token because it sends what the user typed to a third party.
                #
                # Nobody knows their own latitude. Asking for one, which this
                # page did, is asking the user to do the tool's job -- so they
                # type an address and this turns it into coordinates.
                query = str(body.get("query") or "").strip()
                if not query:
                    return self._json(
                        {"errors": {"query": "type an address, suburb or postcode"}},
                        400)
                try:
                    matches = geocode(query)
                except Exception as e:
                    # The lookup is a network call to somebody else's service,
                    # so failing is ordinary. Say so plainly and leave the
                    # coordinate fields usable rather than dead-ending.
                    return self._json(
                        {"error": f"the address lookup did not answer "
                                  f"({type(e).__name__}). You can still type "
                                  f"coordinates directly."}, 502)
                return self._json({"query": query, "matches": matches})

            if parsed.path == "/api/timezone":
                # Explicit, and user-initiated. Deriving the zone silently on
                # save would spend a network call on somebody who may have
                # typed their coordinates precisely to avoid one -- the same
                # line setup.py draws. A button is the honest shape: it says
                # what it is about to do before it does it.
                cfg = load_config()
                loc = body.get("location") or cfg.get("location") or {}
                lat, lon = loc.get("latitude"), loc.get("longitude")
                if lat is None or lon is None:
                    return self._json(
                        {"error": "find your address first — a timezone is "
                                  "looked up from coordinates"}, 400)
                try:
                    # `weather` is imported at module level; naming it again
                    # here would shadow that with a local and read as though
                    # the dependency were optional, which it is not.
                    name = weather.timezone_at(lat, lon)
                except Exception:
                    name = None
                if not name:
                    return self._json(
                        {"error": "the timezone lookup did not answer. You "
                                  "can type an IANA name such as "
                                  "Australia/Brisbane instead."}, 502)
                # Offered, not saved. The user still presses Save, exactly as
                # with the address lookup: a wrong answer must be correctable
                # before it becomes the configuration.
                return self._json({"timezone": name})

            if parsed.path == "/api/sources/discover":
                # A POST because it makes outbound calls and takes parameters,
                # not because it changes anything. Behind the token for the
                # same reason: it is the one route a page can use to spend the
                # user's API quota.
                cfg = load_config()
                location = body.get("location") or cfg.get("location") or {}
                if location.get("latitude") is None or location.get("longitude") is None:
                    return self._json(
                        {"error": "set your location before searching for monitors"}, 400)
                try:
                    radius = float(body.get("radius_km") or 25)
                except (TypeError, ValueError):
                    return self._json({"error": "radius_km must be a number"}, 400)
                radius = max(1.0, min(radius, 200.0))
                slugs = body.get("providers") or sorted(PROVIDERS)
                unknown = [s for s in slugs if s not in PROVIDERS]
                if unknown:
                    return self._json({"error": f"unknown network: {unknown[0]}"}, 400)

                found, failures = discover_sites(location, radius, slugs)
                found, probed, dead = annotate_reporting(found)
                picks = {(s.get("provider"), str(s.get("site_id")))
                         for s in recommend(found)}
                for s in found:
                    s["recommended"] = (s.get("provider"), str(s.get("site_id"))) in picks
                return self._json(scrub_secrets({
                    "sites": found, "failures": failures,
                    "radius_km": radius, "probed": probed, "not_reporting": dead,
                    # Said out loud, because a capped probe that looks
                    # exhaustive is how "unchecked" reads as "fine".
                    "probe_limit": PROBE_LIMIT,
                }))

            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _serve_settings_page(self):
        """The settings page, with this process's token substituted in.

        Served through a handler rather than as a static file so the token
        reaches the page without ever existing as something a caller can ask
        for. An endpoint that hands out the token would undo the point of
        having one: a cross-origin page can issue the request even though it
        cannot read an ordinary response.
        """
        try:
            html = (HERE / "settings.html").read_text(encoding="utf-8")
        except OSError as e:
            return self._json({"error": f"settings.html is missing: {e}"}, 500)
        body = html.replace(SETTINGS_TOKEN_PLACEHOLDER, server_token()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/settings", "/settings.html"):
            # Both spellings, because the static handler would otherwise serve
            # the file with its placeholder intact and every save would fail
            # with a token error nobody could explain.
            return self._serve_settings_page()
        if not parsed.path.startswith("/api/"):
            return super().do_GET()

        q = urllib.parse.parse_qs(parsed.query)
        try:
            cfg = load_config()
            if parsed.path == "/api/ping":
                # Deliberately answers before any data exists. This is the
                # liveness probe every "is Airo already serving?" check uses,
                # so it must never depend on a poll having run. Loopback only,
                # like everything else here.
                return self._json({
                    "airo": True,
                    "project": str(HERE),
                    "data_dir": str(DATA),
                    "location_name": (load_config().get("location") or {}).get("name"),
                })

            if parsed.path == "/api/latest":
                if LATEST_PATH.exists():
                    return self._json(json.loads(LATEST_PATH.read_text(encoding="utf-8")))
                return self._json({"error": "no reading yet"}, 404)

            if parsed.path == "/api/settings":
                # Read-only for now. Writing settings needs the cross-origin
                # defences that do not exist yet -- any page in the user's
                # browser can reach a loopback server.
                return self._json(settings_payload(cfg))

            if parsed.path == "/api/indoor":
                # On demand rather than in latest.json: it reads a week of
                # history, and paying for that on every poll to serve a panel
                # somebody may never scroll to is the wrong trade.
                days = float(q.get("days", ["7"])[0])
                import analyse
                conn = open_store()
                try:
                    return self._json(
                        analyse.indoor_outdoor(conn, cfg, days=days))
                finally:
                    conn.close()

            if parsed.path == "/api/sources":
                conn = open_store()
                try:
                    return self._json(store.counts(conn))
                finally:
                    conn.close()

            if parsed.path == "/api/series":
                days = float(q.get("days", ["3"])[0])
                bucket = q.get("bucket", [None])[0]
                since = datetime.now(timezone.utc) - timedelta(days=days)
                scale_name, scale = get_scale(cfg)

                conn = open_store()
                try:
                    srcs = {s["id"]: s for s in store.list_sources(conn, enabled_only=False)}
                    rows = store.series(conn, since=since,
                                        bucket_minutes=int(bucket) if bucket else None)
                    # Excluded from the series on purpose -- a blocked inlet
                    # reading 900 µg/m³ would swamp the axis and every average
                    # -- but excluded and unmentioned is a silent drop, and
                    # the policy is surface, don't drop. Sent alongside.
                    flagged = store.suspect_readings(conn, since=since)
                finally:
                    conn.close()

                out = {}
                # A row whose observed_utc cannot be parsed used to raise here,
                # and the handler turned that into a 500 for the *whole*
                # series -- so one bad timestamp anywhere in the history blanked
                # every chart for every source. A stored timestamp is only as
                # good as whatever wrote it: migrate_from_csv() passes the old
                # file's `utc` column through untouched, and --repair exists
                # because bad values have reached the database before.
                #
                # Counted rather than silently skipped, per rule 5a: the
                # dashboard can say "3 readings could not be placed in time"
                # instead of quietly drawing a shorter line.
                unreadable = 0
                for r in rows:
                    src = srcs.get(r["source_id"])
                    if not src:
                        continue
                    label = f"{src['provider']}/{src['site_id']}"
                    entry = out.setdefault(label, {
                        "provider": src["provider"],
                        "site_id": src["site_id"],
                        "site_name": src["site_name"],
                        "points": [],
                    })
                    try:
                        if "bucket_epoch" in r:
                            entry["points"].append({
                                "t": int(r["bucket_epoch"]) * 1000,
                                "min": r["pm25_min"], "max": r["pm25_max"],
                                "pm25": r["pm25_mean"],
                                "aqi": aqi_for(r["pm25_mean"], scale_name),
                            })
                        else:
                            t = datetime.fromisoformat(r["observed_utc"])
                            entry["points"].append({
                                "t": int(t.timestamp() * 1000),
                                "pm25": r["pm25"],
                                "aqi": aqi_for(r["pm25"], scale_name),
                                # The judgement store.assess_quality() already
                                # made at ingest. Carried so no surface has to
                                # decide "is this plausible?" a second time --
                                # the dashboard was doing exactly that, with
                                # its own threshold, on the index rather than
                                # on the concentration it was derived from.
                                "quality": r["quality"] if "quality" in r.keys()
                                           else "ok",
                            })
                    except (TypeError, ValueError):
                        unreadable += 1
                if unreadable:
                    log(f"WARN {unreadable} reading(s) have a timestamp that "
                        f"cannot be placed on a chart; run --verify")
                return self._json({"scale": scale_name,
                                   "scale_label": scale["label"],
                                   "suspect": [
                                       {"t": int(datetime.fromisoformat(
                                            r["observed_utc"]).timestamp() * 1000),
                                        "pm25": r["pm25"],
                                        "quality": r["quality"]}
                                       for r in flagged
                                       if _parsable_time(r["observed_utc"])],
                                   # Served, not restated. The page had two
                                   # copies of these and both were Australian
                                   # whatever the configured scale was.
                                   "bands": scale_bands(scale_name),
                                   "series": list(out.values()),
                                   "unreadable_rows": unreadable})

            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


# ------------------------------------------------------------------ status

def print_status():
    cfg = load_config()
    scale_name, scale = get_scale(cfg)
    location = cfg.get("location") or {}

    print(f"data dir     : {DATA}")
    print(f"database     : {db_path()} "
          f"({'exists' if db_path().exists() else 'MISSING'})")
    warn_about_orphans()
    print(f"location     : {location.get('name') or '(unset)'}")
    print(f"scale        : {scale_name} — {scale['label']}")
    print(f"fusion rule  : {(cfg.get('fusion') or {}).get('rule', fusion.DEFAULT_RULE)}")

    srcs = enabled_sources(cfg)
    if not srcs:
        print(f"sources      : NONE CONFIGURED — {how_to('configure')}")
    else:
        print(f"sources      : {len(srcs)} configured")

    if db_path().exists():
        conn = open_store()
        try:
            for c in store.counts(conn):
                flag = "" if c["enabled"] else "  (disabled)"
                name = c["site_name"] or c["site_id"]
                print(f"  {c['provider']}/{c['site_id']} {name}{flag}")
                print(f"      rows {c['rows']:,}"
                      + (f"  {c['first_utc']} → {c['last_utc']}" if c["rows"] else ""))
        finally:
            conn.close()

    # Keys, per source, never printing the value itself.
    for src in srcs:
        provider = get_provider(src)
        label = f"{provider.slug}/{src.get('site_id')}"
        if not provider.needs_key:
            print(f"key {label:22}: not required")
        else:
            found = "found" if get_api_key(src) else "NOT FOUND — see README"
            print(f"key {label:22}: {found}")

    if LATEST_PATH.exists():
        d = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
        val = d.get("aqi", d.get("au_aqi"))
        band = d.get("band", d.get("au_band"))
        print(f"latest.json  : {d.get('scale_label', 'AQI')} {val} ({band}) "
              f"at {d.get('fetched_local')}")
        if d.get("provenance"):
            print(f"             via {d['provenance']}")

    nets = network_status(cfg)
    unused = [n for n in nets if not n["in_use"]]
    if unused:
        print("\nnetworks not in use:")
        for n in unused:
            if n["has_key"]:
                hint = f"ready — add a site: {how_to('configure')}"
            else:
                hint = f"needs a free account: {n['signup_url']}"
            print(f"  {n['provider']:<10} {n['tier']:<10} {hint}")
        if any(not n["has_key"] for n in unused):
            print(f"  add keys: {how_to('configure')}")

    if legacy_csv_present():
        print(f"note         : {CSV_PATH.name} is a pre-v0.5 file. Import it with "
              f"`python3 poller.py --migrate-csv` (safe to re-run).")


def migrate_legacy_key():
    """Copy a pre-v0.4 ~/.airo/apikey to the per-provider name.

    The old layout assumed one network, so the file had no provider in its
    name. get_api_key() still reads it as a fallback -- but only for
    PurpleAir, which means a user in the old layout who adds a second network
    gets an inconsistency they have no way to see.

    Copies rather than moves, and never overwrites: an older Airo, or a
    restored backup, must keep working. The original costs 36 bytes.
    """
    legacy = Path.home() / ".airo" / "apikey"
    modern = Path.home() / ".airo" / "purpleair.key"
    if not legacy.exists() or modern.exists():
        return False
    try:
        key = legacy.read_text(encoding="utf-8").strip()
        if not key:
            return False
        modern.write_text(key + "\n", encoding="utf-8")
        secure_path(modern)
    except OSError as e:
        print(f"  {WARN} could not tidy the legacy key file: {e}")
        return False
    print(f"  {TICK} moved your PurpleAir key to the current layout")
    print("       ~/.airo/purpleair.key — the old ~/.airo/apikey is left in "
          "place and can be deleted")
    print()
    return True


def data_marker_path():
    """Where the last-used data directory is recorded.

    Derived from CONFIG_PATH *at call time*, not captured at import. As a
    module constant it did not follow CONFIG_PATH when that was repointed,
    which meant a test that isolated the config still read -- and wrote -- the
    real user's marker. That is not a hypothetical: it happened, and because
    `--migrate-data` with no explicit source asks other_databases() where to
    migrate *from*, a test run moved the developer's own data directory aside.
    A path that isolation does not follow is a path that escapes it.
    """
    return CONFIG_PATH.parent / "data-location"


def _remembered_data_dir():
    """The directory Airo last wrote to, or None."""
    try:
        raw = data_marker_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(raw) if raw else None


def remember_data_dir():
    """Record where readings are going, so a later change can be noticed.

    Written on every successful poll. Without it, moving data_dir to a new
    path abandons the old database at a location nothing has any record of --
    the tool cannot warn about a directory it was never told existed.
    """
    try:
        marker = data_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        if _remembered_data_dir() != DATA:
            marker.write_text(str(DATA) + "\n", encoding="utf-8")
    except OSError:
        pass          # advisory only; never worth failing a poll over


def ensure_data_dir():
    """Create the data directory, or explain why it cannot be.

    Raw traceback otherwise -- and the poller runs unattended on a schedule,
    so the failure lands in a log nobody reads. A path that cannot be written
    is the ordinary consequence of a mistyped data_dir, an external drive that
    has not mounted, or a synced folder that has not appeared yet.

    Deliberately does NOT fall back to another directory. Quietly writing
    somewhere else is how a user ends up with two databases and no idea which
    one is live -- the failure this refuses to become.
    """
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        probe = DATA / ".airo-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError as e:
        print(f"  {CROSS} cannot use the data directory:")
        print(f"      {DATA}")
        print(f"      {type(e).__name__}: {e}")
        print()
        print("  Nothing was written, and no other directory was substituted —")
        print("  writing somewhere else silently is how you end up with two")
        print("  databases and no idea which one is live.")
        print()
        if os.environ.get("AIRO_DATA", "").strip():
            print("  It is set by $AIRO_DATA.")
        else:
            print(f"  It is set by \"data_dir\" in {CONFIG_PATH}")
        print("  If that is an external drive, mount it and run this again.")
        return False


def other_databases():
    """Databases at known locations that are NOT the active one.

    Making the data directory configurable creates a way to abandon years of
    readings by editing one line: point data_dir at a path that does not
    exist yet -- a typo, an unmounted drive, a synced folder that has not
    appeared yet -- and the poller starts a blank database beside the full
    one, cheerfully, forever.

    The rows are not deleted, which is why this is not a data-loss bug in the
    strict sense. It is worse in practice: the user believes they are logging
    and they are, into a file they will never look at, while the history they
    care about stops growing.

    Returns [(path, rows)] for every other location holding readings.
    """
    candidates = []
    seen = {DATA.resolve()} if DATA.exists() else {DATA}
    # The previously-used directory matters most: data_dir is configurable, so
    # the abandoned database is usually at a path only the old config knew.
    # Checking a fixed list of well-known locations would miss exactly the
    # case this exists for.
    known = [_remembered_data_dir(),
             Path.home() / ".airo" / "data", HERE / "data", LEGACY_DATA]
    for path in known:
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        db = path / "airo.db"
        # Equivalent to the except below today, because mode=ro raises on a
        # missing file rather than creating one -- a mutation sweep reports
        # this as untested and always will. Kept deliberately: it is the only
        # thing standing between a future plain sqlite3.connect() and this
        # function *creating* an empty database in a directory the tool is not
        # using, which is the exact failure it exists to detect.
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            conn.close()
        except Exception:
            continue
        if rows:
            candidates.append((path, rows))
    return candidates


def warn_about_orphans():
    """Say it wherever the user already is. Returns True if anything was said."""
    orphans = other_databases()
    if not orphans:
        return False
    active_rows = 0
    if db_path().exists():
        try:
            conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
            active_rows = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            conn.close()
        except Exception:
            pass
    print()
    print(f"  {WARN} readings exist in a directory Airo is NOT using:")
    for path, rows in orphans:
        print(f"      {path}  ({rows:,} readings)")
    print(f"      currently writing to {DATA} ({active_rows:,} readings)")
    print(f"      move them in with: python3 poller.py --migrate-data")
    print(f"      or point data_dir back in {CONFIG_PATH}")
    return True


def run_doctor(cfg):
    """Exercise every configured source end to end and report honestly.

    Providers change their APIs, revoke keys and retire stations without
    telling anyone. Each of those looks the same from the outside -- readings
    simply stop -- so this checks the whole path (auth, current, history)
    rather than waiting for a hole to appear in the record.
    """
    print(f"{'':2}Airo doctor\n")
    problems = 0
    migrate_legacy_key()
    if warn_about_orphans():
        problems += 1

    # Which zone the wall clock is being read in, and whether it is the one
    # that was configured. Neither half is useful alone: "you configured
    # Australia/Brisbane, this machine thinks UTC" is the sentence that
    # explains alerts arriving at 3am and an evening analysis about the wrong
    # ten hours, and it is invisible from inside either zone.
    for line in timezone_report(cfg):
        print(f"{'':2}{line}")
    # Whether an alert can actually reach a screen. Alerting is on by default
    # and was doing nothing at all on Linux and Windows, reported only in a
    # log nobody reads.
    for line in notification_report():
        print(f"{'':2}{line}")
    if timezone_is_a_problem(cfg):
        problems += 1
    print()

    # A registered agent is not necessarily *this* copy's. The label is fixed,
    # so a second checkout or a moved folder means launchd reports the other
    # install as healthy while this one never runs -- everything looks fine
    # and nothing is collected.
    try:
        import scheduler
        ours, message = scheduler.agent_belongs_to_this_project()
        if not ours:
            print(f"  {CROSS} {message}")
            print()
            problems += 1
    except Exception as e:
        print(f"  {WARN} could not check which folder the agent runs in: {e}")
        print()

    srcs = enabled_sources(cfg)
    if not srcs:
        print(f"  {CROSS} no sources configured — {how_to('configure')}")
        return 1

    for src in srcs:
        provider = get_provider(src)
        label = f"{provider.slug}/{src.get('site_id')}"
        print(f"  {label}  ({provider.label})")

        key = get_api_key(src)
        if provider.needs_key and not key:
            print(f"    {CROSS} no API key — set $%s or write ~/.airo/%s.key"
                  % (provider.key_env, provider.slug))
            print(f"       get one at {provider.key_url}")
            problems += 1
            continue

        try:
            measures, meta = provider.current(src, key)
            pm = measures.get("headline")
            if pm is None:
                print(f"    {WARN} responded, but published no PM2.5 value")
                problems += 1
            else:
                observed = meta.get("last_seen_utc")
                age = ""
                if observed:
                    try:
                        mins = fusion.age_minutes(observed)
                        age = f", {mins:.0f} min old" if mins is not None else ""
                    except Exception:
                        pass
                print(f"    {TICK} current reading: {pm} ug/m3{age}")
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code in (401, 403):
                hint = "  — the key looks wrong, expired or revoked"
            elif e.code == 404:
                hint = "  — this site id may no longer exist"
            elif e.code == 429:
                hint = "  — rate limited; try again shortly"
            print(f"    {CROSS} current reading failed: HTTP {e.code}{hint}")
            problems += 1
            continue
        except Exception as e:
            print(f"    {CROSS} current reading failed: {type(e).__name__}: {e}")
            problems += 1
            continue

        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=1)
            obs = provider.history(src, key, start, end)
            if not obs:
                print(f"    {WARN} history returned nothing for the last 24h "
                      f"— gap repair will not work for this source")
                problems += 1
            else:
                out_of_window = [o for o in obs if not (start <= o["utc"] <= end)]
                if out_of_window:
                    print(f"    {CROSS} history returned {len(out_of_window)} "
                          f"reading(s) outside the requested window")
                    problems += 1
                else:
                    print(f"    {TICK} history: {len(obs)} observations in 24h")
        except Exception as e:
            print(f"    {CROSS} history failed: {type(e).__name__}: {e}")
            problems += 1

    # The store itself.
    print()
    if db_path().exists():
        conn = open_store()
        try:
            issues = store.verify(conn)
        finally:
            conn.close()
        if issues:
            print(f"  {CROSS} database problems:")
            for i in issues:
                print(f"      {i}")
            problems += len(issues)
        else:
            print(f"  {TICK} database integrity ok")
    else:
        print(f"  {WARN} no database yet — run: python3 poller.py --once")

    # Credential protection.
    for src in srcs:
        provider = get_provider(src)
        if not provider.needs_key:
            continue
        kf = Path.home() / ".airo" / f"{provider.slug}.key"
        if kf.exists():
            state = path_is_restricted(kf)
            if state is False:
                print(f"  {CROSS} {kf} is readable by others")
                problems += 1
            elif state is None:
                print(f"  {WARN} could not check permissions on {kf}")

    print()
    if problems:
        print(f"  {problems} problem(s) found.")
        return 1
    print(f"  {TICK} everything checks out.")
    return 0


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description="Airo — poll air quality sources to a local database.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="single poll then exit")
    g.add_argument("--daemon", action="store_true",
                   help="poll on a loop and serve the dashboard (the default "
                        "when no other mode is given)")
    g.add_argument("--backfill", type=int, metavar="DAYS",
                   help="pull N days of history for every source then exit")
    g.add_argument("--forecast", action="store_true",
                   help="the six-hour outlook, or why there is not one yet "
                        "(ROADMAP #9 Phase C)")
    g.add_argument("--backfill-weather", type=int, metavar="DAYS", nargs="?",
                   const=0,
                   help="fetch hourly weather back to where the readings "
                        "start, or for DAYS. A correlation needs history on "
                        "both sides — see ROADMAP #9")
    g.add_argument("--status", action="store_true", help="show what is on disk")
    g.add_argument("--serve", action="store_true",
                   help="serve the dashboard only, no polling")
    g.add_argument("--test-alert", action="store_true",
                   help="fire a test notification so you can confirm alerts work")
    g.add_argument("--migrate-csv", action="store_true",
                   help="import a pre-v0.5 readings.csv into the database")
    g.add_argument("--migrate-data", action="store_true",
                   help="move readings out of the project folder into ~/.airo/data")
    g.add_argument("--doctor", action="store_true",
                   help="test every configured source end to end")
    g.add_argument("--repair", action="store_true",
                   help="replace stored feed sentinels with NULL and re-fetch "
                        "the affected windows (use with --dry-run to preview)")
    g.add_argument("--verify", action="store_true",
                   help="check the database for corruption")
    g.add_argument("--where", action="store_true",
                   help="print where config and data actually live")
    g.add_argument("--prune", action="store_true",
                   help="apply the configured retention policy now")
    g.add_argument("--export", metavar="DIR", nargs="?", const="export",
                   help="write one CSV per source to DIR (default: ./export)")
    g.add_argument("--list-sources", action="store_true",
                   help="list configured sources and the providers available")
    g.add_argument("--alerts", metavar="STATE", choices=["on", "off"],
                   help="turn notifications on or off")
    g.add_argument("--logs", metavar="N", nargs="?", const=40, type=int,
                   help="show the last N lines of the poller's log (default 40)")
    g.add_argument("--stop-server", action="store_true",
                   help="stop the local dashboard server, if this project "
                        "started one")
    g.add_argument("--uninstall", action="store_true",
                   help="stop Airo running. Readings and settings are kept, "
                        "and their location is printed")
    g.add_argument("--first-run", action="store_true",
                   help="prepare a freshly installed app: schedule background "
                        "polls and open the settings page if nothing is "
                        "configured yet. Safe to run every launch")
    g.add_argument("--open", metavar="PAGE", nargs="?", const="dashboard",
                   choices=sorted(SERVE_PAGES),
                   help="open the dashboard or settings page in a browser, "
                        "starting the local server if needed")
    # Same resolution, no browser. The app's own window points a webview at
    # this, so the URL still comes from the one place that knows the port --
    # the tray must never build one itself.
    g.add_argument("--url", metavar="PAGE", nargs="?", const="settings",
                   choices=sorted(SERVE_PAGES),
                   help="print the URL of a served page and nothing else, "
                        "starting the local server if needed")

    # A modifier, not a mode. Inside the mutually exclusive group above it made
    # `--prune --dry-run` an argparse error -- the documented way to preview a
    # destructive delete before running it, printed twice in the README, and it
    # had never worked. Someone checking what pruning would remove got an error
    # and could reasonably have run --prune without the preview.
    ap.add_argument("--dry-run", action="store_true",
                    help="with --prune or --repair, report what would change "
                         "without changing it")
    args = ap.parse_args()

    if args.test_alert:
        okd = notify("Airo",
                     "Test notification",
                     "If you can see this, alerts are working.",
                     sound="Ping")
        print("Notification sent." if okd else "Notification FAILED — see the log.")
        print("\nIf nothing appeared, open System Settings > Notifications and allow"
              "\nnotifications for 'Script Editor' (macOS attributes osascript alerts to it).")
        return 0 if okd else 1

    if args.status:
        print_status()
        return 0

    if args.doctor:
        return run_doctor(load_config())

    if args.repair:
        if not db_path().exists():
            print("no database yet")
            return 0
        conn = open_store()
        try:
            found = store.repair_sentinels(conn, dry_run=args.dry_run)
            if not found:
                print(f"  {TICK} no stored sentinels — nothing to repair")
                return 0

            verb = "would clear" if args.dry_run else "cleared"
            total = sum(f["n"] for f in found)
            print(f"  {verb} {total:,} row(s) holding a feed sentinel:\n")
            for f in found:
                cols = ", ".join(f"{c} x{n}" for c, n in f["columns"].items())
                print(f"    {f['provider']}/{f['site_id']} "
                      f"{f.get('site_name') or ''}".rstrip())
                print(f"      {f['n']:,} row(s), {f['first_utc']} to {f['last_utc']}")
                print(f"      {cols}")
            print()

            if args.dry_run:
                print("  dry run — nothing was changed. Re-run without "
                      "--dry-run to apply.")
                return 0

            # Ask the provider again for the affected windows. A station that
            # was offline may since have published the real values, and a
            # cleared row is otherwise a permanent hole.
            cfg = load_config()
            by_key = {(str(s_.get("provider")), str(s_.get("site_id"))): s_
                      for s_ in enabled_sources(cfg)}
            refetched = 0
            for f in found:
                src = by_key.get((str(f["provider"]), str(f["site_id"])))
                if src is None:
                    print(f"  {WARN} {f['provider']}/{f['site_id']} is no longer "
                          f"configured — cleared, not re-fetched")
                    continue
                try:
                    provider = get_provider(src)
                    start = datetime.fromisoformat(f["first_utc"])
                    n = backfill_source(conn, f["source_id"], src, provider,
                                        since_utc=start)
                    refetched += n
                    print(f"  {TICK} re-fetched {n:,} row(s) for "
                          f"{f['provider']}/{f['site_id']}")
                except Exception as e:
                    print(f"  {WARN} could not re-fetch {f['provider']}/"
                          f"{f['site_id']}: {type(e).__name__}: {e}")

            remaining = store.find_sentinels(conn)
            if remaining:
                print(f"  {WARN} {sum(r['n'] for r in remaining)} row(s) still "
                      f"hold a sentinel — the feed is still returning it")
            else:
                print(f"\n  {TICK} repaired. {refetched:,} row(s) recovered "
                      f"from the provider; the rest are now recorded as "
                      f"'no measurement' rather than a concentration.")
            return 0
        finally:
            conn.close()

    if args.verify:
        if not db_path().exists():
            print("no database yet")
            return 0
        conn = open_store()
        try:
            issues = store.verify(conn)
        finally:
            conn.close()
        if issues:
            for i in issues:
                print(f"  {CROSS} {i}")
            return 1
        print(f"  {TICK} database integrity ok")
        return 0

    if args.where:
        cfg = load_config()
        print(f"config : {CONFIG_PATH}"
              f"{'' if CONFIG_PATH.exists() else '   (not created yet)'}")
        print(f"data   : {DATA}"
              f"{'' if DATA.exists() else '   (not created yet)'}")
        print(f"keys   : {Path.home() / '.airo'}/<provider>.key")
        if db_path().exists():
            size = store.db_size_bytes(db_path())
            print(f"size   : {size / 1e6:.1f} MB")
        keep = int(cfg.get("retention_days") or 0)
        print(f"keep   : {'everything (no limit)' if keep <= 0 else str(keep) + ' days'}")
        if DATA == LEGACY_DATA:
            print()
            print("Your readings are inside the project folder, so a re-clone or a")
            print("move would lose them. Relocate with:")
            print("  python3 poller.py --migrate-data")
        warn_about_orphans()
        return 0

    if args.list_sources:
        cfg = load_config()
        print("Available providers:")
        for slug, p in sorted(PROVIDERS.items()):
            key = "no key needed" if not p.needs_key else f"key: {p.key_env}"
            print(f"  {slug:10} {p.label}")
            print(f"             {p.resolution_minutes} min resolution · {key}")
            print(f"             licence: {p.licence}")
        print("\nConfigured sources:")
        for src in (cfg.get("sources") or []):
            state = "" if src.get("enabled", True) else "  (disabled)"
            print(f"  {src.get('provider')}/{src.get('site_id')} "
                  f"{src.get('site_name') or ''}{state}")
        if not cfg.get("sources"):
            print(f"  (none — {how_to('configure')})")
        return 0

    if args.alerts:
        now = set_alerts(args.alerts == "on")
        print(f"  {TICK} notifications are {'on' if now else 'off'}")
        return 0

    if args.logs is not None:
        lines = recent_log(args.logs)
        if not lines:
            print(f"  no log yet at {LOG_PATH}")
            return 0
        for line in lines:
            print(line)
        return 0

    if args.stop_server:
        if stop_server():
            print(f"  {TICK} stopped the local server")
        else:
            print("  no server of ours was running")
        return 0

    if args.uninstall:
        did = uninstall_everything()
        print()
        for item in did["removed"]:
            print(f"  {TICK} stopped {item}")
        for problem in did["problems"]:
            print(f"  {WARN} {problem}")
        if did["kept"]:
            print()
            print("  Kept, because these are yours and cannot be regenerated:")
            for item in did["kept"]:
                print(f"      {item}")
            print()
            print("  Delete that folder yourself if you want the readings gone.")
            print(f"      {did['data_dir']}")
        print()
        print("  The app itself is a file you can drag to the Bin.")
        return 1 if did["problems"] else 0

    if args.first_run:
        did = first_run()
        print(f"  data dir     : {did['data_dir']}")
        print(f"  background   : {'scheduled' if did['scheduled'] else 'NOT scheduled'}")
        print(f"  configured   : {'yes' if did['configured'] else 'not yet'}")
        if did["opened"]:
            print(f"  opened       : {did['opened']}")
        for problem in did["problems"]:
            print(f"  {WARN} {problem}")
        return 1 if did["problems"] else 0

    if args.forecast:
        print(f"{'':2}Airo outlook\n")
        cfg = load_config()
        conn = open_store()
        try:
            # Score anything now measurable first, so the skill this outlook
            # is gated on includes everything that has happened.
            scored = forecast.verify_pending(
                conn, FORECAST_PENDING_PATH, FORECAST_SKILL_PATH)
            if scored:
                print(f"{'':2}verified {scored} earlier forecast(s)")

            location = cfg.get("location") or {}
            try:
                ahead = weather.forward(location.get("latitude"),
                                        location.get("longitude"),
                                        hours=forecast.HORIZON_HOURS)
            except Exception as e:
                print(f"{'':2}{WARN} could not fetch forecast weather: "
                      f"{type(e).__name__}: {e}")
                ahead = []

            out = forecast.outlook(conn, cfg, ahead, FORECAST_SKILL_PATH)
            if out.get("text"):
                print(f"{'':2}{out['text']}")
                forecast.remember(FORECAST_PENDING_PATH,
                                  when=out["for_hour"],
                                  predicted=out["pm25"],
                                  persistence=out["persistence"])
            else:
                print(f"{'':2}No outlook yet — {out.get('why')}")
            if out.get("accuracy"):
                print(f"\n{'':2}{out['accuracy']}")
        finally:
            conn.close()
        return 0

    if args.backfill_weather is not None:
        cfg = load_config()
        conn = open_store()
        try:
            stored, oldest = backfill_weather(
                conn, cfg, days=args.backfill_weather or None)
        finally:
            conn.close()
        if not oldest:
            print(f"  {CROSS} no weather stored")
            return 1
        print(f"  {TICK} {stored:,} new hour(s); weather now reaches back "
              f"to {oldest[:10]}")
        return 0

    if args.url:
        # Deliberately silent apart from the URL: this is read by another
        # program, and a tick or a "started the server" line on stdout would
        # be parsed as part of the address. Anything worth saying goes to the
        # log, which is where the tray's own troubleshooting looks.
        url, _ = page_url(args.url)
        if url is None:
            print("", end="")
            return 1
        print(url)
        return 0

    if args.open:
        url, started = open_page(args.open)
        if url is None:
            print(f"  {CROSS} could not reach the local server.")
            print(f"      Start it yourself with: python3 poller.py --serve")
            return 1
        if started:
            print(f"  {TICK} started the local server")
        print(f"  {url}")
        return 0

    if args.serve:
        cfg = load_config()
        try:
            serve_forever(cfg.get("serve_port", 8787))
        except KeyboardInterrupt:
            log("server stopped")
        return 0

    cfg = load_config()
    if not ensure_data_dir():
        return 1

    if args.migrate_csv:
        migrate_legacy_csv(cfg)
        return 0

    if args.prune:
        keep = int(cfg.get("retention_days") or 0)
        if keep <= 0:
            log("retention is set to keep everything (retention_days = 0)")
            log("  set it with: python3 setup.py --prefs")
            return 0
        conn = open_store()
        try:
            removed, kept, oldest = store.prune(conn, keep, dry_run=args.dry_run)
        finally:
            conn.close()
        verb = "would remove" if args.dry_run else "removed"
        log(f"retention {keep} days: {verb} {removed:,} readings, "
            f"{kept:,} kept, oldest {oldest}")
        return 0

    if args.migrate_data:
        moved = migrate_data_dir()
        if moved:
            log("done — restart the agent so it picks up the new location:")
            log(f"  {how_to('restart')}")
        return 0

    if args.export is not None:
        conn = open_store()
        try:
            written = store.export_csv(conn, args.export,
                                       terms_by_provider=export_terms())
        finally:
            conn.close()
        for path, n in written:
            print(f"{path}  ({n:,} rows)")
        if not written:
            print("nothing to export — no sources in the database yet")
        return 0

    if not enabled_sources(cfg):
        log("ERROR no sources configured yet.")
        log("       Run: python3 setup.py   (finds monitors near you)")
        log(f"       It writes your settings to {CONFIG_PATH}")
        return 2

    missing = missing_keys(cfg)
    if missing and len(missing) == len(enabled_sources(cfg)):
        for src in missing:
            p = get_provider(src)
            log(f"ERROR no API key for {p.slug}. Set ${p.key_env} or write "
                f"~/.airo/{p.slug}.key — get one at {p.key_url}")
        return 2
    for src in missing:
        p = get_provider(src)
        log(f"WARN skipping {p.slug}/{src.get('site_id')}: no API key")

    if args.backfill:
        conn = open_store()
        try:
            total = 0
            for sid, src, provider in register_sources(conn, cfg):
                if src in missing:
                    continue
                total += backfill_source(conn, sid, src, provider, days=args.backfill)
            log(f"backfill complete: {total} new rows")
        finally:
            conn.close()
        return 0

    if args.once:
        try:
            do_poll(cfg)
            return 0
        except Exception as e:
            log(f"ERROR poll failed: {type(e).__name__}: {e}")
            return 1

    # The polling loop. `--daemon` names it, but every other mode has
    # returned by this point, so reaching here *is* the default and the flag
    # is deliberately not branched on -- `args.daemon` appears nowhere.
    #
    # Written down rather than left to be rediscovered, because a flag that is
    # never read looks like a bug: `tests/test_contracts.py` enumerates the
    # parser and requires each flag to be either read or listed as a synonym
    # for the default, so this stays a decision somebody made instead of an
    # oversight nobody noticed.
    #
    # This is not the scheduled path. ARCHITECTURE §2.1 is `StartInterval`,
    # not a resident daemon: the agent runs `--once` every 15 minutes and
    # exits. This loop is for running Airo in a terminal, or on a system whose
    # scheduler nobody has taught it about yet.
    if cfg.get("serve", True):
        t = threading.Thread(target=serve_forever,
                             args=(cfg.get("serve_port", 8787),), daemon=True)
        t.start()

    interval = max(2, int(cfg.get("poll_minutes", 15))) * 60
    log(f"daemon started — polling every {interval//60} min")
    backoff = 0
    while True:
        try:
            do_poll(cfg)
            backoff = 0
        except urllib.error.HTTPError as e:
            backoff = min(backoff + 1, 5)
            log(f"ERROR HTTP {e.code} — backing off {backoff} cycles")
        except Exception as e:
            backoff = min(backoff + 1, 5)
            log(f"ERROR {type(e).__name__}: {e} — backing off {backoff} cycles")
        time.sleep(interval * (1 + backoff))


if __name__ == "__main__":
    sys.exit(main())
