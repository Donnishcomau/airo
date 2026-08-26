"""What to *show* a measurement in. Never what to store it as.

Rule 6 is not negotiable and this module does not touch it: the database holds
raw µg/m³, Celsius, metres per second, kilometres and hectopascals, because a
stored number that carries a presentation choice is a number nobody can compare
later. Everything here happens on the way out, at the moment of display, and
converting back is always exact.

That is also why resolution is done fresh every time rather than cached or
written into the config at setup. Somebody who moves, or changes their Mac's
region, gets the new units on the next screen they open -- there is no stored
answer to go stale, and nothing to migrate when it changes.

Why per quantity rather than one "metric or imperial" switch
------------------------------------------------------------
Because that switch does not exist in the world. The United Kingdom reports
temperature in Celsius and wind in miles per hour and road distances in miles.
Canada is metric but quotes wind in km/h, not m/s. A single flag forces one of
those to be wrong, and the wrong one is invisible -- a wind speed shown in the
wrong unit is still a plausible number.

So a region maps to a *choice per quantity*, and anything not named falls back
to the metric defaults. The fallback is the common case by a long way and is
deliberately the one that needs no entry.

PM2.5 is absent from all of this on purpose. µg/m³ is the unit everywhere,
including in the United States, where the EPA reports concentrations in µg/m³
and applies its index on top. Offering to "convert" it would invent a
difference that does not exist.
"""

import locale as _locale
import os
import re

#: Every quantity this project shows a user, with the unit it is stored in
#: first. The first entry of each is the canonical one, so a reader can see at
#: a glance what the database holds.
QUANTITIES = ("temperature", "wind", "distance", "pressure")

METRIC = {
    "temperature": "c",
    "wind": "ms",
    "distance": "km",
    "pressure": "hpa",
}

#: Regions that differ from METRIC, and only in the ways they actually differ.
#: Absent regions get METRIC, which is most of the world -- listing them all
#: would be a list to maintain for no benefit, and a region added to the world
#: is not a region this project needs to know about.
BY_REGION = {
    "US": {"temperature": "f", "wind": "mph", "distance": "mi"},
    "LR": {"temperature": "f", "wind": "mph", "distance": "mi"},
    "MM": {"temperature": "f", "wind": "mph", "distance": "mi"},
    # Celsius, but miles and miles per hour. The case a single metric/imperial
    # flag cannot express, and the reason this table is per quantity.
    "GB": {"wind": "mph", "distance": "mi"},
    # Metric, but nobody says metres per second in a forecast.
    "CA": {"wind": "kmh"},
    "AU": {"wind": "kmh"},
    "NZ": {"wind": "kmh"},
    "IE": {"wind": "kmh"},
}

#: value in the stored unit -> (value in the shown unit, label). Exact, and
#: exactly invertible, so nothing here can drift a stored figure.
CONVERSIONS = {
    ("temperature", "c"): (lambda v: v, "°C"),
    ("temperature", "f"): (lambda v: v * 9.0 / 5.0 + 32.0, "°F"),
    ("wind", "ms"): (lambda v: v, "m/s"),
    ("wind", "kmh"): (lambda v: v * 3.6, "km/h"),
    ("wind", "mph"): (lambda v: v * 2.2369362920544, "mph"),
    ("distance", "km"): (lambda v: v, "km"),
    ("distance", "mi"): (lambda v: v * 0.621371192237334, "mi"),
    ("pressure", "hpa"): (lambda v: v, "hPa"),
    ("pressure", "inhg"): (lambda v: v * 0.029529983071445, "inHg"),
}

#: How many decimals each shown unit deserves. A wind speed in mph quoted to
#: two decimals claims a precision the anemometer does not have; one in m/s
#: rounded to whole numbers loses the difference between calm and light, which
#: is the distinction the whole project turns on.
PLACES = {
    "°C": 1, "°F": 1,
    "m/s": 1, "km/h": 1, "mph": 1,
    "km": 1, "mi": 1,
    "hPa": 0, "inHg": 2,
}

#: Where a region code is looked for, in order. LC_MEASUREMENT exists for
#: precisely this question and is checked first where it is set; the rest are
#: what actually carries the answer on a Mac, where LANG is `en_AU.UTF-8` and
#: LC_MEASUREMENT usually is not set at all.
ENV_ORDER = ("LC_MEASUREMENT", "LC_ALL", "LANG", "LANGUAGE")

_REGION = re.compile(r"[a-z]{2,3}[_-]([A-Za-z]{2})\b")


def region(environ=None, fallback=None):
    """The two-letter region code to pick units for, or None.

    Returns None rather than guessing when nothing says. A caller that cannot
    tell where it is should show the metric defaults, not invent a country --
    and None makes that a decision somebody wrote down rather than a silent
    coincidence of which branch ran.
    """
    env = os.environ if environ is None else environ
    for name in ENV_ORDER:
        value = env.get(name)
        if not value:
            continue
        # No explicit test for `C` and `POSIX`. There was one, and removing it
        # turned nothing red, because the pattern below requires `xx_YY` and
        # neither can match it -- the guard was restating what the regex
        # already guarantees. Dead defensive code reads as a handled case that
        # is not one, so it is gone and the reasoning is here instead. The
        # test that those locales yield no region stays, because the behaviour
        # is still worth holding to.
        found = _REGION.search(value)
        if found:
            return found.group(1).upper()

    if fallback is not None:
        found = _REGION.search(str(fallback))
        if found:
            return found.group(1).upper()
    return None


def system_locale():
    """The interpreter's own idea of the locale, as a fallback string.

    Wrapped because `locale.getlocale()` raises on some malformed settings and
    a units lookup must never be the thing that stops a reading being shown.
    """
    try:
        return (_locale.getlocale()[0]
                or _locale.setlocale(_locale.LC_CTYPE))
    except (ValueError, TypeError, _locale.Error):
        return None


def resolve(cfg=None, environ=None):
    """The unit to show each quantity in: {quantity: unit}.

    Order is explicit configuration, then the region, then metric. A user who
    has said what they want is never overridden by where they appear to be --
    somebody working abroad for a month did not ask for their history to
    change units, and somebody who set it deliberately did.

    Passing `environ` means "decide from exactly this", and the interpreter's
    own locale is then left out. Otherwise a caller supplying an environment
    that says nothing would still get the developer's region through the back
    door, which makes the answer depend on the machine the question is asked
    on -- true of a test and, worse, true of a subprocess that inherits a
    stripped environment.
    """
    chosen = dict(METRIC)
    fallback = system_locale() if environ is None else None
    chosen.update(BY_REGION.get(region(environ, fallback) or "", {}))

    override = (cfg or {}).get("units")
    if not isinstance(override, (str, dict)):
        # Anything else is not a setting anyone can act on. Ignored rather
        # than raised: this runs on the way to a screen, and refusing to
        # return would cost somebody the reading. The loud refusal belongs in
        # the validator, where it can be read and corrected, and it is there.
        override = {}
    if isinstance(override, str):
        # A bare "metric"/"us" is accepted because it is the obvious thing to
        # write by hand, and refusing it would send somebody to the source to
        # find out the real shape.
        #
        # `metric` maps to the full METRIC table, not to an empty override.
        # It meant "no override" once, which read as harmless and was not:
        # somebody in Australia who explicitly asked for metric still got
        # km/h, because with nothing to override the region kept winning. A
        # setting that does nothing is worse than one that is refused — they
        # had said what they wanted and been ignored.
        override = {"metric": dict(METRIC), "us": BY_REGION["US"]}.get(
            override.lower(), {})
    for quantity, unit in override.items():
        if (quantity, str(unit).lower()) in CONVERSIONS:
            chosen[quantity] = str(unit).lower()
    return chosen


def convert(quantity, value, units=None, cfg=None, environ=None):
    """(number, label) for display. None in, (None, label) out.

    None is passed through rather than defaulted to zero: an hour with no
    anemometer reading is not an hour of no wind, and every other layer of
    this project draws that distinction.
    """
    units = units or resolve(cfg, environ)
    unit = units.get(quantity, METRIC[quantity])
    func, label = CONVERSIONS[(quantity, unit)]
    if value is None:
        return None, label
    return func(float(value)), label


def show(quantity, value, units=None, cfg=None, environ=None, places=None):
    """A display string, unit included. `—` where there is no measurement."""
    number, label = convert(quantity, value, units, cfg, environ)
    if number is None:
        return f"—{'' if not label else ' ' + label}"
    if places is None:
        places = PLACES.get(label, 1)
    return f"{number:.{places}f} {label}"


def label(quantity, units=None, cfg=None, environ=None):
    """Just the unit's name, for a column heading."""
    return convert(quantity, None, units, cfg, environ)[1]
