#!/usr/bin/env python3
"""
Airo weather capture — the cause, to go with the effect.

ROADMAP #9 Phase A. Airo records what the air *did*; it has never recorded why.
The premise the project was built on is that calm, cold nights trap
particulates in a valley, and without wind, temperature and pressure beside
each reading that premise can be believed but not checked.

This is Phase A only: **capture and backfill**. It correlates nothing and
forecasts nothing. Phase B reproduces the analysis, Phase C makes a forecast,
and `forecast.py` already holds the guardrails for what a forecast is allowed
to say — deliberately shipped before the thing it constrains.

How it fits the whole
---------------------
  reads    nothing of Airo's. It takes a latitude and longitude and returns
           hourly observations. That is the whole interface.
  used by  `poller.py`, which stores what comes back through
           `store.insert_weather()`, and later by Phase B's correlation.
  stores   nothing itself. Keeping the fetch separate from the write is what
           lets the tests exercise parsing without a database, and what will
           let a second source be added without touching the store.

Why Open-Meteo
--------------
The roadmap said "BOM observations (Australia) or a general weather API". BOM
would tie the flagship feature to one country and to an API with no documented
public contract. Open-Meteo:

  * needs no account, so nothing about this feature is gated behind a signup
    the way OpenAQ and PurpleAir are
  * covers everywhere, so the feature is not Australia-only
  * serves a **historical archive** as well as recent hours, which is what
    makes it possible to backfill weather against readings already collected.
    A model that can only learn from today learns nothing for a year.
  * is CC BY 4.0, so the attribution obligation is the same shape as the
    government feeds already carry

Verified live before being committed -- both endpoints, both the recent and the
archive path -- per the ROADMAP rule that an unverified adapter looks identical
to a working one until somebody relies on it.

What it assumes
---------------
  * **Wind is metres per second.** The API returns km/h by default and only
    honours m/s when asked. Phase B's whole finding is stated in m/s -- calm is
    below 0.5 -- so a silent unit change would not fail, it would move every
    threshold by 3.6x and quietly invert the conclusion. The response's own
    declared units are checked on every fetch for exactly that reason: this is
    the same trap as the QLD API silently ignoring an unknown query parameter,
    which returned the wrong window with no error.
  * **Hours are UTC.** Requested explicitly rather than relying on a default,
    and the timestamps are made timezone-aware here so nothing downstream has
    to guess.
  * **A null hour is missing, not zero.** The archive has gaps. They are left
    as None and stored as NULL, because a calm night and an unmeasured night
    must never look the same to a correlation.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SLUG = "open-meteo"

#: Required by CC BY 4.0, and rendered wherever weather is shown. Same
#: obligation the government feeds carry; see LICENSING.md.
ATTRIBUTION = "Weather data by Open-Meteo.com, CC BY 4.0"
LICENCE = "CC BY 4.0"

#: Served beside the attribution in the weather block of the API payload, so
#: the CC BY credit can carry a link to its source wherever it is rendered.
HOMEPAGE = "https://open-meteo.com"

#: The version is poller.VERSION's, copied rather than imported. This module
#: reads nothing of Airo's -- see the docstring above -- and importing poller
#: for a string would make the leaf a branch, and a circular one at that,
#: since poller imports this. **poller.VERSION is canonical: change it there
#: and here together.** A contract test asserts the two stay in step, because
#: this said "0.5" through the whole of 0.5.0.
USER_AGENT = "airo/0.6.0 (https://github.com/Donnishcomau/airo)"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/era5"

#: What we ask for, and what we call it. The API's names are theirs; the
#: column names are ours and carry their units.
FIELDS = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_pct",
    "surface_pressure": "pressure_hpa",
    "wind_speed_10m": "wind_speed_ms",
    "wind_direction_10m": "wind_dir_deg",
}

#: The units each field must come back in. Checked on every response.
EXPECTED_UNITS = {
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    "surface_pressure": "hPa",
    "wind_speed_10m": "m/s",
    "wind_direction_10m": "°",
}

#: The archive lags real time by several days, so asking it for yesterday
#: returns nothing. Anything more recent than this comes from the forecast
#: endpoint, which also serves past days.
ARCHIVE_LAG_DAYS = 6


class WeatherUnavailable(Exception):
    """The fetch failed, or came back in a shape we will not store.

    Raised rather than returning empty so a caller cannot mistake "the service
    was down" for "the weather was calm". Phase B would draw a conclusion from
    the difference.
    """


def _get(url, params, timeout=30):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise WeatherUnavailable(f"{url} answered HTTP {e.code}") from e
    except Exception as e:
        raise WeatherUnavailable(f"{url}: {type(e).__name__}: {e}") from e


def _check_units(payload):
    """Refuse a response whose units are not the ones we asked for.

    The request names its units and the response declares them back, so this
    compares the two rather than trusting either. A mismatch is fatal: storing
    km/h in a column called wind_speed_ms would not fail anywhere, it would
    just make every conclusion drawn from it wrong by a factor of 3.6.
    """
    units = payload.get("hourly_units") or {}
    for field, expected in EXPECTED_UNITS.items():
        if field not in units:
            continue                      # not requested, or not returned
        if units[field] != expected:
            raise WeatherUnavailable(
                f"{field} came back in {units[field]!r}, not {expected!r}. "
                f"Refusing to store it: the column names carry the unit, and "
                f"a silent change here moves every threshold in Phase B.")


def _parse(payload):
    """Turn one response into rows, dropping hours with nothing in them."""
    _check_units(payload)
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    rows = []
    for i, stamp in enumerate(times):
        row = {}
        for api_name, ours in FIELDS.items():
            series = hourly.get(api_name)
            row[ours] = series[i] if series and i < len(series) else None

        # An hour with no measurement at all is not worth a row. An hour with
        # *some* fields is kept: partial weather still constrains a correlation,
        # and the missing ones stay NULL rather than becoming zero.
        if all(v is None for v in row.values()):
            continue

        # The API returns naive local-format stamps; we asked for UTC, so say
        # so explicitly rather than leaving it for a reader to assume.
        row["observed_utc"] = datetime.fromisoformat(stamp).replace(
            tzinfo=timezone.utc).isoformat(timespec="seconds")
        rows.append(row)
    return rows


# An IANA zone name: "Area/Location", optionally "Area/Sub/Location", plus the
# handful of bare names like "UTC". Deliberately strict. Whatever comes back
# gets written into config.json and every later lookup depends on it, so a
# value that cannot possibly be a zone is better refused here than stored and
# reported afterwards as the user's typo.
_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+_-]+){0,2}$")


def timezone_at(latitude, longitude, timeout=15):
    """The IANA timezone name for a location, or None.

    Nobody types a timezone name, for the same reason nobody types their own
    latitude: it is the tool's job. Setup already turns an address into
    coordinates, and Open-Meteo will name the zone for those coordinates
    without a key -- so the field is filled from something the user has
    already given, rather than by asking a question whose wrong answer is
    invisible until alerts start arriving at 3am.

    Never raises. A timezone is a nicety and a location is the product: a
    weather API being down must not stop somebody finishing setup. The caller
    gets None and the existing "timezone not set" reporting covers it.
    """
    try:
        payload = _get(FORECAST_URL, {
            # Two decimals, about 1.1 km. Everywhere else that coordinates
            # leave this machine they carry four (11 m) because a provider is
            # matching a sensor; here the question is only which zone a point
            # is in, and a suburb-sized cell answers it.
            #
            # Not coarser, though it is tempting. One decimal is 11 km, and
            # Coolangatta to Tweed Heads is less than that across a state line
            # where one side observes daylight saving and the other does not --
            # which is the exact failure this whole feature exists to prevent.
            "latitude": round(float(latitude), 2),
            "longitude": round(float(longitude), 2),
            # No `hourly`, so no weather is fetched. The question is only
            # which zone this point is in, and asking for a forecast as well
            # would be a bigger request than the answer needs against a
            # service doing this for nothing.
            "timezone": "auto",
            "forecast_days": 1,
        }, timeout=timeout)
    except Exception:
        return None

    name = payload.get("timezone") if isinstance(payload, dict) else None
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name if _ZONE_RE.match(name) else None


def recent(latitude, longitude, past_days=2, timeout=30):
    """Recent hours, including the last few days.

    The forecast endpoint serves past days as well, which is what makes it the
    right one for routine capture: a poll that missed a day catches up without
    a separate mechanism.
    """
    return _parse(_get(FORECAST_URL, {
        "latitude": round(float(latitude), 4),
        "longitude": round(float(longitude), 4),
        "hourly": ",".join(FIELDS),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "past_days": max(0, min(int(past_days), 92)),
        "forecast_days": 1,
    }, timeout=timeout))


def forward(latitude, longitude, hours=6, timeout=30):
    """The next `hours` of forecast weather, oldest first.

    ROADMAP #9 Phase C needs the weather that has not happened yet, which is
    the same endpoint `recent()` uses with the window pointed the other way.
    Units are declared and checked exactly as they are for stored weather: a
    forecast in km/h scored against history in m/s would be wrong by 3.6x and
    fail nowhere.
    """
    rows = _parse(_get(FORECAST_URL, {
        "latitude": round(float(latitude), 4),
        "longitude": round(float(longitude), 4),
        "hourly": ",".join(FIELDS),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "past_days": 0,
        "forecast_days": 2,
    }, timeout=timeout))

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    ahead = [r for r in rows
             if datetime.fromisoformat(r["observed_utc"]) > now]
    return ahead[:max(0, int(hours))]


def history(latitude, longitude, start, end, timeout=60):
    """Hourly weather between two dates, from the reanalysis archive.

    This is what lets weather be backfilled against readings already collected.
    The archive lags real time, so the caller is expected to use `recent()` for
    anything inside ARCHIVE_LAG_DAYS -- `plan_backfill()` splits a window for
    exactly that reason.
    """
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(end, datetime):
        end = end.date()
    if end < start:
        raise ValueError(f"end {end} is before start {start}")

    return _parse(_get(ARCHIVE_URL, {
        "latitude": round(float(latitude), 4),
        "longitude": round(float(longitude), 4),
        "hourly": ",".join(FIELDS),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }, timeout=timeout))


def plan_backfill(since, until=None, now=None):
    """Split a window into the calls that can actually answer it.

    Two endpoints with different reach: the archive holds everything up to
    about a week ago, the forecast endpoint holds the last 92 days. Asking
    either for the other's range returns an empty series rather than an error,
    which is the failure mode this exists to prevent -- an empty answer reads
    as "no weather" and there is nothing to distinguish it from a bad request.

    Returns a list of ("archive"|"recent", start, end) in chronological order.
    """
    now = now or datetime.now(timezone.utc)
    until = until or now
    if since > until:
        return []

    cutoff = now - timedelta(days=ARCHIVE_LAG_DAYS)
    plan = []
    if since < cutoff:
        plan.append(("archive", since, min(until, cutoff)))
    if until >= cutoff:
        plan.append(("recent", max(since, cutoff), until))
    return plan


def fetch_window(latitude, longitude, since, until=None, now=None):
    """Everything available for a window, from whichever endpoint reaches it.

    Deduplicated on the hour, because the two endpoints overlap at the seam and
    the archive is the better answer where both have one -- it is a reanalysis
    rather than a running model.
    """
    by_hour = {}
    for kind, start, end in plan_backfill(since, until, now):
        if kind == "archive":
            rows = history(latitude, longitude, start, end)
        else:
            span = max(1, (datetime.now(timezone.utc) - start).days + 1)
            rows = recent(latitude, longitude, past_days=span)
        for row in rows:
            # Archive first in the plan, so `setdefault` keeps it and the
            # forecast endpoint only fills what the archive did not reach.
            by_hour.setdefault(row["observed_utc"], row)
    return [by_hour[k] for k in sorted(by_hour)]
