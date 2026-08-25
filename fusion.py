#!/usr/bin/env python3
"""
Airo fusion — deciding what one number to show when several sources disagree.

This is the most safety-relevant code in the project. Sources differ in
instrument, calibration, distance from the user and update latency, so any
rule for combining them is a judgement call with consequences: someone may
open or close a window because of the number this returns.

Three principles hold across every rule:

  1. Never invent a measurement. Only 'blend' computes a value no instrument
     reported, and it is opt-in and labelled as such for that reason.
  2. Always report provenance. Every result names the source it came from and
     how old it is, so a user can tell "12 from the sensor in my street two
     minutes ago" from "12 from a monitor 8 km away an hour ago".
  3. Stale data is not current data. A source that has gone quiet past its
     own reporting interval is skipped rather than presented as now.

Standard library only.
"""

import math
from datetime import datetime, timezone

# Rules the user may pick in config.json. 'nearest' is the default: the whole
# point of the tool is *local* air, and the nearest instrument is usually the
# best answer to "what am I breathing".
RULES = ("nearest", "freshest", "all", "blend")
DEFAULT_RULE = "nearest"

# How far past its own reporting interval a source may drift before we stop
# treating it as current. Two intervals plus a grace minute tolerates one
# missed report without declaring an outage.
STALE_INTERVALS = 2
STALE_GRACE_MINUTES = 5


def _aware(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_minutes(observed_utc, now=None):
    """How long ago the air was measured, in minutes. None if unknown.

    Measured from `observed_utc`, never from when we fetched it -- a
    regulatory feed can hand over an hour-old observation the instant it is
    asked, and treating the fetch as the age would present it as current.

    Clamped at zero rather than allowed to go negative: a provider whose clock
    runs ahead of ours would otherwise produce a reading that is fresher than
    now, and every staleness comparison downstream would read as "very fresh"
    for precisely the source that cannot be trusted about time.
    """
    dt = _aware(observed_utc)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 60.0)


def is_stale(reading, now=None):
    """Has this source gone quiet past its own reporting cadence?

    Judged against the source's own interval, not a fixed number: 40 minutes
    of silence is an outage for a 10-minute consumer sensor and completely
    normal for an hourly regulatory feed.
    """
    age = age_minutes(reading.get("observed_utc"), now)
    if age is None:
        return True
    interval = float(reading.get("resolution_minutes") or 10)
    return age > interval * STALE_INTERVALS + STALE_GRACE_MINUTES


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. None if any coordinate is missing."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def annotate(readings, location=None, now=None):
    """Attach age, staleness and distance to each source's latest reading."""
    now = now or datetime.now(timezone.utc)
    lat = (location or {}).get("latitude")
    lon = (location or {}).get("longitude")

    out = []
    for r in readings:
        d = dict(r)
        d["age_minutes"] = age_minutes(r.get("observed_utc"), now)
        d["stale"] = is_stale(r, now)
        d["distance_km"] = haversine_km(lat, lon,
                                        r.get("latitude"), r.get("longitude"))
        out.append(d)
    return out


def _usable(readings):
    """Fresh readings with an actual value, excluding flagged faults."""
    return [r for r in readings
            if r.get("pm25") is not None
            and not r.get("stale")
            and r.get("quality") != "suspect"]


# ------------------------------------------------------------- corroboration
#
# A single sensor reading far above every neighbour is one of three things:
# a genuine very local source (a wood heater next door), an instrument fault,
# or a real regional event that has not reached the other sensors yet. They
# look identical in isolation, and the difference matters — one is worth
# closing a window for, one is worth cleaning a sensor for.
#
# Two pieces of evidence separate them:
#   1. Do neighbouring sources agree?
#   2. Does *this* source habitually read this much higher at this hour?
#
# The second is what stops a valley sensor being permanently accused of
# lying. A sensor that always reads 3x its neighbours after sunset is
# measuring a real drainage effect; the same sensor reading 11x for the first
# time in ninety days is not.
#
# Crucially, an uncorroborated reading is *flagged, never discarded*. If there
# genuinely is a fire next door, that is the air the user is breathing, and
# suppressing it would be the more dangerous error.

# How far above the peer level counts as needing corroboration at all.
UNCORROBORATED_RATIO = 3.0
# Ignore ratios when everything is low: 2 vs 0.5 ug/m3 is noise, not an event.
UNCORROBORATED_FLOOR_UGM3 = 12.0
# How much above its own historical p90 a source must run to be called unusual.
HISTORY_TOLERANCE = 1.3
# Below this many historical comparisons, don't claim to know what is typical.
MIN_HISTORY_SAMPLES = 20


def _median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def corroborate(readings, history=None, now=None):
    """Annotate each reading with whether its peers support it.

    `history` maps source_id -> the dict returned by
    store.peer_ratio_history(). Optional: without it the check still works,
    it just cannot tell "unusual for this sensor" from "normal for this
    sensor", and says so rather than guessing.

    Adds to each reading:
      peer_pm25      the median of the other usable sources, or None
      peer_ratio     this reading divided by that, or None
      corroboration  'corroborated' | 'typical_for_site' | 'uncorroborated'
                     | 'single_source' | 'unknown'
      corroboration_note  human-readable reason
    """
    history = history or {}
    out = [dict(r) for r in readings]
    for r in out:
        if "stale" not in r:
            r["stale"] = is_stale(r, now)
    usable = _usable(out)

    for r in out:
        r.setdefault("peer_pm25", None)
        r.setdefault("peer_ratio", None)
        r["corroboration"] = "unknown"
        r["corroboration_note"] = None

        if r.get("pm25") is None or r.get("stale") or r.get("quality") == "suspect":
            continue

        # Identity first. Only compare source_id when both actually have one --
        # `None != None` is False, which previously excluded every peer and
        # made a two-source setup look like a single source.
        def _other(p):
            if p is r:
                return False
            pid, rid = p.get("source_id"), r.get("source_id")
            if pid is not None and rid is not None:
                return pid != rid
            return True

        peers = [p["pm25"] for p in usable if _other(p)]
        if not peers:
            r["corroboration"] = "single_source"
            r["corroboration_note"] = (
                "only one source — nothing to cross-check against")
            continue

        peer_level = _median(peers)
        r["peer_pm25"] = peer_level

        # Everything is low: ratios here are noise amplification, not signal.
        if r["pm25"] < UNCORROBORATED_FLOOR_UGM3:
            r["corroboration"] = "corroborated"
            r["corroboration_note"] = "in line with nearby sources"
            if peer_level:
                r["peer_ratio"] = round(r["pm25"] / peer_level, 2)
            continue

        if not peer_level or peer_level <= 0:
            r["corroboration"] = "unknown"
            r["corroboration_note"] = "nearby sources reported no usable value"
            continue

        ratio = r["pm25"] / peer_level
        r["peer_ratio"] = round(ratio, 2)

        if ratio <= UNCORROBORATED_RATIO:
            r["corroboration"] = "corroborated"
            r["corroboration_note"] = "in line with nearby sources"
            continue

        # Well above the neighbours. Is that normal for this particular site?
        h = history.get(r.get("source_id")) or {}
        n, p90 = h.get("n") or 0, h.get("p90")

        if n >= MIN_HISTORY_SAMPLES and p90:
            if ratio <= p90 * HISTORY_TOLERANCE:
                r["corroboration"] = "typical_for_site"
                basis = h.get("basis") or "its history"
                r["corroboration_note"] = (
                    f"{ratio:.1f}x nearby sources, but this site normally runs "
                    f"up to {p90:.1f}x ({basis}) — consistent with its own "
                    f"history, so probably a real local effect")
                continue
            r["corroboration"] = "uncorroborated"
            basis = h.get("basis") or "its history"
            r["corroboration_note"] = (
                f"{ratio:.1f}x nearby sources, well above this site's usual "
                f"{p90:.1f}x ({basis}, n={n}) — likely a very local source "
                f"such as a fire nearby, or a sensor fault")
        else:
            r["corroboration"] = "uncorroborated"
            r["corroboration_note"] = (
                f"{ratio:.1f}x nearby sources, and there is not enough history "
                f"to say whether that is normal here")

    return out


def fuse(readings, rule=DEFAULT_RULE, location=None, now=None, history=None):
    """Pick the headline reading.

    Returns a dict with the chosen pm25, the rule used, provenance, and every
    contributing source so the UI can show the working. Returns None for
    'pm25' when nothing usable is available -- an honest "no current data"
    rather than the last known value dressed up as current.
    """
    rule = rule if rule in RULES else DEFAULT_RULE
    annotated = corroborate(annotate(readings, location, now), history, now)
    usable = _usable(annotated)

    result = {
        "rule": rule,
        "pm25": None,
        "source": None,
        "sources": annotated,
        "contributing": [],
        "degraded": False,
        "note": None,
        # True when the headline is not supported by neighbouring sources.
        # Deliberately not a reason to hide the reading -- if there is a fire
        # next door that is genuinely the air being breathed -- but the UI
        # must say so plainly rather than present it as settled fact.
        "uncorroborated": False,
        "corroboration_note": None,
    }

    if not usable:
        stale_with_value = [r for r in annotated if r.get("pm25") is not None]
        result["note"] = (
            "no source has reported recently" if stale_with_value
            else "no data from any configured source")
        # Surface the most recent stale reading so the UI can show it greyed
        # out with its age, rather than an empty panel.
        if stale_with_value:
            stale_with_value.sort(key=lambda r: r.get("age_minutes") or 1e9)
            result["last_known"] = stale_with_value[0]
        return result

    if rule == "nearest":
        # Sources with known coordinates rank by distance; any without fall
        # back to recency, so a source missing coordinates is still usable
        # rather than silently dropped.
        located = [r for r in usable if r.get("distance_km") is not None]
        if located:
            located.sort(key=lambda r: r["distance_km"])
            chosen = located[0]
            # A source with no position can never win this rule, however close
            # it actually is. Silently ranking it behind everything means the
            # headline can come from a farther instrument with no indication
            # why -- and on the reference install two sources differ 4x.
            adrift = [r for r in usable if r.get("distance_km") is None]
            if adrift:
                names = ", ".join(
                    str(r.get("site_name") or r.get("provider")) for r in adrift)
                result["note"] = (
                    f"{names}: no coordinates stored, so not considered for "
                    f"'nearest'. Re-run setup, or add latitude/longitude to "
                    f"the source in your config.")
        else:
            usable.sort(key=lambda r: r.get("age_minutes") or 1e9)
            chosen = usable[0]
            result["note"] = "no source has coordinates; fell back to freshest"
        result["pm25"] = chosen["pm25"]
        result["source"] = chosen
        result["contributing"] = [chosen]
        result["degraded"] = len(usable) < len([r for r in annotated
                                                if r.get("pm25") is not None])

    elif rule == "freshest":
        usable.sort(key=lambda r: r.get("age_minutes") or 1e9)
        chosen = usable[0]
        result["pm25"] = chosen["pm25"]
        result["source"] = chosen
        result["contributing"] = [chosen]

    elif rule == "all":
        # No single headline is chosen by preference, but a tray icon needs
        # one number, so the nearest usable source is reported as the
        # representative while every source is returned for display.
        rep = sorted(usable,
                     key=lambda r: (r.get("distance_km") is None,
                                    r.get("distance_km") or 0,
                                    r.get("age_minutes") or 0))[0]
        result["pm25"] = rep["pm25"]
        result["source"] = rep
        result["contributing"] = usable
        result["note"] = "showing all sources; headline is the nearest usable one"

    elif rule == "blend":
        # Inverse-distance and recency weighted. This produces a number no
        # instrument measured, which is why it is opt-in and labelled.
        total_w, acc = 0.0, 0.0
        for r in usable:
            d = r.get("distance_km")
            w_dist = 1.0 / (1.0 + d) if d is not None else 0.5
            age = r.get("age_minutes") or 0.0
            interval = float(r.get("resolution_minutes") or 10)
            w_age = 1.0 / (1.0 + age / max(interval, 1.0))
            w = w_dist * w_age
            acc += r["pm25"] * w
            total_w += w
        if total_w > 0:
            result["pm25"] = round(acc / total_w, 2)
        result["contributing"] = usable
        result["source"] = None
        result["note"] = ("weighted blend of %d sources — a computed value, "
                          "not a single instrument reading" % len(usable))

    chosen = result.get("source")
    if chosen:
        result["uncorroborated"] = chosen.get("corroboration") == "uncorroborated"
        result["corroboration_note"] = chosen.get("corroboration_note")
    elif result["rule"] == "blend" and result.get("contributing"):
        flagged = [c for c in result["contributing"]
                   if c.get("corroboration") == "uncorroborated"]
        result["uncorroborated"] = bool(flagged)
        if flagged:
            result["corroboration_note"] = (
                f"{len(flagged)} of {len(result['contributing'])} blended "
                f"sources are not corroborated by their neighbours")

    return result


def describe(result):
    """One-line provenance string for the tray and menu bar."""
    if result.get("pm25") is None:
        return result.get("note") or "no data"

    if result["rule"] == "blend":
        return "blend of %d sources" % len(result.get("contributing") or [])

    src = result.get("source") or {}
    name = src.get("site_name") or src.get("site_id") or src.get("provider")
    age = src.get("age_minutes")
    dist = src.get("distance_km")

    bits = [str(name)]
    if dist is not None:
        bits.append("%.1f km" % dist if dist >= 0.1 else "here")
    if age is not None:
        bits.append("%d min ago" % round(age) if age >= 1 else "just now")
    return " · ".join(bits)
