# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guardrails for anything Airo says about the future.

There is no forecast yet -- ROADMAP #9 Phase C. This module exists *before*
the feature because both constraints on it are the kind that are easy to
satisfy on day one and impossible to retrofit once a model is trained and a
number is on screen.

Two distinct risks, two separate mechanisms:

1. **Australian Consumer Law s4.** A forecast is a representation about a
   future matter. If the maker cannot show reasonable grounds for it, the law
   deems it misleading -- the burden sits with the maker, not the complainant.
   `phrase()` is the only sanctioned way to render a forward-looking
   statement: it refuses certainty wording, requires a stated basis, and
   refuses to speak at all until there is a measured skill score to stand on.

2. **PurpleAir ToS s4.4.** PurpleAir take a perpetual, sublicensable licence
   over models derived from their data. A forecast trained on a PurpleAir
   feed is therefore not exclusively the author's, and neither is anything
   built on it later. `training_sources()` excludes those rows by default, so
   the licence is avoided by construction rather than by remembering.

Nothing here forecasts. It decides what a forecast would be allowed to say.
"""

import json
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path

import store

# ---------------------------------------------------------------- ACL s4

# Wording that asserts a future fact. A forecast may say a night looks likely
# to trap; it may not say it will.
CERTAINTY_PATTERNS = [
    r"\bwill be\b", r"\bis going to\b", r"\bguarantee", r"\bcertain(ly)?\b",
    r"\bdefinitely\b", r"\bwon't\b", r"\bno risk\b", r"\bsafe\b",
    r"\bexpect(ed)? to reach\b", r"\bshall\b",
]

# At least one of these must appear, so the statement reads as a likelihood.
HEDGES = ["looks", "likely", "may", "might", "could", "suggests", "tends to",
          "often", "usually", "typically", "conditions for", "signs of"]

# Below this, the rules are not measurably better than saying "same as now",
# and there are no reasonable grounds to publish anything.
MIN_SKILL = 0.05

# Minimum verified predictions before a skill score means anything.
MIN_VERIFIED = 30


class NoReasonableGrounds(Exception):
    """Raised instead of emitting a forecast that could not be defended.

    Deliberately an exception rather than a silent fallback: a forecast that
    quietly degrades to a hedge is still a forecast on screen, and the user
    cannot tell the difference.
    """


def phrase(text, basis, skill):
    """Return `text` if it is a defensible forward-looking statement.

    `basis` is the short explanation of *why* -- the reasonable grounds --
    and is appended so the user sees the reasoning, not just the conclusion.
    `skill` is the measured skill score from `Skill.score()`.

    Raises NoReasonableGrounds rather than softening bad wording, because
    softening it here would hide the problem from whoever wrote it.
    """
    if not basis or not str(basis).strip():
        raise NoReasonableGrounds("a forecast must state what it is based on")

    if skill is None:
        raise NoReasonableGrounds(
            "no measured skill yet -- nothing may be forecast until the rules "
            f"have been verified against at least {MIN_VERIFIED} outcomes")
    if skill < MIN_SKILL:
        raise NoReasonableGrounds(
            f"skill {skill:.3f} is no better than persistence; publishing it "
            "would be a representation without reasonable grounds")

    low = text.lower()
    for pat in CERTAINTY_PATTERNS:
        m = re.search(pat, low)
        if m:
            raise NoReasonableGrounds(
                f"{m.group(0)!r} asserts a future fact; say what is likely, "
                "not what will happen")
    if not any(h in low for h in HEDGES):
        raise NoReasonableGrounds(
            "no likelihood wording: a forecast must read as a probability. "
            f"Use one of: {', '.join(HEDGES[:6])}")

    return f"{text} — {basis}"


# ------------------------------------------------------- measured skill

class Skill:
    """Prediction/outcome ledger, and the skill score derived from it.

    Skill is measured against **persistence** -- "the next six hours look like
    the last six" -- because that is the honest baseline. A model that cannot
    beat persistence has not learned anything about the weather; it has
    learned that air quality is autocorrelated.

        score = 1 - (MSE_model / MSE_persistence)

    1.0 is perfect, 0.0 is no better than persistence, negative is worse.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.records = []
        if self.path.exists():
            try:
                self.records = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.records = []

    def record(self, predicted, persistence, actual, when=None):
        """Log one verified prediction. `actual` is what happened."""
        self.records.append({"predicted": float(predicted),
                             "persistence": float(persistence),
                             "actual": float(actual),
                             "when": store.canonical_utc(when) if when else None})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.records, indent=1), encoding="utf-8")

    def score(self):
        """Skill against persistence, or None if too few verified outcomes.

        None is not zero. Zero means measured and useless; None means not yet
        entitled to an opinion -- and `phrase()` treats them differently.
        """
        if len(self.records) < MIN_VERIFIED:
            return None
        mse_m = sum((r["predicted"] - r["actual"]) ** 2 for r in self.records)
        mse_p = sum((r["persistence"] - r["actual"]) ** 2 for r in self.records)
        if mse_p == 0:
            return None                      # nothing changed; nothing to beat
        return 1.0 - (mse_m / mse_p)

    def summary(self):
        """One line fit to show a user, per 'publish accuracy' in ROADMAP #9."""
        s = self.score()
        if s is None:
            return (f"Not forecasting yet — {len(self.records)} of "
                    f"{MIN_VERIFIED} verifications needed to measure accuracy.")
        errs = [abs(r["predicted"] - r["actual"]) for r in self.records]
        mae = sum(errs) / len(errs)
        return (f"Measured over {len(self.records)} forecasts: "
                f"{mae:.1f} µg/m³ mean error, "
                f"{s * 100:+.0f}% versus assuming no change.")


# ------------------------------------------------------ the outlook itself
#
# ROADMAP #9 Phase C. Deliberately a transparent rules-based score rather than
# a learned model: it is explainable, debuggable, needs no training data, and
# a user can see why it said what it said. A learned model is only worth
# reaching for once there is enough labelled history to beat this, measured
# honestly against it.
#
# The rules are fitted to the user's *own* record rather than to constants
# chosen here, which does two things at once. It makes the forecast local --
# a valley and a ridge have different numbers -- and it supplies the
# reasonable grounds ACL s4 demands, because the basis is "your own hours",
# with the count stated.

#: How far ahead an outlook speaks. Six hours is the window ROADMAP #9 asks
#: for: long enough to close up before an evening episode, short enough that
#: a forecast endpoint is still describing weather rather than climate.
HORIZON_HOURS = 6


def band_means(conn, cfg, nights=90):
    """Mean PM2.5 per wind band from this user's record, per band name.

    Returns {band: (mean, hours)}. This is Phase B's table, reused as the
    forecast's grounds -- so the two features cannot disagree about what the
    record says, and the number quoted in a forecast is one the user can
    reproduce with `analyse.py correlate`.
    """
    import analyse
    import store as _store

    location = (cfg or {}).get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        return {}

    place = _store.place_key(lat, lon)
    since = datetime.now(timezone.utc) - timedelta(days=nights)
    rows = _store.hourly_with_weather(conn, place, since=since)

    # Which sources may be modelled at all. PurpleAir's ToS s4.4 grants them
    # a licence over models derived from their data, so they are excluded by
    # construction rather than by anybody remembering to.
    allowed_ids = {s["id"] for s in _store.list_sources(conn, enabled_only=False)
                   if licence_permits_modelling(s["provider"])}

    out = {}
    for low, high, name in analyse.WIND_BANDS:
        picked = [r["pm25"] for r in rows
                  if r["source_id"] in allowed_ids
                  and r["wind_speed_ms"] is not None
                  and r["wind_speed_ms"] >= low
                  and (high is None or r["wind_speed_ms"] < high)]
        if picked:
            out[name] = (sum(picked) / len(picked), len(picked))
    return out


def _band_for(wind):
    import analyse
    for low, high, name in analyse.WIND_BANDS:
        if wind is not None and wind >= low and (high is None or wind < high):
            return name
    return None


def outlook(conn, cfg, forward_hours, skill_path, nights=90):
    """A six-hour outlook, or an honest refusal.

    Always returns a dict. `text` is None whenever anything is missing --
    grounds, measured skill, or a licence that permits modelling -- and `why`
    says which, because a feature that goes quiet without explanation is
    indistinguishable from one that is broken.
    """
    result = {"text": None, "why": None, "pm25": None, "persistence": None,
              "accuracy": None, "for_hour": None}

    means = band_means(conn, cfg, nights=nights)
    if not means:
        # Distinguish "nothing to learn from" from "not allowed to learn from
        # it". They are different problems with different answers, and telling
        # somebody to backfill weather they already have would waste their
        # time and their provider's quota.
        import store as _store
        sources = [dict(s) for s in _store.list_sources(conn, enabled_only=False)]
        usable, excluded = training_sources(sources)
        if excluded and not usable:
            result["why"] = explain_exclusion(excluded)
        else:
            result["why"] = ("no paired weather and readings yet — run "
                             "`poller.py --backfill-weather`")
        return result

    target = None
    for hour in forward_hours or []:
        when = hour.get("observed_utc")
        if not when:
            continue
        target = hour
        break
    if target is None:
        result["why"] = "no forecast weather to reason about"
        return result

    band = _band_for(target.get("wind_speed_ms"))
    if band not in means:
        result["why"] = (f"your record holds no hours in the {band or 'that'} "
                         f"wind band, so there is nothing to base it on")
        return result

    mean, hours = means[band]
    predicted = float(mean)

    # Persistence is the honest baseline: "the next six hours look like now".
    #
    # No None check. `band_means()` above draws from the same rows this does
    # -- readings with a value, not marked suspect -- so a non-empty set of
    # means guarantees there is a reading to find, and the only difference is
    # that means is bounded to `nights` while this is not. A guard here would
    # be unreachable, and dead defensive code reads as a handled case that is
    # not one. Written down rather than left to be rediscovered.
    persistence = _latest_pm25(conn)

    skill = Skill(skill_path)
    result.update({"pm25": round(predicted, 1),
                   "persistence": float(persistence),
                   "for_hour": target.get("observed_utc"),
                   "accuracy": skill.summary()})

    direction = ""
    if predicted >= persistence * 1.3:
        direction = "worse than now"
    elif predicted <= persistence * 0.7:
        direction = "clearer than now"
    else:
        direction = "much like now"

    basis = (f"{band} wind forecast, and {band} hours in your own record "
             f"average {mean:.1f} µg/m³ across {hours} hours")
    try:
        # "are likely to be", not "look" -- phrase() checks for a hedge from
        # HEDGES and "look" is not "looks". Caught by its own guard, which is
        # the guard working: the wording is the product here.
        result["text"] = phrase(
            f"The next {HORIZON_HOURS} hours are likely to be {direction}, "
            f"around {predicted:.0f} µg/m³",
            basis, skill.score())
    except NoReasonableGrounds as e:
        result["why"] = str(e)
    return result


def _latest_pm25(conn):
    row = conn.execute(
        "SELECT pm25 FROM readings WHERE pm25 IS NOT NULL "
        "AND quality != 'suspect' ORDER BY observed_utc DESC LIMIT 1"
    ).fetchone()
    return float(row[0]) if row else None


# ------------------------------------------------------------ verification
#
# A prediction nobody checks is a claim. Each one is written down with the
# hour it is about, and scored once that hour has been measured -- which is
# the only way the gate above ever opens.

def remember(path, when, predicted, persistence):
    """Write down a prediction so it can be scored later."""
    path = Path(path)
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        pending = []
    # Canonical on the way in, so the ledger holds one form. The dedup below
    # compares strings, and `...T10:00:00Z` and `...T10:00:00+00:00` are the
    # same instant written two ways -- promising for both would score one hour
    # twice and inflate skill, which is the number the gate to speaking turns
    # on. Same reasoning as store._iso: normalise at the writer.
    when = store.canonical_utc(when) or when
    if any(store.canonical_utc(p.get("when")) == when for p in pending):
        return len(pending)          # already promised something for that hour
    pending.append({"when": when, "predicted": float(predicted),
                    "persistence": float(persistence)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending, indent=1), encoding="utf-8")
    return len(pending)


def verify_pending(conn, pending_path, skill_path):
    """Score every prediction whose hour has now been measured.

    An hour with no reading stays pending rather than being scored or thrown
    away: the station may have been offline, and scoring against a gap would
    invent skill out of missing data. A prediction is removed only once it has
    been scored, so nothing is counted twice.
    """
    pending_path = Path(pending_path)
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0

    skill = Skill(skill_path)
    now = datetime.now(timezone.utc)
    kept, scored = [], 0

    for p in pending:
        # One parse, through the sanctioned reader, used for both the "has it
        # happened yet" test and the hour key. Slicing the raw string for the
        # key while parsing it properly for the comparison meant a ledger entry
        # written with a real offset -- `...T10:00:00+10:00` -- asked the
        # database for hour 10 when the reading was stored under hour 00. It
        # matched nothing, stayed pending, and could never be scored.
        when = store.canonical_utc(p.get("when"))
        if when is None:
            continue                 # unparseable: dropped, not scored
        at = datetime.fromisoformat(when)
        if at > now:
            kept.append(p)
            continue                 # not yet happened

        row = conn.execute(
            "SELECT AVG(pm25) FROM readings WHERE pm25 IS NOT NULL "
            "AND quality != 'suspect' AND substr(observed_utc, 1, ?) = ?",
            (store.HOUR_KEY_LEN, when[:store.HOUR_KEY_LEN])).fetchone()
        actual = row[0] if row else None
        if actual is None:
            kept.append(p)
            continue                 # measured nothing; still pending

        skill.record(p["predicted"], p["persistence"], float(actual), when=when)
        scored += 1

    pending_path.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    return scored


# ------------------------------------------------- PurpleAir ToS s4.4

# Providers whose terms claim rights over models derived from their data.
# A forecast trained on these is not exclusively the author's, and neither is
# anything built on it afterwards -- the licence is perpetual.
MODEL_ENCUMBERED = {"purpleair"}


def licence_permits_modelling(provider):
    """Whether a provider's data may train a model kept by its author."""
    return str(provider or "").lower() not in MODEL_ENCUMBERED


def training_sources(sources):
    """Filter a source list down to what may safely train a model.

    Returns (usable, excluded). Callers must surface `excluded` -- silently
    dropping a user's best sensor would be worse than the licence problem it
    avoids, and the user may legitimately decide they do not care.
    """
    usable, excluded = [], []
    for s in sources or []:
        prov = (s or {}).get("provider")
        (usable if licence_permits_modelling(prov) else excluded).append(s)
    return usable, excluded


def explain_exclusion(excluded):
    """Say, in a sentence a user can act on, why a source was left out.

    Silence would be worse than the exclusion: someone who sees their nearest
    sensor missing from a model reasonably concludes the tool is broken. The
    reason is contractual rather than technical, and saying so is what stops
    it reading as a bug -- the data is still used for live readings and
    history, only not for training.
    """
    if not excluded:
        return ""
    names = sorted({str((s or {}).get("provider")) for s in excluded})
    return (f"Excluded from model training: {', '.join(names)}. "
            "Their terms grant the provider a perpetual, sublicensable licence "
            "over anything derived from their data, which would extend to this "
            "model. They are still used for live readings and history.")
