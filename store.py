#!/usr/bin/env python3
"""
Airo storage layer — SQLite.

Why SQLite rather than the CSV this project started with (reversing
ARCHITECTURE S2.5, deliberately — see ARCHITECTURE S2.5a for the full
reasoning):

  1. Multiple sources per location means cross-source joins and time
     bucketing. That is a query, and hand-rolling one over N CSV files in
     Python is building a worse query engine.
  2. A Tauri tray shell reads the same data from Rust. With SQLite the
     fusion rule is one SQL view both languages execute, so the menu bar
     and the dashboard cannot disagree. With CSVs the rule would be
     implemented twice, in two languages, and have to stay in lockstep.
  3. append_rows() rewrote the whole file every poll: 44 ms at 17k rows,
     but 399 ms and 13.7 MB at 160k (three sources, one year, 10-minute).
     That is ~1.3 GB of writes a day for a modest setup.

The preservation argument in S2.5 is answered rather than dismissed: SQLite
is one of only five storage formats the US Library of Congress recommends
for datasets (https://www.sqlite.org/locrsf.html), and `airo export` writes
per-source CSV that CI round-trip tests. The fifty-year guarantee stands; we
just stop paying for it on every poll.

Standard library only — sqlite3 ships with Python.

How it fits the whole
---------------------
Everything that touches readings comes through here. `poller.py` calls
insert_readings() and backfill; `fusion.py` reads what these queries return but
never queries itself; `backup.py` snapshots through SQLite's own backup API
rather than copying the file, so an archive taken mid-poll is still coherent;
the Rust tray reads `latest.json` and never opens the database -- it stats
`airo.db` in one place to notice a legacy data directory, and that is the whole
of its contact with the store.

This module makes no judgement about air quality beyond flagging obvious
instrument faults (assess_quality, below). Which reading is the *headline* is
fusion's decision, and what a number means is poller's. Keep it that way: a
threshold here would be a third place a health-relevant boundary could live.

Schema decisions whose consequences are not obvious from the SQL
---------------------------------------------------------------
  * **PRIMARY KEY (source_id, observed_utc)** is what makes ingest idempotent.
    Overlapping backfill windows are therefore free, and a repaired gap cannot
    double-count -- which is the property rule 5 depends on. Nothing else
    enforces it; change this key and every "did we already have that?" claim in
    the project stops being true.
  * **WITHOUT ROWID** because that composite key *is* the natural ordering. The
    table is written in observation order and read in ranges, so storing rows
    in key order removes an indirection from every query that matters.
  * **WAL** so readers never block the writer. The dashboard queries while a
    poll is inserting; without it, one would wait on the other.
  * **ON DELETE CASCADE** on readings.source_id means removing a source
    destroys its history. `forget_source()` exports before deleting for exactly
    that reason -- the cascade is convenient and unforgiving.
  * **Times are ISO-8601 UTC strings, not integers.** They sort correctly as
    text, they are readable in a CSV export fifty years from now, and no
    consumer needs to know an epoch convention. The cost is that comparisons
    are string comparisons, so anything writing a differently formatted
    timestamp silently sorts wrong -- canonical_utc() is the only sanctioned
    writer, and every write goes through it. (This line named _iso() for a
    while after that stopped being true. _iso() now only builds query bounds,
    and a fault injected into it to check the migration journey came back
    green because it no longer guards anything that writes.)

What it assumes
---------------
  * **Readings are append-only in the normal path.** Nothing in a poll updates
    a row. repair_sentinels() is the single deliberate exception and nulls a
    non-measurement in place rather than deleting the row, because the row is
    evidence that we asked and the station answered.
  * **Raw µg/m³ is canonical.** No derived index is ever stored (rule 6).
  * **A negative mass concentration is a sentinel, not a reading.** Rejected
    here as well as in poller.clean_measures(), on purpose: this is the last
    place it can be stopped, and a stored -9999 renders as "Very good".
  * **Nothing is silently discarded.** A suspect reading is flagged and kept.
    If there is a fire next door, that is genuinely the air being breathed.
"""

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 9

# Raw ug/m3 is canonical everywhere. Any air quality index is derived at
# presentation time, because the same air gives very different index numbers
# on different national scales and we must not bake one country's opinion
# into stored data.
SCHEMA = """
PRAGMA journal_mode = WAL;          -- readers (tray, dashboard) never block the writer
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    provider           TEXT    NOT NULL,
    site_id            TEXT    NOT NULL,
    site_name          TEXT,
    latitude           REAL,
    longitude          REAL,
    resolution_minutes INTEGER NOT NULL DEFAULT 10,
    enabled            INTEGER NOT NULL DEFAULT 1,
    added_utc          TEXT,
    -- Where the instrument is, which decides what its reading is allowed to
    -- mean. 'outdoor', 'indoor' or 'unknown' -- see PLACEMENTS.
    placement          TEXT    NOT NULL DEFAULT 'unknown',
    UNIQUE (provider, site_id)
);

CREATE TABLE IF NOT EXISTS readings (
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,

    -- observed_utc is when the air was measured; fetched_utc is when we asked.
    -- They differ by minutes on a consumer sensor and by up to an hour on a
    -- regulatory feed, and conflating them is how a stale reading gets
    -- presented as current.
    observed_utc     TEXT    NOT NULL,
    fetched_utc      TEXT,
    kind             TEXT    NOT NULL DEFAULT 'live',   -- 'live' | 'history'

    pm25             REAL,   -- canonical, ug/m3
    pm25_now         REAL,
    pm25_30min       REAL,
    pm25_60min       REAL,
    pm25_6hr         REAL,
    pm25_24hr        REAL,
    pm25_1week       REAL,

    -- Per-channel readings. A PurpleAir holds two laser counters; when they
    -- disagree the instrument is faulty or obstructed, which is the single
    -- most reliable fault signal available and looks nothing like bad air.
    pm25_a           REAL,
    pm25_b           REAL,
    confidence       REAL,   -- provider's own 0-100 self-assessment, if any

    humidity         REAL,
    temperature      REAL,
    temperature_unit TEXT,

    -- 'ok' | 'extreme' | 'suspect'. Surfaced, never silently dropped
    -- (ROADMAP #6). 'extreme' is implausibly high air with no sign the
    -- instrument is at fault; 'suspect' is the instrument. Only the
    -- latter is withheld from charts and aggregates.
    quality          TEXT    NOT NULL DEFAULT 'ok',

    PRIMARY KEY (source_id, observed_utc)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_observed  ON readings(observed_utc);
CREATE INDEX IF NOT EXISTS idx_readings_src_obs   ON readings(source_id, observed_utc DESC);

-- v4. Weather, kept in its own table rather than as columns on `readings`.
--
-- ROADMAP #9 Phase A says "add wind, temperature, humidity and pressure to
-- each row". Deviating from that deliberately, for three reasons, the last of
-- which is the one that settles it:
--
--   * Weather belongs to a place and an hour, not to a sensor. A 10-minute
--     PurpleAir writes six rows an hour and would carry six copies of one
--     observation, which is six chances to disagree.
--   * Phase B correlates PM2.5 against the weather *at that hour*. That is a
--     join, and joining is what this shape is for.
--   * `readings` already has `temperature` and `humidity`, and they are the
--     SENSOR's own — a PurpleAir's onboard thermometer sits inside a warm
--     enclosure and reads several degrees above ambient. Writing ambient
--     weather into those columns would corrupt both meanings at once, and the
--     humidity one is load-bearing: it is what explains a consumer sensor
--     over-reading.
--
-- Keyed on a rounded place string rather than raw floats: a REAL primary key
-- invites precision surprises, and three decimals is about 100 m, which is
-- finer than any weather model this reads from.
CREATE TABLE IF NOT EXISTS weather (
    place          TEXT NOT NULL,        -- "lat,lon" to 3dp; see place_key()
    observed_utc   TEXT NOT NULL,
    source         TEXT NOT NULL,
    fetched_utc    TEXT,

    -- Units are in the names on purpose. The correlation Phase B reproduces
    -- is stated in m/s, and this API returns km/h unless told otherwise --
    -- a silent unit change would not fail, it would just quietly move every
    -- threshold by 3.6x.
    temperature_c  REAL,
    humidity_pct   REAL,
    pressure_hpa   REAL,
    wind_speed_ms  REAL,
    wind_dir_deg   REAL,

    PRIMARY KEY (place, observed_utc)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_weather_observed ON weather(observed_utc);
"""

# Above this, a low-cost optical sensor is almost certainly reporting a fault
# rather than air. Flagged, not deleted -- see ARCHITECTURE S3.5.
SUSPECT_PM25 = 350.0

# A PurpleAir contains two independent laser counters reading the same air.
# When they disagree badly the instrument is faulty or obstructed -- a spider
# in the inlet is the classic cause. This is the most reliable fault signal
# available and it looks nothing like genuinely bad air, so it is worth
# checking before anything else.
CHANNEL_DISAGREE_RATIO = 2.0     # one channel double the other
CHANNEL_DISAGREE_FLOOR = 5.0     # ignore ratios on trivially small numbers
LOW_CONFIDENCE = 50.0            # provider's own 0-100 self-assessment


def self_checked(pm25_a=None, pm25_b=None):
    """Whether this reading came from an instrument able to check itself.

    Two channels that agree are the strongest fault signal this project has.
    One channel is not a fault and not a corroboration either -- most of the
    regulatory network is single-valued. Reported so a surface can say which
    it is rather than leaving "no fault found" to imply two channels agreed.
    """
    return pm25_a is not None and pm25_b is not None


def assess_quality(pm25, pm25_a=None, pm25_b=None, confidence=None):
    """Classify a single reading as 'ok', 'extreme' or 'suspect'.

    Deliberately conservative: only obvious instrument faults are flagged.
    A genuinely high reading corroborated by both channels is *not* suspect,
    however unusual it looks -- deciding whether it reflects regional air is
    a separate question, handled by corroboration in fusion.py.

    Three verdicts, because two were not enough:

      ok        nothing to say
      extreme   implausibly high for ambient air, and nothing suggests the
                instrument is wrong. Shown *and counted*.
      suspect   positive evidence the instrument is wrong. Shown, not counted.

    The split matters more than it looks. This function used to check the
    level first and answer 'suspect', which asserts "the sensor is broken" on
    the strength of a number that is a statement about the *air*. Australian
    suburbs sat well past 350 µg/m³ for days during Black Summer, and on
    exactly those days every aggregate went quiet -- the readings that mattered
    most were being filed as faults and filtered out of the chart, the evening
    analysis and the alert. A tool that goes silent when the air gets
    dangerous is worse than one that never claimed to watch it.

    So fault evidence is examined *first*, and the level is only consulted when
    there is none. A single-value government feed has no way to self-check, and
    that is not suspicious -- it is most of the network.
    """
    if pm25 is None:
        return "ok"

    # Evidence about the instrument, which outranks any statement about the
    # air. Channel disagreement first: it is the most reliable signal here and
    # looks nothing like bad air.
    if pm25_a is not None and pm25_b is not None:
        hi, lo = max(pm25_a, pm25_b), min(pm25_a, pm25_b)
        if hi > CHANNEL_DISAGREE_FLOOR and lo >= 0:
            if lo == 0 or hi / max(lo, 0.1) > CHANNEL_DISAGREE_RATIO:
                return "suspect"
    # The provider's self-assessment, and only where it means something.
    #
    # PurpleAir derives `confidence` from how far its two laser counters
    # disagree. A sensor reporting a single channel has nothing to disagree
    # with, so a low number there is not evidence about the instrument -- it
    # is the absence of a second opinion, which is a different thing.
    #
    # An indoor PA-I reporting channel A only, at confidence 30, had every
    # live reading filed as a fault: excluded from the chart, from the
    # evening analysis and from the inside-against-outside comparison, while
    # PurpleAir's own map showed it healthy. The docstring above already
    # states the principle this broke -- "a single-value government feed has
    # no way to self-check, and that is not suspicious" -- and a
    # single-channel PurpleAir is the same case wearing a confidence figure.
    #
    # Not silently trusted either: `self_checked` below reports whether the
    # instrument was able to check itself, so a surface can say "one channel"
    # rather than implying two agreed.
    # Narrow: only the case where the figure is known to be measuring an
    # absent partner. With both channels it is a real self-check; with
    # neither, the provider is doubting its own reading without saying why,
    # and that is still worth heeding. It is *one* channel present that makes
    # the number uninterpretable.
    half_reported = (pm25_a is None) != (pm25_b is None)
    if (not half_reported and confidence is not None
            and confidence < LOW_CONFIDENCE):
        return "suspect"

    # No reason to doubt the instrument. A number this high is then a claim
    # about the air, flagged so every surface can mark it as unverified.
    if pm25 > SUSPECT_PM25:
        return "extreme"
    return "ok"


def connect(db_path):
    """Open the database, creating the schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _upgrade(conn)
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    else:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                     (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _upgrade(conn):
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS does nothing to an existing table, so new
    columns have to be added explicitly or an upgraded install would fail on
    every write with 'no such column'.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
    for col, decl in (("pm25_a", "REAL"), ("pm25_b", "REAL"),
                      ("confidence", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {decl}")
    conn.commit()

    # v3: temperature is stored in Celsius. Rows written before this held the
    # provider's native unit -- Fahrenheit for PurpleAir -- with the unit only
    # recorded alongside. Convert them once, in a transaction, so the column
    # means one thing. Guarded by temperature_unit so it cannot double-convert.
    stale = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE temperature_unit = 'F'"
    ).fetchone()["n"]
    if stale:
        conn.execute("""
            UPDATE readings
               SET temperature = ROUND((temperature - 32.0) * 5.0 / 9.0, 1),
                   temperature_unit = 'C'
             WHERE temperature_unit = 'F' AND temperature IS NOT NULL
        """)
        conn.execute(
            "UPDATE readings SET temperature_unit = 'C' WHERE temperature_unit = 'F'")
        conn.commit()
        # Worth announcing when it is someone's real history; not worth
        # printing for the handful of rows a test fixture creates.
        if stale >= 100:
            print(f"migrated {stale:,} temperature readings "
                  f"from Fahrenheit to Celsius")

    # v9: a single-channel sensor is no longer a fault. `assess_quality` used
    # the provider's confidence figure whatever the reading carried, and that
    # figure is derived from how far two laser counters disagree -- so a sensor
    # reporting one channel was condemned by a number that could not be about
    # it. Every live reading from an indoor PA-I was filed as an instrument
    # fault and excluded from the chart, the analysis and the comparison.
    #
    # Re-derived from the evidence still stored, exactly as the v5
    # reassessment does: the channels and the confidence are columns, so this
    # sees what the original call saw. Only rows currently marked 'suspect'
    # are looked at -- nothing that was 'ok' can become a fault here, and a
    # verdict corrected by hand afterwards is not quietly overwritten.
    if _schema_version(conn) < 9:
        _reassess_single_channel(conn)

    # v8: a source records where it is. Adds the column to a database written
    # before placement existed, and answers the question the migration cannot
    # avoid: what were the sources already there?
    #
    # They were outdoor. Every one of them could only have been added by
    # `discover()`, which sends `location_type: 0` and returns outdoor sensors
    # only, or is a government regulatory monitor. Marking them 'unknown'
    # would be the cautious-looking choice and would be wrong: 'unknown' is
    # excluded from anything describing outdoor air, so every existing install
    # would lose its headline, its alerts and its analysis on upgrade.
    #
    # A hand-edited config could in principle have carried an indoor sensor
    # before this shipped, and this migration cannot tell. That case is
    # corrected on the next poll, which reads `location_type` from the API and
    # writes the real answer — so the worst case is one polling interval of the
    # behaviour the install already had, rather than a broken install for
    # everybody.
    if _schema_version(conn) < 8:
        _add_placement_column(conn)

    # v7: a backfilled temperature carries its unit. `capture_reading()` has
    # always normalised to Celsius and labelled the row 'C'; `backfill_source()`
    # did neither, copying the provider's value and setting no unit at all.
    # That is worse than storing 'F' -- the v3 migration above repairs a row
    # marked 'F' and cannot see a row marked nothing, so a Fahrenheit backfill
    # from PurpleAir was permanent and, from the data alone, undetectable.
    #
    # Repairable only because the source knows its provider and the provider
    # declares its unit. Keyed on the unit being NULL, which the fixed writer
    # never produces, so this cannot double-convert a row written afterwards.
    if _schema_version(conn) < 7:
        _label_unmarked_temperatures(conn)

    # v6: one timestamp format. Times are stored as text and compared as
    # text, so two spellings of one instant are two rows under the primary key
    # and sort around each other -- '+' is 0x2B and 'Z' is 0x5A. OpenAQ's
    # current() passed the API's Z-suffixed string straight through, so a real
    # database held 73 such rows and 64 collisions. The writers are fixed;
    # this repairs what they already wrote.
    if _schema_version(conn) < 6:
        _canonicalise_timestamps(conn)

    # v5: 'suspect' split into 'suspect' and 'extreme'. Rows written before
    # the split call every reading over SUSPECT_PM25 an instrument fault,
    # which is what kept the worst air anyone had recorded off their own
    # chart. Re-derive the verdict from the evidence that is still stored --
    # the two channels and the confidence figure are columns, so
    # assess_quality() sees exactly what it saw the first time.
    #
    # Guarded by the schema version rather than by inspecting the rows, so
    # this cannot run on every poll, and so a verdict corrected by hand
    # afterwards is not quietly overwritten.
    if _schema_version(conn) < 5:
        rows = conn.execute(
            "SELECT source_id, observed_utc, pm25, pm25_a, pm25_b, confidence, "
            "quality FROM readings WHERE quality <> 'ok'").fetchall()
        changed = [
            (verdict, r["source_id"], r["observed_utc"])
            for r in rows
            for verdict in [assess_quality(r["pm25"], r["pm25_a"],
                                           r["pm25_b"], r["confidence"])]
            if verdict != r["quality"]
        ]
        if changed:
            conn.executemany(
                "UPDATE readings SET quality = ? "
                "WHERE source_id = ? AND observed_utc = ?", changed)
            conn.commit()
            reclaimed = sum(1 for v, _, _ in changed if v == "extreme")
            if reclaimed >= 100:
                print(f"re-assessed {reclaimed:,} readings previously filed as "
                      f"sensor faults: they are extreme air, not a broken "
                      f"instrument, and now appear on the chart")


def _reported_unit(slug):
    """The temperature unit a provider reports, asked of the provider.

    Imported lazily: `store` must not depend on `poller` at module scope --
    poller imports store, and a cycle would break every entry point. Falls
    back to Celsius for a slug the registry no longer carries, which is what
    an archived CSV from a retired provider looks like.
    """
    try:
        import poller
        return getattr(poller.PROVIDERS[slug], "temperature_unit", "C")
    except (ImportError, KeyError, AttributeError):
        return "C"


def _reassess_single_channel(conn):
    """Re-derive verdicts that were only ever about a missing second channel."""
    # Only rows where exactly one channel was reported, which is the case
    # this migration is about. Re-deriving every suspect row would also redo
    # the v5 reassessment's work and announce it in the wrong words -- it did,
    # and a test that checks the smoke reassessment says "extreme air" caught
    # it saying "single channel" instead.
    rows = conn.execute(
        "SELECT source_id, observed_utc, pm25, pm25_a, pm25_b, confidence "
        "FROM readings WHERE quality = 'suspect' "
        "  AND ((pm25_a IS NULL) <> (pm25_b IS NULL))").fetchall()
    changed = [
        (verdict, r["source_id"], r["observed_utc"])
        for r in rows
        for verdict in [assess_quality(r["pm25"], r["pm25_a"], r["pm25_b"],
                                       r["confidence"])]
        if verdict != "suspect"
    ]
    if changed:
        conn.executemany(
            "UPDATE readings SET quality = ? "
            "WHERE source_id = ? AND observed_utc = ?", changed)
        conn.commit()
        if len(changed) >= 20:
            print(f"reassessed {len(changed):,} readings that were marked "
                  f"faulty for having a single channel")
    return len(changed)


def _add_placement_column(conn):
    """Add `sources.placement` and mark what is already there as outdoor."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if "placement" not in existing:
        conn.execute("ALTER TABLE sources ADD COLUMN placement TEXT "
                     "NOT NULL DEFAULT 'unknown'")
    # See the note at the call site for why this is 'outdoor' and not
    # 'unknown'. Only rows that predate the column are touched: a source added
    # after it exists carries whatever it was registered with.
    conn.execute("UPDATE sources SET placement = 'outdoor' "
                 "WHERE placement IS NULL OR placement = 'unknown'")
    conn.commit()
    return conn.total_changes


def _label_unmarked_temperatures(conn):
    """Give every unlabelled temperature its unit, converting where needed.

    Rows are attributed by their source's provider, and the unit comes from
    the provider class rather than from a slug compared here -- the same
    reason `_reported_unit` exists. A provider the registry no longer carries
    is treated as Celsius and only labelled, never converted: guessing a
    conversion on a retired adapter would turn a recoverable unknown into a
    confident wrong number, and rule 5's shape says an unknown is not a zero.
    """
    rows = conn.execute(
        "SELECT s.provider AS provider, COUNT(*) AS n FROM readings r "
        "JOIN sources s ON s.id = r.source_id "
        "WHERE r.temperature_unit IS NULL AND r.temperature IS NOT NULL "
        "GROUP BY s.provider").fetchall()
    if not rows:
        # Deliberately does *not* stamp the schema version. My first version
        # did, reasoning that nothing to repair is a completed migration --
        # and stamping here set the version to 7 before the v6 and v5 blocks
        # had run, so both saw a current database and skipped themselves.
        # Six tests went red at once, which is the only reason this is not
        # still in. The version is stamped once, after every block.
        return 0

    converted = 0
    for row in rows:
        provider = row["provider"]
        if _reported_unit(provider) == "F":
            conn.execute("""
                UPDATE readings
                   SET temperature = ROUND((temperature - 32.0) * 5.0 / 9.0, 1),
                       temperature_unit = 'C'
                 WHERE temperature_unit IS NULL
                   AND temperature IS NOT NULL
                   AND source_id IN (SELECT id FROM sources WHERE provider = ?)
            """, (provider,))
            converted += row["n"]
        else:
            conn.execute("""
                UPDATE readings SET temperature_unit = 'C'
                 WHERE temperature_unit IS NULL
                   AND temperature IS NOT NULL
                   AND source_id IN (SELECT id FROM sources WHERE provider = ?)
            """, (provider,))
    conn.commit()
    if converted >= 100:
        print(f"converted {converted:,} backfilled temperatures from "
              f"Fahrenheit; they had been stored with no unit recorded")
    return converted


def _canonicalise_timestamps(conn):
    """Rewrite every stored timestamp into one form, merging what collides.

    Rule 5 governs this: two rows for one instant must become one row that
    keeps everything either of them held. A naive UPDATE would hit the primary
    key and either fail or, with OR REPLACE, silently discard whichever row it
    overwrote -- and in the database that prompted this, one of each colliding
    pair carried a humidity the other lacked.

    So each group is merged field by field, preferring a value over a NULL,
    then written once. Counted and announced when it is somebody's real
    history rather than a fixture.
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(readings)")]
    rows = conn.execute("SELECT * FROM readings").fetchall()

    groups = {}
    changed = False
    for row in rows:
        canon = canonical_utc(row["observed_utc"])
        if canon is None:
            continue                    # unparseable: left alone, not deleted
        if canon != row["observed_utc"]:
            changed = True
        key = (row["source_id"], canon)
        keep = groups.get(key)
        if keep is None:
            groups[key] = dict(row)
            groups[key]["observed_utc"] = canon
            continue
        # Merge: a value beats a NULL. Neither row is more authoritative, so
        # the rule has to be about information rather than about order.
        changed = True
        for c in cols:
            if keep.get(c) is None and row[c] is not None:
                keep[c] = row[c]

    if not changed:
        return 0

    merged = len(rows) - len(groups)
    conn.execute("DELETE FROM readings")
    conn.executemany(
        f"INSERT INTO readings ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(g[c] for c in cols) for g in groups.values()])
    conn.commit()

    if merged >= 10:
        print(f"merged {merged:,} duplicate reading(s) that were the same "
              f"instant written two different ways")
    return merged


def _schema_version(conn):
    """The version recorded in the database, or 0 if it does not say.

    Read during _upgrade(), which connect() calls *after* running SCHEMA --
    so `meta` always exists by this point and a missing table would be a
    programming error rather than an old database. It is not caught here: the
    first draft wrapped this in `except sqlite3.Error: return 0`, which could
    not fire, and dead defensive code reads as a handled case that is not.

    A missing or unparseable row does mean an old database, and that answers
    0, so every migration runs.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------- sources

#: Where a sensor is, and therefore what its readings may be used for.
#:
#: Three values, not a boolean. An unprobed sensor is not an outdoor one, and
#: defaulting the unknown case to 'outdoor' is exactly how an indoor sensor
#: ends up speaking for the air outside: it is ~0 km away, `nearest` is the
#: default fusion rule, and the headline becomes a reading from somebody's
#: kitchen with "avoid outdoor exertion" printed beside it.
#:
#: How `unknown` is treated is decided per consumer and is not the same
#: everywhere. It is excluded from anything that claims to describe outdoor
#: air, and included anywhere that merely displays what was collected --
#: refusing to show a reading would be discarding it, which rule 5a forbids.
PLACEMENTS = ("outdoor", "indoor", "unknown")

#: The placements a statement about *outdoor* air may be built from. Named
#: rather than written as `!= "indoor"` at each call site: there are five such
#: sites and a fourth placement would have to find all of them.
OUTDOOR_PLACEMENTS = ("outdoor",)


def _default_placement(provider):
    """What this network's sensors are, when the caller did not say.

    Asked of the provider registry rather than decided here, the same way
    `_reported_unit` asks about temperature and for the same reason: deciding
    it from the slug in this file would be a second list to keep in step, and
    the one in `poller` is the one that knows.

    Lazy import, because `poller` imports `store` and a module-level import
    would be a cycle that breaks every entry point.

    A regulatory network answers 'outdoor'. PurpleAir answers None, because it
    genuinely varies and is detected per sensor -- so a PurpleAir source whose
    placement nobody has established stays 'unknown', which is excluded from
    anything describing outdoor air. That is the safe direction: an
    unidentified consumer sensor could be in a kitchen.
    """
    try:
        import poller
        return getattr(poller.PROVIDERS[str(provider).lower()],
                       "default_placement", None)
    except (ImportError, KeyError, AttributeError):
        return None


def _clean_placement(value):
    """A stored placement, or None to leave whatever is there alone.

    None and an unrecognised value are different: None means "I have nothing
    to say", which must not overwrite a known answer, while a value this
    project does not recognise is a bug worth landing as 'unknown' rather than
    being written through to the column.
    """
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    return cleaned if cleaned in PLACEMENTS else "unknown"


def is_outdoor(placement):
    """Whether a reading from here may describe the air outside."""
    return str(placement or "unknown").lower() in OUTDOOR_PLACEMENTS


def upsert_source(conn, provider, site_id, site_name=None, latitude=None,
                  longitude=None, resolution_minutes=10, enabled=True,
                  placement=None):
    """Register a source, or update its metadata. Returns the source id."""
    site_id = str(site_id)
    conn.execute(
        """
        INSERT INTO sources (provider, site_id, site_name, latitude, longitude,
                             resolution_minutes, enabled, added_utc, placement)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'))
        ON CONFLICT(provider, site_id) DO UPDATE SET
            site_name          = COALESCE(excluded.site_name, sources.site_name),
            latitude           = COALESCE(excluded.latitude, sources.latitude),
            longitude          = COALESCE(excluded.longitude, sources.longitude),
            resolution_minutes = excluded.resolution_minutes,
            enabled            = excluded.enabled,
            -- NULLIF before COALESCE, so a caller who does not know leaves
            -- the stored answer alone. The column is NOT NULL, so "nothing to
            -- say" arrives here as 'unknown' rather than as NULL, and a plain
            -- COALESCE would then overwrite a sensor somebody has already
            -- identified every time a poll ran without reaching the API.
            --
            -- The cost is that 'unknown' cannot be written back over a known
            -- placement. That is the right way round: forgetting what a sensor
            -- is should take a deliberate act, not a network failure.
            placement          = COALESCE(NULLIF(excluded.placement, 'unknown'),
                                          sources.placement)
        """,
        (provider, site_id, site_name, latitude, longitude,
         int(resolution_minutes), 1 if enabled else 0,
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         _clean_placement(placement if placement is not None
                          else _default_placement(provider))),
    )
    conn.commit()
    cur = conn.execute(
        "SELECT id FROM sources WHERE provider = ? AND site_id = ?",
        (provider, site_id))
    return cur.fetchone()["id"]


def list_sources(conn, enabled_only=True):
    sql = "SELECT * FROM sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql)]


def remove_source(conn, provider, site_id):
    """Disable a source. Its readings are always kept.

    This used to take `delete_readings=True`, which deleted the sources row --
    and readings.source_id is ON DELETE CASCADE, so that silently destroyed
    every reading the source had ever produced. Nothing called it, but the
    parameter sat one argument away from erasing years of irreplaceable
    history with no log, no confirmation and no backup, which is exactly what
    rule 5 exists to prevent.

    Purging a source's history is now a separate, explicit operation:
    forget_source(), which exports before it deletes.
    """
    conn.execute(
        "UPDATE sources SET enabled = 0 WHERE provider = ? AND site_id = ?",
        (provider, str(site_id)))
    conn.commit()


def forget_source(conn, provider, site_id, export_dir=None, dry_run=False):
    """Delete a source AND its readings. Returns (rows, export_path).

    The one operation in the codebase that destroys readings on purpose, so
    it is deliberately awkward: it reports what it will remove, writes a CSV
    of the doomed rows first unless explicitly told not to, and returns the
    count so a caller cannot ignore what happened.

    `export_dir=False` skips the export. That is spelled out at the call site
    rather than being the default, because the default must be the safe one.
    """
    row = conn.execute(
        """SELECT s.id, COUNT(r.source_id) AS n
             FROM sources s LEFT JOIN readings r ON r.source_id = s.id
            WHERE s.provider = ? AND s.site_id = ?
         GROUP BY s.id""", (provider, str(site_id))).fetchone()
    if row is None:
        return 0, None

    if dry_run:
        return row["n"], None

    exported = None
    if export_dir is not False:
        target = Path(export_dir) if export_dir else (Path.cwd() / "export")
        written = export_csv(conn, target, source_id=row["id"])
        exported = written[0][0] if written else None

    conn.execute("PRAGMA foreign_keys = ON")      # make the cascade deliberate
    conn.execute("DELETE FROM sources WHERE id = ?", (row["id"],))
    conn.commit()
    return row["n"], exported


# ------------------------------------------------------------------ readings

def insert_readings(conn, source_id, rows):
    """Insert observations for one source. Returns the number newly stored.

    Idempotent by (source_id, observed_utc), so overlapping backfill windows
    are free and a repaired gap can never double-count.
    """
    # An early-out with no observable effect: the count difference below is
    # zero for an empty list either way. Kept because it is clearer than
    # querying twice to prove nothing happened, and recorded as deliberate
    # because a mutation sweep will keep reporting it as an untested guard --
    # it is untestable rather than untested.
    if not rows:
        return 0

    before = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE source_id = ?",
        (source_id,)).fetchone()["n"]

    payload = []
    for r in rows:
        observed = canonical_utc(r.get("observed_utc") or r.get("utc"))
        if not observed:
            # No timestamp, or one nothing can parse. Skipped rather than
            # stored: an unreadable value in the primary key is permanent, and
            # every range query would sort around it wrongly forever after.
            continue
        pm = r.get("pm25")
        a, b = r.get("pm25_a"), r.get("pm25_b")
        # Defence in depth behind poller.clean_measures(). A mass
        # concentration cannot be negative, so a negative is a feed sentinel
        # (-9999 in the Queensland API) rather than a low reading. Storing it
        # corrupts every average computed from this row, and verify() would
        # only report it afterwards. Dropped to NULL, which the gap detector
        # then treats as the missing observation it actually is.
        if pm is not None and pm < 0:
            pm = None
        if a is not None and a < 0:
            a = None
        if b is not None and b < 0:
            b = None
        payload.append((
            source_id, observed, r.get("fetched_utc"), r.get("kind", "live"),
            pm, r.get("pm25_now"), r.get("pm25_30min"), r.get("pm25_60min"),
            r.get("pm25_6hr"), r.get("pm25_24hr"), r.get("pm25_1week"),
            a, b, r.get("confidence"),
            r.get("humidity"), r.get("temperature"), r.get("temperature_unit"),
            assess_quality(pm, a, b, r.get("confidence")),
        ))

    # A live poll should overwrite a placeholder written by backfill for the
    # same instant, but must never blank a real value with a null.
    conn.executemany(
        """
        INSERT INTO readings (
            source_id, observed_utc, fetched_utc, kind,
            pm25, pm25_now, pm25_30min, pm25_60min, pm25_6hr, pm25_24hr,
            pm25_1week, pm25_a, pm25_b, confidence,
            humidity, temperature, temperature_unit, quality)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id, observed_utc) DO UPDATE SET
            pm25        = COALESCE(excluded.pm25,        readings.pm25),
            pm25_now    = COALESCE(excluded.pm25_now,    readings.pm25_now),
            pm25_30min  = COALESCE(excluded.pm25_30min,  readings.pm25_30min),
            pm25_60min  = COALESCE(excluded.pm25_60min,  readings.pm25_60min),
            pm25_6hr    = COALESCE(excluded.pm25_6hr,    readings.pm25_6hr),
            pm25_24hr   = COALESCE(excluded.pm25_24hr,   readings.pm25_24hr),
            pm25_1week  = COALESCE(excluded.pm25_1week,  readings.pm25_1week),
            pm25_a      = COALESCE(excluded.pm25_a,      readings.pm25_a),
            pm25_b      = COALESCE(excluded.pm25_b,      readings.pm25_b),
            confidence  = COALESCE(excluded.confidence,  readings.confidence),
            humidity    = COALESCE(excluded.humidity,    readings.humidity),
            temperature = COALESCE(excluded.temperature, readings.temperature),
            quality     = excluded.quality
        """,
        payload,
    )
    conn.commit()

    after = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE source_id = ?",
        (source_id,)).fetchone()["n"]
    return after - before


def latest_per_source(conn, include_disabled=False):
    """Most recent observation for every source, newest first.

    This is the input to every fusion rule.
    """
    sql = """
        SELECT s.id AS source_id, s.provider, s.site_id, s.site_name,
               s.latitude, s.longitude, s.resolution_minutes, s.enabled,
               s.placement,
               r.observed_utc, r.fetched_utc, r.pm25, r.pm25_now,
               r.pm25_30min, r.pm25_60min, r.pm25_6hr, r.pm25_24hr,
               r.pm25_1week, r.pm25_a, r.pm25_b, r.confidence,
               r.humidity, r.temperature, r.temperature_unit, r.quality
        FROM sources s
        JOIN readings r ON r.source_id = s.id
        WHERE r.observed_utc = (
            SELECT MAX(observed_utc) FROM readings
            WHERE source_id = s.id AND pm25 IS NOT NULL
        )
    """
    if not include_disabled:
        sql += " AND s.enabled = 1"
    sql += " ORDER BY r.observed_utc DESC"
    return [dict(r) for r in conn.execute(sql)]


def last_observed(conn, source_id):
    """Newest observation time for one source, as an aware datetime."""
    row = conn.execute(
        "SELECT MAX(observed_utc) AS m FROM readings WHERE source_id = ?",
        (source_id,)).fetchone()
    if not row or not row["m"]:
        return None
    try:
        return datetime.fromisoformat(row["m"])
    except ValueError:
        return None


def suspect_readings(conn, since=None, until=None, source_id=None):
    """The readings assess_quality() flagged, so a surface can say so.

    series() excludes them by default, which is right for a chart -- a blocked
    inlet reading 900 µg/m³ would swamp the axis and every average. But
    excluded and *unmentioned* is a silent drop, and the policy is surface,
    don't drop. This is how the count reaches the page without putting the
    values back into the aggregates.
    """
    where, params = ["pm25 IS NOT NULL", "quality != 'ok'"], []
    if source_id is not None:
        where.append("source_id = ?")
        params.append(source_id)
    if since is not None:
        where.append("observed_utc >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("observed_utc <= ?")
        params.append(_iso(until))
    return [dict(r) for r in conn.execute(
        f"SELECT source_id, observed_utc, pm25, quality FROM readings "
        f"WHERE {' AND '.join(where)} ORDER BY observed_utc", params)]


def series(conn, source_id=None, since=None, until=None, bucket_minutes=None,
           include_suspect=False):
    """Time series of raw ug/m3.

    With bucket_minutes set, returns the min and max in each bucket rather
    than an average -- the spikes are the signal in this data, and averaging
    is exactly what would erase them (ROADMAP #7).

    Excludes instrument faults, and *includes* extreme air. Those were the same
    thing until assess_quality() learned to tell them apart, which meant the
    worst air on record was the air no chart drew. Every row carries its
    `quality` so a surface can mark what it is showing; see ARCHITECTURE §3.5,
    surface don't drop. include_suspect=True brings back the faults as well,
    for anything auditing the record rather than reporting the air.
    """
    where, params = ["pm25 IS NOT NULL"], []
    if not include_suspect:
        where.append("quality != 'suspect'")
    if source_id is not None:
        where.append("source_id = ?")
        params.append(source_id)
    if since is not None:
        where.append("observed_utc >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("observed_utc <= ?")
        params.append(_iso(until))
    clause = " AND ".join(where)

    if not bucket_minutes:
        sql = (f"SELECT source_id, observed_utc, pm25, quality FROM readings "
               f"WHERE {clause} ORDER BY observed_utc")
        return [dict(r) for r in conn.execute(sql, params)]

    # Bucket by flooring the epoch seconds. strftime('%s') is UTC, so this is
    # DST-safe in a way that local-midnight arithmetic is not.
    secs = int(bucket_minutes) * 60
    sql = f"""
        SELECT source_id,
               (CAST(strftime('%s', observed_utc) AS INTEGER) / {secs}) * {secs}
                   AS bucket_epoch,
               MIN(pm25) AS pm25_min,
               MAX(pm25) AS pm25_max,
               AVG(pm25) AS pm25_mean,
               COUNT(*)  AS n
        FROM readings
        WHERE {clause}
        GROUP BY source_id, bucket_epoch
        ORDER BY bucket_epoch
    """
    return [dict(r) for r in conn.execute(sql, params)]


#: The one stored form: ISO-8601 UTC, seconds precision, offset spelled out.
#: Twenty-five characters. Everything written to a time column looks like this.
CANONICAL_UTC_LEN = 25


def canonical_utc(v):
    """One instant, one string. None if `v` is not a time at all.

    Times are stored as text and compared as text, so *how* an instant is
    spelled decides both sorting and identity. `2026-07-31T11:00:00Z` and
    `2026-07-31T11:00:00+00:00` are the same moment and different strings:
    the primary key `(source_id, observed_utc)` treats them as two readings,
    and `'+'` (0x2B) sorts before `'Z'` (0x5A) so a range query behaves
    differently either side of the change.

    That is not hypothetical. `_iso()` was documented as the only sanctioned
    writer and ended `return str(v)`, and `insert_readings()` normalised a
    datetime while passing a string through -- so OpenAQ's `current()`, which
    hands back the API's own Z-suffixed string, put 73 rows of a real database
    into a form nothing else used, and 64 of them collided with a row for the
    same instant.

    A naive datetime is read as UTC rather than local. Every naive timestamp
    reaching the store has already been given a zone by its provider --
    Queensland's is a written-down +10:00 -- so applying the *reader's* offset
    here is the bug `QLD_TIMEZONE` exists to prevent, one layer further in.
    """
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat(timespec="seconds")

    if not isinstance(v, str):
        return None
    text = v.strip()
    if not text:
        return None
    try:
        # fromisoformat only learned 'Z' in 3.11 and this project supports 3.9.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _iso(v):
    """Canonical form for a query bound as text, falling back to the input.

    Query parameters are compared against stored values, so they have to be
    spelled the same way. The fallback keeps a caller that passes something
    exotic from crashing a read -- a write goes through canonical_utc() and
    is refused instead, because a bad value in a key is permanent and a bad
    value in a WHERE clause simply matches nothing.
    """
    return canonical_utc(v) or str(v)


def counts(conn):
    """Row counts per source, for status output."""
    return [dict(r) for r in conn.execute("""
        SELECT s.provider, s.site_id, s.site_name, s.enabled,
               COUNT(r.observed_utc) AS rows,
               MIN(r.observed_utc)   AS first_utc,
               MAX(r.observed_utc)   AS last_utc
        FROM sources s
        LEFT JOIN readings r ON r.source_id = s.id
        GROUP BY s.id
        ORDER BY s.id
    """)]


# The bucket key peer_ratio_history groups on: 'YYYY-MM-DDTHH', from the
# strftime in its query. Thirteen characters, fixed width.
HOUR_KEY_LEN = 13


def _utc_hour(hr):
    """The UTC hour an hourly bucket key names, or None if it is malformed."""
    try:
        return int(str(hr)[-2:])
    except (ValueError, TypeError):
        return None


def _local_hour(hr, tz=None):
    """The *local* hour that bucket mostly falls in, or None if malformed.

    Two things make this more than adding an offset to a number.

    The offset depends on the *date* wherever daylight saving is observed:
    02:00 UTC is 6pm in Los Angeles in January and 7pm in August. Labelling
    from a fixed reference date is right for half the year and an hour out
    for the rest -- and an evening-premium tool being an hour out about the
    evening is the finding itself, misplaced.

    And where the offset is not a whole number of hours -- India, Adelaide,
    Nepal, Newfoundland, Chatham -- a UTC-hour bucket straddles two local
    hours, so no label is exactly right. Taking the *midpoint* rather than
    the start picks the local hour holding most of the bucket, which for a
    half-hour offset is the one a reading at that local hour actually landed
    in: 7pm in Adelaide is 09:30 UTC, bucket 09, midpoint 09:30, back to 7pm.

    `tz` exists to be tested: a function whose answer depends on the machine's
    timezone needs the timezone as an input if it is ever to be checked from
    another one. Same reasoning as folder_chooser_commands() taking os_name.

    The key is taken apart by slicing rather than with strptime, which is the
    expensive half of this: `agreement --by-hour` runs the filter 24 times
    over every bucket, and on three years of two sources strptime alone put
    2.9x on that stage. Caching was the other option and it is worse here --
    a per-date cache is only correct until the day of a changeover, which is
    precisely the day this function exists for.
    """
    s = str(hr)
    if len(s) != HOUR_KEY_LEN:
        return None
    try:
        # Built at half past directly: the midpoint, without a second object.
        utc = datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]),
                       30, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return utc.astimezone(tz).hour


def peer_ratio_history(conn, source_id, hour_of_day=None, days=90,
                       min_peer_pm=1.0, hour_is_local=False, tz=None):
    """How this source has historically compared with the others.

    Returns the distribution of (this source / mean of other sources) at the
    same hour, so "is this reading unusual?" can be answered from the record
    rather than from a guess.

    This is the difference between a false positive and a real finding. A
    sensor in a valley that *always* reads 4x its neighbours after sunset is
    measuring a genuine local effect; the same sensor suddenly reading 4x for
    the first time is far more likely to be someone's fire, or a fault.

    Hours where the peers read near zero are dropped: dividing by a very small
    number manufactures enormous ratios out of noise.

    `hour_of_day` is a UTC hour by default, which is what the live
    corroboration check in poller.py wants: it asks about the hour happening
    now and compares like with like. Pass hour_is_local=True to select by the
    hour a person would name instead -- what a report shows someone reasoning
    about their own evening. The two differ by more than an offset where
    daylight saving is observed, which is why this is a flag and not a
    conversion applied afterwards.

    `tz` is the zone a local hour is reckoned in, defaulting to this machine's.
    Passed in rather than looked up: the machine running the query is often not
    where the user is, and a report about which hour of the evening is worst
    must not depend on which computer asked.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute("""
        WITH hourly AS (
            SELECT source_id,
                   strftime('%Y-%m-%dT%H', observed_utc) AS hr,
                   AVG(pm25) AS pm
            FROM readings
            -- Faults out, extreme air in. A site that is right about a fire
            -- must not be told it is exaggerating because its highest hours
            -- were dropped from the comparison.
            WHERE pm25 IS NOT NULL AND quality != 'suspect' AND observed_utc >= ?
            GROUP BY source_id, hr
        ),
        target AS (SELECT hr, pm FROM hourly WHERE source_id = ?),
        peers AS (
            SELECT hr, AVG(pm) AS pm FROM hourly
            WHERE source_id != ? GROUP BY hr
        )
        SELECT t.hr AS hr, t.pm AS target_pm, p.pm AS peer_pm
        FROM target t JOIN peers p ON p.hr = t.hr
        WHERE p.pm >= ?
    """, (since, source_id, source_id, min_peer_pm)).fetchall()

    # Converted once, and a nonsense hour asks for no rows rather than
    # raising -- which is what the old per-row try/except did by accident,
    # kept deliberately: the caller in poller.py degrades to all-hours
    # history, and losing a source's history entirely is the worse failure.
    try:
        want_hour = None if hour_of_day is None else int(hour_of_day)
    except (ValueError, TypeError):
        return {"n": 0, "median": None, "p90": None, "max": None}

    ratios = []
    for r in rows:
        if want_hour is not None:
            hour = (_local_hour(r["hr"], tz) if hour_is_local
                    else _utc_hour(r["hr"]))
            if hour != want_hour:
                continue
        if r["peer_pm"]:
            ratios.append(r["target_pm"] / r["peer_pm"])

    ratios.sort()
    if not ratios:
        return {"n": 0, "median": None, "p90": None, "max": None}
    def pct(q):
        i = min(len(ratios) - 1, max(0, int(round(q * (len(ratios) - 1)))))
        return ratios[i]
    return {"n": len(ratios), "median": pct(0.5), "p90": pct(0.9),
            "max": ratios[-1]}


# -------------------------------------------------------------------- weather

def place_key(latitude, longitude):
    """The key a location is filed under, to three decimals.

    Rounded rather than exact so a configured location that shifts by metres --
    a re-geocode, a typo corrected, a coordinate re-entered by hand -- keeps
    landing on the same rows instead of silently starting a second series. Three
    decimals is about 100 m, far finer than any weather model this reads from,
    and coarse enough that it is not a home address.
    """
    return f"{float(latitude):.3f},{float(longitude):.3f}"


def insert_weather(conn, place, rows, source="open-meteo"):
    """Store hourly weather. Returns the number newly stored.

    Idempotent on (place, observed_utc), exactly as readings are on
    (source_id, observed_utc), so a backfill window that overlaps what is
    already held costs nothing and cannot double up.
    """
    if not rows:
        return 0

    before = conn.execute(
        "SELECT COUNT(*) AS n FROM weather WHERE place = ?", (place,)
    ).fetchone()["n"]

    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = []
    for r in rows:
        observed = canonical_utc(r.get("observed_utc"))
        if not observed:
            continue        # same reasoning as insert_readings
        payload.append((
            place, observed, source, fetched,
            _f(r.get("temperature_c")), _f(r.get("humidity_pct")),
            _f(r.get("pressure_hpa")), _f(r.get("wind_speed_ms")),
            _f(r.get("wind_dir_deg")),
        ))

    conn.executemany("""
        INSERT OR IGNORE INTO weather
            (place, observed_utc, source, fetched_utc,
             temperature_c, humidity_pct, pressure_hpa,
             wind_speed_ms, wind_dir_deg)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, payload)
    conn.commit()

    after = conn.execute(
        "SELECT COUNT(*) AS n FROM weather WHERE place = ?", (place,)
    ).fetchone()["n"]
    return after - before


def weather_at(conn, place, observed_utc):
    """The weather for the hour containing `observed_utc`, or None.

    Readings arrive every ten minutes and weather every hour, so the join is
    "which hour was this reading in" rather than an exact match. Truncating is
    the honest answer: an hourly model has no opinion about 18:37 specifically.
    """
    if isinstance(observed_utc, datetime):
        observed_utc = observed_utc.astimezone(timezone.utc).isoformat()
    hour = str(observed_utc)[:13] + ":00:00+00:00"
    row = conn.execute(
        "SELECT * FROM weather WHERE place = ? AND observed_utc = ?",
        (place, hour)).fetchone()
    return dict(row) if row else None


def hourly_by_placement(conn, since=None, include_extreme=True):
    """Hourly mean PM2.5 for indoor and outdoor, separately.

    Deliberately not the weather join. That one pairs a reading with the wind
    at that hour and requires a weather row to exist, which is right for the
    correlation and wrong here: comparing inside with outside needs neither
    the weather nor an install that has backfilled any.

    Returns {"indoor": {hour: mean}, "outdoor": {hour: mean}}, keyed by the
    same hour string the rest of this module uses, so the two can be lined up
    by lookup rather than by position -- a missing hour on one side is normal
    (a sensor drops out) and zipping two lists would silently pair the wrong
    hours from that point on.

    Instrument faults are excluded. Extreme air is not: a night of real smoke
    is exactly the night somebody wants this comparison for.
    """
    where = ["r.pm25 IS NOT NULL", "r.quality != 'suspect'"]
    args = []
    if not include_extreme:
        where.append("r.quality = 'ok'")
    if since is not None:
        where.append("r.observed_utc >= ?")
        args.append(_iso(since))

    rows = conn.execute(f"""
        SELECT LOWER(COALESCE(s.placement, 'unknown')) AS placement,
               substr(r.observed_utc, 1, {HOUR_KEY_LEN}) || ':00:00+00:00' AS hr,
               AVG(r.pm25) AS pm25,
               COUNT(*)    AS samples
          FROM readings r
          JOIN sources s ON s.id = r.source_id
         WHERE {' AND '.join(where)}
         GROUP BY placement, hr
         ORDER BY hr
    """, args).fetchall()

    out = {"indoor": {}, "outdoor": {}, "unknown": {}}
    for row in rows:
        out.setdefault(row["placement"], {})[row["hr"]] = row["pm25"]
    return out


def hourly_with_weather(conn, place, source_id=None, since=None,
                        include_extreme=True, outdoor_only=True):
    """Every hour that has both a reading and the weather that went with it.

    ROADMAP #9 Phase B is a join, which is the reason weather is its own table
    rather than columns on each row: a ten-minute sensor would carry six copies
    of one hourly observation, and six chances to disagree with itself.

    Readings are averaged to the hour first. That is the resolution the weather
    has, and pairing a 10-minute reading with an hourly wind speed and calling
    the result an observation would overstate what is known -- an hourly model
    has no opinion about 18:37 specifically.

    Instrument faults are excluded and extreme air is not: a night of genuine
    smoke is exactly the night this correlation is about, and dropping it would
    remove the evidence for the premise being tested. `include_extreme=False`
    exists for a caller who wants the ordinary range only, and says so.

    **Indoor sensors are excluded by default**, and this is the right place for
    it rather than in each caller. The whole function pairs a reading with the
    *outdoor* weather at that hour; an indoor reading against outdoor wind is
    not a weak signal, it is a meaningless one. Both callers -- Phase B's
    correlation and Phase C's wind bands -- would otherwise fit a model to it,
    and Phase C's forecast is fitted to Phase B's bands, so one contaminated
    join reaches every claim this project makes about the future.

    `outdoor_only=False` exists for the indoor/outdoor comparison, which wants
    exactly the rows this excludes and knows what it is asking for.
    """
    where = ["r.pm25 IS NOT NULL", "r.quality != 'suspect'"]
    # Bound in the order the placeholders appear in the SQL below, which is
    # the CTE's WHERE first and `w.place` last -- not the order the arguments
    # arrive in. Getting that backwards silently returns no rows rather than
    # raising, because every value is a string and the comparisons simply do
    # not match.
    args = []
    if outdoor_only:
        # Added here, with its arguments, rather than beside the other WHERE
        # terms above. `args` is filled in the order the placeholders appear in
        # the SQL, and appending a clause without its values in the same breath
        # is how `source_id` ends up bound to a placement -- which matches
        # nothing and returns no rows rather than raising. The docstring warns
        # about this, and the first attempt here did it anyway.
        #
        # Enumerated from OUTDOOR_PLACEMENTS rather than written as
        # `!= 'indoor'`, so a placement added later is excluded until somebody
        # decides it belongs. That is the safe direction for anything feeding a
        # forecast.
        where.append(
            "r.source_id IN (SELECT id FROM sources WHERE "
            "LOWER(COALESCE(placement, 'unknown')) IN ("
            + ", ".join("?" for _ in OUTDOOR_PLACEMENTS) + "))")
        args.extend(OUTDOOR_PLACEMENTS)
    if not include_extreme:
        where.append("r.quality = 'ok'")
    if source_id is not None:
        where.append("r.source_id = ?")
    if since is not None:
        where.append("r.observed_utc >= ?")

    sql = f"""
        WITH hourly AS (
            SELECT source_id,
                   substr(observed_utc, 1, 13) || ':00:00+00:00' AS hr,
                   AVG(pm25) AS pm25,
                   MAX(pm25) AS pm25_max,
                   COUNT(*)  AS samples
              FROM readings r
             WHERE {' AND '.join(where)}
             GROUP BY source_id, hr
        )
        SELECT h.source_id, h.hr AS observed_utc, h.pm25, h.pm25_max,
               h.samples, w.wind_speed_ms, w.wind_dir_deg,
               w.temperature_c, w.humidity_pct, w.pressure_hpa
          FROM hourly h
          JOIN weather w ON w.observed_utc = h.hr AND w.place = ?
         ORDER BY h.hr
    """
    if source_id is not None:
        args.append(source_id)
    if since is not None:
        args.append(_iso(since))
    args.append(place)
    return [dict(r) for r in conn.execute(sql, args)]


def weather_span(conn, place=None):
    """What weather is held: how many hours, and from when to when."""
    where, args = ("WHERE place = ?", (place,)) if place else ("", ())
    row = conn.execute(f"""
        SELECT COUNT(*) AS hours, MIN(observed_utc) AS first,
               MAX(observed_utc) AS last
          FROM weather {where}
    """, args).fetchone()
    return dict(row)


def weather_gaps(conn, place, since, until=None):
    """Hours in the window with no weather stored, oldest first.

    Returned rather than counted so a caller can fetch exactly what is missing.
    Weather is fetched in date ranges, so knowing the hours lets the caller
    collapse them into the fewest requests rather than asking for a month to
    fill an afternoon.
    """
    until = until or datetime.now(timezone.utc)
    if isinstance(since, datetime):
        since = since.astimezone(timezone.utc)
    if isinstance(until, datetime):
        until = until.astimezone(timezone.utc)

    have = {r["observed_utc"] for r in conn.execute(
        "SELECT observed_utc FROM weather WHERE place = ? "
        "AND observed_utc >= ? AND observed_utc <= ?",
        (place, since.isoformat(timespec="seconds"),
         until.isoformat(timespec="seconds")))}

    missing = []
    cursor = since.replace(minute=0, second=0, microsecond=0)
    while cursor <= until:
        stamp = cursor.isoformat(timespec="seconds")
        if stamp not in have:
            missing.append(stamp)
        cursor += timedelta(hours=1)
    return missing


# ----------------------------------------------------------------- integrity

# Every column holding a PM2.5 concentration. A sentinel can land in any of
# them: Queensland's -9999 arrives in the live channels, and a PurpleAir
# channel fault shows up in pm25_a/pm25_b, which feed the disagreement check.
PM_COLUMNS = ("pm25", "pm25_now", "pm25_30min", "pm25_60min", "pm25_6hr",
              "pm25_24hr", "pm25_1week", "pm25_a", "pm25_b")


def find_sentinels(conn):
    """Locate stored values that cannot be concentrations.

    Ingest rejects these now, but a database written by an earlier version
    still holds whatever the feed sent. Returns a list of dicts, one per
    affected source, with the column counts and the span to re-fetch.
    """
    where = " OR ".join(f"{c} < 0" for c in PM_COLUMNS)
    rows = conn.execute(
        f"""SELECT r.source_id, s.provider, s.site_id, s.site_name,
                   COUNT(*) AS n,
                   MIN(r.observed_utc) AS first_utc,
                   MAX(r.observed_utc) AS last_utc
              FROM readings r JOIN sources s ON s.id = r.source_id
             WHERE {where}
          GROUP BY r.source_id""").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["columns"] = {}
        for col in PM_COLUMNS:
            n = conn.execute(
                f"SELECT COUNT(*) AS n FROM readings "
                f"WHERE source_id = ? AND {col} < 0",
                (r["source_id"],)).fetchone()["n"]
            if n:
                item["columns"][col] = n
        out.append(item)
    return out


def repair_sentinels(conn, dry_run=False):
    """Replace stored sentinels with NULL. Returns what was (or would be) changed.

    NULL rather than a guess: the correct value is unknown, and inventing one
    would be worse than the sentinel. The row itself is kept -- deleting it
    would erase the fact that we asked at that time and the station answered,
    and would make the gap detector re-fetch a window the provider has already
    said it cannot fill.

    Rule 5a is not violated: a sentinel is the absence of a reading, not a
    reading. What is discarded is a non-measurement, and it is reported.
    """
    found = find_sentinels(conn)
    if dry_run or not found:
        return found
    for col in PM_COLUMNS:
        conn.execute(f"UPDATE readings SET {col} = NULL WHERE {col} < 0")
    # quality was derived from the sentinel, so a row cleared above may be
    # carrying a verdict computed from a number that was never a measurement.
    # The column is NOT NULL, and 'ok' is its documented default for "nothing
    # to flag" -- which is the truth once there is no value to judge.
    conn.execute("UPDATE readings SET quality = 'ok' "
                 "WHERE pm25 IS NULL AND quality <> 'ok'")
    conn.commit()
    return found


def verify(conn):
    """Check the database is intact and internally sensible.

    SQLite corruption is rare but silent: a truncated write or a bad disk
    surfaces as missing rows, not an error. Since this data cannot be
    regenerated, it is worth asking rather than assuming.

    Returns a list of problems; empty means healthy.
    """
    problems = []

    for row in conn.execute("PRAGMA integrity_check"):
        result = row[0] if not isinstance(row, sqlite3.Row) else row[0]
        if str(result).lower() != "ok":
            problems.append(f"integrity_check: {result}")

    for row in conn.execute("PRAGMA foreign_key_check"):
        problems.append(f"orphaned row referencing a missing source: {tuple(row)}")

    orphans = conn.execute("""
        SELECT COUNT(*) AS n FROM readings r
        WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.id = r.source_id)
    """).fetchone()["n"]
    if orphans:
        problems.append(f"{orphans:,} readings belong to no known source")

    # A reading in the future is a clock or parsing fault, and would poison
    # every average it lands in.
    ahead = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE observed_utc > ?",
        ((datetime.now(timezone.utc) + timedelta(hours=6))
         .isoformat(timespec="seconds"),)).fetchone()["n"]
    if ahead:
        problems.append(f"{ahead:,} readings are dated in the future")

    negative = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE pm25 < 0").fetchone()["n"]
    if negative:
        problems.append(f"{negative:,} readings have a negative PM2.5 value")

    return problems


# ----------------------------------------------------------------- retention

def db_size_bytes(db_path):
    """On-disk size, including the write-ahead log."""
    db_path = Path(db_path)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def prune(conn, keep_days, dry_run=False):
    """Delete readings older than keep_days.

    Returns (removed, kept, oldest_kept).

    Deliberately never called unless a finite retention has been configured.
    The default is to keep everything: this is a record of what someone
    breathed, it cannot be regenerated, and a tool that quietly discards it
    because a disk looked full would be worse than useless. Storage is roughly
    a megabyte a year per source.
    """
    if not keep_days or keep_days <= 0:
        return 0, None, None

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=int(keep_days))).isoformat(timespec="seconds")

    doomed = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE observed_utc < ?",
        (cutoff,)).fetchone()["n"]

    if not dry_run and doomed:
        conn.execute("DELETE FROM readings WHERE observed_utc < ?", (cutoff,))
        conn.commit()
        # Reclaim the space rather than leaving the file the same size, which
        # would make the whole exercise pointless to anyone doing it for disk.
        conn.execute("VACUUM")

    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(observed_utc) AS oldest FROM readings"
    ).fetchone()
    return doomed, row["n"], row["oldest"]


# -------------------------------------------------------------------- export

def export_csv(conn, out_dir, source_id=None, terms_by_provider=None):
    """Write one CSV per source.

    This is the preservation guarantee that lets the operational store be
    SQLite: the archival copy stays plain text, greppable and diffable, and
    CI round-trip tests it. Written per source so each file keeps a single
    provenance rather than silently interleaving instruments.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    cols = ["observed_utc", "fetched_utc", "kind", "pm25", "pm25_now",
            "pm25_30min", "pm25_60min", "pm25_6hr", "pm25_24hr", "pm25_1week",
            "humidity", "temperature", "temperature_unit", "quality"]

    for src in list_sources(conn, enabled_only=False):
        if source_id is not None and src["id"] != source_id:
            continue
        path = out_dir / f"{src['provider']}-{src['site_id']}.csv"
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM readings WHERE source_id = ? "
            f"ORDER BY observed_utc", (src["id"],)).fetchall()
        with path.open("w", newline="", encoding="utf-8") as f:
            # An export is the copy that leaves the machine -- emailed,
            # uploaded, attached to an issue. It has to carry its own terms,
            # because whoever opens it will not have read our README, and
            # some of these licences forbid redistribution outright.
            for line in _export_header(src, terms_by_provider):
                f.write(f"# {line}\n")
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
        written.append((path, len(rows)))
    return written


# Kept here rather than imported from poller to avoid a circular import; the
# authority is Provider.licence / .attribution, and the test suite asserts
# these stay in step.
_SOURCE_TERMS = {
    "purpleair": ("Powered by PurpleAir",
                  "PurpleAir Terms of Service. DO NOT REDISTRIBUTE: ToS S4.3 "
                  "prohibits making PurpleAir data available to third parties."),
    "qld": ("Contains Queensland Government data, CC BY 4.0",
            "CC BY 4.0 — redistribution permitted with attribution."),
    "nsw": ("Contains NSW Government data, CC BY 4.0",
            "CC BY 4.0 — redistribution permitted with attribution."),
    "openaq": ("Data via OpenAQ",
               "Licence varies BY SOURCE STATION. Check the OpenAQ Licenses "
               "resource before redistributing."),
}


EXPORT_COMMENT = "#"


def read_export(path):
    """Read an exported CSV back, skipping the licence header.

    The header has to travel with the file -- a sidecar gets separated from
    the data the first time someone emails it, and some of these licences
    forbid redistribution outright. `#` comments are the conventional way to
    carry CSV metadata and every serious reader can skip them:

        pandas.read_csv(path, comment="#")
        read.csv(path, comment.char="#")            # R
        numpy.genfromtxt(path, comments="#")

    This helper is the same thing for anyone using the standard library.
    """
    with Path(path).open(newline="", encoding="utf-8") as f:
        rows = [line for line in f if not line.startswith(EXPORT_COMMENT)]
    return list(csv.DictReader(rows))


def _export_header(src, terms_by_provider=None):
    """The licence block that travels inside every exported CSV.

    `terms_by_provider` lets the caller supply {slug: (attribution, terms)}
    from the live provider registry. Without it this fell back to "" for any
    provider not in the table below -- so a network added tomorrow exported
    with **no attribution at all**, which for a CC BY feed omits the one thing
    the licence actually requires. The table stays because it carries the
    redistribution wording, which is longer and more specific than a
    provider's one-line attribution; the registry is what stops a gap in it
    being silent.
    """
    provider = str(src.get("provider") or "")
    supplied = (terms_by_provider or {}).get(provider)
    attribution, terms = _SOURCE_TERMS.get(
        provider,
        supplied or ("", "Licence unknown — check before redistributing."))
    return [
        f"Airo export — {provider}/{src.get('site_id')} "
        f"({src.get('site_name') or 'unnamed'})",
        f"Exported {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        attribution,
        f"TERMS: {terms}",
        "PM2.5 in ug/m3. Consumer sensors over-read at high humidity.",
        "Not medical advice. Not a calibrated instrument.",
        "This file may reveal a location and when someone is home.",
    ]


# ------------------------------------------------------------------ migration

def migrate_from_csv(conn, csv_path, provider, site_id, site_name=None,
                     latitude=None, longitude=None, resolution_minutes=10):
    """Import a pre-v0.4 single-source readings.csv.

    The old file stored one source's readings with the fetch time in 'utc'.
    That is the closest thing it has to an observation time, so it becomes
    observed_utc; there is no separate fetch time to recover.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return 0, 0

    sid = upsert_source(conn, provider, site_id, site_name, latitude,
                        longitude, resolution_minutes)

    rows, skipped = [], 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            utc = (rec.get("utc") or "").strip()
            pm = _f(rec.get("pm25_10min"))
            if not utc:
                skipped += 1
                continue
            rows.append({
                "observed_utc": utc,
                "fetched_utc": utc,
                "kind": rec.get("source") or "live",
                "pm25": pm,
                "pm25_now": _f(rec.get("pm25_now")),
                "pm25_30min": _f(rec.get("pm25_30min")),
                "pm25_60min": _f(rec.get("pm25_60min")),
                "pm25_6hr": _f(rec.get("pm25_6hr")),
                "pm25_24hr": _f(rec.get("pm25_24hr")),
                "pm25_1week": _f(rec.get("pm25_1week")),
                "humidity": _f(rec.get("humidity")),
                "temperature": _f(rec.get("temperature")),
                # Asked of the provider rather than decided here. This read
                # `"F" if provider == "purpleair" else "C"`, which is the
                # check-written-as-a-list shape: a second Fahrenheit network
                # would have been imported as Celsius and nothing would say so.
                "temperature_unit": _reported_unit(provider),
            })

    added = insert_readings(conn, sid, rows)
    return added, skipped


def _f(v):
    """A CSV field as a float, or None. Never 0.0 for a blank -- a
    measurement nobody took must not be indistinguishable from one they did.

    Character-for-character `poller.fnum()`, and deliberately not imported
    from it: this module imports nothing of Airo's, which is what lets poller
    import *it* (see the note above `_SOURCE_TERMS` -- the same constraint
    keeps the licence strings duplicated here). Collapsing the two would mean
    store importing poller, and poller already imports store. If one changes,
    change both.
    """
    try:
        # Stating the two blank forms rather than letting float() raise on
        # them. Behaviourally the same as falling through to the except below,
        # so a mutation sweep reports this as an untested guard forever; it is
        # untestable rather than untested. Explicit because "" and None are
        # the expected content of an empty column, not an error.
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
