#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Airo analysis — reproduce the evening-premium finding, and tune corroboration.

Two jobs:

  evening    The analysis that motivated the project: is air quality
             systematically worse after sunset than during the day, and on
             which nights? Reproduces what the dashboard shows, from the
             command line, so a claim can be checked rather than trusted.

  correlate  Does the weather explain the readings? Mean PM2.5 by wind band,
             the correlations with wind, temperature and humidity, and which
             way the wind was blowing on the worst hours. ROADMAP #9 Phase B,
             and it can answer "no clear signature" — a tool that can only
             confirm its own premise is not checking anything.

  agreement  How your sources actually compare over time. The corroboration
             thresholds in fusion.py (3x peers, 1.3x historical p90) are
             defaults, not measurements. This prints the real distribution so
             you can set them from your own data instead of my guess.

Standard library only, as ever.

    python3 analyse.py evening --nights 30
    python3 analyse.py agreement
    python3 analyse.py agreement --by-hour
    python3 analyse.py correlate --nights 90
"""

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fusion  # noqa: E402
import poller  # noqa: E402
import store   # noqa: E402

# The evening window the project is built around -- cold-air drainage traps
# particulates after sunset and releases them after midnight -- used to sit
# here as EVENING_START_HOUR = 15 / EVENING_END_HOUR = 1.
#
# It is a *configured* setting, `risk_window` in config.json, because the
# phenomenon is local: somewhere flat and coastal has no such window. Holding
# the literals here meant `analyse.py evening` reported on 3pm-1am whatever
# the user had set, while the menu bar and the alerts used their real window.
# Two answers to one configured question from one install. `poller.risk_window()`
# is now the only reader, and its defaults are the only copy of 15 and 1.


def _local(iso, tz=None):
    """A stored UTC timestamp as the user's wall clock.

    `tz` is the configured zone, or None for this machine's. It is an argument
    rather than a lookup because the answer depends on it: reading the record
    on a server in another zone silently re-buckets every night, and the whole
    report is about which hours a reading fell in.
    """
    try:
        return datetime.fromisoformat(iso).astimezone(tz)
    except (ValueError, TypeError):
        return None


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


# ------------------------------------------------------------------ evening

def evening(conn, cfg, nights):
    """Print the evening-premium analysis: is it worse after sunset, and when?

    This is the finding the project exists to check, so it is reproduced here
    from the command line rather than left as something the dashboard asserts.
    The same bucketing runs in dashboard.html; two implementations of one
    calculation is a real cost, accepted because a claim nobody can check
    independently of the tool making it is not worth much.

    Per source, never pooled. Two instruments a few kilometres apart can have
    genuinely different evenings, and averaging them would hide exactly the
    effect being looked for.
    """
    scale_name, scale = poller.get_scale(cfg)
    tz = poller.local_zone(cfg)
    # The user's window, not this module's idea of one. Same call the served
    # payload and the time-of-day hint make, so the report, the dashboard and
    # the menu bar cannot describe different evenings.
    window = poller.risk_window(cfg)
    start_hour, end_hour = window["start_hour"], window["end_hour"]
    wraps = start_hour > end_hour

    def in_window(hour):
        return (hour >= start_hour or hour < end_hour) if wraps \
            else (start_hour <= hour < end_hour)

    sources = {s["id"]: s for s in store.list_sources(conn, enabled_only=False)}

    since = datetime.now(timezone.utc) - timedelta(days=nights + 1)
    rows = store.series(conn, since=since)
    if not rows:
        print("No readings in that window.")
        return

    # Bucket by night. A reading at 00:30 belongs to the previous evening --
    # using the calendar date directly would split every episode in half.
    per_source = defaultdict(lambda: defaultdict(lambda: {"eve": [], "day": []}))
    for r in rows:
        dt = _local(r["observed_utc"], tz)
        if dt is None:
            continue
        h = dt.hour
        night = dt.date()
        # A window that wraps midnight is the normal case (15 to 1), but it is
        # not the only legal one -- a user somewhere with a morning inversion
        # can set 5 to 9, and then nothing rolls over and "the night before"
        # is meaningless. Both shapes, one rule, matching poller's inside().
        if wraps and h < end_hour:
            night = (dt - timedelta(days=1)).date()
        bucket = "eve" if in_window(h) else "day"
        per_source[r["source_id"]][night][bucket].append(r["pm25"])
        per_source[r["source_id"]][night].setdefault(bucket + "_h", set()).add(h)
        # Extreme air counts, and says that it did. These readings used to be
        # filtered out before they arrived -- so the nights this tool exists
        # to find were the nights it had least to say about. Counted now,
        # marked so nobody reads a 12x ratio as an ordinary Tuesday.
        if r["quality"] == "extreme":
            per_source[r["source_id"]][night]["extreme"] = \
                per_source[r["source_id"]][night].get("extreme", 0) + 1

    # A partly-logged day produces a meaningless ratio, so require real cover
    # in both buckets before reporting one -- measured in HOURS, not samples.
    # A sample count means different things per provider: 6 samples is one
    # hour of PurpleAir but six hours of an hourly government feed. The
    # dashboard had the same rule set to 12, which no hourly source could ever
    # satisfy because the evening window is only 10 hours long.
    MIN_HOURS = 3

    for sid, nights_map in per_source.items():
        src = sources.get(sid, {})
        label = f"{src.get('provider')}/{src.get('site_id')} {src.get('site_name') or ''}"
        print(f"\n{label.strip()}")
        print(f"  {'night':<12} {'day':>8} {'evening':>9} {'ratio':>7} {'peak':>8}")
        print("  " + "-" * 48)

        ratios = []
        for night in sorted(nights_map):
            v = nights_map[night]
            if (len(v.get("eve_h", ())) < MIN_HOURS
                    or len(v.get("day_h", ())) < MIN_HOURS):
                continue
            d, e = _mean(v["day"]), _mean(v["eve"])
            if not d:
                continue
            ratio = e / d
            ratios.append(ratio)
            flag = "  <-- trapping night" if ratio >= 1.5 else ""
            n_extreme = v.get("extreme", 0)
            if n_extreme:
                flag += f"  [{n_extreme} extreme]"
            print(f"  {night!s:<12} {d:8.1f} {e:9.1f} {ratio:6.2f}x "
                  f"{max(v['eve']):7.1f}{flag}")

        if ratios:
            ratios.sort()
            print(f"\n  {len(ratios)} complete nights")
            print(f"  median evening premium : {_pct(ratios, 0.5):.2f}x")
            print(f"  worst                  : {ratios[-1]:.2f}x")
            print(f"  nights above 1.5x      : "
                  f"{sum(1 for r in ratios if r >= 1.5)} of {len(ratios)}")
            # Name the hours. Without them a ratio is uninterpretable, and a
            # reader who has moved their window has no way to tell whether
            # this report honoured it.
            print(f"\n  Evening window {start_hour:02d}:00-{end_hour:02d}:00; "
                  f"reported in {scale['label']}; stored as raw ug/m3.")
        else:
            print("  Not enough complete nights yet to report a ratio.")


# ---------------------------------------------------------------- agreement

def agreement(conn, cfg, by_hour):
    """How the sources compare, so corroboration can be tuned from real data."""
    tz = poller.local_zone(cfg)
    sources = {s["id"]: s for s in store.list_sources(conn, enabled_only=False)}
    if len(sources) < 2:
        print("Only one source configured — nothing to compare.")
        print("Add a second source to config.json; a nearby government monitor")
        print("is the most useful cross-check for a consumer sensor.")
        return

    print("Corroboration thresholds currently in force (fusion.py):")
    print(f"  flag above            {fusion.UNCORROBORATED_RATIO}x the median of peers")
    print(f"  ignore below          {fusion.UNCORROBORATED_FLOOR_UGM3} ug/m3")
    print(f"  historical tolerance  {fusion.HISTORY_TOLERANCE}x that site's p90")
    print(f"  minimum samples       {fusion.MIN_HISTORY_SAMPLES}")
    print("\nThese are defaults, not measurements. What your data actually says:\n")

    for sid, src in sources.items():
        label = f"{src['provider']}/{src['site_id']}"
        overall = store.peer_ratio_history(conn, sid, days=3650)
        if not overall["n"]:
            print(f"{label:<28} no overlapping readings with other sources yet")
            continue

        print(f"{label:<28} n={overall['n']:<6} median={overall['median']:.2f}x  "
              f"p90={overall['p90']:.2f}x  max={overall['max']:.2f}x")

        # How often would the current threshold fire?
        would_flag = overall["p90"] > fusion.UNCORROBORATED_RATIO
        if would_flag:
            print(f"{'':28} {poller.WARN} this site exceeds "
                  f"{fusion.UNCORROBORATED_RATIO}x routinely — "
                  f"raise the threshold or it will cry wolf")
        elif overall["max"] > fusion.UNCORROBORATED_RATIO:
            print(f"{'':28} occasionally exceeds {fusion.UNCORROBORATED_RATIO}x "
                  f"(max {overall['max']:.1f}x) — flagging should be rare and meaningful")
        else:
            print(f"{'':28} never exceeds {fusion.UNCORROBORATED_RATIO}x — "
                  f"the threshold may be looser than it needs to be")

        if by_hour:
            print(f"{'':28} by hour of day (local):")
            for local_hour in range(24):
                # Local hours, asked for as local hours. This used to select
                # UTC hours and relabel them using the offset in force on
                # 1 January, which is wrong for half the year anywhere that
                # observes daylight saving -- and the whole point of this
                # breakdown is which hour of the evening is worst.
                h = store.peer_ratio_history(conn, sid, hour_of_day=local_hour,
                                             days=3650, hour_is_local=True,
                                             tz=tz)
                if not h["n"]:
                    continue
                bar = "#" * min(40, int((h["median"] or 0) * 8))
                print(f"{'':30} {local_hour:02d}:00  n={h['n']:<4} "
                      f"median={h['median']:.2f}x  p90={h['p90']:.2f}x  {bar}")
        print()

    print("If a site's p90 is consistently above the flag threshold, it is measuring")
    print("something real about its location, not malfunctioning. Raise")
    print("UNCORROBORATED_RATIO, or rely on the per-site history check which already")
    print("accounts for it.")


# ------------------------------------------------------------------ phase B
#
# ROADMAP #9 Phase B. The project's premise is that calm, cold nights trap
# particulates in a valley. Phase A put the weather beside the readings; this
# is what checks the premise rather than assuming it, and it has to be able to
# answer no.
#
# Everything below is a statement about the past. Under Australian Consumer Law
# s4 a representation about a *future* matter puts the burden of reasonable
# grounds on whoever makes it -- describing what happened is not that, and
# forecast.py holds the guardrails for the part that would be.

#: The bands the original finding used. Changing them silently would make this
#: output incomparable with the analysis it exists to reproduce.
WIND_BANDS = ((0.0, 0.5, "calm"), (0.5, 1.0, "light"), (1.0, None, "breezy"))

#: Below this many paired hours, a correlation is noise with a decimal point.
#: Refused rather than reported: the person most likely to act on a spurious
#: r = -0.9 is the one who installed the tool yesterday.
MIN_PAIRED_HOURS = 72

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass(degrees):
    """A bearing as an eight-point compass name, or None.

    None in, None out: a provider that did not report a direction must not be
    counted as north, or the tool manufactures whichever finding 0 degrees
    supports. 0 and 360 are both north -- dropping either loses a whole
    direction, and the direction lost may be the one a sea breeze or a
    drainage flow arrives on.
    """
    if degrees is None:
        return None
    try:
        d = float(degrees) % 360.0
    except (TypeError, ValueError):
        return None
    return COMPASS[int((d + 22.5) // 45) % 8]


def pearson(xs, ys):
    """Correlation of two equal-length series, or None if it has no meaning.

    None rather than 0.0 when a series does not vary: a flat line has no
    correlation with anything, and reporting 0.0 would read as "measured, and
    unrelated" instead of "not measurable".
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / ((sxx ** 0.5) * (syy ** 0.5))


def _band_of(wind):
    for low, high, name in WIND_BANDS:
        if wind >= low and (high is None or wind < high):
            return name
    return None


def correlate(conn, cfg, nights=90):
    """Does the weather explain the readings? ROADMAP #9 Phase B.

    Per source, never pooled -- for the same reason `evening()` is: two
    instruments a few kilometres apart can sit on different ground, and the
    whole point of the siting note in the README is that elevation matters
    more than distance. Averaging them would hide exactly the effect being
    looked for.
    """
    location = cfg.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        print("No location configured, so there is no weather to line the "
              "readings up against.")
        return

    place = store.place_key(lat, lon)
    # No local-time conversion here, deliberately. This report is about the
    # weather that accompanied a reading, not about the hour it arrived in --
    # `evening` already answers the time-of-day question, and answering it
    # twice is how two surfaces start disagreeing. A `tz` was fetched here in
    # the first draft and used for nothing.
    since = datetime.now(timezone.utc) - timedelta(days=nights)
    sources = {s["id"]: s for s in store.list_sources(conn, enabled_only=False)}

    span = store.weather_span(conn, place)
    if not span.get("hours"):
        print("No weather stored for this location yet, so nothing can be")
        print("correlated. Fetch it with:")
        print("  python3 poller.py --backfill-weather")
        return

    rows = store.hourly_with_weather(conn, place, since=since)
    if not rows:
        print(f"No hour in the last {nights} days has both a reading and the")
        print("weather that went with it. Fetch the weather with:")
        print("  python3 poller.py --backfill-weather")
        return

    by_source = defaultdict(list)
    for r in rows:
        by_source[r["source_id"]].append(r)

    print(f"Weather against particulates — {len(rows)} paired hour(s) "
          f"over {nights} days\n")
    print("Correlation is not cause. These are the conditions that "
          "accompanied\nhigh readings in your own record, not a claim about "
          "why.\n")

    for sid, hours in sorted(by_source.items()):
        src = sources.get(sid, {})
        label = f"{src.get('provider')}/{src.get('site_id')} " \
                f"{src.get('site_name') or ''}"
        print(f"{label.strip()}")

        if len(hours) < MIN_PAIRED_HOURS:
            print(f"  Not enough paired hours to say anything honest: "
                  f"{len(hours)} of {MIN_PAIRED_HOURS} needed.")
            print(f"  A correlation over this little data is noise with a "
                  f"decimal point.\n")
            continue

        _wind_bands(hours, cfg)
        _correlations(hours)
        _worst_hours_by_direction(hours)
        _summarise(hours)
        print()


def _wind_bands(hours, cfg=None):
    """Mean PM2.5 by wind speed — the table that motivated the project.

    The band *edges* are m/s because that is what is stored and what the bands
    are defined in; the numbers shown beside them are converted, so a reader in
    the US sees the same bands described in mph rather than a table they have
    to convert in their head. The banding itself never moves — converting the
    threshold rather than the label would silently redefine "calm".
    """
    import units as _units
    unit = _units.resolve(cfg)
    speed = lambda v: _units.convert("wind", v, unit)[0]
    name_of = _units.label("wind", unit)

    print(f"\n  {'wind (' + name_of + ')':<16} {'hours':>7} {'mean PM2.5':>12}")
    print("  " + "-" * 37)
    for low, high, name in WIND_BANDS:
        picked = [h["pm25"] for h in hours
                  if h["wind_speed_ms"] is not None
                  and _band_of(h["wind_speed_ms"]) == name]
        window = (f"< {speed(high):.1f}" if low == 0 else
                  (f"> {speed(low):.1f}" if high is None else
                   f"{speed(low):.1f}–{speed(high):.1f}"))
        if picked:
            print(f"  {name + ' ' + window:<16} {len(picked):>7} "
                  f"{sum(picked) / len(picked):>11.1f}")
        else:
            # Shown empty rather than omitted: a missing row reads as "no
            # data for this band", an absent one as though the band does not
            # exist.
            print(f"  {name + ' ' + window:<16} {0:>7} {'—':>12}")


def _correlations(hours):
    print()
    for label, key in (("wind speed vs PM2.5", "wind_speed_ms"),
                       ("temperature vs PM2.5", "temperature_c"),
                       ("humidity vs PM2.5", "humidity_pct")):
        xs = [h[key] for h in hours]
        ys = [h["pm25"] for h in hours]
        n = sum(1 for x, y in zip(xs, ys) if x is not None and y is not None)
        r = pearson(xs, ys)
        if r is None:
            print(f"  {label:<26} not measurable (n={n})")
        else:
            print(f"  {label:<26} r = {r:+.2f}  (n={n})")


def _worst_hours_by_direction(hours):
    """Which way the wind was blowing on the worst hours on record."""
    ranked = sorted(hours, key=lambda h: h["pm25"], reverse=True)[:20]
    counts = Counter(c for c in (compass(h["wind_dir_deg"]) for h in ranked)
                     if c is not None)
    if not counts:
        return
    named = ", ".join(f"{d} {n}" for d, n in counts.most_common())
    print(f"\n  worst {len(ranked)} hours came on: {named}")


def _summarise(hours):
    """The premise, reached from the data rather than asserted.

    "Calm and cold, not calm and dry" is the finding this project started
    from. It is stated here only when this record actually shows it, and the
    opposite is stated just as plainly -- a tool that can only confirm its own
    premise is not checking anything.
    """
    wind = pearson([h["wind_speed_ms"] for h in hours],
                   [h["pm25"] for h in hours])
    temp = pearson([h["temperature_c"] for h in hours],
                   [h["pm25"] for h in hours])
    humid = pearson([h["humidity_pct"] for h in hours],
                    [h["pm25"] for h in hours])

    parts = []
    if wind is not None and wind <= -0.3:
        parts.append("calm")
    elif wind is not None and wind >= 0.3:
        parts.append("windy")
    if temp is not None and temp <= -0.3:
        parts.append("cold")
    elif temp is not None and temp >= 0.3:
        parts.append("warm")

    print()
    if not parts:
        print("  Your record does not show a clear weather signature. That is")
        print("  a result, not a failure — it may be the terrain, the siting,")
        print("  or simply a mild few weeks.")
        return

    print(f"  In your record, worse air came with: {' and '.join(parts)}.")
    if humid is not None:
        # With its n, like every other correlation printed here. The summary
        # line originally omitted it, which a test caught -- a coefficient
        # without a sample count is the exact thing this report refuses to
        # print everywhere else.
        n = sum(1 for h in hours
                if h["humidity_pct"] is not None and h["pm25"] is not None)
        drier = "drier" if humid <= -0.3 else ("wetter" if humid >= 0.3
                                               else "no clear humidity signal")
        print(f"  Humidity: {drier} (r = {humid:+.2f}, n={n}).")
        if "cold" in parts and humid >= 0.3:
            print("  Cold and damp rather than cold and dry — which is what")
            print("  distinguishes air being trapped from air simply being dry.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("evening", help="evening-premium analysis")
    e.add_argument("--nights", type=int, default=30, help="how many nights back")

    a = sub.add_parser("agreement", help="how sources compare, for tuning")
    a.add_argument("--by-hour", action="store_true",
                   help="break the comparison down by hour of day")

    c = sub.add_parser("correlate",
                       help="does the weather explain the readings? (#9B)")
    c.add_argument("--nights", type=int, default=90,
                   help="how many nights back to include")

    args = ap.parse_args()

    cfg = poller.load_config()
    db = poller.db_path()
    if not db.exists():
        raise SystemExit(f"No database at {db}. Run: python3 poller.py --once")

    conn = store.connect(db)
    try:
        if args.cmd == "evening":
            evening(conn, cfg, args.nights)
        elif args.cmd == "correlate":
            correlate(conn, cfg, args.nights)
        else:
            agreement(conn, cfg, args.by_hour)
    finally:
        conn.close()
    return 0



# ------------------------------------------------- inside against outside

#: Paired hours before this will say anything about a building. Fewer than a
#: day cannot separate "the envelope leaks" from "somebody cooked last night",
#: and a claim about somebody's house from six hours is not a claim worth
#: making. Deliberately lower than MIN_PAIRED_HOURS, which governs a weather
#: correlation: this compares two series directly rather than fitting to a
#: third, so it needs less to say something honest.
MIN_PAIRED_HOURS_INSIDE = 24

#: Indoor levels follow outdoor ones by roughly an hour in an ordinary house.
#: Both offsets are measured and the stronger reported, because the lag varies
#: with the building and asserting one would be inventing a number.
LAGS_HOURS = (0, 1, 2)

#: Where this tool draws its lines. Reporting thresholds, not physics: the
#: ratio itself is always stated so a reader can disagree with the label and
#: keep the measurement.
#:
#: 0.7 for infiltration -- indoor sitting at 70% of outdoor, sustained, is a
#: building that is not doing much. 1.5 for an indoor source -- half again
#: above outdoor is past what infiltration alone produces.
INFILTRATION_RATIO = 0.7
INDOOR_SOURCE_RATIO = 1.5

#: A ratio needs an outdoor reading big enough to divide by. Below this, both
#: numbers are near the floor of what the instrument can resolve and their
#: ratio is noise wearing a decimal point.
RATIO_FLOOR_UGM3 = 3.0


def _paired(indoor, outdoor, lag_hours=0):
    """Hours present on both sides, outdoor shifted back by `lag_hours`.

    Looked up by hour rather than zipped. A sensor dropping out for an hour is
    ordinary, and zipping two lists would pair the wrong hours from that point
    on and produce a confident number from nonsense.
    """
    from datetime import datetime, timedelta

    pairs = []
    for hour, inside in sorted(indoor.items()):
        if lag_hours:
            try:
                shifted = (datetime.fromisoformat(hour)
                           - timedelta(hours=lag_hours))
            except ValueError:
                continue
            key = shifted.isoformat(timespec="seconds")
        else:
            key = hour
        outside = outdoor.get(key)
        if outside is not None and inside is not None:
            pairs.append((hour, inside, outside))
    return pairs


def indoor_outdoor(conn, cfg=None, days=7):
    """How the inside compares with the outside, and which way it is failing.

    Two things go wrong in a house and they have opposite remedies, which is
    the entire reason this does not simply print two numbers:

      * **infiltration** -- indoor follows outdoor, lagged. The envelope or
        the filtration is not keeping the outside out. Close up; do not
        ventilate.
      * **an indoor source** -- indoor rises while outdoor stays flat.
        Cooking, a candle, a heater. Ventilate, *if* outdoor is clean enough
        to ventilate with.

    Tell somebody the wrong one and you have advised them to open a window
    during a smoke event, or to seal the house around a fire they lit.

    Always returns a dict. `verdict` is None when the record cannot support
    one, with `why` saying which thing is missing -- silence with a reason is
    an answer this project already knows how to give, and inventing a
    classification from nine hours of data is not.

    Nothing here claims cause. It reports what accompanied what, in the user's
    own record, with the hours counted -- the same standard `correlate()` holds
    itself to.
    """
    from datetime import datetime, timedelta, timezone

    result = {"verdict": None, "why": None, "hours": 0, "ratio": None,
              "correlation": None, "lag_hours": None, "advice": None,
              "indoor_mean": None, "outdoor_mean": None, "days": days}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    by_place = store.hourly_by_placement(conn, since=since)
    indoor, outdoor = by_place.get("indoor") or {}, by_place.get("outdoor") or {}

    if not indoor:
        result["why"] = ("no indoor sensor yet — add one in Settings to "
                         "compare inside with outside")
        return result
    if not outdoor:
        result["why"] = ("no outdoor sensor reporting, so there is nothing to "
                         "compare the inside with")
        return result

    pairs = _paired(indoor, outdoor, 0)
    result["hours"] = len(pairs)
    if len(pairs) < MIN_PAIRED_HOURS_INSIDE:
        result["why"] = (
            f"only {len(pairs)} hour(s) with both a reading inside and out; "
            f"{MIN_PAIRED_HOURS_INSIDE} are needed before this says anything "
            f"about a building")
        return result

    # The correlation is supporting evidence, not a precondition.
    #
    # It was a precondition in the first version, and that made a *perfectly
    # steady* house unclassifiable: `pearson` is undefined without variance,
    # so a home holding 2 µg/m³ against a steady 30 outside returned "not
    # enough hours" while reporting 72 of them. The ratio is the measurement;
    # the correlation says how confidently the two move together, and its
    # absence is worth reporting rather than fatal.
    best = None
    for lag in LAGS_HOURS:
        lagged = _paired(indoor, outdoor, lag)
        if len(lagged) < MIN_PAIRED_HOURS_INSIDE:
            continue
        r = pearson([p[1] for p in lagged], [p[2] for p in lagged])
        if r is not None and (best is None or abs(r) > abs(best[1])):
            best = (lag, r, lagged)

    if best is not None:
        lag, correlation, pairs = best
        result.update({"hours": len(pairs), "lag_hours": lag,
                       "correlation": round(correlation, 2)})

    inside_values = [p[1] for p in pairs]
    outside_values = [p[2] for p in pairs]
    result["indoor_mean"] = round(sum(inside_values) / len(inside_values), 1)
    result["outdoor_mean"] = round(sum(outside_values) / len(outside_values), 1)

    # Hours where the outdoor reading is too small to divide by are dropped
    # from the ratio and kept in everything else. Two numbers near the
    # instrument's floor produce a ratio that swings wildly and means nothing.
    usable = [(i, o) for _, i, o in pairs if o >= RATIO_FLOOR_UGM3]
    if not usable:
        result["why"] = (
            f"outdoor air has stayed below {RATIO_FLOOR_UGM3:g} µg/m³ for the "
            f"whole window, which is too clean to measure a ratio against")
        return result

    ratios = sorted(i / o for i, o in usable)
    middle = ratios[len(ratios) // 2]
    result["ratio"] = round(middle, 2)

    # The classification. Ordered so the dangerous mistake cannot happen: an
    # indoor source is checked first, because telling somebody to ventilate
    # when the problem is outside is the error that costs them.
    if middle >= INDOOR_SOURCE_RATIO:
        result["verdict"] = "indoor source"
        result["advice"] = (
            "Something inside is producing particulates — cooking, a candle, "
            "a heater. Ventilating helps, but check the outdoor reading "
            "first: it is only an improvement while outside is cleaner.")
    elif middle >= INFILTRATION_RATIO:
        result["verdict"] = "outdoor air getting in"
        result["advice"] = (
            "Inside is tracking outside, so the building is not keeping much "
            "out. Closing up and filtering helps; opening windows does not.")
    else:
        result["verdict"] = "holding"
        result["advice"] = (
            "Inside is staying cleaner than outside. Nothing to do.")

    if result["correlation"] is None:
        # Said, not omitted. "Too steady to correlate" is a fact about the
        # record, and leaving the sentence out would read as an oversight.
        movement = ("Neither reading moved enough over this window to "
                    "correlate them.")
    else:
        movement = (f"Correlation {result['correlation']} at a "
                    f"{result['lag_hours']}-hour lag.")

    result["basis"] = (
        f"{result['hours']} paired hours over {days} days; inside averaged "
        f"{result['indoor_mean']} µg/m³ against {result['outdoor_mean']} "
        f"outside, a median ratio of {result['ratio']}. {movement} This "
        f"describes what accompanied what in your own record; it is not a "
        f"claim about cause.")
    return result


if __name__ == "__main__":
    sys.exit(main())
